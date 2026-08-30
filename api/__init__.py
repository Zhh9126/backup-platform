# -*- coding: utf-8 -*-
"""REST API 蓝图聚合。"""
import os

from flask import Blueprint, jsonify, current_app, request, session
from urllib.parse import urlparse

api_bp = Blueprint("api", __name__, url_prefix="/api")

# 公开端点白名单（匿名可访问；当前除登录页外均为受保护 API）
PUBLIC_API_PATHS = {"/api/meta", "/api/health"}


@api_bp.before_request
def _api_security_gate():
    """API 全局安全钩子（必须在嵌套蓝图注册前声明）。

    1) 鉴权兜底：任何 API 都要求已登录（白名单除外）。
       即使个别路由遗漏 @login_required，也不会匿名可访问。
    2) CSRF 防护：写操作校验 Origin/Referer 同源。
       无 Origin/Referer 头（如本机 curl/脚本）且已登录时放行。
    """
    if request.path not in PUBLIC_API_PATHS and "user" not in session:
        return jsonify({"success": False, "error": "未登录或会话已过期"}), 401
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = (request.headers.get("Origin") or request.headers.get("Referer") or "").strip()
        if origin:
            o = urlparse(origin)
            if o.netloc and o.netloc != request.host:
                return jsonify({"success": False, "error": "CSRF 校验失败：跨站请求被拒绝"}), 403


def safe_download_path(path: str):
    """安全整改：仅允许下载备份根目录（BACKUP_ROOT）内的文件。

    防止路径穿越 / 任意文件读取。返回 realpath 供 send_file 使用；
    非法路径返回 None（调用方返回 400/404）。
    """
    if not path or not os.path.isabs(path):
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(str(current_app.config.get("BACKUP_ROOT") or "backups"))
    if real != root and not real.startswith(root + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return real


from . import (tasks, records, restore, system, hosts, sync, inspection, deploy,
                 restore_extras_api, drills, storage, policy, lifecycle, migration,
                 clone, itsm, link, ai_alert, datamining, ai_agent, rt, plugins,
                 restore_verify, synthesize, dedup, jdbc, data_compare)  # noqa: E402,F401
