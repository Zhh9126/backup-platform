# -*- coding: utf-8 -*-
"""容灾演练 API：排程、执行、评估、趋势、基线。"""
import json

from flask import request, jsonify

import core.db as db
from auth import login_required
from core import models, drill as drill_engine
from . import api_bp


@api_bp.route("/drills", methods=["GET"])
@login_required
def list_drills():
    return jsonify(models.list_drills())


@api_bp.route("/drills", methods=["POST"])
@login_required
def create_drill():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "name 必填"}), 400
    if not data.get("task_id"):
        return jsonify({"error": "task_id 必填"}), 400
    drill_id = models.create_drill(data)
    return jsonify({"id": drill_id, "ok": True}), 201


@api_bp.route("/drills/<int:drill_id>", methods=["GET"])
@login_required
def get_drill(drill_id):
    d = models.get_drill(drill_id)
    return jsonify(d) if d else (jsonify({"error": "不存在"}), 404)


@api_bp.route("/drills/<int:drill_id>/run", methods=["POST"])
@login_required
def run_drill(drill_id):
    if not models.get_drill(drill_id):
        return jsonify({"error": "不存在"}), 404
    drill_engine.run_drill_async(drill_id)
    return jsonify({"accepted": True, "drill_id": drill_id}), 202


@api_bp.route("/drills/<int:drill_id>", methods=["DELETE"])
@login_required
def delete_drill(drill_id):
    if not models.get_drill(drill_id):
        return jsonify({"error": "不存在"}), 404
    models.delete_drill(drill_id)
    return jsonify({"ok": True})


# ------------------------- Phase 4：趋势 / 基线 / 排程 -------------------------
@api_bp.route("/drills/trend", methods=["GET"])
@login_required
def drills_trend():
    """RTO/RPO/评分历史趋势（供前端趋势图）。"""
    task_id = request.args.get("task_id", type=int)
    days = request.args.get("days", default=90, type=int)
    if days <= 0 or days > 3650:
        days = 90
    return jsonify(drill_engine.get_trend(task_id=task_id, days=days))


@api_bp.route("/drills/baseline", methods=["GET"])
@login_required
def drills_baseline():
    """RTO/RPO 基线（历史均值/中位数）与保护策略目标对比。"""
    task_id = request.args.get("task_id", type=int)
    if not task_id:
        return jsonify({"error": "task_id 必填"}), 400
    return jsonify(drill_engine.get_baseline(task_id))


@api_bp.route("/drills/schedule", methods=["GET"])
@login_required
def get_drill_schedule():
    """读取季度演练排程配置（drill_schedule）。"""
    return jsonify(drill_engine.get_drill_schedule())


@api_bp.route("/drills/schedule", methods=["POST"])
@login_required
def save_drill_schedule():
    """保存/更新季度演练排程配置。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        cfg = drill_engine.save_drill_schedule(data)
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"参数非法: {e}"}), 400
    return jsonify({"ok": True, "config": cfg})


@api_bp.route("/drills/schedule/run", methods=["POST"])
@login_required
def run_drill_schedule_now():
    """立即按当前排程触发一次（用于测试/手动执行，忽略 next_run）。"""
    summary = drill_engine.run_scheduled_drill(force=True)
    return jsonify(summary)
