# -*- coding: utf-8 -*-
"""
克隆服务 API：克隆申请 / 审批 / 驳回 / 销毁 / 到期，以及查询。

路由前缀: /api/clone（通过共享 api_bp 注册）
- POST /api/clone                      申请克隆
- GET  /api/clone                      列表
- GET  /api/clone/<id>                详情
- POST /api/clone/<id>/approve        审批通过（拉起 VDB）
- POST /api/clone/<id>/reject         驳回
- POST /api/clone/<id>/destroy        手动销毁
- POST /api/clone/<id>/expire         到期自动销毁（scheduler 调用）
"""
from flask import request, jsonify

import core.clone_service as clone_mod
from auth import login_required
from . import api_bp

_service = clone_mod.clone_service


@api_bp.route("/clone", methods=["GET"])
@login_required
def api_list_clones():
    return jsonify(_service.list_clones())


@api_bp.route("/clone", methods=["POST"])
@login_required
def api_request_clone():
    data = request.get_json(silent=True) or {}
    source_record_id = data.get("source_record_id")
    target_env = data.get("target_env")
    requested_by = data.get("requested_by") or "anonymous"
    if not source_record_id:
        return jsonify({"error": "source_record_id 必填"}), 400
    if not target_env:
        return jsonify({"error": "target_env 必填"}), 400
    try:
        req = _service.request_clone(
            int(source_record_id), target_env, requested_by,
            note=data.get("note", ""), itsm_system=data.get("itsm_system"),
            target_host=data.get("target_host") or "127.0.0.1",
            target_password=data.get("target_password") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": req["id"], "request": req}), 201


@api_bp.route("/clone/<int:request_id>", methods=["GET"])
@login_required
def api_get_clone(request_id):
    req = _service.get_clone(request_id)
    if not req:
        return jsonify({"error": "克隆请求不存在"}), 404
    return jsonify(req)


@api_bp.route("/clone/<int:request_id>/approve", methods=["POST"])
@login_required
def api_approve_clone(request_id):
    body = request.get_json(silent=True) or {}
    try:
        req = _service.approve_clone(request_id, approved_by=body.get("approved_by") or "admin")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "request": req})


@api_bp.route("/clone/<int:request_id>/reject", methods=["POST"])
@login_required
def api_reject_clone(request_id):
    body = request.get_json(silent=True) or {}
    try:
        req = _service.reject_clone(request_id, by=body.get("by") or "admin")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "request": req})


@api_bp.route("/clone/<int:request_id>/destroy", methods=["POST"])
@login_required
def api_destroy_clone(request_id):
    try:
        req = _service.destroy_clone(request_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "request": req})


@api_bp.route("/clone/<int:request_id>/verify", methods=["POST"])
@login_required
def api_verify_clone(request_id):
    """就绪克隆连接校验：探活 + 统计表数量。"""
    try:
        res = _service.verify_clone(request_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(res)


@api_bp.route("/clone/<int:request_id>/expire", methods=["POST"])
@login_required
def api_expire_clone(request_id):
    try:
        req = _service.expire_clone(request_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "request": req})
