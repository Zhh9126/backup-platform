# -*- coding: utf-8 -*-
"""数据同步任务 API：增删改查、立即同步、同步记录。"""
from flask import request, jsonify

from auth import login_required
from core import models
from core import sync as sync_engine
from . import api_bp


@api_bp.route("/sync/tasks", methods=["GET"])
@login_required
def list_sync_tasks():
    return jsonify(models.list_sync_tasks(include_secret=False))


@api_bp.route("/sync/tasks", methods=["POST"])
@login_required
def create_sync_task():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "同步任务名称必填"}), 400
    stype = data.get("source_type", "managed")
    if stype == "managed":
        if not data.get("source_task_id"):
            return jsonify({"error": "托管模式需选择一台已纳管的数据库任务"}), 400
    else:
        if not data.get("src_db_type") or not data.get("src_host"):
            return jsonify({"error": "手动模式需填写源数据库类型与主机"}), 400
    if not data.get("tgt_db_type") or not data.get("tgt_host"):
        return jsonify({"error": "请填写目标数据库类型与主机"}), 400
    sid = models.create_sync_task(data)
    return jsonify({"id": sid, "ok": True}), 201


@api_bp.route("/sync/tasks/<int:sync_id>", methods=["GET"])
@login_required
def get_sync_task(sync_id):
    t = models.get_sync_task(sync_id, include_secret=False)
    if not t:
        return jsonify({"error": "同步任务不存在"}), 404
    return jsonify(t)


@api_bp.route("/sync/tasks/<int:sync_id>", methods=["PUT"])
@login_required
def update_sync_task(sync_id):
    data = request.get_json(force=True, silent=True) or {}
    if not models.get_sync_task(sync_id):
        return jsonify({"error": "同步任务不存在"}), 404
    models.update_sync_task(sync_id, data)
    return jsonify({"ok": True})


@api_bp.route("/sync/tasks/<int:sync_id>", methods=["DELETE"])
@login_required
def delete_sync_task(sync_id):
    if not models.get_sync_task(sync_id):
        return jsonify({"error": "同步任务不存在"}), 404
    models.delete_sync_task(sync_id)
    return jsonify({"ok": True})


@api_bp.route("/sync/tasks/<int:sync_id>/run", methods=["POST"])
@login_required
def run_sync(sync_id):
    rec = sync_engine.run_sync(sync_id)
    if not rec:
        return jsonify({"error": "同步任务不存在"}), 404
    return jsonify(rec), 201


@api_bp.route("/sync/records", methods=["GET"])
@login_required
def list_sync_records():
    return jsonify(models.list_sync_records(limit=200))
