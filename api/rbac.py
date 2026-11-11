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
        "role_labels": getattr(rbac, "ROLE_LABELS", {}),
        "role_permissions": rbac.ROLE_PERMISSIONS,
        "password_policy_enabled": rbac.password_policy_enabled(),
    })


@api_bp.route("/rbac/me", methods=["GET"])
@login_required
def me():
    u = session.get("user") or {}
    uid = u.get("id") if isinstance(u, dict) else 0
    row = rbac.get_by_id(uid) if uid else None
    role = (row or {}).get("role") or (u.get("role") if isinstance(u, dict) else "admin")
    from core import features as _features
    _who = row if row else (u if isinstance(u, dict) else {})
    payload = {
        "username": (row or {}).get("username") or (u.get("username") if isinstance(u, dict) else ""),
        "display_name": (row or {}).get("display_name") or (u.get("display_name") if isinstance(u, dict) else ""),
        "role": role,
        "permissions": _features.filter_permissions(
            _who, sorted(rbac.effective_permissions(_who))),
        # 当前账号可用功能项 + 全量清单（前端按 paths 反查隐藏菜单）
        "features": sorted(_features.allowed_features(_who)),
        "feature_catalog": _features.catalog(),
        "must_change_password": (row or {}).get("must_change_password", 0),
        "password_change_required": bool(session.get("pwd_change_required")),
        "force_password_change_enabled": rbac.force_password_change_enabled(),
        # 出厂口令告警只透给超管自己，避免把安全状态扩散给只读账号
        "default_password_in_use": (
            rbac.default_admin_password_in_use()
            if role == "admin" else False),
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
    ok_pwd, err_pwd = rbac.validate_password(new)
    if not ok_pwd:
        return jsonify({"error": err_pwd}), 400
    try:
        rbac.set_password(uid, new)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # 改密成功即解除首次登录闸门（set_password 已清 must_change_password）
    session.pop("pwd_change_required", None)
    return jsonify({"ok": True, "password_change_required": False})


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
    ok_pwd, err_pwd = rbac.validate_password(pwd)
    if not ok_pwd:
        return jsonify({"error": err_pwd}), 400
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
    ok_pwd, err_pwd = rbac.validate_password(pwd)
    if not ok_pwd:
        return jsonify({"error": err_pwd}), 400
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

# --------------- 强口令策略（三员管理配套） ---------------
@api_bp.route("/rbac/password-policy", methods=["GET", "POST"])
@login_required
@permission_required("security.manage")
def password_policy():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        rbac.set_password_policy(bool(data.get("enabled")))
        import core.db as _db
        _db.add_log("warning", "auth",
                    f"强口令策略已{'开启' if data.get('enabled') else '关闭'}"
                    f"（操作者 {(session.get('user') or {}).get('username', '')}）")
    return jsonify({"enabled": rbac.password_policy_enabled()})


# --------------- 首次登录强制改密（默认关闭，存量部署零破坏） ---------------
@api_bp.route("/rbac/password-change-policy", methods=["GET", "POST"])
@login_required
@permission_required("security.manage")
def password_change_policy():
    """开关 must_change_password 账号的强制改密流程。

    关闭时 admin/admin123 等存量账号登录行为完全不变；开启后首次登录
    只能访问自助端点，其余 API 返回 428，改密成功后自动解锁。
    env AIDBM_FORCE_PASSWORD_CHANGE=1 可强制开启（优先级最高）。
    """
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        rbac.set_force_password_change(bool(data.get("enabled")))
        import core.db as _db
        _db.add_log("warning", "auth",
                    f"首次登录强制改密已{'开启' if data.get('enabled') else '关闭'}"
                    f"（操作者 {(session.get('user') or {}).get('username', '')}）")
    import config as _cfg
    return jsonify({
        "enabled": rbac.force_password_change_enabled(),
        "env_override": bool(getattr(_cfg, "FORCE_PASSWORD_CHANGE", False)),
        "default_password_in_use": rbac.default_admin_password_in_use(),
    })


# --------------- 功能项：按需启用 + 权限分离 ---------------
@api_bp.route("/rbac/features", methods=["GET"])
@login_required
@permission_required("system.settings")
def feature_config_get():
    """返回功能项清单、当前配置、角色与用户（管理界面用）。"""
    from core import features
    return jsonify({
        "ok": True,
        "catalog": features.catalog(),
        "config": features.get_config(),
        "roles": rbac.VALID_ROLES,
        "role_labels": rbac.ROLE_LABELS,
        "users": [
            {"id": u.get("id"), "username": u.get("username"),
             "role": u.get("role")}
            for u in rbac.list_users(include_disabled=True)
        ],
    })


@api_bp.route("/rbac/features", methods=["POST"])
@login_required
@permission_required("system.settings")
def feature_config_save():
    """保存平台功能开关 + 角色/用户功能分配。"""
    from core import features
    data = request.get_json(silent=True) or {}
    cfg = features.save_config(data)
    import core.db as _db
    _operator = (session.get("user") or {}).get("username", "")
    _db.add_log("WARNING", "auth", "功能项配置已更新（操作者 %s）" % _operator)
    return jsonify({"ok": True, "config": cfg})
