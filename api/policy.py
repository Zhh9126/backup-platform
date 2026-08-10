# -*- coding: utf-8 -*-
"""
保护策略管理 API：CRUD + 任务绑定。

路由前缀: /api/policy（通过共享 api_bp 注册）
提供保护策略的增删改查，以及将策略批量绑定/解绑到备份任务的能力。
敏感字段脱敏风格保持与本平台一致（本模块无密钥，但仍统一返回结构）。
"""
import json

from flask import request, jsonify

import core.models as models
from auth import login_required
from . import api_bp


# ------------------------- 工具函数 -------------------------

def _row_to_dict(row: dict) -> dict:
    """将保护策略行转为 dict，并反序列化 JSON 字段。"""
    d = dict(row)
    for f in ("backup_strategy", "link_strategy", "retention"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except (json.JSONDecodeError, TypeError):
                pass
    d["enabled"] = bool(d.get("enabled"))
    return d


# ------------------------- API 端点 -------------------------

@api_bp.route("/policy", methods=["GET"])
@login_required
def api_list_policies():
    rows = models.list_protection_policies()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["bound_task_count"] = models.count_tasks_by_policy(r["id"])
        out.append(d)
    return jsonify(out)


@api_bp.route("/policy", methods=["POST"])
@login_required
def api_create_policy():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "策略名称为必填"}), 400
    level = data.get("level") or "general"
    if level not in ("core", "important", "general"):
        return jsonify({"error": "保护等级无效（应为 core/important/general）"}), 400
    data["level"] = level
    pid = models.create_protection_policy(data)
    return jsonify({"id": pid, "ok": True}), 201


@api_bp.route("/policy/<int:policy_id>", methods=["GET"])
@login_required
def api_get_policy(policy_id):
    row = models.get_protection_policy(policy_id)
    if not row:
        return jsonify({"error": "策略不存在"}), 404
    d = _row_to_dict(row)
    d["bound_task_count"] = models.count_tasks_by_policy(policy_id)
    d["bound_tasks"] = models.list_tasks_by_policy(policy_id)
    return jsonify(d)


@api_bp.route("/policy/<int:policy_id>", methods=["PUT"])
@login_required
def api_update_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    data = request.get_json(silent=True) or {}
    if "level" in data and data["level"] not in ("core", "important", "general"):
        return jsonify({"error": "保护等级无效（应为 core/important/general）"}), 400
    models.update_protection_policy(policy_id, data)
    return jsonify({"ok": True})


@api_bp.route("/policy/<int:policy_id>", methods=["DELETE"])
@login_required
def api_delete_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    # 先解绑所有关联任务，避免悬空引用
    models.unbind_all_tasks_by_policy(policy_id)
    models.delete_protection_policy(policy_id)
    return jsonify({"ok": True})


@api_bp.route("/policy/<int:policy_id>/bind", methods=["POST"])
@login_required
def api_bind_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids") or []
    if not isinstance(task_ids, list):
        return jsonify({"error": "task_ids 必须是数组"}), 400
    bound = models.bind_policy_to_tasks(policy_id, [int(t) for t in task_ids])
    return jsonify({"ok": True, "bound": bound})


@api_bp.route("/policy/<int:policy_id>/bind", methods=["DELETE"])
@login_required
def api_unbind_policy(policy_id):
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids") or []
    if task_ids:
        models.unbind_policy_from_tasks([int(t) for t in task_ids])
    else:
        models.unbind_all_tasks_by_policy(policy_id)
    return jsonify({"ok": True})
