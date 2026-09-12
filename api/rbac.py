# -*- coding: utf-8 -*-
"""RBAC API：当前用户、用户 CRUD、权限点/角色定义、修改密码。"""
from flask import jsonify, request, session

from auth import login_required, permission_required
import core.rbac as rbac
from . import api_bp


def _scrub(u: dict) -> dict:
    """返回给前端的用户：去掉密码哈希与盐。"""
    if not u:
        return {}
    out = {k: u.get(k) for k in (
        "id", "username", "display_name", "email", "role", "permissions",
        "enabled", "must_change_password",
        "last_login_at", "last_login_ip",
        "created_at", "updated_at", "created_by")}
    return {k: v for k, v in out.items() if v is not None}


# --------------- 元信息 ---------------
@api_bp.route("/rbac/permissions", methods=["GET"])
@login_required
def list_permissions():
    """返回权限点 + 角色权限映射（前端「用户管理」表单用）。"""
    return jsonify({
        "permissions": [
            {"key": k, "name": n, "group": g} for (k, n, g) in rbac.PERMISSIONS
        ],
        "roles": rbac.VALID_ROLES,
        "role_permissions": rbac.ROLE_PERMISSIONS,
    })


@api_bp.route("/rbac/me", methods=["GET"])
@login_required
def me():
    u = session.get("user") or {}
    uid = u.get("id") if isinstance(u, dict) else 0
    row = rbac.get_by_id(uid) if uid else None
    payload = {
        "username": (row or {}).get("username") or (u.get("username") if isinstance(u, dict) else ""),
        "display_name": (row or {}).get("display_name") or (u.get("display_name") if isinstance(u, dict) else ""),
        "role": (row or {}).get("role") or (u.get("role") if isinstance(u, dict) else "admin"),
        "permissions": sorted(rbac.effective_permissions(
            row if row else (u if isinstance(u, dict) else {}))),
        "must_change_password": (row or {}).get("must_change_password", 0),
    }
    return jsonify(payload)


@api_bp.route("/rbac/me/password", methods=["POST"])
@login_required
def change_my_password():
    """修改当前用户自己的密码（需提供旧密码）。"""
    data = request.get_json(force=True, silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    if not old or not new:
        return jsonify({"error": "旧密码与新密码均为必填"}), 400
    u = session.get("user") or {}
    uid = u.get("id") if isinstance(u, dict) else 0
    row = rbac.get_by_id(uid) if uid else None
    if not row:
        return jsonify({"error": "当前账号未在 users 表中（内置账号，请用 config.WEB_PASSWORD 登录后通过 init 创建）"}), 400
    if not rbac.verify_password(old, row.get("password_hash") or ""):
        return jsonify({"error": "旧密码错误"}), 401
    try:
        rbac.set_password(uid, new)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# --------------- 用户 CRUD ---------------
@api_bp.route("/rbac/users", methods=["GET"])
@login_required
@permission_required("users.manage")
def list_users():
    rows = rbac.list_users(include_disabled=True)
    return jsonify({"users": [_scrub(u) for u in rows]})


@api_bp.route("/rbac/users", methods=["POST"])
@login_required
@permission_required("users.manage")
def create_user():
    data = request.get_json(force=True, silent=True) or {}
    pwd = data.get("password") or ""
    try:
        new_id = rbac.create(data, pwd, created_by=(session.get("user") or {}).get("username", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": new_id})


@api_bp.route("/rbac/users/<int:uid>", methods=["PUT"])
@login_required
@permission_required("users.manage")
def update_user(uid: int):
    data = request.get_json(force=True, silent=True) or {}
    try:
        ok = rbac.update(uid, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify({"ok": True})


@api_bp.route("/rbac/users/<int:uid>/password", methods=["POST"])
@login_required
@permission_required("users.manage")
def reset_password(uid: int):
    data = request.get_json(force=True, silent=True) or {}
    pwd = data.get("password") or ""
    try:
        rbac.set_password(uid, pwd)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@api_bp.route("/rbac/users/<int:uid>", methods=["DELETE"])
@login_required
@permission_required("users.manage")
def delete_user(uid: int):
    ok, msg = rbac.delete(uid)
    if not ok:
        return jsonify({"error": msg or "删除失败"}), 400
    return jsonify({"ok": True})