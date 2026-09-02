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
        if not shutil.which("mysql"):
            return {}
        env = self._auth_env()
        cmd = ["mysql", "--no-defaults", "-h", str(self.host), "-P", str(self.port),
               "-u", str(self.username), "-N", "-e", "SHOW MASTER STATUS"]
        try:
            out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                 timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.debug("[rt.cdc] task=%s 查询 master status 失败: %s",
                              self.task_id, exc)
            return {}
        if out.returncode != 0 or not (out.stdout or "").strip():
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

    def _maybe_flush_logs(self) -> None:
        """当 live/ 只有一个长时间不增长的段时，主动 FLUSH 促使 binlog 轮转。

        轮转后旧文件不再被写入，基类的「非最后一个文件即完整」判定就能生效，
        从而把 RPO 稳定在 ``RT_DB_SEAL_INTERVAL_SEC`` 量级。
        """
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return
        if not shutil.which("mysql"):
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
        cmd = ["mysql", "-h", str(self.host), "-P", str(self.port),
               "-u", str(self.username), "-N", "-e", "FLUSH BINARY LOGS"]
        try:
            subprocess.run(cmd, env=self._auth_env(), capture_output=True,
                           text=True, timeout=15)
            self.logger.info("[rt.cdc] task=%s 已触发 FLUSH BINARY LOGS", self.task_id)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.debug("[rt.cdc] task=%s FLUSH BINARY LOGS 失败: %s",
                              self.task_id, exc)

    # ------------------------------------------------------------------
    def probe_replication_lib(self) -> dict:
        """探测可选依赖 ``mysql-replication`` 是否可用（自检面板用）。"""
        reader, reason = _import_mysql_replication()
        return {"installed": reader is not None, "reason": reason,
                "hint": "" if reader else "pip install mysql-replication 可启用位点精确探测"}
