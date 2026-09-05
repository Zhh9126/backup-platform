# -*- coding: utf-8 -*-
"""
GFS 保留策略（M5，Grandfather-Father-Son 祖孙父代保留法）：

按任务维度配置保留模板（extra_options.gfs_policy 或 system_config
gfs_default_policy），清理备份记录与产物：
  daily:   保留最近 N 天的每日最新一份
  weekly:  每周最后一个备份保留 N 周
  monthly: 每月最后一个备份保留 N 月
  yearly:  每年最后一个备份保留 N 年

标记删除：记录 status='expired_gfs' + detail 记录原因，产物文件可选
物理删除（delete_files=true）或保留由存储层回收。物理全量/合成全量若
仍被后续增量链引用则跳过（保守不删）。

入口：apply_gfs(task_id=None)（None=全部启用 GFS 的任务）；由 scheduler
每日周期调用。
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta

import core.db as db
import core.models as models

_logger = None


def _log():
    global _logger
    if _logger is None:
        _logger = db.get_logger("gfs")
    return _logger


def _policy_of(task: dict) -> dict:
    extra = task.get("extra_options") or {}
    if isinstance(extra, str):
        import json
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    pol = (extra or {}).get("gfs_policy")
    if not pol:
        pol = db.get_system_config("gfs_default_policy")
        if isinstance(pol, str):
            import json
            try:
                pol = json.loads(pol)
            except Exception:
                pol = None
    return pol or {}


def _bucket_key(finished: str, kind: str) -> str:
    d = (finished or "")[:10]
    if not d:
        return ""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return ""
    if kind == "daily":
        return d
    if kind == "weekly":
        return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
    if kind == "monthly":
        return d[:7]
    if kind == "yearly":
        return d[:4]
    return ""


def apply_gfs(task_id: int = None, dry_run: bool = False) -> dict:
    """执行 GFS 清理。返回统计 {task_id: {kept, marked}}。"""
    tasks = ([models.get_task(task_id)] if task_id
             else [t for t in (models.list_tasks() or []) if _policy_of(t)])
    report = {}
    for t in tasks:
        pol = _policy_of(t)
        if not pol:
            continue
        tid = t["id"]
        rows = db.query(
            "SELECT id, backup_path, finished_at, backup_type, status "
            "FROM backup_records WHERE task_id=? AND status='success' "
            "ORDER BY finished_at DESC, id DESC", (tid,))
        if not rows:
            continue
        # 分桶：每个桶内最新一份保留
        keep_ids, mark_ids = set(), set()
        for kind, n in (("daily", int(pol.get("daily", 0) or 0)),
                        ("weekly", int(pol.get("weekly", 0) or 0)),
                        ("monthly", int(pol.get("monthly", 0) or 0)),
                        ("yearly", int(pol.get("yearly", 0) or 0))):
            if n <= 0:
                continue
            buckets = defaultdict(list)
            for r in rows:
                k = _bucket_key(r["finished_at"] or "", kind)
                if k:
                    buckets[k].append(r)
            recent = sorted(buckets.keys(), reverse=True)[:n]
            for k in recent:
                if buckets[k]:
                    keep_ids.add(buckets[k][0]["id"])
        # 未被任何桶保留的 → 标记清理；被保留的跳过
        for r in rows:
            if r["id"] not in keep_ids:
                mark_ids.add(r["id"])
        # 保守规则：物理备份/合成备份若仍是链基备（有增量引用）不删
        for rid in list(mark_ids):
            refs = db.query(
                "SELECT COUNT(*) AS c FROM backup_sets WHERE parent_set_id=?",
                (rid,))
            if refs and refs[0]["c"] > 0:
                mark_ids.discard(rid)
        deleted_files = 0
        if mark_ids and not dry_run:
            for rid in mark_ids:
                rec = db.query_one(
                    "SELECT backup_path FROM backup_records WHERE id=?", (rid,))
                if rec and pol.get("delete_files") and rec.get("backup_path"):
                    try:
                        if os.path.isfile(rec["backup_path"]):
                            os.remove(rec["backup_path"])
                            deleted_files += 1
                    except Exception as e:
                        _log().warning("[gfs] 删除文件失败 %s: %s",
                                       rec["backup_path"], e)
                db.execute(
                    "UPDATE backup_records SET status='expired_gfs' WHERE id=?",
                    (rid,))
        report[tid] = {"kept": len(keep_ids), "marked": len(mark_ids),
                       "deleted_files": deleted_files}
        if mark_ids:
            _log().info("[gfs] task=%s GFS 清理: 保留 %d，标记 %d（dry_run=%s）",
                        tid, len(keep_ids), len(mark_ids), dry_run)
    return report
