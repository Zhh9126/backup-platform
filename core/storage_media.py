# -*- coding: utf-8 -*-
"""存储介质统一总览（Storage Media Overview）。

## 为什么需要

存储入口原本按介质分散在多个页面（存储池 / 对象存储 / 磁带库 / NAS / FTP / NFS /
外挂），运维想知道"我有几块介质、哪块坏了、用了多少、最近什么时候往里写过"
必须在页面之间来回跳。本模块把**所有介质**收敛成统一行模型，供
``GET /api/storage/media`` 与 ``/storage-media`` 页面消费。

## 行模型

    {
      "key": "pool-5" | "tape-3",           # 稳定标识（前端刷新/探测定向用）
      "category": "存储池"|"磁带库",          # 一级分类
      "name": "E2E-外挂介质",
      "type": "mount",                       # 介质类型（local/minio/s3/sftp/tape/mount/ftp）
      "type_label": "外挂存储 / NFS",
      "endpoint": "/tmp/e2e_mount_media",
      "tier": 2,
      "enabled": True,
      "is_default": False,
      "status": "ok"|"error"|"untested"|"disabled",
      "status_text": "已连接"/"异常：..." /"未测试"/"已禁用",
      "used_bytes": int|None, "capacity_bytes": int|None, "used_pct": float|None,
      "usage_note": "已采集"/"未采集（点「探测」）/不含容量语义",
      "recent_backup": {"record_id":..,"task_name":..,"at":..,"size_bytes":..}|None,
      "detail_url": "/storage" | "/tape",
      "probe": True                          # 是否支持"探测"（拉实时用量/连通性）
    }

## 诚实边界

- **用量的可得性因介质而异**：本地/挂载点用 ``shutil.disk_usage`` 精确；对象存储
  /SFTP/FTP 需要联网探测（概览默认不求值，避免页面被慢介质拖死），磁带库用带库
  自身统计。取不到就明确写"未采集"，不编造数字。
- 最近备份按 ``backup_records.storage_tier`` 与介质类型匹配（同类型取最近一条），
  属**近似归因**：平台尚未把每条备份精确绑定到 storage_target_id。
"""
from __future__ import annotations

import os
import shutil

import core.db as db

_TYPE_LABELS = {
    "local": "本地路径", "minio": "MinIO", "s3": "S3/OSS/OBS",
    "sftp": "SFTP/NAS", "tape": "磁带库", "mount": "外挂存储/NFS",
    "ftp": "FTP/FTPS",
}


def _type_label(t: str) -> str:
    return _TYPE_LABELS.get(str(t or "").lower(), str(t or "-"))


def _status_of(row: dict) -> tuple:
    if not row.get("enabled"):
        return "disabled", "已禁用"
    if (row.get("last_error") or "").strip():
        return "error", "异常：%s" % str(row["last_error"])[:80]
    if row.get("last_test_at"):
        return "ok", "已连接（%s 测试）" % str(row["last_test_at"])[:16].replace("T", " ")
    return "untested", "未测试"


def _local_usage(endpoint: str) -> dict:
    """本地路径/挂载点用量（精确）。取不到返回空 dict。"""
    path = (endpoint or "").strip()
    if not path:
        return {}
    if not os.path.isdir(path):
        return {}
    try:
        du = shutil.disk_usage(path)
        pct = round(du.used * 100.0 / du.total, 1) if du.total else None
        return {"used_bytes": du.used, "capacity_bytes": du.total, "used_pct": pct}
    except Exception:
        return {}


def _recent_backups(limit: int = 3000) -> dict:
    """同类型介质最近一次成功备份（近似归因，见模块 docstring）。"""
    try:
        rows = db.query(
            "SELECT r.id, r.task_id, r.storage_tier, r.size_bytes, "
            "COALESCE(r.finished_at, r.started_at) AS at, t.name AS task_name "
            "FROM backup_records r LEFT JOIN backup_tasks t ON t.id = r.task_id "
            "WHERE r.status='success' ORDER BY r.id DESC LIMIT ?", (int(limit),))
    except Exception:
        return {}
    out = {}
    for r in rows or []:
        tier = str(r.get("storage_tier") or "").strip().lower()
        if tier and tier not in out:
            out[tier] = {"record_id": r.get("id"), "task_id": r.get("task_id"),
                         "task_name": r.get("task_name") or "",
                         "at": r.get("at") or "",
                         "size_bytes": int(r.get("size_bytes") or 0)}
    return out


