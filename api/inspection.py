# -*- coding: utf-8 -*-
"""备份任务巡检 API：触发巡检、查询巡检记录、导出、调度。"""
import json
from collections import Counter
from flask import request, jsonify, Response

from auth import login_required
from core import models, inspection as inspection_engine, reports, scheduler
from . import api_bp


@api_bp.route("/inspection/run", methods=["POST"])
@login_required
def run_inspection():
    data = request.get_json(force=True, silent=True) or {}
    task_id = data.get("task_id")
    triggered_by = data.get("triggered_by") or "manual"
    summary = inspection_engine.run_inspection(
        task_id=task_id, triggered_by=triggered_by)
    return jsonify(summary)


@api_bp.route("/inspection/records", methods=["GET"])
@login_required
def list_inspection_records():
    return jsonify(models.list_inspections(limit=200))


@api_bp.route("/inspection/schedule", methods=["GET"])
@login_required
def get_inspection_schedule():
    """读取巡检调度配置（system_config.inspection_schedule）。"""
    raw = models.get_system_config("inspection_schedule") if hasattr(models, "get_system_config") else None
    # models 没有 get_system_config 时直接走 db
    if raw is None:
        from core import db
        raw = db.get_system_config("inspection_schedule")
    cfg = {}
    if raw:
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            cfg = {}
    # 顺手返回当前 job 的 next_run（如果已注册）
    next_run = None
    try:
        from core import db as _db
        sch = scheduler.start_scheduler() or scheduler._scheduler
        if sch:
            j = sch.get_job("inspection_global")
            if j:
                nrt = j.next_run_time
                next_run = nrt.isoformat() if nrt else None
    except Exception:
        pass
    return jsonify({
        "enabled": bool(cfg.get("enabled", False)),
        "cron": cfg.get("cron", ""),
        "next_run": next_run,
    })


@api_bp.route("/inspection/schedule", methods=["POST"])
@login_required
def save_inspection_schedule():
    """保存巡检调度配置并立即 reload 调度器。"""
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", False))
    cron = (data.get("cron") or "").strip()
    if enabled and not cron:
        return jsonify({"error": "启用时必须填写 cron 表达式"}), 400
    # 基础 cron 校验
    if cron:
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(cron)
        except Exception as e:
            return jsonify({"error": f"cron 表达式非法: {e}"}), 400
    cfg = {"enabled": enabled, "cron": cron}
    from core import db
    db.set_system_config("inspection_schedule", json.dumps(cfg, ensure_ascii=False))
    # 立即重载调度器
    scheduler.reload_scheduler()
    # 重新读取 next_run
    next_run = None
    try:
        sch = scheduler._scheduler
        if sch:
            j = sch.get_job("inspection_global")
            if j and j.next_run_time:
                next_run = j.next_run_time.isoformat()
    except Exception:
        pass
    return jsonify({"ok": True, "enabled": enabled, "cron": cron, "next_run": next_run})


@api_bp.route("/inspection/records/export", methods=["GET"])
@login_required
def export_inspection_records():
    """导出巡检记录。支持 csv / docx / pdf 三种格式（?format=xxx）。"""
    fmt = (request.args.get("format") or "csv").lower()
    rows = models.list_inspections(limit=5000)
    headers = ["ID", "任务ID", "任务名", "类型", "巡检时间", "结果", "详情", "触发方式"]
    table = [[r.get("id"), r.get("task_id"), r.get("task_name"), r.get("db_type"),
              r.get("started_at"), r.get("status"), r.get("detail"), r.get("triggered_by")]
             for r in rows]
    # 汇总
    status_count = Counter((r.get("status") or "") for r in rows)
    summary = {
        "报告类型": "备份任务巡检报告",
        "巡检记录总数": len(rows),
        "正常 (pass)": status_count.get("pass", 0),
        "警告 (warn)": status_count.get("warn", 0),
        "失败 (fail)": status_count.get("fail", 0),
        "导出范围": f"最近 {len(rows)} 次巡检",
    }
    try:
        mime, ext, content = reports.build_report(
            fmt, "备份任务巡检报告", summary, headers, table)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    fname = f"backup_inspection_report.{ext}"
    return Response(
        content,
        mimetype=mime,
        headers={"Content-Disposition": f"attachment; filename={fname}"})
