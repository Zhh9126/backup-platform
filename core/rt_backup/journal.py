# -*- coding: utf-8 -*-
"""PIT 恢复点日志（Recovery Journal）读写。

所有写入经 ``core.models`` → ``core.db.execute()``（内含 _write_lock），
禁止自建 sqlite3 连接。时间统一 ``db.now_iso()``，同秒多点用 pit_seq 区分。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import core.db as db
import core.models as models

from .types import (
    KIND_DB_LOG,
    KIND_FILE,
    RP_BASE_FULL,
    RP_DB_FULL,
    RP_DB_LOG,
    RP_FILE_INC,
    RecoveryPoint,
    norm_path,
)

# 同秒序号分配的进程内互斥（SQLite 层还有唯一索引兜底）
_seq_lock = threading.Lock()


def _parse_ts(value: str) -> Optional[datetime]:
    """宽松解析 ISO8601；失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _to_epoch(value: str) -> float:
    """ISO8601 → epoch 秒；不可解析时返回 0.0。"""
    dt = _parse_ts(value)
    return dt.timestamp() if dt else 0.0


class RecoveryJournal:
    """恢复点日志仓库。无状态，可安全地在多个线程各自实例化。"""

    def __init__(self, logger=None) -> None:
        self.logger = logger or db.get_logger("rt.journal")

    # ---------------- 写入 ----------------
    def append(self, task_id: int, point: dict) -> RecoveryPoint:
        """原子追加一个恢复点。

        自动分配 ``pit_seq``（同 pit_at 内自增），自动计算 ``expires_at``。
        ``object_key`` 冲突时走幂等更新而非报错。

        Args:
            task_id: 任务 id。
            point: 恢复点字段字典，至少含 ``object_key``。
                   可选 ``retention_days`` 用于计算过期时间。

        Returns:
            落库后的 RecoveryPoint（含 id）。
        """
        data = dict(point or {})
        data["task_id"] = int(task_id)
        pit_at = data.get("pit_at") or db.now_iso()
        data["pit_at"] = pit_at

        retention_days = int(data.pop("retention_days", 0) or 0)
        if not data.get("expires_at") and retention_days > 0:
            base_dt = _parse_ts(pit_at) or datetime.now()
            data["expires_at"] = (base_dt + timedelta(days=retention_days)) \
                .isoformat(timespec="seconds")

        # 自动补齐大小 / 校验和（本地 Tier1 产物）
        object_key = data.get("object_key") or ""
        if object_key and int(data.get("storage_tier") or 1) == 1 \
                and os.path.isfile(object_key):
            if not data.get("size_bytes"):
                data["size_bytes"] = os.path.getsize(object_key)
            if not data.get("checksum"):
                data["checksum"] = db.sha256_file(object_key)

        with _seq_lock:
            if data.get("pit_seq") is None:
                data["pit_seq"] = models.next_pit_seq(int(task_id), pit_at)
            rp_id = models.create_recovery_point(data)

        row = models.get_recovery_point(rp_id) or {}
        rp = RecoveryPoint.from_row(row)
        self.logger.info("[rt.journal] task=%s 追加恢复点 #%s %s @%s (%s)",
                         task_id, rp.id, rp.rp_kind, rp.pit_at,
                         db.human_size(rp.size_bytes))
        return rp

    def mark_uploaded(self, rp_id: int, set_id: int, tier: int,
                      bundle_key: str = None) -> None:
        """回填上云信息（backup_sets 关联 + 所属 bundle）。"""
        models.update_recovery_point(int(rp_id), {
            "set_id": int(set_id) if set_id else None,
            "storage_tier": int(tier or 1),
            "bundle_key": bundle_key or "",
        })

    def mark_verified(self, rp_id: int, ok: bool, msg: str = "") -> None:
        """标记校验结果。"""
        models.update_recovery_point(int(rp_id), {
            "verified": 1 if ok else 0,
            "verify_msg": msg or "",
        })

    # ---------------- 查询 ----------------
    def get(self, rp_id: int) -> Optional[RecoveryPoint]:
        row = models.get_recovery_point(int(rp_id))
        return RecoveryPoint.from_row(row) if row else None

    def list_points(self, task_id: int, start: str = None, end: str = None,
                    kind: str = None, limit: int = 500, offset: int = 0,
                    order: str = "desc") -> List[RecoveryPoint]:
        """按条件列出恢复点。默认按时间倒序（UI 明细用）。"""
        rows = models.list_recovery_points(
            task_id=int(task_id), start=start, end=end, kind=kind,
            limit=int(limit), offset=int(offset), order=order)
        return [RecoveryPoint.from_row(r) for r in rows]

    def latest(self, task_id: int, kind: str = None) -> Optional[RecoveryPoint]:
        """最近一个恢复点。"""
        rows = models.list_recovery_points(task_id=int(task_id), kind=kind,
                                           limit=1, order="desc")
        return RecoveryPoint.from_row(rows[0]) if rows else None

    def nearest_before(self, task_id: int, target_ts: str,
                       kind: str = None) -> Optional[RecoveryPoint]:
        """时间轴选点核心：返回 pit_at <= target_ts 的最近一个恢复点。"""
        rows = models.list_recovery_points(
            task_id=int(task_id), end=target_ts, kind=kind,
            limit=1, order="desc")
        return RecoveryPoint.from_row(rows[0]) if rows else None

    def count(self, task_id: int, kind: str = None) -> int:
        return models.count_recovery_points(task_id=int(task_id), kind=kind)

    # ---------------- 恢复链 ----------------
    def resolve_chain(self, task_id: int, target_ts: str) -> List[RecoveryPoint]:
        """解析到 target_ts 的完整恢复链（按 pit_at 升序）。

        File: ``[最近的 base-full]`` + 其后到 target_ts 的所有 ``file-inc``
        DB  : ``[最近的 db-full]``   + 其后到 target_ts 的所有 ``db-log`` 段

        链头缺失时返回仅含增量/日志段的列表，由 :meth:`validate_chain` 判定不完整。
        """
        task_id = int(task_id)
        target_ts = target_ts or db.now_iso()

        # 先判定该任务属于哪种恢复语义：优先看是否存在 db-log 恢复点
        db_log_cnt = models.count_recovery_points(task_id, kind=RP_DB_LOG)
        if db_log_cnt > 0:
            full_kind, inc_kind = RP_DB_FULL, RP_DB_LOG
        else:
            full_kind, inc_kind = RP_BASE_FULL, RP_FILE_INC

        base = self.nearest_before(task_id, target_ts, kind=full_kind)
        start = base.pit_at if base else None
        inc_rows = models.list_recovery_points(
            task_id=task_id, start=start, end=target_ts, kind=inc_kind,
            limit=100000, order="asc")
        chain: List[RecoveryPoint] = []
        if base:
            chain.append(base)
        for row in inc_rows:
            point = RecoveryPoint.from_row(row)
            # 与链头同一时刻的增量也要纳入（同秒多点场景）
            if base and point.pit_at == base.pit_at and point.pit_seq < base.pit_seq:
                continue
            chain.append(point)
        # DB 日志段补纳：段封存时间(pit_at)常晚于目标时间点，但其内容
        # 覆盖到封存前一刻的事件；若严格按 pit_at<=target 过滤会把覆盖
        # 目标窗口的最后一个段排除掉，导致回放缺段。这里补纳首个
        # pit_at > target_ts 的日志段，精确截断由回放侧
        # --stop-datetime / --stop-position 保证（文件/增量段不适用）。
        if inc_kind == RP_DB_LOG:
            try:
                nxt_rows = models.list_recovery_points(
                    task_id=task_id, start=target_ts, kind=inc_kind,
                    limit=1, order="asc")
            except Exception:
                nxt_rows = []
            for row in nxt_rows:
                point = RecoveryPoint.from_row(row)
                if point.pit_at <= target_ts:
                    continue  # 已在链中（end<=target 已纳入）
                if any(p.id == point.id for p in chain):
                    continue
                chain.append(point)
        return chain

    def validate_chain(self, chain: List[RecoveryPoint]) -> Tuple[bool, str]:
        """校验链完整性。

        依次检查：①链头必须是 full；②相邻节点 parent_rp_id 连续；
        ③DB 段位点连续；④object_key 对应文件存在且 checksum 匹配。

        Returns:
            ``(ok, reason)``；ok=True 时 reason 为空字符串。
        """
        if not chain:
            return False, "恢复链为空：该时间点之前没有任何可用恢复点"

        head = chain[0]
        if not head.is_full:
            return False, (f"链头缺失基准全量：最早可用点 #{head.id} 为 "
                           f"{head.rp_kind}，请选择更晚的时间点或先做一次全量备份")

        prev: Optional[RecoveryPoint] = None
        for point in chain:
            # ④ 产物存在性与校验和
            if point.storage_tier == 1:
                if not point.object_key or not os.path.isfile(point.object_key):
                    return False, (f"恢复点 #{point.id}（{point.pit_at}）产物缺失: "
                                   f"{norm_path(point.object_key)}")
                if point.checksum:
                    actual = db.sha256_file(point.object_key)
                    if actual != point.checksum:
                        return False, (f"恢复点 #{point.id}（{point.pit_at}）校验和不匹配，"
                                       f"产物可能已损坏")
                elif os.path.getsize(point.object_key) <= 0:
                    return False, f"恢复点 #{point.id}（{point.pit_at}）产物为空文件"

            if prev is not None:
                # ② parent 连续性（允许 parent_rp_id 为空的历史数据，仅告警不阻断）
                if point.parent_rp_id and point.parent_rp_id != prev.id:
                    return False, (f"恢复链断裂：#{point.id} 的父节点为 "
                                   f"#{point.parent_rp_id}，但链上前驱是 #{prev.id}，"
                                   f"中间存在缺失的恢复点")
                # ③ DB 段位点连续性
                if point.rp_kind == RP_DB_LOG and prev.rp_kind == RP_DB_LOG:
                    ok, reason = self._check_position_continuity(prev, point)
                    if not ok:
                        return False, reason
            prev = point

        return True, ""

    @staticmethod
    def _check_position_continuity(prev: RecoveryPoint,
                                   cur: RecoveryPoint) -> Tuple[bool, str]:
        """校验相邻两个 DB 日志段的位点是否连续。"""
        # MySQL：前段 end_file/end_pos 应等于后段的起始 file/pos，
        # 或后段起始于下一个 binlog 文件的头部（pos<=4）
        if prev.binlog_end_file and cur.binlog_file:
            same_file = prev.binlog_end_file == cur.binlog_file
            if same_file and cur.binlog_pos > prev.binlog_end_pos:
                return False, (f"binlog 位点不连续：{prev.binlog_end_file}:"
                               f"{prev.binlog_end_pos} → {cur.binlog_file}:"
                               f"{cur.binlog_pos}，中间有日志缺口")
            if not same_file and cur.binlog_pos > 4:
                return False, (f"binlog 文件切换处位点异常：{cur.binlog_file}:"
                               f"{cur.binlog_pos}（应从文件头 4 开始）")
        # PG：前段 end_lsn 应 <= 后段起始 lsn（字符串比较不可靠，改为解析）
        if prev.wal_end_lsn and cur.wal_lsn:
            prev_val = RecoveryJournal._lsn_to_int(prev.wal_end_lsn)
            cur_val = RecoveryJournal._lsn_to_int(cur.wal_lsn)
            if prev_val and cur_val and cur_val > prev_val:
                return False, (f"WAL LSN 不连续：{prev.wal_end_lsn} → {cur.wal_lsn}，"
                               f"中间有 WAL 缺口")
        return True, ""

    @staticmethod
    def _lsn_to_int(lsn: str) -> int:
        """把 PG LSN（形如 ``0/1A2B3C48``）转成整数；不可解析返回 0。"""
        try:
            high, low = str(lsn).split("/", 1)
            return (int(high, 16) << 32) + int(low, 16)
        except (ValueError, AttributeError):
            return 0

    # ---------------- 清理 ----------------
    def prune(self, task_id: int, retention_days: int, repo=None) -> int:
        """删除过期恢复点（DB 行 + 磁盘文件）。

        永不删除仍被"未过期恢复链"引用的 full 链头；
        删除顺序为先删 DB 行、再删磁盘文件（避免留下无法索引的孤儿）。

        Args:
            task_id: 任务 id。
            retention_days: 保留天数。
            repo: 可选 LogRepository，用于安全删除（限制在仓库根内）。

        Returns:
            实际删除的恢复点数量。
        """
        task_id = int(task_id)
        days = max(1, int(retention_days or 1))
        cutoff_dt = datetime.now().astimezone() - timedelta(days=days)
        cutoff = cutoff_dt.isoformat(timespec="seconds")

        rows = models.list_recovery_points(task_id=task_id, limit=100000,
                                           order="asc")
        if not rows:
            return 0
        points = [RecoveryPoint.from_row(r) for r in rows]

        # 找出"仍需保留的最新 full 链头"：所有未过期增量所依赖的那个 full
        survivors = [p for p in points if p.pit_at >= cutoff]
        protected_full_ids = set()
        if survivors:
            earliest_survivor = survivors[0]
            for point in points:
                if point.is_full and point.pit_at <= earliest_survivor.pit_at:
                    protected_full_ids = {point.id}   # 逐个覆盖 → 保留最后一个
        # 无幸存增量时，仍保留最近一个 full，保证"至少能恢复到某个点"
        if not protected_full_ids:
            fulls = [p for p in points if p.is_full]
            if fulls:
                protected_full_ids = {fulls[-1].id}

        victims = [p for p in points
                   if p.pit_at < cutoff and p.id not in protected_full_ids]
        if not victims:
            return 0

        # 先删 DB 行
        models.delete_recovery_points([p.id for p in victims])
        # 再删磁盘文件
        for point in victims:
            if point.storage_tier != 1 or not point.object_key:
                continue
            if repo is not None:
                repo.remove_object(point.object_key)
            elif os.path.isfile(point.object_key):
                try:
                    os.remove(point.object_key)
                except OSError as exc:
                    self.logger.warning("[rt.journal] 删除产物失败 %s: %s",
                                        norm_path(point.object_key), exc)
        if repo is not None:
            repo.prune_empty_dirs()

        self.logger.info("[rt.journal] task=%s prune 保留 %d 天，删除 %d 个恢复点",
                         task_id, days, len(victims))
        db.add_log("info", "rt.journal",
                   f"任务 {task_id} 清理过期恢复点 {len(victims)} 个（保留 {days} 天）")
        return len(victims)

    # ---------------- 时间轴聚合 ----------------
    def timeline(self, task_id: int, start: str = None, end: str = None,
                 buckets: int = 200, detail_limit: int = 200) -> dict:
        """给前端时间轴的聚合数据。

        Returns:
            ``{'kind','start','end','buckets':[{'ts','count','bytes','has_gap'}],
               'points':[...],'gaps':[{'from','to','reason'}],'total'}``
        """
        task_id = int(task_id)
        buckets = max(10, min(int(buckets or 200), 2000))

        end = end or db.now_iso()
        if not start:
            start_dt = (_parse_ts(end) or datetime.now().astimezone()) - timedelta(days=1)
            start = start_dt.isoformat(timespec="seconds")

        rows = models.list_recovery_points(task_id=task_id, start=start, end=end,
                                           limit=100000, order="asc")
        points = [RecoveryPoint.from_row(r) for r in rows]

        db_log_cnt = sum(1 for p in points if p.rp_kind in (RP_DB_LOG, RP_DB_FULL))
        kind = KIND_DB_LOG if db_log_cnt > 0 else KIND_FILE

        t0, t1 = _to_epoch(start), _to_epoch(end)
        if t1 <= t0:
            t1 = t0 + 1.0
        span = (t1 - t0) / buckets

        grid = [{"ts": datetime.fromtimestamp(t0 + i * span)
                 .astimezone().isoformat(timespec="seconds"),
                 "count": 0, "bytes": 0, "has_gap": False}
                for i in range(buckets)]

        for point in points:
            epoch = _to_epoch(point.pit_at)
            idx = int((epoch - t0) / span) if span > 0 else 0
            idx = max(0, min(buckets - 1, idx))
            grid[idx]["count"] += 1
            grid[idx]["bytes"] += int(point.size_bytes or 0)

        # 缺口检测：相邻恢复点间隔超过 3 倍中位间隔即视为缺口
        gaps = self._detect_gaps(points)
        for gap in gaps:
            g0, g1 = _to_epoch(gap["from"]), _to_epoch(gap["to"])
            i0 = max(0, min(buckets - 1, int((g0 - t0) / span) if span > 0 else 0))
            i1 = max(0, min(buckets - 1, int((g1 - t0) / span) if span > 0 else 0))
            for i in range(i0, i1 + 1):
                grid[i]["has_gap"] = True

        detail = points[-int(detail_limit):] if detail_limit else points
        return {
            "kind": kind,
            "start": start,
            "end": end,
            "buckets": grid,
            "points": [p.to_dict() for p in detail],
            "gaps": gaps,
            "total": len(points),
            "total_bytes": sum(int(p.size_bytes or 0) for p in points),
        }

    @staticmethod
    def _detect_gaps(points: List[RecoveryPoint]) -> List[dict]:
        """基于相邻间隔的中位数检测时间轴缺口。"""
        if len(points) < 3:
            return []
        stamps = [_to_epoch(p.pit_at) for p in points]
        deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        if not deltas:
            return []
        deltas_sorted = sorted(deltas)
        median = deltas_sorted[len(deltas_sorted) // 2]
        threshold = max(median * 3, 60.0)
        gaps: List[dict] = []
        for idx in range(1, len(points)):
            delta = stamps[idx] - stamps[idx - 1]
            if delta > threshold:
                gaps.append({
                    "from": points[idx - 1].pit_at,
                    "to": points[idx].pit_at,
                    "seconds": int(delta),
                    "reason": f"间隔 {int(delta)}s 超过正常节奏（约 {int(median)}s）的 3 倍",
                })
        return gaps
