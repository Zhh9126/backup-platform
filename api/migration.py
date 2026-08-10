# -*- coding: utf-8 -*-
"""
迁移全流程保护 API：迁移计划的增删查 + 三阶段（pre/mid/post）触发 + 黄金点验证。

路由前缀: /api/migration（通过共享 api_bp 注册）
"""
from flask import request, jsonify

import core.models as models
import core.db as db
import core.migration as migration_mod
from auth import login_required
from . import api_bp

_engine = migration_mod.migration_engine
_VALID_STAGES = ("pre", "mid", "post")


@api_bp.route("/migration", methods=["GET"])
@login_required
def api_list_migrations():
    return jsonify(_engine.list_plans())


@api_bp.route("/migration", methods=["POST"])
@login_required
def api_create_migration():
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id 必填"}), 400
    if not models.get_task(task_id):
        return jsonify({"error": f"备份任务不存在: {task_id}"}), 404
    stage = (data.get("stage") or "pre")
    if stage not in _VALID_STAGES:
        return jsonify({"error": f"阶段非法（应为 {_VALID_STAGES}）"}), 400
    note = data.get("note", "")

    # 复用同一任务的迁移计划（保持 pre → mid → post 连续），不存在则新建
    existing = db.query_one(
        "SELECT * FROM migration_plans WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,))
    if existing:
        pid = existing["id"]
    else:
        pid = _engine.create_plan(task_id, note)
    # 触发所选阶段
    try:
        if stage == "pre":
            plan = _engine.start_pre(task_id, note)
        elif stage == "mid":
            plan = _engine.start_mid(task_id, note)
        else:  # post
            plan = _engine.start_post(task_id, data.get("old_retention_days"), note)
    except Exception as e:
        return jsonify({"error": f"触发阶段失败: {e}"}), 500
    return jsonify({"id": pid, "ok": True, "stage": stage, "plan": plan}), 201


@api_bp.route("/migration/<int:plan_id>", methods=["GET"])
@login_required
def api_get_migration(plan_id):
    plan = _engine.get_status(plan_id)
    if not plan:
        return jsonify({"error": "迁移计划不存在"}), 404
    return jsonify(plan)


@api_bp.route("/migration/<int:plan_id>/verify", methods=["POST"])
@login_required
def api_verify_migration(plan_id):
    plan = _engine.get_plan(plan_id)
    if not plan:
        return jsonify({"error": "迁移计划不存在"}), 404
    result = _engine.verify_golden(plan_id)
    return jsonify(result)
