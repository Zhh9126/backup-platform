# -*- coding: utf-8 -*-
"""品牌设置 API：登录页 Logo / 侧栏 Logo / 品牌文字（系统配置页调用）。"""
from flask import jsonify, request

from auth import login_required
from core import brand as brand_mod

from . import api_bp


@api_bp.route("/brand", methods=["GET"])
@login_required
def brand_get():
    """读取当前品牌设置（含默认值与限制，供设置页渲染）。"""
    return jsonify({"success": True, "data": brand_mod.get_brand()})


@api_bp.route("/brand/logo", methods=["POST"])
@login_required
def brand_upload_logo():
    """上传 Logo（multipart/form-data：kind=login|sidebar, file=<图片>）。"""
    kind = (request.form.get("kind") or request.args.get("kind") or "").strip().lower()
    f = request.files.get("file") or request.files.get("logo")
    if not f:
        return jsonify({"success": False,
                        "message": "未收到文件（表单字段名应为 file）"}), 400
    try:
        raw = f.read()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": "读取上传内容失败: %s" % e}), 400
    try:
        result = brand_mod.save_logo(kind, raw)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    result["brand"] = brand_mod.get_brand()
    return jsonify({"success": True, "data": result,
                    "message": result.get("message", "已保存")})


@api_bp.route("/brand/logo", methods=["DELETE"])
@login_required
def brand_reset_logo():
    """恢复默认 Logo。"""
    kind = (request.args.get("kind") or request.form.get("kind") or "").strip().lower()
    try:
        result = brand_mod.reset_logo(kind)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    result["brand"] = brand_mod.get_brand()
    return jsonify({"success": True, "data": result,
                    "message": result.get("message", "已恢复默认")})


@api_bp.route("/brand/text", methods=["POST"])
@login_required
def brand_save_text():
    """保存品牌文字（name=侧栏品牌名称, title=首页大标题；空串=恢复默认该项）。"""
    data = request.get_json(silent=True) or {}
    try:
        result = brand_mod.save_text(name=data.get("name"), title=data.get("title"))
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    result["brand"] = brand_mod.get_brand()
    return jsonify({"success": True, "data": result,
                    "message": result.get("message", "已保存")})


@api_bp.route("/brand/text", methods=["DELETE"])
@login_required
def brand_reset_text():
    """恢复默认品牌文字。"""
    result = brand_mod.reset_text()
    result["brand"] = brand_mod.get_brand()
    return jsonify({"success": True, "data": result,
                    "message": result.get("message", "已恢复默认")})
