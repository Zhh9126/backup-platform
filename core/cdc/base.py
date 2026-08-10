# -*- coding: utf-8 -*-
"""
T03 CDC 守护抽象基类。

一个 CDCDaemon 负责「把某个数据库的事务日志流持续搬到本地日志仓库」：

    源库 ──(mysqlbinlog --stop-never / pg_receivewal)──▶ repo.live_dir()
                                                            │ 段完整
                                                            ▼
                                                    repo.seal() ──▶ sealed/<day>/
                                                            │
                                                            ▼
                                        RecoveryJournal.append(rp_kind='db-log')

契约（设计文档 §3.2）：
- ``start()``      拉起子进程/线程，失败返回 False 且写 ``last_error``，不抛异常；
- ``tick()``       由 Supervisor 周期调用，做「封存 + 位点刷新 + 存活检查」；
- ``seal_ready_segments()``  只封存**已完整**的段——正在写入的段绝不封存，
  这是避免「半截 binlog 入 journal 导致恢复失败」的核心防线；
- ``stop()``       幂等，保证在 timeout 内回收子进程（先 terminate 后 kill）。

所有实现都必须能在「客户端工具缺失 / DEMO_MODE=on」时被工厂替换为
:class:`core.cdc.simulated.SimulatedCDCDaemon`，保证 import 不崩、流程可跑。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import config
import core.db as db

# 判定「正在写入的段已停滞」的默认秒数（超过则允许强制封存）
DEFAULT_STALL_SEC = 60


class CDCDaemon:
    """数据库日志流捕获守护的抽象基类。

    Attributes:
        engine_key: 实现标识，落到 ``rt_capture_state.engine``。
        display_name: 中文展示名。
        required_clients: 依赖的命令行客户端（用 ``shutil.which`` 探测）。
        is_simulated: 是否为仿真实现（恢复点会被打上 ``is_simulated=1``）。
        seal_all_immediately: True 表示 live 目录中的所有文件都已完整可封存
            （仿真实现专用；真实流式实现必须保持 False）。
    """

    engine_key: str = "base"
    display_name: str = "CDC 守护"
    required_clients: List[str] = []
    is_simulated: bool = False
    seal_all_immediately: bool = False

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        """构造守护。

        Args:
            task: backup_tasks 行（含明文密码，供客户端认证）。
            rt_config: :class:`core.rt_backup.types.RtConfig`。
            repo: :class:`core.rt_backup.repo.LogRepository`。
            logger: 日志器。
        """
        self.task: dict = dict(task or {})
        self.rt = rt_config
        self.repo = repo
        self.logger = logger or db.get_logger("rt.cdc")

        self.task_id: int = int(self.task.get("id") or 0)
        self.task_name: str = self.task.get("name") or f"task_{self.task_id}"
        self.host: str = self.task.get("host") or "127.0.0.1"
        self.port: int = int(self.task.get("port") or 0)
        self.username: str = self.task.get("username") or ""
        self.password: str = self.task.get("password") or ""
        self.db_name: str = self.task.get("db_name") or ""

        self.proc: Optional[subprocess.Popen] = None
        self.started_at: str = ""
        self.last_error: str = ""
        self.degrade_reason: str = ""
        self.position: Dict[str, object] = {}
        self.segments_sealed: int = 0
        self.bytes_sealed: int = 0

        self._size_cache: Dict[str, Tuple[int, float]] = {}
        self._stderr_path: str = ""
        self._stderr_fh = None

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    @classmethod
    def check_client(cls) -> Tuple[bool, str]:
        """探测所需命令行客户端是否可用。

        Returns:
            ``(可用, 原因)``。不可用时原因形如「缺少客户端工具: mysqlbinlog」。
        """
        missing = [name for name in cls.required_clients if not shutil.which(name)]
        if missing:
            return False, f"缺少客户端工具: {', '.join(missing)}"
        return True, ""

    @classmethod
    def is_available(cls, task: dict) -> Tuple[bool, str]:
        """在给定任务上该实现是否可用（客户端 + 连接信息齐全）。"""
        ok, reason = cls.check_client()
        if not ok:
            return False, reason
        if not (task or {}).get("host"):
            return False, "任务未配置数据库主机"
        return True, ""

    # ------------------------------------------------------------------
    # 生命周期（子类实现 start）
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """启动日志流捕获。子类必须实现。"""
        raise NotImplementedError

    def stop(self, timeout: float = 10.0) -> None:
        """停止捕获并回收资源。幂等。

        停止前会做一次**强制封存**，把最后一个正在写入的段也收进 sealed/，
        避免进程退出后残留在 live/ 里永远不入 journal。
        """
        self._kill(timeout=timeout)
        try:
            self.seal_ready_segments(force=True)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 停止时封存异常: %s",
                                self.task_id, exc)
        self._close_stderr()

    def is_alive(self) -> bool:
        """子进程是否存活。无子进程模型的实现应重写本方法。"""
        return bool(self.proc and self.proc.poll() is None)

    # ------------------------------------------------------------------
    # 周期驱动
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """一次周期驱动：刷新位点 → 封存完整段 → 汇报状态。

        Returns:
            ``{'alive','segments','position','error','sealed_bytes'}``；
            ``segments`` 为本次封存的段信息列表（可直接入 journal）。
        """
        alive = self.is_alive()
        if not alive and not self.last_error:
            self.last_error = self._read_stderr_tail() or "日志流进程已退出"

        segments: List[dict] = []
        try:
            segments = self.seal_ready_segments()
        except Exception as exc:
            self.last_error = f"封存日志段失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)

        return {
            "alive": alive,
            "segments": segments,
            "position": self.current_position(),
            "error": self.last_error,
            "sealed_bytes": sum(int(s.get("size") or 0) for s in segments),
        }

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def current_position(self) -> dict:
        """守护自身已捕获到的位点（来自最近封存段 / 内部推进）。"""
        return dict(self.position)

    def source_position(self) -> dict:
        """查询源库当前最新位点，用于计算 lag。默认返回空字典。"""
        return {}

    def lag_seconds(self) -> int:
        """捕获落后秒数。默认用「最近封存时刻距今」近似。"""
        last = self.position.get("sealed_at") or self.started_at
        if not last:
            return 0
        try:
            from datetime import datetime
            return int(max(0.0, time.time()
                           - datetime.fromisoformat(str(last)).timestamp()))
        except (TypeError, ValueError):
            return 0

    def resume_from(self, state: dict) -> None:
        """从持久化状态恢复位点（进程重启后续传）。"""
        state = state or {}
        pos = state.get("position")
        if isinstance(pos, dict) and pos:
            self.position.update(pos)
            self.logger.info("[rt.cdc] task=%s 从续传状态恢复位点: %s",
                             self.task_id, self.position)

    def state(self) -> dict:
        """供 ``LogRepository.save_state`` 持久化的守护状态。"""
        return {
            "engine_key": self.engine_key,
            "position": dict(self.position),
            "segments_sealed": self.segments_sealed,
            "bytes_sealed": self.bytes_sealed,
            "started_at": self.started_at,
            "is_simulated": self.is_simulated,
        }

    # ------------------------------------------------------------------
    # 封存
    # ------------------------------------------------------------------
    def seal_ready_segments(self, force: bool = False) -> List[dict]:
        """把 live/ 下**已完整**的段搬进 sealed/<day>/。

        完整性判定（按优先级）：
          1. ``seal_all_immediately``（仿真实现）或 ``force``（停机时） → 全部可封存；
          2. 不是 live/ 中字典序最后一个文件 → 该段已被日志流轮转掉，完整；
          3. 是最后一个文件，但守护已死，或文件大小在 ``stall_sec`` 内无增长 → 可封存。

        Returns:
            段信息列表 ``[{'path','name','size','checksum','sealed_at','kind',
            'position'}]``。空段（size==0）已被 ``repo.seal`` 过滤。
        """
        live = self.repo.live_dir()
        try:
            names = sorted(name for name in os.listdir(live)
                           if not name.startswith("."))
        except OSError:
            return []
        if not names:
            return []

        alive = self.is_alive()
        ready: List[str] = []
        for idx, name in enumerate(names):
            path = os.path.join(live, name)
            if not os.path.isfile(path):
                continue
            if self.seal_all_immediately or force:
                ready.append(path)
                continue
            if idx < len(names) - 1:
                ready.append(path)
                continue
            # 最后一个：正在写入的段。只有守护已死或长时间无增长才封存
            if not alive or self._is_stalled(path):
                ready.append(path)

        sealed: List[dict] = []
        for path in ready:
            try:
                info = self.repo.seal(path, kind="db-log")
            except Exception as exc:
                self.logger.warning("[rt.cdc] task=%s 封存 %s 失败: %s",
                                    self.task_id, os.path.basename(path), exc)
                continue
            if not info:
                continue
            self._size_cache.pop(os.path.abspath(path), None)
            info["position"] = self._position_for_segment(info)
            self.segments_sealed += 1
            self.bytes_sealed += int(info.get("size") or 0)
            self.position["sealed_at"] = info.get("sealed_at") or db.now_iso()
            self.position["last_segment"] = info.get("name") or ""
            sealed.append(info)
            self.logger.info("[rt.cdc] task=%s 封存日志段 %s (%s)",
                             self.task_id, info.get("name"),
                             db.human_size(int(info.get("size") or 0)))
        if sealed:
            try:
                self.repo.save_state(self.state())
            except Exception:
                pass
        return sealed

    def _position_for_segment(self, info: dict) -> dict:
        """为一个封存段计算起止位点。子类按引擎语义重写。"""
        return dict(self.position)

    def _is_stalled(self, path: str) -> bool:
        """文件在 ``stall_sec`` 内是否没有增长（判定日志流已停滞）。"""
        key = os.path.abspath(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        now = time.time()
        prev = self._size_cache.get(key)
        if prev is None or prev[0] != size:
            self._size_cache[key] = (size, now)
            return False
        stall_sec = max(10, int(getattr(config, "RT_DB_SEAL_INTERVAL_SEC",
                                        DEFAULT_STALL_SEC)))
        return (now - prev[1]) >= stall_sec

    # ------------------------------------------------------------------
    # 子进程管理
    # ------------------------------------------------------------------
    def _popen(self, cmd: List[str], cwd: str = None,
               env: dict = None) -> Optional[subprocess.Popen]:
        """启动子进程并把 stderr 重定向到仓库内的日志文件。

        Args:
            cmd: 命令行参数列表。
            cwd: 工作目录（流式客户端通常把段写在 cwd 下）。
            env: 环境变量（密码走 env，绝不进命令行）。

        Returns:
            Popen 对象；启动失败返回 None 且 ``last_error`` 已写入原因。
        """
        self._close_stderr()
        self._stderr_path = os.path.join(self.repo.base,
                                         f"{self.engine_key}_stderr.log")
        try:
            self._stderr_fh = open(self._stderr_path, "a", encoding="utf-8",
                                   errors="replace")
        except OSError:
            self._stderr_fh = None
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd or self.repo.live_dir(),
                env=env or os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_fh or subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            self.last_error = f"启动 {cmd[0] if cmd else '?'} 失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)
            self.proc = None
            self._close_stderr()
            return None
        self.started_at = db.now_iso()
        self.logger.info("[rt.cdc] task=%s 启动 %s（pid=%s）",
                         self.task_id, self.engine_key, self.proc.pid)
        return self.proc

    def _kill(self, timeout: float = 10.0) -> None:
        """回收子进程：先 terminate，超时再 kill。幂等。"""
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is not None:
            self.proc = None
            return
        try:
            proc.terminate()
            proc.wait(timeout=max(0.5, float(timeout)))
        except subprocess.TimeoutExpired:
            self.logger.warning("[rt.cdc] task=%s 子进程未响应 terminate，强制 kill",
                                self.task_id)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as exc:
                self.logger.warning("[rt.cdc] task=%s kill 失败: %s",
                                    self.task_id, exc)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s terminate 异常: %s",
                                self.task_id, exc)
        finally:
            self.proc = None

    def _close_stderr(self) -> None:
        """关闭 stderr 文件句柄。"""
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None

    def _read_stderr_tail(self, limit: int = 400) -> str:
        """读取子进程 stderr 末尾若干字符，用于错误上报。"""
        path = self._stderr_path
        if not path or not os.path.isfile(path):
            return ""
        try:
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if size > limit:
                    fh.seek(size - limit)
                return fh.read().strip().replace("\n", " ")[-limit:]
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _auth_env(self) -> dict:
        """构造带密码的环境变量副本（密码绝不出现在命令行）。子类重写。"""
        return os.environ.copy()

    def describe(self) -> dict:
        """守护自述，供 API / UI 展示。"""
        return {
            "engine_key": self.engine_key,
            "display_name": self.display_name,
            "required_clients": list(self.required_clients),
            "is_simulated": self.is_simulated,
            "alive": self.is_alive(),
            "started_at": self.started_at,
            "position": self.current_position(),
            "segments_sealed": self.segments_sealed,
            "bytes_sealed": self.bytes_sealed,
            "degrade_reason": self.degrade_reason,
            "last_error": self.last_error,
        }
