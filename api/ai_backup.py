# -*- coding: utf-8 -*-
"""AI 工作负载保护计划 API（跨层一致性组）。"""
import json

from flask import request, jsonify
from auth import login_required
from core import ai_protection, db
from . import api_bp


@api_bp.route("/ai-plans", methods=["GET"])
@login_required
def list_ai_plans():
    return jsonify({"plans": ai_protection.list_plans(),
                    "layers": [{"key": k, "name": v}
                               for k, v in ai_protection.LAYERS]})


@api_bp.route("/ai-plans", methods=["POST"])
@login_required
def create_ai_plan():
    from core import license as license_mod
    data = request.get_json(force=True, silent=True) or {}
    try:
        license_mod.check_quota("ai_plan")
        pid = ai_protection.create_plan(data)
        return jsonify({"ok": True, "id": pid})
    except license_mod.QuotaExceededError as qe:
        return jsonify({"error": str(qe), "quota_exceeded": True}), 403
    except ai_protection.PlanError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/ai-plans/<int:plan_id>", methods=["PUT"])
@login_required
def update_ai_plan(plan_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": ai_protection.update_plan(plan_id, data)})
    except ai_protection.PlanError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/ai-plans/<int:plan_id>", methods=["DELETE"])
@login_required
def delete_ai_plan(plan_id):
    return jsonify({"ok": ai_protection.delete_plan(plan_id)})


@api_bp.route("/ai-plans/<int:plan_id>/run", methods=["POST"])
@login_required
def run_ai_plan(plan_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        report = ai_protection.run_plan(plan_id, operator=data.get("operator") or "")
        return jsonify(report)
    except ai_protection.PlanError as e:
        return jsonify({"error": str(e)}), 400
