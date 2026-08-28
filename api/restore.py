# -*- coding: utf-8 -*-
"""数据恢复 API。"""
from flask import request, jsonify

from auth import login_required
from core import models, scheduler
from . import api_bp


@api_bp.route("/restores", methods=["GET"])
@login_required
def list_restores():
    keyword = request.args.get("keyword", type=str)
    return jsonify(models.list_restores(limit=200, keyword=keyword))


@api_bp.route("/records/enriched", methods=["GET"])
@login_required
def list_records_enriched():
    """返回带任务/主机信息的备份记录（供恢复页面下拉使用）。"""
    rows = models.list_records(limit=500)
    enriched = []
    for r in rows:
        enriched.append({
                "id": r["id"],
                "task_id": r["task_id"],
                "task_name": r.get("task_name", "-"),
                # 展示用业务系统标签（R2 已在 models 层计算），前端只读它
                "biz_label": r.get("biz_label", "-"),
                "host_ip": r.get("host_ip", "-"),
                "db_type": r.get("db_type"),
                "db_type_display": r.get("db_type_display"),
                "backup_type": r.get("backup_type"),
                "backup_type_display": r.get("backup_type_display"),
                "source_host": r.get("host_ip", ""),
                "source_port": "",
                "source_db": "",
                "status": r.get("status"),
                "started_at": r.get("started_at"),
                "size_bytes": r.get("size_bytes", 0),
                "backup_path": r.get("backup_path"),
                "binlog_file": r.get("binlog_file"),
                "binlog_pos": r.get("binlog_pos"),
                "wal_lsn": r.get("wal_lsn"),
                "verified": r.get("verified"),
                "verify_msg": r.get("verify_msg"),
            })
    return jsonify(enriched)


@api_bp.route("/restores", methods=["POST"])
@login_required
def create_restore():
    data = request.get_json(force=True, silent=True) or {}
    record_id = data.get("record_id")
    if not record_id:
        return jsonify({"error": "record_id 必填"}), 400
    result = scheduler.run_restore_now(
        record_id,
        target_host_id=data.get("target_host_id"),
        target_host=data.get("target_host"),
        target_db=data.get("target_db"),
        target_port=data.get("target_port"),
        operator=data.get("operator"),
        target_host_user=data.get("target_host_user"),
        target_host_password=data.get("target_host_password"),
    )
    if not result:
        return jsonify({"error": "备份记录不存在"}), 404
    return jsonify(result), 201
