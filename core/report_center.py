# -*- coding: utf-8 -*-
"""报表中心（P2 池）：备份/恢复/校验运行报表聚合与导出。

定位：把散落在备份记录/恢复记录/恢复校验/数据校验页的运行数据聚合成
一张周期运行报表（PDF/DOCX/CSV 导出，复用 core/reports.py）。

诚实边界：聚合的是平台元数据库里已有的记录，不做任何推断/补数；
空库返回结构化空报表（可渲染，不报错）。
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List

from core import db


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=int(days or 7))).strftime(
        "%Y-%m-%d %H:%M:%S")


def build_summary(days: int = 7) -> Dict[str, Any]:
    """聚合近 N 天运行报表。所有查询失败均返回 0（不掩盖为异常）。"""
    days = max(1, min(365, int(days or 7)))
    cut = _cutoff(days)
    out: Dict[str, Any] = {"window_days": days, "generated_at": db.now_iso()}

    def _q(sql: str, params=()):
        try:
            return db.query(sql, params)
        except Exception:
            return []

    # ---- 备份 ----
    rows = _q(
        "SELECT status, COUNT(*) c, COALESCE(SUM(size_bytes),0) s, "
        "COALESCE(AVG(duration_sec),0) d FROM backup_records "
        "WHERE started_at >= ? GROUP BY status", (cut,))
    agg = {str(r["status"] or "").lower(): r for r in rows}
    total = sum(r["c"] for r in rows)
    out["backup"] = {
        "total": total,
        "success": agg.get("success", {}).get("c", 0),
        "failed": agg.get("failed", {}).get("c", 0),
        "running": agg.get("running", {}).get("c", 0),
        "size_bytes": sum(r["s"] for r in rows),
        "avg_duration_sec": round(sum(r["d"] * r["c"] for r in rows) / total, 1) if total else 0,
    }

    # ---- 恢复 ----
    rows = _q(
        "SELECT status, COUNT(*) c FROM restore_records "
        "WHERE started_at >= ? GROUP BY status", (cut,))
    ragg = {str(r["status"] or "").lower(): r["c"] for r in rows}
    rtotal = sum(r["c"] for r in rows)
    out["restore"] = {
        "total": rtotal,
        "success": ragg.get("success", 0),
        "failed": ragg.get("failed", 0),
    }

    # ---- 恢复校验 ----
    try:
        vrows = _q(
            "SELECT status, COUNT(*) c FROM restore_test_reports "
            "WHERE created_at >= ? GROUP BY status", (cut,))
    except Exception:
        vrows = []
    vagg = {str(r["status"] or "").lower(): r["c"] for r in vrows}
    vtotal = sum(r["c"] for r in vrows)
    out["verify"] = {
        "total": vtotal,
        "success": vagg.get("success", 0) + vagg.get("passed", 0) + vagg.get("pass", 0),
        "failed": vagg.get("failed", 0) + vagg.get("error", 0),
    }

    # ---- 按库型聚合 ----
    out["by_db_type"] = _q(
        "SELECT COALESCE(db_type,'unknown') db_type, COUNT(*) total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, "
        "COALESCE(SUM(size_bytes),0) size_bytes "
        "FROM backup_records WHERE started_at >= ? GROUP BY db_type "
        "ORDER BY total DESC", (cut,)) or []

    # ---- 失败任务 Top（join 任务名，含已删任务的兜底展示） ----
    out["top_failed_tasks"] = _q(
        "SELECT r.task_id task_id, COALESCE(t.name, '已删除任务') name, "
        "COUNT(*) failed_count, MAX(r.started_at) last_failed_at "
        "FROM backup_records r LEFT JOIN backup_tasks t ON t.id = r.task_id "
        "WHERE r.started_at >= ? AND r.status='failed' "
        "GROUP BY r.task_id ORDER BY failed_count DESC LIMIT 10", (cut,)) or []

    # ---- 不可变备份（锁定产物） ----
    try:
        lk = _q("SELECT COUNT(*) c FROM backup_records WHERE locked=1")
        out["locked_records"] = (lk[0]["c"] if lk else 0)
    except Exception:
        out["locked_records"] = 0
    return out


def report_rows(days: int = 7, limit: int = 2000) -> List[Dict[str, Any]]:
    """明细行（导出用）：近 N 天备份记录逐条列表。"""
    days = max(1, min(365, int(days or 7)))
    cut = _cutoff(days)
    limit = max(1, min(20000, int(limit or 2000)))
    return db.query(
        "SELECT r.id, r.started_at, COALESCE(t.name, '已删除任务') task_name, "
        "r.db_type, r.backup_type, r.status, r.size_bytes, r.duration_sec, "
        "r.verified, r.locked, SUBSTR(COALESCE(r.message,''),1,200) message "
        "FROM backup_records r LEFT JOIN backup_tasks t ON t.id=r.task_id "
        "WHERE r.started_at >= ? ORDER BY r.started_at DESC LIMIT ?",
        (cut, limit))


def export_report(fmt: str, days: int = 7) -> tuple:
    """生成报表文件：复用 core/reports.build_report，返回 (mime, ext, bytes)。"""
    from core import reports
    s = build_summary(days)
    b, r = s.get("backup", {}), s.get("restore", {})
    v = s.get("verify", {})
    summary = {
        "统计窗口": f"近 {s['window_days']} 天",
        "备份总数": b.get("total", 0),
        "备份成功": b.get("success", 0),
        "备份失败": b.get("failed", 0),
        "备份数据量": f"{(b.get('size_bytes') or 0) / 1024 / 1024:.1f} MB",
        "恢复总数": r.get("total", 0),
        "恢复成功": r.get("success", 0),
        "恢复失败": r.get("failed", 0),
        "恢复校验": f"{v.get('success', 0)}/{v.get('total', 0)} 通过",
        "锁定产物": s.get("locked_records", 0),
        "生成时间": s.get("generated_at", ""),
    }
    headers = ["ID", "开始时间", "任务名", "库型", "备份类型", "状态",
               "大小(字节)", "耗时(秒)", "已校验", "锁定", "消息"]
    rows = [[it.get(k) for k in ("id", "started_at", "task_name", "db_type",
                                 "backup_type", "status", "size_bytes",
                                 "duration_sec", "verified", "locked",
                                 "message")]
            for it in report_rows(days)]
    title = f"AIDBM 备份运行报表（近 {s['window_days']} 天）"
    return reports.build_report(fmt, title, summary, headers, rows)
