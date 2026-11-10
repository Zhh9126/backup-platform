# -*- coding: utf-8 -*-
"""REST API 蓝图聚合。"""
import os

from flask import Blueprint, jsonify, current_app, request, session
from urllib.parse import urlparse

api_bp = Blueprint("api", __name__, url_prefix="/api")

from . import contract  # noqa: E402  统一契约（错误码/分页/版本头）

# 公开端点白名单（匿名可访问；当前除登录页外均为受保护 API）
PUBLIC_API_PATHS = {"/api/meta", "/api/health"}


def _is_public_path(path: str) -> bool:
    """公开端点判定：/api/v1 与 /api 前缀视为同一端点。"""
    if path.startswith(contract.V1_PREFIX):
        path = contract.LEGACY_PREFIX + path[len(contract.V1_PREFIX):]
    return path in PUBLIC_API_PATHS


# 首次登录强制改密期间仍允许访问的端点（仅自助读/改密与元信息）
_PASSWORD_CHANGE_PATHS = frozenset({
    "/api/rbac/me", "/api/rbac/me/password", "/api/rbac/permissions",
    "/api/meta", "/api/health",
})


@api_bp.before_request
def _api_security_gate():
    """API 全局安全钩子（必须在嵌套蓝图注册前声明）。

    1) 鉴权兜底：任何 API 都要求已登录或持有有效外部调用令牌（白名单除外）。
       Bearer / X-API-Token（api_tokens 表）与 AccessKey HMAC 签名均受支持。
    2) 首次登录强制改密：默认关闭（零破坏），开启后 must_change_password
       账号只能访问自助端点，其余 API 返回 428。
    3) CSRF 防护：浏览器会话写操作校验 Origin/Referer 严格同源。
       外部令牌认证不是浏览器请求，不适用 CSRF。
    4) 写端点权限点映射：239 个写端点统一经 api/perm_map.py 映射到 RBAC
       权限点，替代逐端点手写装饰器与"只读角色子串白名单"兜底。
       探测/预览/校验与自助改密走精确豁免；已有 @permission_required
       的端点保有更细粒度判定。
    """
    from auth import (_extract_bearer_token, _valid_api_token,
                      _valid_ak_signature, _current_user_dict)
    from core import rbac as _rbac
    from . import perm_map

    path = request.path
    if _is_public_path(path):
        return None

    # 1) 鉴权兜底；_current_user_dict 兼容旧字符串 session，避免 AttributeError
    user = _current_user_dict()
    token_auth = False
    if not user:
        token = _extract_bearer_token()
        if not (_valid_api_token(token) or _valid_ak_signature()):
            return jsonify({"success": False,
                            "error": "未登录或会话已过期（外部调用请携带 "
                                     "Authorization: Bearer <token>）"}), 401
        token_auth = True
        # 外部令牌按可配置等效角色校验（默认 admin，兼容存量外部调用方）
        user = {"id": 0, "username": "external-token",
                "role": _rbac.external_auth_role(), "_external": True}

    # 2) 首次登录强制改密闸
    if session.get("pwd_change_required") and not token_auth:
        p = perm_map.normalize((path or "").rstrip("/") or "/")
        if p not in _PASSWORD_CHANGE_PATHS:
            return jsonify({"success": False,
                            "error": "首次登录需先修改默认口令",
                            "password_change_required": True}), 428

    if request.method not in perm_map.WRITE_METHODS:
        return None

    # 3) CSRF：Origin: null / 缺 netloc 不能当作同源
    if not token_auth:
        origin = (request.headers.get("Origin") or request.headers.get("Referer") or "").strip()
        if origin:
            o = urlparse(origin)
            if not o.scheme or not o.netloc or o.netloc != request.host:
                return jsonify({"success": False, "error": "CSRF 校验失败：跨站请求被拒绝"}), 403

    # 4) 统一写端点权限映射（豁免返回 perm=None）
    perm, _reason = perm_map.resolve(path, request.method)
    if perm and not _rbac.has_permission(user, perm):
        return jsonify({"success": False,
                        "error": f"权限不足：缺少 {perm}",
                        "required_permission": perm}), 403
    return None


def safe_download_path(path: str):
    """安全整改：仅允许下载备份根目录（BACKUP_ROOT）内的文件。

    防止路径穿越 / 任意文件读取。返回 realpath 供 send_file 使用；
    非法路径返回 None（调用方返回 400/404）。
    """
    if not path or not os.path.isabs(path):
        return None
    real = os.path.realpath(path)
    # 备份根目录支持界面运行时修改，故优先取 config 当前值（flask 配置为启动快照）
    try:
        import config as _cfg
        _root = str(_cfg.get_backup_root())
    except Exception:
        _root = str(current_app.config.get("BACKUP_ROOT") or "backups")
    root = os.path.realpath(_root)
    if real != root and not real.startswith(root + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return real


from . import (tasks, records, restore, system, hosts, sync, inspection, deploy,
                restore_extras_api, drills, storage, policy, lifecycle, migration,
                clone, itsm, link, dr_dns, ai_alert, datamining, rt, plugins,
                restore_verify, synthesize, dedup, jdbc, data_compare,
                tape, logs, db_adapters, rbac, cdc, vm, openapi,
                agentless, object_storage, backup_cleanup, meta_db,
                agents, op_guard, oracle_txn, pool, file_sync, ai_backup,
                report_center, access_key, snmp, job_monitor, netdev, license_api,
                win_protect, synology, remote_pull, ransomware)  # noqa: E402,F401

# AI 助手（收费功能）：镜像/离线交付包中不含 core/ai_agent 与 api/ai_agent.py，
# 这里必须容错注册——缺失时平台其余功能不受影响（2026-09-19 用户决定）。
try:
    from . import ai_agent  # noqa: E402,F401
except Exception as _e:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("aidbm").info("AI 助手模块未随本包交付（收费功能），已跳过: %s", _e)

# 统一契约钩子（三段式错误体 + 版本/弃用响应头）。必须在 app.register_blueprint
# 之前声明：Flask 在注册蓝图时冻结其请求钩子列表。
contract.register(api_bp)
