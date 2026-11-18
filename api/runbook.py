# -*- coding: utf-8 -*-
"""Runbook 恢复编排 API。

权限模型（三员分立兼容）：
  - 剧本 CRUD（manage）        → dr.link.manage（灾备编排管理）
  - 执行 / 审批 / 停止（run）  → restore.run（恢复执行）
"""
import json

from flask import jsonify, request

from auth import current_operator, login_required, permission_required
from . import api_bp
from core import runbook


def _operator() -> str:
    return current_operator()


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


# ------------------------- 剧本 CRUD -------------------------
@api_bp.route("/runbooks", methods=["GET"])
@login_required
def rb_list():
    return jsonify({"ok": True, "items": runbook.list_runbooks()})


@api_bp.route("/runbooks", methods=["POST"])
@permission_required("dr.link.manage")
def rb_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        rb = runbook.create_runbook(data, operator=_operator())
        return jsonify({"ok": True, "id": rb["id"], "item": rb}), 201
    except ValueError as e:
        return _err(str(e))


@api_bp.route("/runbooks/<int:rb_id>", methods=["GET"])
@login_required
def rb_get(rb_id):
    rb = runbook.get_runbook(rb_id)
    return jsonify({"ok": True, "item": rb}) if rb else _err("剧本不存在", 404)


@api_bp.route("/runbooks/<int:rb_id>", methods=["PUT"])
@permission_required("dr.link.manage")
def rb_update(rb_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        rb = runbook.update_runbook(rb_id, data)
        return jsonify({"ok": True, "item": rb})
    except ValueError as e:
        return _err(str(e))


@api_bp.route("/runbooks/<int:rb_id>", methods=["DELETE"])
@permission_required("dr.link.manage")
def rb_delete(rb_id):
    try:
        ok = runbook.delete_runbook(rb_id)
        return jsonify({"ok": True}) if ok else _err("剧本不存在", 404)
    except ValueError as e:
        return _err(str(e))


# ------------------------- 执行 / 审批 / 停止 -------------------------
@api_bp.route("/runbooks/<int:rb_id>/run", methods=["POST"])
@permission_required("restore.run")
def rb_run(rb_id):
    try:
        run = runbook.run_runbook(rb_id, operator=_operator())
        return jsonify({"ok": True, "run": run}), 202
    except ValueError as e:
        return _err(str(e))


@api_bp.route("/runbook-runs", methods=["GET"])
@login_required
def rb_runs():
    rb_id = request.args.get("runbook_id", type=int)
    limit = request.args.get("limit", type=int, default=100)
    return jsonify({"ok": True,
                    "items": runbook.list_runs(runbook_id=rb_id,
                                               limit=min(limit or 100, 500))})


@api_bp.route("/runbook-runs/<int:run_id>", methods=["GET"])
@login_required
def rb_run_get(run_id):
    run = runbook.get_run(run_id)
    return jsonify({"ok": True, "item": run}) if run else _err("运行记录不存在", 404)


@api_bp.route("/runbook-runs/<int:run_id>/approve", methods=["POST"])
@permission_required("restore.run")
def rb_approve(run_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        run = runbook.approve_run(run_id, int(data.get("seq") or 0),
                                  operator=_operator())
        return jsonify({"ok": True, "item": run})
    except (ValueError, TypeError) as e:
        return _err(str(e))


@api_bp.route("/runbook-runs/<int:run_id>/stop", methods=["POST"])
@permission_required("restore.run")
def rb_stop(run_id):
    try:
        run = runbook.stop_run(run_id, operator=_operator())
        return jsonify({"ok": True, "item": run})
    except ValueError as e:
        return _err(str(e))


# ------------------------- 元数据（步骤类型，前端下拉用） -------------------------
@api_bp.route("/runbooks/meta", methods=["GET"])
@login_required
def rb_meta():
    return jsonify({"ok": True,
                    "step_types": [{"type": t, "label": runbook.STEP_TYPE_LABELS[t]}
                                   for t in runbook.STEP_TYPES],
                    "max_steps": runbook.MAX_STEPS})
