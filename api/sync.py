# -*- coding: utf-8 -*-
"""数据同步 API（DataX/LinkUp 风格：Reader/Writer + 字段映射）。"""
import json
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import login_required
from core import models, scheduler
from core.sync.engine import (
    generate_flink_config,
    list_sync_columns,
    list_sync_tables,
    run_sync_task,
    test_sync_connection,
    validate_sync_task,
    verify_sync_task,
)

sync_bp = Blueprint("sync", __name__)


@sync_bp.route("/sync-tasks", methods=["GET"])
@login_required
def list_tasks():
    rows = models.list_sync_tasks()
    return jsonify({"success": True, "data": rows})


@sync_bp.route("/sync-tasks", methods=["POST"])
@login_required
def create_task():
    data = request.get_json(silent=True) or {}
    now = datetime.now().isoformat(timespec="seconds")
    payload = _prepare_payload(data)
    payload["status"] = "never"
    payload["last_status"] = "never"
    payload["created_at"] = now
    payload["updated_at"] = now
    try:
        tid = models.create_sync_task(payload)
        scheduler.reload_scheduler()
        return jsonify({"success": True, "data": {"id": tid}})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@sync_bp.route("/sync-tasks/<int:task_id>", methods=["GET"])
@login_required
def get_task(task_id):
    row = models.get_sync_task(task_id)
    if not row:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    return jsonify({"success": True, "data": row})


@sync_bp.route("/sync-tasks/<int:task_id>", methods=["PUT"])
@login_required
def update_task(task_id):
    data = request.get_json(silent=True) or {}
    payload = _prepare_payload(data)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        models.update_sync_task(task_id, payload)
        scheduler.reload_scheduler()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@sync_bp.route("/sync-tasks/<int:task_id>", methods=["DELETE"])
@login_required
def delete_task(task_id):
    try:
        models.delete_sync_task(task_id)
        scheduler.reload_scheduler()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@sync_bp.route("/sync-tasks/<int:task_id>/run", methods=["POST"])
@login_required
def run_task(task_id):
    """立即执行一次同步（异步线程，不阻塞请求）。"""
    task = models.get_sync_task(task_id)
    if not task:
        return jsonify({"success": False, "message": "同步任务不存在"}), 404

    started_at = datetime.now().isoformat(timespec="seconds")
    models.update_sync_task(task_id, {
        "status": "running",
        "last_status": "running",
        "last_run_at": started_at,
        "message": "同步执行中...",
        "updated_at": started_at,
    })

    def _job():
        res = run_sync_task(task_id)
        finished_at = datetime.now().isoformat(timespec="seconds")
        status = "success" if res.get("success") else "failed"
        models.update_sync_task(task_id, {
            "status": status,
            "last_status": status,
            "message": res.get("message", ""),
            "updated_at": finished_at,
        })
        models.create_sync_record({
            "sync_task_id": task_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "rows_synced": res.get("total_write", 0),
            "message": res.get("message", ""),
        })

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({"success": True, "message": "同步任务已启动"})


@sync_bp.route("/sync-tasks/<int:task_id>/test/<side>", methods=["POST"])
@login_required
def test_connection(task_id, side):
    if side not in ("source", "target"):
        return jsonify({"success": False, "message": "side 必须是 source 或 target"}), 400
    return jsonify(test_sync_connection(task_id, side))


@sync_bp.route("/sync-tasks/<int:task_id>/tables", methods=["GET"])
@login_required
def get_tables(task_id):
    return jsonify(list_sync_tables(task_id))


@sync_bp.route("/sync-tasks/<int:task_id>/columns", methods=["GET"])
@login_required
def get_columns(task_id):
    task = models.get_sync_task(task_id)
    if not task:
        return jsonify({"success": False, "message": "同步任务不存在"}), 404
    table = request.args.get("table", "")
    if not table:
        return jsonify({"success": False, "message": "缺少 table 参数"}), 400
    return jsonify(list_sync_columns(task_id, table))


@sync_bp.route("/sync-tasks/<int:task_id>/records", methods=["GET"])
@login_required
def list_records(task_id):
    rows = models.list_sync_records(task_id)
    return jsonify({"success": True, "data": rows})


@sync_bp.route("/sync-tasks/<int:task_id>/flink-config", methods=["GET"])
@login_required
def flink_config(task_id):
    return jsonify(generate_flink_config(task_id))


@sync_bp.route("/sync-tasks/<int:task_id>/validate", methods=["POST"])
@login_required
def validate_task(task_id):
    """Schema 兼容性校验（pg2mysql Validator）。"""
    return jsonify(validate_sync_task(task_id))


@sync_bp.route("/sync-tasks/<int:task_id>/verify", methods=["POST"])
@login_required
def verify_task(task_id):
    """迁移后数据校验（pg2mysql Verifier）。"""
    return jsonify(verify_sync_task(task_id))


# ---------- 旧版 /api/sync/* 路径兼容别名 ----------
# app.js 中旧版同步模块仍使用 /api/sync/tasks 与 /api/sync/records，为避免页面
# 打开时这些请求 404，保留兼容别名；新页面统一使用 /api/sync-tasks/*。
@sync_bp.route("/sync/tasks", methods=["GET"])
@login_required
def compat_list_tasks():
    return list_tasks()


@sync_bp.route("/sync/tasks", methods=["POST"])
@login_required
def compat_create_task():
    return create_task()


@sync_bp.route("/sync/tasks/<int:task_id>", methods=["GET"])
@login_required
def compat_get_task(task_id):
    return get_task(task_id)


@sync_bp.route("/sync/tasks/<int:task_id>", methods=["PUT"])
@login_required
def compat_update_task(task_id):
    return update_task(task_id)


@sync_bp.route("/sync/tasks/<int:task_id>", methods=["DELETE"])
@login_required
def compat_delete_task(task_id):
    return delete_task(task_id)


@sync_bp.route("/sync/tasks/<int:task_id>/run", methods=["POST"])
@login_required
def compat_run_task(task_id):
    return run_task(task_id)


@sync_bp.route("/sync/records", methods=["GET"])
@login_required
def compat_list_all_records():
    rows = models.list_sync_records()
    return jsonify({"success": True, "data": rows})


def _prepare_payload(data: dict) -> dict:
    """把前端 payload 规范化并存入 sync_tasks 字段。"""
    payload = {}
    # 字符串/数字字段直接透传
    for k in [
        "name", "source_type", "source_task_id", "src_db_type", "src_host",
        "src_port", "src_username", "src_password", "src_db_name", "src_schema",
        "tgt_db_type", "tgt_host", "tgt_port", "tgt_username", "tgt_password",
        "tgt_db_name", "tgt_schema", "source_table", "target_table",
        "sync_mode", "save_mode", "field_ide", "incremental_column",
        "incremental_value", "source_where", "schedule_type", "cron_expr",
        "interval_minutes",
    ]:
        if k in data:
            payload[k] = data[k]
    # JSON / 列表字段
    for k in ["column_mapping", "source_tables_list", "flink_config"]:
        if k in data:
            v = data[k]
            payload[k] = json.dumps(v, ensure_ascii=False) if v is not None else None
    # 整数/布尔字段
    for k in ["batch_size", "error_threshold", "enabled", "realtime_enabled",
              "full_db_migrate", "validate_before_run", "verify_after_run"]:
        if k in data:
            payload[k] = int(data[k]) if data[k] is not None else None
    return payload


# 注册到统一 api_bp（副作用方式，与项目其他模块一致）
from api import api_bp  # noqa: E402
api_bp.register_blueprint(sync_bp)
