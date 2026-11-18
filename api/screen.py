# -*- coding: utf-8 -*-
"""领导驾驶舱大屏 API：按任务类型统计运行态势。

诚实口径（不虚构数据）：
- running 以 backup_records 中真实 status='running' 的 DISTINCT task_id 判定；
  任务表 last_status 只是缓存，进程中断后会残留 running 僵尸态，不能作为依据。
- failed 以「已启用且 last_status='failed'」的任务判定，另附最近失败作业明细。
- 虚拟机走 vm_protected / vm_jobs 实表，不折算成备份任务。
"""
from datetime import datetime

from flask import jsonify

from auth import login_required
from core import db, models
from . import api_bp

_FILE_TYPES = {"file", "file_sync"}
_OBJECT_TYPES = {"object_storage", "object-storage", "objectstorage", "s3", "oss", "cos", "obs"}
_VM_TYPES = {"vm", "virtual", "vsphere", "esxi", "hyperv"}

# 分组元信息（顺序即大屏展示顺序）
GROUP_META = [
    ("db", "数据库", "database"),
    ("file", "文件", "folder2-open"),
    ("object", "对象存储", "cloud"),
    ("vm", "虚拟机", "pc-display"),
]


def _classify(db_type: str) -> str:
    d = (db_type or "").strip().lower()
    if d in _FILE_TYPES:
        return "file"
    if d in _OBJECT_TYPES:
        return "object"
    if d in _VM_TYPES:
        return "vm"
    return "db"


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@api_bp.route("/screen/board", methods=["GET"])
@login_required
def screen_board():
    tasks = models.list_tasks(include_secret=False) or []

    # ---- 真实运行中的任务（按运行记录反查，防 last_status 僵尸态） ----
    running_ids = set()
    try:
        for r in db.query("SELECT DISTINCT task_id FROM backup_records WHERE status='running'"):
            if r.get("task_id") is not None:
                running_ids.add(_safe_int(r["task_id"]))
    except Exception:
        running_ids = set()

    groups = {}
    for key, label, icon in GROUP_META:
        groups[key] = {"key": key, "label": label, "icon": icon,
                       "total": 0, "enabled": 0, "running": 0, "failed": 0}

    for t in tasks:
        key = _classify(t.get("db_type"))
        g = groups[key]
        g["total"] += 1
        if _safe_int(t.get("enabled")) == 1:
            g["enabled"] += 1
        if _safe_int(t.get("id")) in running_ids:
            g["running"] += 1
        # 失败：只看已启用任务，停用任务的历史失败不计入当前风险
        if _safe_int(t.get("enabled")) == 1 and (t.get("last_status") or "") == "failed":
            g["failed"] += 1

    # ---- 虚拟机：受保护台数 + 恢复作业状态（无对应备份任务，单独取实表） ----
    try:
        prot = db.query_one("SELECT COUNT(*) AS n FROM vm_protected") or {}
        groups["vm"]["total"] = _safe_int(prot.get("n"))
        groups["vm"]["enabled"] = groups["vm"]["total"]
        for row in db.query("SELECT status, COUNT(*) AS n FROM vm_jobs GROUP BY status"):
            st = (row.get("status") or "").lower()
            if st == "running":
                groups["vm"]["running"] = _safe_int(row.get("n"))
            elif st == "failed":
                groups["vm"]["failed"] = _safe_int(row.get("n"))
    except Exception:
        pass

    ordered = [groups[k] for k, _l, _i in GROUP_META]

    # ---- 正在运行的作业明细（大屏滚动条用） ----
    name_map = {t["id"]: t.get("name") for t in tasks}
    type_map = {t["id"]: _classify(t.get("db_type")) for t in tasks}
    running_jobs = []
    if running_ids:
        try:
            rows = db.query(
                "SELECT id, task_id, started_at, db_type FROM backup_records "
                "WHERE status='running' ORDER BY started_at DESC LIMIT 40")
            for r in rows:
                tid = _safe_int(r.get("task_id"))
                running_jobs.append({
                    "record_id": r.get("id"),
                    "task_id": tid,
                    "task_name": name_map.get(tid) or ("任务 %s" % tid),
                    "group": type_map.get(tid) or _classify(r.get("db_type")),
                    "started_at": r.get("started_at"),
                })
        except Exception:
            running_jobs = []

    # ---- 最近失败明细（按时间倒序，最多 20 条） ----
    failed_jobs = []
    try:
        rows = db.query(
            "SELECT id, task_id, started_at, size_bytes FROM backup_records "
            "WHERE status='failed' ORDER BY started_at DESC LIMIT 20")
        for r in rows:
            tid = _safe_int(r.get("task_id"))
            failed_jobs.append({
                "record_id": r.get("id"),
                "task_id": tid,
                "task_name": name_map.get(tid) or ("任务 %s" % tid),
                "group": type_map.get(tid) or "db",
                "started_at": r.get("started_at"),
            })
    except Exception:
        failed_jobs = []

    totals = {
        "total": sum(g["total"] for g in ordered),
        "enabled": sum(g["enabled"] for g in ordered),
        "running": sum(g["running"] for g in ordered),
        "failed": sum(g["failed"] for g in ordered),
    }

    return jsonify({
        "success": True,
        "data": {
            "groups": ordered,
            "totals": totals,
            "running_jobs": running_jobs,
            "failed_jobs": failed_jobs,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    })
