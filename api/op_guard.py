# -*- coding: utf-8 -*-
"""高危操作防护 API（P0-2）：

- GET  /api/verify-code      生成一次性操作验证码（展示给操作者照抄输入）
- GET  /api/op-guard/config  查询高危操作验证码开关
- POST /api/op-guard/config  保存开关（verify_code_enabled: bool）

高危端点（恢复执行 / 备份清理 / 删除任务 / 删除主机）在开关开启时要求
请求体携带 verify_code，由 auth.verify_code_gate 统一校验。

安全整改：关闭防护属于安全配置变更，必须带 security.manage 权限点，
否则任意登录用户（含 viewer）都能先关闸再执行高危动作。
"""
from flask import jsonify, request

from auth import (issue_verify_code, op_verify_enabled, permission_required,
                  set_op_verify_enabled, login_required)
from core import db
from . import api_bp


@api_bp.route("/verify-code", methods=["GET"])
@login_required
def api_issue_verify_code():
    return jsonify(issue_verify_code())


def _restore_precheck_enabled():
    try:
        return str(db.get_system_config("restore_auto_precheck") or "1") \
            .strip().lower() not in ("0", "false", "off", "no")
    except Exception:
        return True


@api_bp.route("/op-guard/config", methods=["GET"])
@login_required
def api_op_guard_config():
    return jsonify({"ok": True, "verify_code_enabled": op_verify_enabled(),
                    "restore_auto_precheck": _restore_precheck_enabled()})


@api_bp.route("/op-guard/config", methods=["POST"])
@login_required
@permission_required("security.manage")
def api_op_guard_save_config():
    data = request.get_json(silent=True) or {}
    if "restore_auto_precheck" in data:
        # 恢复前自动校验（P0-3）：默认开；关闭=允许跳过预检直接恢复
        #（与验证码开关同级的安全配置变更，同样要求 security.manage 权限）
        db.set_system_config("restore_auto_precheck",
                             "1" if data.get("restore_auto_precheck") else "0")
    if "verify_code_enabled" in data:
        set_op_verify_enabled(bool(data.get("verify_code_enabled")))
    return jsonify({"ok": True, "verify_code_enabled": op_verify_enabled(),
                    "restore_auto_precheck": _restore_precheck_enabled()})
