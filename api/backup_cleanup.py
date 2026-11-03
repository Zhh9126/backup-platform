# -*- coding: utf-8 -*-
"""
备份产物定期清理 API：策略配置 / 扫描预览（dry-run）/ 立即执行。

路由前缀 /api/backup-cleanup（通过共享 api_bp 注册）
- GET  /api/backup-cleanup                     配置 + 上次执行结果
- POST /api/backup-cleanup/config              保存开关 / cron / 兜底保留份数
- POST /api/backup-cleanup/preview             扫描预览（只读，不删任何东西）
- POST /api/backup-cleanup/preview/<task_id>   单任务预览
- POST /api/backup-cleanup/run                 立即执行一次（支持 dry_run）

预览与执行都走 core.backup_cleanup：按每个任务的 retention_days /
retention_count 判定过期，keep_min 兜底保证至少留住最近 N 份成功备份。
"""
from flask import request, jsonify

from auth import login_required, verify_code_gate
from . import api_bp


@api_bp.route("/backup-cleanup", methods=["GET"])
@login_required
def api_cleanup_status():
    from core import backup_cleanup
    cfg = backup_cleanup.get_config()
    return jsonify({"ok": True, "config": cfg})


@api_bp.route("/backup-cleanup/config", methods=["POST"])
@login_required
def api_cleanup_save_config():
    from core import backup_cleanup
    data = request.get_json(silent=True) or {}
    try:
        cfg = backup_cleanup.save_config(data)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "config": cfg})


@api_bp.route("/backup-cleanup/preview", methods=["POST"])
@api_bp.route("/backup-cleanup/preview/<int:task_id>", methods=["POST"])
@login_required
def api_cleanup_preview(task_id=None):
    from core import backup_cleanup
    data = request.get_json(silent=True) or {}
    keep_min = data.get("keep_min")
    plans = backup_cleanup.build_plan(
        task_id=task_id,
        keep_min=int(keep_min) if keep_min is not None else None)
    return jsonify({"ok": True, "plans": plans,
                    "summary": backup_cleanup.summary_of(plans)})


@api_bp.route("/backup-cleanup/run", methods=["POST"])
@login_required
def api_cleanup_run():
    from core import backup_cleanup
    data = request.get_json(silent=True) or {}
    # P0-2：dry_run 只读预览不拦；真实删除动作需要操作验证码（开关开启时）
    if not data.get("dry_run"):
        gate = verify_code_gate(data)
        if gate:
            return jsonify(gate[0]), gate[1]
    keep_min = data.get("keep_min")
    purge = data.get("purge_records")
    report = backup_cleanup.run_cleanup(
        task_id=data.get("task_id"),
        dry_run=bool(data.get("dry_run")),
        keep_min=int(keep_min) if keep_min is not None else None,
        purge_records=None if purge is None else bool(purge))
    return jsonify({"ok": True, "report": report})
