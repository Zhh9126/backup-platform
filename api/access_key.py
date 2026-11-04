# -*- coding: utf-8 -*-
"""AccessKey 管理 API（P2，对标鼎甲 E7）：AK/SK 免密签名认证。"""
from flask import jsonify, request, session

from auth import login_required, permission_required
from core import access_key, db
from . import api_bp


@api_bp.route("/access-keys", methods=["GET"])
@login_required
@permission_required("security.manage")
def list_ak():
    return jsonify({"success": True, "data": access_key.list_keys()})


@api_bp.route("/access-keys", methods=["POST"])
@login_required
@permission_required("security.manage")
def create_ak():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "message": "请填写 AccessKey 名称"}), 400
    info = access_key.create_key(
        name, created_by=(session.get("user") or {}).get("username", ""))
    db.add_log("warning", "auth",
               f"创建 AccessKey {info['access_key']}（{name}，操作者 "
               f"{(session.get('user') or {}).get('username', '')}）")
    # SK 明文仅本次返回（不落库明文）
    return jsonify({"success": True, "data": info,
                    "note": "secret_key 仅此一次展示，请立即保存；可用其生成请求签名"})


@api_bp.route("/access-keys/<int:key_id>/revoke", methods=["POST"])
@login_required
@permission_required("security.manage")
def revoke_ak(key_id: int):
    access_key.revoke_key(key_id)
    db.add_log("warning", "auth", f"吊销 AccessKey id={key_id}")
    return jsonify({"success": True})


@api_bp.route("/access-keys/verify", methods=["POST"])
@login_required
@permission_required("security.manage")
def verify_ak_doc():
    """签名自检：按当前请求参数重算签名示例（供联调排障）。"""
    data = request.get_json(force=True, silent=True) or {}
    import hmac as _hmac
    import hashlib as _hl
    sk = data.get("secret_key") or ""
    method = (data.get("method") or "GET").upper()
    path = data.get("path") or "/api/health"
    ts = data.get("timestamp") or str(int(__import__("time").time()))
    msg = f"{method}\n{path}\n{ts}"
    sign = _hmac.new(sk.encode(), msg.encode(), _hl.sha256).hexdigest()
    return jsonify({"success": True, "data": {
        "string_to_sign": msg, "signature": sign, "timestamp": ts,
        "headers": {"X-AK-Access-Key": data.get("access_key", ""),
                    "X-AK-Timestamp": ts, "X-AK-Signature": sign}}})
