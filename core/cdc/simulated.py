# -*- coding: utf-8 -*-
"""
仿真 CDC 守护（Simulated）。

用途：
1. ``DEMO_MODE=on`` 或任务标记 ``demo_only`` 时的默认实现；
2. 真实客户端（mysqlbinlog / pg_receivewal）缺失时的**兜底**实现——
   保证「准 CDP 全链路」在任何环境都能跑通、能演示、能被测试覆盖。

行为：每次 :meth:`tick` 生成一个体积很小但内容真实（含元数据 JSON）的日志段，
并按引擎语义推进伪位点：

- MySQL 系：``mysql-bin.000001:4 → mysql-bin.000001:1234 → ...``，
  超过阈值后轮转到下一个 binlog 文件；
- PostgreSQL：``0/1000000 → 0/1010000 → ...`` 的 LSN 递增。

所有由本实现产生的恢复点都会被打上 ``is_simulated=1``，UI 上以「仿真」徽标
区分，绝不冒充真实备份。
"""
from __future__ import annotations

import json
import os
import time
from typing import Tuple

import core.db as db

from .base import CDCDaemon

# 单个 binlog 文件的伪最大位点，超过即轮转
_BINLOG_ROTATE_POS = 100000
# 每次 tick 推进的伪字节数
_POS_STEP = 4096
# LSN 每次推进量
_LSN_STEP = 0x10000


class SimulatedCDCDaemon(CDCDaemon):
    """仿真日志流守护：无外部依赖，恒可用。"""

    engine_key = "simulated"
    display_name = "仿真日志流（无客户端兜底）"
    required_clients = []
    is_simulated = True
    # 仿真段是「一次写完」的，因此可以立即封存
    seal_all_immediately = True

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self._running: bool = False
        self._seq: int = 0
        self._source_engine: str = (self.task.get("db_type") or "mysql").lower()
        self.degrade_reason = "仿真日志流：未使用真实数据库客户端"

        # 位点初值按引擎语义区分
        if self._is_pg():
            self.position = {"wal_lsn": "0/1000000", "wal_end_lsn": "0/1000000"}
        else:
            self.position = {"binlog_file": "mysql-bin.000001", "binlog_pos": 4,
                             "binlog_end_file": "mysql-bin.000001",
                             "binlog_end_pos": 4}

    # ------------------------------------------------------------------
    @classmethod
    def check_client(cls) -> Tuple[bool, str]:
        """仿真实现不依赖任何客户端，恒可用。"""
        return True, ""

    @classmethod
    def is_available(cls, task: dict) -> Tuple[bool, str]:
        """恒可用——这是整条链路的兜底实现。"""
        return True, ""

    def _is_pg(self) -> bool:
        return self._source_engine in ("postgresql", "postgres", "pg")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """标记为运行中。仿真实现不拉起任何子进程。"""
        self._running = True
        self.started_at = db.now_iso()
        self.last_error = ""
        state = {}
        try:
            state = self.repo.load_state() or {}
        except Exception:
            state = {}
        self.resume_from(state)
        self.logger.info("[rt.cdc] task=%s 启动仿真日志流（源引擎=%s）",
                         self.task_id, self._source_engine)
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """停止仿真流，并把 live/ 中残留段全部封存。"""
        self._running = False
        try:
            self.seal_ready_segments(force=True)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 仿真停止封存异常: %s",
                                self.task_id, exc)
        try:
            self.repo.save_state(self.state())
        except Exception:
            pass

    def is_alive(self) -> bool:
        """仿真流在 start 之后恒存活。"""
        return self._running

    # ------------------------------------------------------------------
    # 周期驱动
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """生成一个仿真日志段并立即封存。"""
        if not self._running:
            return {"alive": False, "segments": [], "position": self.current_position(),
                    "error": self.last_error, "sealed_bytes": 0}
        try:
            self._write_segment()
        except Exception as exc:
            self.last_error = f"生成仿真日志段失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)
        return super().tick()

    def _write_segment(self) -> str:
        """在 live/ 写一个完整的仿真日志段，并推进位点。

        Returns:
            段文件绝对路径。
        """
        self._seq += 1
        start_pos = self._snapshot_position()
        self._advance()
        end_pos = self._snapshot_position()

        payload = {
            "_simulated": True,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "engine": self._source_engine,
            "seq": self._seq,
            "generated_at": db.now_iso(),
            "start_position": start_pos,
            "end_position": end_pos,
            "note": "仿真日志段：内容不可用于真实恢复，仅用于演示与链路验证",
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        if self._is_pg():
            name = f"{int(time.time())}_{self._seq:06d}.simwal"
        else:
            name = f"{self.position.get('binlog_file', 'mysql-bin.000001')}." \
                   f"{self._seq:06d}.simlog"
        path = os.path.join(self.repo.live_dir(), name)

        def _write(tmp_path: str) -> None:
            with open(tmp_path, "wb") as fh:
                fh.write(body)

        self.repo.atomic_write(_write, path, suffix=".simlog")
        self._pending_start = start_pos
        self._pending_end = end_pos
        return path

    def _snapshot_position(self) -> dict:
        """当前位点的浅拷贝（只含引擎相关字段）。"""
        if self._is_pg():
            return {"wal_lsn": self.position.get("wal_lsn", "0/1000000")}
        return {"binlog_file": self.position.get("binlog_file", "mysql-bin.000001"),
                "binlog_pos": int(self.position.get("binlog_pos", 4))}

    def _advance(self) -> None:
        """推进伪位点。"""
        if self._is_pg():
            cur = self._lsn_to_int(self.position.get("wal_lsn", "0/1000000"))
            nxt = cur + _LSN_STEP
            self.position["wal_lsn"] = self._int_to_lsn(nxt)
            self.position["wal_end_lsn"] = self.position["wal_lsn"]
            return
        pos = int(self.position.get("binlog_pos", 4)) + _POS_STEP
        name = self.position.get("binlog_file", "mysql-bin.000001")
        if pos >= _BINLOG_ROTATE_POS:
            try:
                stem, num = name.rsplit(".", 1)
                name = f"{stem}.{int(num) + 1:06d}"
            except ValueError:
                name = "mysql-bin.000002"
            pos = 4
        self.position["binlog_file"] = name
        self.position["binlog_pos"] = pos
        self.position["binlog_end_file"] = name
        self.position["binlog_end_pos"] = pos

    def _position_for_segment(self, info: dict) -> dict:
        """仿真段的起止位点取写入时记录的快照。"""
        start = getattr(self, "_pending_start", {}) or {}
        end = getattr(self, "_pending_end", {}) or {}
        merged = dict(start)
        if self._is_pg():
            merged["wal_end_lsn"] = end.get("wal_lsn", "")
        else:
            merged["binlog_end_file"] = end.get("binlog_file", "")
            merged["binlog_end_pos"] = end.get("binlog_pos", 0)
        return merged

    def source_position(self) -> dict:
        """仿真源位点恒等于已捕获位点（lag=0）。"""
        return self._snapshot_position()

    def lag_seconds(self) -> int:
        """仿真流永远不落后。"""
        return 0

    # ------------------------------------------------------------------
    @staticmethod
    def _lsn_to_int(lsn: str) -> int:
        """``0/1A2B3C48`` → int；不可解析返回 0x1000000。"""
        try:
            high, low = str(lsn).split("/", 1)
            return (int(high, 16) << 32) + int(low, 16)
        except (ValueError, AttributeError):
            return 0x1000000

    @staticmethod
    def _int_to_lsn(value: int) -> str:
        """int → ``0/1A2B3C48``。"""
        return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"
