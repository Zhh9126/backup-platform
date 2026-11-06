# -*- coding: utf-8 -*-
"""AIDBM 系统授权与纳管配额 API。"""
import logging
from flask import jsonify, request

from api import api_bp
from auth import login_required
from core import license

logger = logging.getLogger("aidbm.license_api")


@api_bp.route("/system/license", methods=["GET"])
@login_required
def get_system_license():
    """获取当前系统授权状态与纳管配额使用情况。"""
    try:
        info = license.get_license_info()
        quotas = license.get_quotas()
        return jsonify({
            "code": 0,
            "data": {
                "license": info,
                "quotas": quotas,
            }
        })
    except Exception as e:
        logger.error("获取授权状态失败: %s", e)
        return jsonify({"code": 500, "error": f"获取授权信息异常: {e}"}), 500


@api_bp.route("/system/license/mac", methods=["GET"])
@login_required
def get_system_mac():
    """获取当前服务器物理网卡 MAC 地址。"""
    return jsonify({
        "code": 0,
        "primary_mac": license.get_primary_mac(),
        "local_macs": license.get_local_mac_addresses(),
    })


@api_bp.route("/system/license/activate", methods=["POST"])
@login_required
def activate_system_license():
    """激活与当前服务器物理 MAC 绑定的永久企业授权码。"""
    data = request.get_json(silent=True) or {}
    code = (data.get("license_code") or "").strip()
    if not code:
        return jsonify({"code": 400, "error": "请输入授权码"}), 400

    try:
        res = license.activate_license(code)
        return jsonify({
            "code": 0,
            "msg": "企业永久授权激活成功！所有模块已解除纳管配额限制，无上限使用。",
            "data": res,
            "quotas": license.get_quotas(),
        })
    except license.LicenseError as le:
        return jsonify({"code": 400, "error": str(le)}), 400
    except Exception as e:
        logger.exception("激活授权异常: %s", e)
        return jsonify({"code": 500, "error": f"授权激活失败: {e}"}), 500


@api_bp.route("/system/license/deactivate", methods=["POST"])
@login_required
def deactivate_system_license():
    """注销当前商业授权，恢复至社区免费版。"""
    try:
        license.deactivate_license()
        return jsonify({
            "code": 0,
            "msg": "已注销当前授权，系统已恢复为社区免费版（各模块限制最多纳管 5 个）。",
            "data": license.get_license_info(),
            "quotas": license.get_quotas(),
        })
    except Exception as e:
        return jsonify({"code": 500, "error": f"注销授权失败: {e}"}), 500

