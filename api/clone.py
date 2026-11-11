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
from auth import login_required, verify_code_gate
from . import api_bp

_service = clone_mod.clone_service


@api_bp.route("/clone", methods=["GET"])
@login_required
def api_list_clones():
    return jsonify(_service.list_clones())


@api_bp.route("/clone/preflight", methods=["POST"])
@login_required
def api_clone_preflight():
    """只读克隆预检：不创建申请、不拉起 VDB，返回逐项检查结论。

    供页面在提交克隆前展示「源产物 / 目标端口 / 磁盘余量 / 凭据 /
    安全隔离态」检查结果，避免拉起半成品 VDB 后才发现失败。
    """
    from core import models
    from core.clone_preflight import preflight
    data = request.get_json(silent=True) or {}
    rid = data.get("source_record_id")
    if not rid:
        return jsonify({"ok": False, "error": "source_record_id 必填"}), 400
    try:
        rec = models.get_record(int(rid))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "source_record_id 非法"}), 400
    if not rec:
        return jsonify({"ok": False, "error": "源备份记录不存在"}), 404
    return jsonify(preflight(
        rec, target_host=data.get("target_host") or "127.0.0.1",
        target_port=data.get("target_port") or None,
        target_password=data.get("target_password") or "",
        note=data.get("note") or "",
        clone_mode=data.get("clone_mode") or "logical"))


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
            target_password=data.get("target_password") or "",
            clone_mode=data.get("clone_mode") or "logical",
            live_source_db=data.get("live_source_db") or "",
            purpose=data.get("purpose") or "test")
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


@api_bp.route("/clone/<int:request_id>/takeover-check", methods=["POST"])
@login_required
def api_clone_takeover_check(request_id):
    """应急接管就绪检查（B3）：验证 ready 克隆库可否临时承载业务。"""
    from flask import session as _sess
    operator = ( (_sess.get("user") or {}).get("username") or "unknown")
    try:
        res = _service.takeover_check(request_id, operator=operator)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": res.get("ok"), **res})


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


# ------------------------- VDB 快照（时间点 / 回滚 / 再克隆） -------------------------
@api_bp.route("/clone/<int:request_id>/snapshots", methods=["GET"])
@login_required
def api_list_vdb_snapshots(request_id):
    if not _service.get_clone(request_id):
        return jsonify({"error": "克隆请求不存在"}), 404
    return jsonify(_service.list_snapshots(request_id))


@api_bp.route("/clone/<int:request_id>/snapshots", methods=["POST"])
@login_required
def api_create_vdb_snapshot(request_id):
    body = request.get_json(silent=True) or {}
    try:
        snap = _service.create_snapshot(
            request_id, name=body.get("name") or "",
            created_by=body.get("created_by") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "snapshot": snap}), 201


@api_bp.route("/clone/<int:request_id>/snapshots/<int:snapshot_id>/rollback",
              methods=["POST"])
@login_required
def api_rollback_vdb_snapshot(request_id, snapshot_id):
    body = request.get_json(silent=True) or {}
    gate = verify_code_gate(body)
    if gate:
        return jsonify(gate[0]), gate[1]
    try:
        res = _service.rollback_snapshot(
            request_id, snapshot_id,
            drop_new_tables=bool(body.get("drop_new_tables", True)),
            operator=body.get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **res})


@api_bp.route("/clone/<int:request_id>/snapshots/<int:snapshot_id>/reclone",
              methods=["POST"])
@login_required
def api_reclone_vdb_snapshot(request_id, snapshot_id):
    body = request.get_json(silent=True) or {}
    try:
        res = _service.reclone_snapshot(
            request_id, snapshot_id,
            target_env=body.get("target_env") or "",
            requested_by=body.get("requested_by") or "",
            operator=body.get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(res), 201


@api_bp.route("/clone/<int:request_id>/snapshots/<int:snapshot_id>",
              methods=["DELETE"])
@login_required
def api_delete_vdb_snapshot(request_id, snapshot_id):
    body = request.get_json(silent=True) or {}
    try:
        res = _service.delete_snapshot(
            request_id, snapshot_id, operator=body.get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(res)
