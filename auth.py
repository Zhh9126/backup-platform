# -*- coding: utf-8 -*-
"""登录鉴权：基于 Flask session 的简单口令鉴权，供页面与 API 复用。"""
from functools import wraps
from flask import session, request, jsonify, redirect, url_for


def login_required(f):
    @wraps(f)
    def decorator(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorator
