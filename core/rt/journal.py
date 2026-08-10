# -*- coding: utf-8 -*-
"""
PIT 恢复点日志（Recovery Journal）读写。

本类提供 T01 验收标准所需的方法接口：
  - record(rp)           写入一条恢复点
  - list_by_task(...)    按任务查恢复点
  - list_by_time_range   按时间范围查恢复点
  - get_latest(...)      获取最新恢复点
  - find_chain(...)      给定目标时间返回有序恢复链（PITR 恢复引擎核心）
  - delete_by_task(...)  任务删除时清理所有恢复点记录

底层复用 core/rt_backup/journal.py 的 RecoveryJournal 实现，
本类是对其的 T01 验收接口适配层，方法名对齐任务需求描述。
上层 supervisor / pitr / health 可直接使用 rt_backup 版本（功能更全），
也可使用本版本（接口更简明）。
"""
from typing import List, Optional

import core.db as db
import core.models as models
from core.rt_backup.journal import RecoveryJournal as _InnerJournal
from core.rt_backup.types import RecoveryPoint


class RecoveryJournal:
    """PIT 恢复点日志仓库。无状态，可安全地在多个线程各自实例化。

    方法名与 T01 任务需求对齐，底层委托 core/rt_backup/journal.py 实现。
    """

    def __init__(self, logger=None) -> None:
        self._inner = _InnerJournal(logger=logger)
        self.logger = logger or db.get_logger("rt.journal")

    # ---------------- 写入 ----------------
    def record(self, rp: dict) -> int:
        """写入一条恢复点到 recovery_journal 表。

        Args:
            rp: 恢复点字段字典，至少含 task_id 和 object_key。
                可包含 rp_kind、pit_at、binlog_file、binlog_pos、
                wal_lsn、file_snapshot_hash 等字段。
                若 rp 含 "rp_kind" 但值不在此表字段集中（如设计文档的扩展字段）
                则直接写入 recovery_journal 的对应列。

        Returns:
            新恢复点 id。
        """
        data = dict(rp or {})
        task_id = int(data.get("task_id") or 0)
        if task_id <= 0:
            raise ValueError("record() 需要 rp['task_id'] > 0")

        point = self._inner.append(task_id, data)
        return int(point.id)

    # ---------------- 查询 ----------------
    def list_by_task(self, task_id: int, kind: str = None,
                     limit: int = 50) -> List[dict]:
        """按 task 查恢复点，可选按 kind 过滤。

        Args:
            task_id: 任务 id。
            kind: 可选，按 rp_kind 过滤（如 'file-inc'、'db-log'、'base-full'）。
            limit: 返回数量上限，默认 50。

        Returns:
            恢复点字典列表（按 pit_at 降序）。
        """
        rows = models.list_recovery_points(
            task_id=int(task_id), kind=kind,
            limit=int(limit), order="desc")
        return [dict(r) for r in rows]

    def list_by_time_range(self, task_id: int, start: str, end: str,
                           kind: str = None, limit: int = 500) -> List[dict]:
        """按时间范围查恢复点（用于 PITR 恢复时间轴）。

        Args:
            task_id: 任务 id。
            start: 起始时间 ISO8601。
            end: 结束时间 ISO8601。
            kind: 可选，按 rp_kind 过滤。
            limit: 返回数量上限。

        Returns:
            恢复点字典列表（按 pit_at 升序，用于恢复链）。
        """
        rows = models.list_recovery_points(
            task_id=int(task_id), start=start, end=end, kind=kind,
            limit=int(limit), order="asc")
        return [dict(r) for r in rows]

    def get_latest(self, task_id: int, kind: str = None) -> Optional[dict]:
        """获取最新恢复点。

        Args:
            task_id: 任务 id。
            kind: 可选，按 rp_kind 过滤。

        Returns:
            最新恢复点字典，无结果返回 None。
        """
        rows = models.list_recovery_points(
            task_id=int(task_id), kind=kind,
            limit=1, order="desc")
        return dict(rows[0]) if rows else None

    # ---------------- 恢复链 ----------------
    def find_chain(self, task_id: int, target_time: str) -> List[dict]:
        """给定目标恢复时间点，返回一条有序恢复链。

        链结构：
          [全量/基准, 增量1, 增量2, ..., db_log段1, db_log段2, ...]

        恢复链按 pit_at 升序排列，链头为 base-full/db-full，
        其后为对应的增量/日志段。T04 PITR 恢复引擎会调用此方法。

        Args:
            task_id: 任务 id。
            target_time: 目标恢复时间点（ISO8601）。

        Returns:
            恢复点字典列表（按 pit_at 升序）。链不完整时仍返回最接近的有序子集，
            由调用方校验完整性。
        """
        chain: List[RecoveryPoint] = self._inner.resolve_chain(
            int(task_id), target_time)
        return [p.to_dict() for p in chain]

    # ---------------- 删除 ----------------
    def delete_by_task(self, task_id: int) -> int:
        """任务删除时清理所有恢复点记录。

        Args:
            task_id: 任务 id。

        Returns:
            删除的恢复点数量。
        """
        rows = models.list_recovery_points(
            task_id=int(task_id), limit=100000, order="asc")
        count = len(rows)
        if count > 0:
            ids = [int(r["id"]) for r in rows]
            models.delete_recovery_points(ids)
        self.logger.info("[rt.journal] delete_by_task task=%s, 删除 %d 个恢复点",
                         task_id, count)
        return count
