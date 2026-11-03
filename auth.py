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


# ---------------------------------------------------------------------------
# 高危操作验证码（P0-2，对标鼎甲高危操作二次确认）
#
# 设计：默认关闭（system_config op_verify_code_enabled，env AIDBM_OP_VERIFY_CODE
# 可强制覆盖）。开启后，恢复执行 / 备份清理 / 删除任务 / 删除主机等高危端点
# 必须携带 GET /api/verify-code 下发并展示给操作者的 4 位验证码。
# 验证码存 Flask session：5 分钟有效、一次性（校验即销毁，无论成败）。
# ---------------------------------------------------------------------------

_VC_TTL_SECONDS = 300
_VC_TRUE = ("1", "true", "on", "yes")


def op_verify_enabled() -> bool:
    """高危操作验证码开关：env 强制覆盖 > system_config（默认关）。"""
    import os
    import core.db as db
    env = (os.environ.get("AIDBM_OP_VERIFY_CODE") or "").strip().lower()
    if env:
        return env in _VC_TRUE
    val = (db.get_system_config("op_verify_code_enabled") or "").strip().lower()
    return val in _VC_TRUE


def set_op_verify_enabled(on: bool) -> None:
    import core.db as db
    db.set_system_config("op_verify_code_enabled", "1" if on else "0")


def issue_verify_code() -> dict:
    """生成并暂存一次性操作验证码（返回给前端展示，操作者照抄输入）。"""
    import secrets
    import time
    code = "%04d" % secrets.randbelow(10000)
    session["op_vc_code"] = code
    session["op_vc_expires"] = time.time() + _VC_TTL_SECONDS
    return {"code": code, "expires_in": _VC_TTL_SECONDS,
            "purpose": "高危操作确认"}


def check_verify_code(code) -> tuple:
    """校验操作验证码。返回 (ok, err)；无论成败均销毁（一次性+防重放）。"""
    import time
    want = session.get("op_vc_code")
    expires = session.get("op_vc_expires") or 0
    session.pop("op_vc_code", None)
    session.pop("op_vc_expires", None)
    if not want:
        return False, "未获取操作验证码，请先刷新验证码后重试"
    if time.time() > expires:
        return False, "操作验证码已过期（5 分钟有效），请重新获取"
    if str(code or "").strip() != want:
        return False, "操作验证码不正确（验证码已失效，请重新获取输入）"
    return True, ""


def verify_code_gate(data: dict):
    """高危 API 校验钩子。返回 None=放行；(payload, status)=拒绝响应。

    开关关闭时恒放行（零破坏：存量脚本/自动化不受影响）。
    """
    if not op_verify_enabled():
        return None
    ok, err = check_verify_code((data or {}).get("verify_code"))
    if not ok:
        return ({"error": err, "verify_code_required": True}, 403)
    return None
