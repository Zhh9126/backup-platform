# -*- coding: utf-8 -*-
"""登录鉴权：页面走 Flask session；外部系统走 Bearer API Token（api_tokens 表）。

session["user"] 结构（RBAC 后）
-------------------------------
登录成功时设为 dict：{"id","username","display_name","role"}。
旧版本（升级兼容）可能是字符串 username，login_required 仍按"已登录"放行，
权限校验时回退为内置 admin（仅第一次部署生效）。
"""
from functools import wraps
from flask import session, request, jsonify, redirect, url_for, g


def _extract_bearer_token() -> str:
    """从 Authorization: Bearer <token> 或 X-API-Token 头提取外部调用令牌。"""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-API-Token") or "").strip()


def _valid_api_token(token: str) -> bool:
    if not token:
        return False
    try:
        import core.models as models
        row = models.verify_api_token(token)
        if row:
            g.api_token = {"id": row["id"], "name": row["name"]}
            return True
    except Exception:
        pass
    return False


def _current_user_dict() -> dict:
    """把 session['user'] 标准化为 dict（兼容旧字符串值）。"""
    u = session.get("user")
    if isinstance(u, dict):
        return u
    if isinstance(u, str) and u:
        # 旧 session：回退为内置 admin 角色（一次性升级窗口）
        return {"id": 0, "username": u, "display_name": u,
                "role": "admin", "_legacy": True}
    return {}


def login_required(f):
    @wraps(f)
    def decorator(*args, **kwargs):
        if session.get("user"):
            return f(*args, **kwargs)
        # 外部系统调用：Bearer Token / X-API-Token 认证
        if _valid_api_token(_extract_bearer_token()):
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录"}), 401
        return redirect(url_for("login_page"))
    return decorator


def permission_required(perm: str):
    """装饰器：要求当前用户拥有某个权限（admin 自动放行）。

    用法：
        @api_bp.route("/users", methods=["POST"])
        @login_required
        @permission_required("users.manage")
        def create_user(): ...
    """
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = _current_user_dict()
            # 外部 API Token 视为超管（兼容既有外部调用方）
            if g.get("api_token"):
                return f(*args, **kwargs)
            if not u:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "未登录"}), 401
                return redirect(url_for("login_page"))
            try:
                import core.rbac as rbac
                if not rbac.has_permission(u, perm):
                    return jsonify({"error": f"权限不足：缺少 {perm}"}), 403
            except Exception:
                return jsonify({"error": "权限校验失败"}), 500
            return f(*args, **kwargs)
        return wrapper
    return deco
