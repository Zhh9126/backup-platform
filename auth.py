# -*- coding: utf-8 -*-
"""登录鉴权：页面走 Flask session；外部系统走 Bearer API Token（api_tokens 表）。"""
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
