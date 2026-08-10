# -*- coding: utf-8 -*-
"""
ITSM 联动 API：工单查询 + 平台内审批（审批结果回写 clone_requests / migration_plans）。

路由前缀: /api/itsm（通过共享 api_bp 注册）
- GET  /api/itsm/tickets                      工单列表（?ref_type=&ref_id=）
- POST /api/itsm/ticket/<id>/approve          审批通过（回写关联对象）
- POST /api/itsm/ticket/<id>/reject           驳回（回写关联对象）
- GET  /api/itsm/config                        当前 ITSM 后端配置
"""
from flask import request, jsonify

import core.db as db
import core.models as models
import core.clone_service as clone_mod
from core.itsm import get_itsm_adapter, VALID_SYSTEMS
from auth import login_required
from . import api_bp


@api_bp.route("/itsm/tickets", methods=["GET"])
@login_required
def api_list_itsm_tickets():
    ref_type = request.args.get("ref_type")
    ref_id = request.args.get("ref_id", type=int)
    tickets = models.list_itsm_tickets(ref_type=ref_type, ref_id=ref_id)
    return jsonify(tickets)


@api_bp.route("/itsm/ticket/<int:ticket_id>/approve", methods=["POST"])
@login_required
def api_approve_itsm_ticket(ticket_id):
    body = request.get_json(silent=True) or {}
    by = body.get("by") or "admin"
    ticket = models.get_itsm_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "工单不存在"}), 404
    adapter = get_itsm_adapter(ticket["system"])
    adapter.approve_ticket(ticket_id, by)
    # 审批回调：回写关联对象状态
    cascade = _cascade(ticket, "approve", by)
    return jsonify({"ok": True, "ticket": models.get_itsm_ticket(ticket_id),
                    "cascade": cascade})


@api_bp.route("/itsm/ticket/<int:ticket_id>/reject", methods=["POST"])
@login_required
def api_reject_itsm_ticket(ticket_id):
    body = request.get_json(silent=True) or {}
    by = body.get("by") or "admin"
    ticket = models.get_itsm_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "工单不存在"}), 404
    adapter = get_itsm_adapter(ticket["system"])
    adapter.reject_ticket(ticket_id, by)
    cascade = _cascade(ticket, "reject", by)
    return jsonify({"ok": True, "ticket": models.get_itsm_ticket(ticket_id),
                    "cascade": cascade})


@api_bp.route("/itsm/config", methods=["GET"])
@login_required
def api_itsm_config():
    system = db.get_system_config("itsm_system") or "internal"
    auto_approve = bool(int(db.get_system_config("itsm_auto_approve") or 0))
    return jsonify({
        "system": system,
        "auto_approve": auto_approve,
        "adapters": list(VALID_SYSTEMS),
    })


def _cascade(ticket: dict, action: str, by: str) -> dict:
    """审批结果回写关联对象（克隆 / 迁移 / 演练）。当前支持 clone。"""
    ref_type = ticket.get("ref_type")
    ref_id = ticket.get("ref_id")
    if ref_type == "clone" and ref_id:
        try:
            if action == "approve":
                clone_mod.clone_service.approve_clone(ref_id, by)
            else:
                clone_mod.clone_service.reject_clone(ref_id, by)
            return {"type": "clone", "id": ref_id, "action": action}
        except Exception as e:
            return {"type": "clone", "id": ref_id, "action": action,
                    "error": str(e)}
    return {"type": ref_type, "id": ref_id, "action": action, "cascaded": False}
