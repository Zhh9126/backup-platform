# -*- coding: utf-8 -*-
"""
MySQL / MariaDB binlog 流式捕获守护。

实现方式（优先级从高到低）：

1. **mysqlbinlog --read-from-remote-server --raw --stop-never**（首选）
   官方客户端伪装成从库持续拉取 binlog，原样落盘到 ``live/``，
   段完整性由 binlog 轮转天然保证（旧文件不再被写）。
2. **python-mysql-replication**（可选依赖 ``mysql-replication``）
   仅在 mysqlbinlog 缺失但 Python 包存在时用于**位点探测**，
   不作为主捕获路径（纯 Python 解析吞吐不足以承担生产日志量）。

不满足条件时由 :mod:`core.cdc` 工厂降级到
:class:`core.cdc.simulated.SimulatedCDCDaemon`。

安全：密码一律经 ``MYSQL_PWD`` 环境变量传递，绝不出现在命令行（会被 ps 看到）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

import config
import core.db as db

from .base import CDCDaemon

# 形如 mysql-bin.000007 的 binlog 文件名
_BINLOG_NAME_RE = re.compile(r"^(?P<stem>.+)\.(?P<num>\d{6,})$")

# mysql 客户端候选目录（离线环境不依赖 PATH；与 base._COMMON_TOOL_DIR_GLOBS 同源思路）
_MYSQL_CLIENT_CANDIDATES = (
    "/opt/mysql*/bin/mysql", "/usr/local/mysql*/bin/mysql",
    "/usr/local/bin/mysql", "/usr/bin/mysql", "/opt/mysql840b/bin/mysql",
)


def _resolve_mysql_client() -> str:
    """解析平台侧 mysql 客户端：PATH → 常见安装目录 glob。"""
    found = shutil.which("mysql")
    if found:
        return found
    import glob as _glob
    for pattern in _MYSQL_CLIENT_CANDIDATES:
        for cand in sorted(_glob.glob(pattern)):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return ""


def check_binlog_ready(task: dict) -> Dict:
    """MySQL/MariaDB 实时任务开启前预检：log_bin 是否开启、binlog_format
    是否 ROW、位点是否可取。错误前置——此前用户只能等 worker 启动失败
    （"无法获取源库当前 binlog 位点"）才在日志里发现。

    Returns:
        {"ok": bool, "level": "pass|fail|warn", "message": str, "detail": {...}}
    """
    host = str(task.get("host") or "127.0.0.1")
    port = int(task.get("port") or 3306)
    user = str(task.get("username") or "root")
    try:
        import core.db as _db
        pw = _db.decrypt_secret(str(task.get("password") or ""))
    except Exception:  # noqa: BLE001
        pw = str(task.get("password") or "")

    detail = {"host": host, "port": port}
    mysql_bin = _resolve_mysql_client()
    if not mysql_bin:
        return {"ok": False, "level": "fail",
                "message": "平台未找到 mysql 客户端（PATH 与常见安装目录均已探测）",
                "detail": detail}
    env = os.environ.copy()
    if pw:
        env["MYSQL_PWD"] = pw
    cmd = [mysql_bin, "--no-defaults", "-h", host, "-P", str(port),
           "-u", user, "-N", "-e",
           "SHOW VARIABLES WHERE Variable_name IN "
           "('log_bin','binlog_format')"]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "level": "fail",
                "message": f"无法连接源库探测 binlog 配置: {exc}",
                "detail": detail}
    if out.returncode != 0:
        return {"ok": False, "level": "fail",
                "message": f"源库连接失败: {(out.stderr or '')[:200]}",
                "detail": detail}
    vars_ = {}
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            vars_[parts[0]] = parts[1]
    detail["variables"] = vars_
    log_bin = (vars_.get("log_bin") or "").upper()
    fmt = (vars_.get("binlog_format") or "").upper()
    if log_bin in ("OFF", "0", "FALSE", ""):
        return {"ok": False, "level": "fail",
                "message": ("源库未开启 binlog（log_bin=OFF）。请在 my.cnf 配置 "
                            "log_bin=mysql-bin 并重启实例后重试；未开启 binlog "
                            "无法提供实时备份与时间点恢复"),
                "detail": detail}
    if fmt and fmt not in ("ROW",):
        return {"ok": True, "level": "warn",
                "message": (f"binlog_format={fmt}（建议 ROW）：非 ROW 格式下"
                            "binlog 捕获按语句回放，跨时区/不确定性语句有恢复"
                            "偏差风险"),
                "detail": detail}
    return {"ok": True, "level": "pass",
            "message": "binlog 已开启且格式为 ROW", "detail": detail}


def _import_mysql_replication():
    """惰性导入 ``mysql-replication``（可选依赖）。

    Returns:
        ``(BinLogStreamReader | None, 原因)``。
    """
    try:
        from pymysqlreplication import BinLogStreamReader  # type: ignore
        return BinLogStreamReader, ""
    except ImportError as exc:
        return None, f"mysql-replication 未安装（{exc}）"
    except Exception as exc:  # pragma: no cover —— 保护性兜底
        return None, f"mysql-replication 导入失败: {exc}"


class MySQLBinlogDaemon(CDCDaemon):
    """MySQL / MariaDB 的 binlog 持续捕获守护。"""

    engine_key = "mysql_binlog"
    display_name = "MySQL binlog 流式捕获"
    required_clients = ["mysqlbinlog"]
    is_simulated = False
    seal_all_immediately = False

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self.port = int(self.task.get("port") or 3306)
        self.username = self.task.get("username") or "root"
        # server-id 必须与源库及其它从库不同，用 task_id 偏移保证进程内唯一
        self.server_id: int = 100000 + (self.task_id % 100000)
        self.position.setdefault("binlog_file", "")
        self.position.setdefault("binlog_pos", 4)

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    @classmethod
    def is_available(cls, task: dict) -> Tuple[bool, str]:
        """mysqlbinlog 可用 + 主机信息齐全时才可用。"""
        ok, reason = cls.check_client()
        if not ok:
            return False, reason
        if not (task or {}).get("host"):
            return False, "任务未配置数据库主机"
        return True, ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """拉起 mysqlbinlog 持续拉流子进程。"""
        state = {}
        try:
            state = self.repo.load_state() or {}
        except Exception:
            state = {}
        self.resume_from(state)

        start_file = str(self.position.get("binlog_file") or "")
        if not start_file:
            src = self.source_position()
            start_file = str(src.get("binlog_file") or "")
            if src.get("binlog_pos"):
                self.position["binlog_pos"] = int(src["binlog_pos"])
        if not start_file:
            # 拿不到起始文件名就无法拉流，交由工厂/Supervisor 降级
            self.last_error = "无法获取源库当前 binlog 文件名（SHOW MASTER STATUS 失败）"
            self.degrade_reason = self.last_error
            self.logger.warning("[rt.cdc] task=%s %s", self.task_id, self.last_error)
            return False
        self.position["binlog_file"] = start_file

        cmd = [
            "mysqlbinlog",
            # --no-defaults 必须最前：屏蔽 /root/.my.cnf 等残留 [client]
            # password——配置文件优先级高于 MYSQL_PWD 环境变量，会导致
            # "Access denied"（与 restore_extras 同源问题）
            "--no-defaults",
            "--read-from-remote-server",
            "--raw",
            "--stop-never",
            f"--stop-never-slave-server-id={self.server_id}",
            "--host", str(self.host),
            "--port", str(self.port),
            "--user", str(self.username),
            "--result-file", "",          # 落到 cwd，占位保持前缀为空
            start_file,
        ]
        # --result-file 传空串在部分版本会报错，直接移除该对参数
        idx = cmd.index("--result-file")
        del cmd[idx:idx + 2]

        proc = self._popen(cmd, cwd=self.repo.live_dir(), env=self._auth_env())
        if proc is None:
            return False
        self.last_error = ""
        db.add_log("info", "rt.cdc",
                   f"任务 {self.task_name} 启动 binlog 流式捕获（起始 {start_file}）")
        return True

    def _auth_env(self) -> dict:
        """密码走 MYSQL_PWD，不进命令行。"""
        env = os.environ.copy()
        if self.password:
            env["MYSQL_PWD"] = str(self.password)
        return env

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def source_position(self) -> dict:
        """查询源库 ``SHOW MASTER STATUS`` 得到最新 binlog 位点。"""
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return {}
        mysql_bin = _resolve_mysql_client()
        if not mysql_bin:
            self.logger.warning(
                "[rt.cdc] task=%s 平台未找到 mysql 客户端（已尝试 PATH 与 "
                "/opt/mysql* /usr/local/mysql* 等），无法查询 binlog 位点；"
                "请确认客户端工具就绪或在插件市场安装", self.task_id)
            return {}
        env = self._auth_env()
        cmd = [mysql_bin, "--no-defaults", "-h", str(self.host), "-P", str(self.port),
               "-u", str(self.username), "-N", "-e", "SHOW MASTER STATUS"]
        try:
            out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                 timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.warning("[rt.cdc] task=%s 查询 master status 失败: %s",
                                self.task_id, exc)
            return {}
        if out.returncode != 0:
            self.logger.warning(
                "[rt.cdc] task=%s SHOW MASTER STATUS 失败 rc=%d: %s（常见原因："
                "账号无 REPLICATION CLIENT/SLAVE 权限、连接被拒）",
                self.task_id, out.returncode,
                (out.stderr or "")[:200])
            return {}
        if not (out.stdout or "").strip():
            self.logger.warning(
                "[rt.cdc] task=%s SHOW MASTER STATUS 返回空——源库极可能未开启 "
                "log_bin（my.cnf 需配置 log_bin=mysql-bin 且重启实例）",
                self.task_id)
            return {}
        parts = out.stdout.strip().split("\t")
        try:
            return {"binlog_file": parts[0],
                    "binlog_pos": int(parts[1]) if len(parts) > 1 else 4}
        except (ValueError, IndexError):
            return {}

    def _position_for_segment(self, info: dict) -> dict:
        """由段文件名推导起止位点。

        ``mysqlbinlog --raw`` 落盘的文件名就是源端 binlog 文件名，
        因此一个段 = 一个完整 binlog 文件：起点为该文件头（pos=4），
        终点为该文件末尾（pos=文件大小）。
        """
        name = info.get("name") or ""
        size = int(info.get("size") or 0)
        pos = {
            "binlog_file": name,
            "binlog_pos": 4,
            "binlog_end_file": name,
            "binlog_end_pos": size,
        }
        self.position.update({
            "binlog_file": self._next_binlog_name(name),
            "binlog_pos": 4,
            "binlog_end_file": name,
            "binlog_end_pos": size,
        })
        return pos

    @staticmethod
    def _next_binlog_name(name: str) -> str:
        """``mysql-bin.000007`` → ``mysql-bin.000008``；无法解析时原样返回。"""
        match = _BINLOG_NAME_RE.match(name or "")
        if not match:
            return name or ""
        num = match.group("num")
        return f"{match.group('stem')}.{int(num) + 1:0{len(num)}d}"

    def lag_seconds(self) -> int:
        """用「源端最新文件 vs 已捕获文件」的差值近似落后程度。

        文件序号相同 → 0；否则按每个 binlog 段约一个封存周期估算。
        """
        src = self.source_position()
        if not src:
            return super().lag_seconds()
        src_name = str(src.get("binlog_file") or "")
        cur_name = str(self.position.get("binlog_end_file") or "")
        m_src = _BINLOG_NAME_RE.match(src_name)
        m_cur = _BINLOG_NAME_RE.match(cur_name)
        if not m_src or not m_cur:
            return super().lag_seconds()
        delta = max(0, int(m_src.group("num")) - int(m_cur.group("num")))
        if delta == 0:
            return 0
        return delta * max(10, int(config.RT_DB_SEAL_INTERVAL_SEC))

    # ------------------------------------------------------------------
    # 轮转促进
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """周期驱动：必要时 FLUSH BINARY LOGS 促使轮转，再走基类封存。"""
        result = super().tick()
        if config.RT_DB_FLUSH_LOGS and result.get("alive") and not result["segments"]:
            self._maybe_flush_logs()
        return result

    def force_seal(self) -> List[dict]:
        """手动"立即封存"（真实守护路径）：FLUSH BINARY LOGS 促成轮转，
        旧文件变为完整段后立即入账。

        此前 trigger_now 的 force 仅对仿真通道生效，真实 binlog 流的活跃段
        永远封不掉——手动触发只会得到"无可封存的完整日志段"。
        """
        try:
            self._flush_logs()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[rt.cdc] task=%s force_seal flush 失败: %s",
                              self.task_id, exc)
        import time as _time
        segments: List[dict] = []
        # 等待 mysqlbinlog 侧完成轮转落盘（通常亚秒级），短暂重试
        for _ in range(12):
            try:
                segments = self.seal_ready_segments(force=False) or []
            except Exception:  # noqa: BLE001
                segments = []
            if segments:
                break
            _time.sleep(0.3)
        return segments

    def _flush_logs(self) -> None:
        """对源库执行 FLUSH BINARY LOGS。

        必须带 --no-defaults（/root/.my.cnf 密码毒，配置文件优先级高于
        MYSQL_PWD）且客户端走目录解析兜底（与 source_position 一致）。
        """
        mysql_bin = _resolve_mysql_client()
        if not mysql_bin:
            return
        cmd = [mysql_bin, "--no-defaults", "-h", str(self.host),
               "-P", str(self.port), "-u", str(self.username), "-N",
               "-e", "FLUSH BINARY LOGS"]
        try:
            out = subprocess.run(cmd, env=self._auth_env(), capture_output=True,
                                 text=True, timeout=15)
            if out.returncode == 0:
                self.logger.info("[rt.cdc] task=%s 已触发 FLUSH BINARY LOGS",
                                 self.task_id)
            else:
                self.logger.warning(
                    "[rt.cdc] task=%s FLUSH BINARY LOGS 失败 rc=%d: %s "
                    "（需要 RELOAD 权限）",
                    self.task_id, out.returncode, (out.stderr or "")[:150])
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.debug("[rt.cdc] task=%s FLUSH BINARY LOGS 异常: %s",
                              self.task_id, exc)

    def _maybe_flush_logs(self) -> None:
        """当 live/ 只有一个长时间不增长的段时，主动 FLUSH 促使 binlog 轮转。

        轮转后旧文件不再被写入，基类的「非最后一个文件即完整」判定就能生效，
        从而把 RPO 稳定在 ``RT_DB_SEAL_INTERVAL_SEC`` 量级。
        """
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return
        if not _resolve_mysql_client():
            return
        live = self.repo.live_dir()
        try:
            names = [n for n in os.listdir(live) if not n.startswith(".")]
        except OSError:
            return
        if len(names) != 1:
            return
        path = os.path.join(live, names[0])
        if not self._is_stalled(path):
            return
        self._flush_logs()

    # ------------------------------------------------------------------
    def probe_replication_lib(self) -> dict:
        """探测可选依赖 ``mysql-replication`` 是否可用（自检面板用）。"""
        reader, reason = _import_mysql_replication()
        return {"installed": reader is not None, "reason": reason,
                "hint": "" if reader else "mysql-replication 库应随平台离线包内置（部署完整安装包以启用位点精确探测）"}
