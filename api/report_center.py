# -*- coding: utf-8 -*-
"""报表中心 API：运行报表聚合查询与导出（P2 池）。"""
from flask import jsonify, request, send_file
import io

from auth import login_required
from core import report_center
from . import api_bp


@api_bp.route("/report-center/summary", methods=["GET"])
@login_required
def report_summary():
    try:
        days = int(request.args.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    return jsonify({"success": True, "data": report_center.build_summary(days)})


@api_bp.route("/report-center/export", methods=["GET"])
@login_required
def report_export():
    # 参数名统一：历史上本端点用 fmt，而全站其它导出端点（/records/export、
    # 数据对比报表导出）都用 format。第三方/脚本按 format 调用时会静默拿到
    # CSV（2026-11-14 实测），故两者都接受；未知格式显式 400 而不是回落。
    fmt = (request.args.get("format") or request.args.get("fmt") or "csv").strip().lower()
    if fmt in ("excel", "xls"):
        fmt = "xlsx"
    if fmt == "word":
        fmt = "docx"
    if fmt not in ("csv", "xlsx", "docx", "pdf"):
        return jsonify({"success": False,
                        "message": f"不支持的导出格式: {fmt}（可选 csv/xlsx/docx/pdf）"}), 400
    try:
        days = int(request.args.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    try:
        mime, ext, content = report_center.export_report(fmt, days)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    from datetime import datetime as _dt
    fname = f"aidbm_report_{_dt.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    return send_file(io.BytesIO(content), mimetype=mime,
                     as_attachment=True, download_name=fname)
