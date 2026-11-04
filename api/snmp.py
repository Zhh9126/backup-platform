# -*- coding: utf-8 -*-
"""SNMP Trap 告警配置 API（P2，对标鼎甲 E9）。"""
from flask import jsonify, request, session

from auth import login_required, permission_required
from core import db, snmp_trap
from . import api_bp


@api_bp.route("/snmp/config", methods=["GET"])
@login_required
@permission_required("security.manage")
def snmp_cfg():
    cfg = snmp_trap.snmp_config()
    return jsonify({"success": True, "data": cfg})


@api_bp.route("/snmp/config", methods=["POST"])
@login_required
@permission_required("security.manage")
def snmp_save():
    data = request.get_json(force=True, silent=True) or {}
    try:
        snmp_trap.set_snmp_config(
            bool(data.get("enabled")), data.get("target") or "",
            int(data.get("port") or 162), data.get("community") or "public",
            data.get("enterprise") or "1.3.6.1.4.1.60000")
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "message": f"参数非法: {e}"}), 400
    db.add_log("warning", "auth",
               f"SNMP 告警配置已更新（{'启用' if data.get('enabled') else '停用'}，"
               f"目标 {data.get('target')}，操作者 "
               f"{(session.get('user') or {}).get('username', '')}）")
    return jsonify({"success": True})


@api_bp.route("/snmp/test", methods=["POST"])
@login_required
@permission_required("security.manage")
def snmp_test():
    """发送测试 trap（真实 UDP 报文；网管侧收到即验证成功）。"""
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("target") or snmp_trap.snmp_config()["target"]
    if not target:
        return jsonify({"success": False, "message": "请先填写网管目标地址"}), 400
    r = snmp_trap.send_trap(
        target, int(data.get("port") or snmp_trap.snmp_config()["port"]),
        data.get("community") or snmp_trap.snmp_config()["community"],
        f"AIDBM 测试 Trap（操作者 {(session.get('user') or {}).get('username', '')}）",
        "warning", data.get("enterprise") or snmp_trap.snmp_config()["enterprise"])
    return jsonify({"success": bool(r.get("sent")), "data": r})
