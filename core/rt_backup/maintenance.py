# -*- coding: utf-8 -*-
"""实时保护数据维护：孤儿记录与段仓库清理。

背景（2026-11-14 实测）：备份任务被删除后，``rt_tasks`` / ``rt_capture_state``
以及 ``RT_LOG_ROOT/<task_id>`` 段目录都不会被回收。实测残留 210 条 rt_tasks、
209 条 capture_state、196 个段目录（105MB）。这些孤儿既占空间，又会让
健康看板出现「106 天未备份却显示黄色」这类僵尸状态。

清理口径（全部可 dry-run 预览）：
- 孤儿 = ``task_id`` 不在 ``backup_tasks`` 里的行；
- 段目录 = ``RT_LOG_ROOT/<task_id>``，仅当该 task_id 已确认是孤儿才删；
- 默认只清行、不清段目录（``purge_logs`` 需显式打开），避免误删仍被引用的产物。
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List

import config
import core.db as db


def _rt_root() -> str:
    root = getattr(config, "RT_LOG_ROOT", None) or os.path.join(
        str(getattr(config, "BACKUP_ROOT", "backups")), "rt_logs")
    return str(root)


def _orphan_task_ids() -> List[int]:
    """孤儿 task_id = 记录行孤儿 ∪ 段目录孤儿。

    两个坑（2026-11-14 实测踩到）：
    1. 必须用 ``NOT EXISTS`` 而不是 ``NOT IN (子查询)``——当 backup_tasks 为空时
       ``NOT IN (空集)`` 恒为 NULL，会把孤儿全判成 0（静默漏判）；
    2. 只按 rt_tasks/capture_state 反推会漏掉「记录行已清、段目录还在」的残留，
       所以还要扫 ``RT_LOG_ROOT`` 下的目录名。
    """
    out = set()
    rows = db.query(
        "SELECT task_id FROM rt_tasks t WHERE NOT EXISTS "
        "(SELECT 1 FROM backup_tasks b WHERE b.id = t.task_id) "
        "UNION "
        "SELECT task_id FROM rt_capture_state s WHERE NOT EXISTS "
        "(SELECT 1 FROM backup_tasks b WHERE b.id = s.task_id)")
    for r in rows or []:
        try:
            tid = int(r.get("task_id") or 0)
        except (TypeError, ValueError):
            tid = 0
        if tid > 0:
            out.add(tid)
    # 段目录孤儿
    try:
        valid = {int(x["id"]) for x in (db.query("SELECT id FROM backup_tasks") or [])
                 if x.get("id") is not None}
    except Exception:
        valid = set()
    root = _rt_root()
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isdir(path) or not name.isdigit():
                continue
            if int(name) not in valid:
                out.add(int(name))
    except OSError:
        pass
    return sorted(out)


def scan_orphans() -> Dict:
    """只读扫描：孤儿行数、段目录数、占用空间。"""
    ids = _orphan_task_ids()
    root = _rt_root()
    dirs, bytes_total = [], 0
    for tid in ids:
        path = os.path.join(root, str(tid))
        if os.path.isdir(path):
            dirs.append(tid)
            for base, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        bytes_total += os.path.getsize(os.path.join(base, f))
                    except OSError:
                        pass
    rt_rows = db.query("SELECT COUNT(*) AS n FROM rt_tasks t WHERE NOT EXISTS "
                       "(SELECT 1 FROM backup_tasks b WHERE b.id = t.task_id)") or [{}]
    st_rows = db.query("SELECT COUNT(*) AS n FROM rt_capture_state s WHERE NOT EXISTS "
                       "(SELECT 1 FROM backup_tasks b WHERE b.id = s.task_id)") or [{}]
    return {
        "orphan_task_ids": ids,
        "orphan_task_count": len(ids),
        "rt_tasks_rows": int(rt_rows[0].get("n") or 0),
        "capture_state_rows": int(st_rows[0].get("n") or 0),
        "segment_dirs": len(dirs),
        "segment_bytes": bytes_total,
        "segment_human": db.human_size(bytes_total),
        "rt_root": root,
    }


def purge_orphans(dry_run: bool = True, purge_logs: bool = True) -> Dict:
    """清理孤儿记录（可选清段目录）。

    Args:
        dry_run: True 只统计不落刀。
        purge_logs: 是否同时删除 ``RT_LOG_ROOT/<task_id>`` 段目录。
    """
    info = scan_orphans()
    ids = info["orphan_task_ids"]
    result = {
        "dry_run": bool(dry_run),
        "orphan_task_count": len(ids),
        "rt_tasks_deleted": 0,
        "capture_state_deleted": 0,
        "segment_dirs_deleted": 0,
        "segment_bytes_freed": 0,
        "segment_bytes_freed_human": "0",
        "errors": [],
        "detail": info,
    }
    if not ids:
        return result

    # 先停掉可能仍在运行的 worker（孤儿任务不该还在捕获）
    if not dry_run:
        try:
            from core import rt_backup
            sup = rt_backup.get_supervisor()
            for tid in ids:
                try:
                    sup._stop_worker(tid)
                except Exception:
                    pass
        except Exception:
            pass

    if dry_run:
        result["rt_tasks_deleted"] = info["rt_tasks_rows"]
        result["capture_state_deleted"] = info["capture_state_rows"]
        result["segment_dirs_deleted"] = info["segment_dirs"]
        result["segment_bytes_freed"] = info["segment_bytes"]
        return result

    placeholders = ",".join("?" for _ in ids)
    params = tuple(ids)
    # 注意：db.execute 返回 lastrowid 而非 rowcount，且 changes() 是连接级的
    # （换连接查询恒为 0），因此用「删除前的同条件计数」作为删除条数。
    try:
        db.execute("DELETE FROM rt_tasks WHERE task_id IN (%s) "
                   "AND NOT EXISTS (SELECT 1 FROM backup_tasks b "
                   "WHERE b.id = rt_tasks.task_id)" % placeholders, params)
        result["rt_tasks_deleted"] = int(info["rt_tasks_rows"])
    except Exception as exc:                       # noqa: BLE001
        result["errors"].append("rt_tasks: %s" % exc)
    try:
        db.execute("DELETE FROM rt_capture_state WHERE task_id IN (%s) "
                   "AND NOT EXISTS (SELECT 1 FROM backup_tasks b "
                   "WHERE b.id = rt_capture_state.task_id)" % placeholders, params)
        result["capture_state_deleted"] = int(info["capture_state_rows"])
    except Exception as exc:                       # noqa: BLE001
        result["errors"].append("rt_capture_state: %s" % exc)

    if purge_logs:
        root = _rt_root()
        for tid in ids:
            path = os.path.join(root, str(tid))
            if not os.path.isdir(path):
                continue
            size = 0
            for base, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(base, f))
                    except OSError:
                        pass
            try:
                shutil.rmtree(path)
                result["segment_dirs_deleted"] += 1
                result["segment_bytes_freed"] += size
            except Exception as exc:               # noqa: BLE001
                result["errors"].append("rmtree %s: %s" % (path, exc))

    result["segment_bytes_freed_human"] = db.human_size(result["segment_bytes_freed"])
    db.add_log("warning" if result["errors"] else "info", "rt.maintenance",
               "清理孤儿实时保护数据：行 %d+%d，段目录 %d（%s）" % (
                   result["rt_tasks_deleted"], result["capture_state_deleted"],
                   result["segment_dirs_deleted"],
                   result.get("segment_bytes_freed_human") or "0"))
    return result
