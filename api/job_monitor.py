# -*- coding: utf-8 -*-
"""备份任务作业监控 API（对标 Commvault/Veeam 作业监控）。"""
from flask import jsonify, request

from auth import login_required
from core import job_monitor
from . import api_bp


@api_bp.route("/job-monitor/summary", methods=["GET"])
@login_required
def jm_summary():
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"success": True,
                    "data": job_monitor.summary(limit=min(max(limit, 10), 200))})