def _tape_summary(target_id: int) -> dict:
    try:
        from core import tape_library as tl
        return tl.media_summary(target_id) or {}
    except Exception:
        return {}


def media_overview() -> dict:
    """返回统一介质总览：rows + 汇总计数。"""
    try:
        targets = [dict(r) for r in (db.query(
            "SELECT * FROM storage_targets ORDER BY tier, id") or [])]
    except Exception:
        targets = []
    recent = _recent_backups()
    rows = []
    for t in targets:
        st, stext = _status_of(t)
        mtype = str(t.get("type") or "local").lower()
        usage = {}
        usage_note = "未采集（点「探测」拉实时用量）"
        probe = True
        if mtype in ("local", "mount"):
            usage = _local_usage(t.get("endpoint") or "")
            usage_note = "已采集（本地精确）" if usage else "路径不可读，用量未知"
            probe = False
        elif mtype == "tape":
            ts = _tape_summary(int(t["id"]))
            if ts:
                usage = {"used_bytes": ts.get("used_bytes"),
                         "capacity_bytes": ts.get("capacity_bytes"),
                         "used_pct": ts.get("used_pct")}
                usage = {k: v for k, v in usage.items() if v is not None}
                usage_note = "带库统计（%s 盘介质）" % ts.get("media_count", "-")
            else:
                usage_note = "无带库统计"
        else:
            usage_note = "需联网探测（点「探测」）"
        rb = recent.get(mtype) or recent.get(str(t.get("pool_kind") or "").lower())
        rows.append({
            "key": "pool-%d" % int(t["id"]),
            "target_id": int(t["id"]),
            "category": "磁带库" if mtype == "tape" else "存储池",
            "name": t.get("name") or "",
            "type": mtype,
            "type_label": _type_label(mtype),
            "endpoint": t.get("endpoint") or "",
            "bucket": t.get("bucket") or "",
            "tier": t.get("tier"),
            "enabled": bool(t.get("enabled")),
            "is_default": bool(t.get("is_default")),
            "status": st, "status_text": stext,
            "used_bytes": usage.get("used_bytes"),
            "capacity_bytes": usage.get("capacity_bytes"),
            "used_pct": usage.get("used_pct"),
            "usage_note": usage_note,
            "recent_backup": rb,
            "detail_url": "/tape" if mtype == "tape" else "/storage",
            "probe": probe,
        })

    summary = {
        "total": len(rows),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "error": sum(1 for r in rows if r["status"] == "error"),
        "untested": sum(1 for r in rows if r["status"] == "untested"),
        "disabled": sum(1 for r in rows if r["status"] == "disabled"),
        "total_used_bytes": sum(int(r["used_bytes"] or 0) for r in rows),
        "total_capacity_bytes": sum(int(r["capacity_bytes"] or 0) for r in rows),
    }
    return {"rows": rows, "summary": summary, "generated_at": db.now_iso()}


def probe_media(target_id: int) -> dict:
    """对单个介质做实时探测：连通性 + 用量（可能联网，按需调用）。"""
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (int(target_id),))
    if not row:
        return {"ok": False, "message": "存储介质不存在"}
    t = dict(row)
    mtype = str(t.get("type") or "local").lower()
    out = {"ok": True, "target_id": int(target_id), "type": mtype}
    if mtype in ("local", "mount"):
        usage = _local_usage(t.get("endpoint") or "")
        ok = bool(usage)
        out.update({"ok": ok, "message": "路径可读" if ok else "路径不可读/不存在",
                    **usage})
        db.execute("UPDATE storage_targets SET last_test_at=?, last_error=? WHERE id=?",
                   (db.now_iso(), "" if ok else "路径不可读", int(target_id)))
        return out
    # 其它类型：走存储后端（内部各自实现 test_connection / get_used_space）
    try:
        from core.storage_backends import get_backend
        be = get_backend(mtype, t)
        ok, msg = be.test_connection()
        used = None
        try:
            used = be.get_used_space()
        except Exception:
            used = None
        db.execute("UPDATE storage_targets SET last_test_at=?, last_error=? WHERE id=?",
                   (db.now_iso(), "" if ok else str(msg)[:300], int(target_id)))
        out.update({"ok": bool(ok), "message": msg, "used_bytes": used})
        return out
    except Exception as e:
        db.execute("UPDATE storage_targets SET last_test_at=?, last_error=? WHERE id=?",
                   (db.now_iso(), str(e)[:300], int(target_id)))
        return {"ok": False, "target_id": int(target_id),
                "message": "探测失败: %s" % str(e)[:200]}
