# -*- coding: utf-8 -*-
"""全局带宽仲裁状态接口（只读）。

GET /api/bw-broker  当前活跃备份的带宽分配（Max-Min 公平水填充结果）
配置入口：system_config ``global_bandwidth_kbps``（0/未配置 = 不启用仲裁）
"""
from flask import jsonify

from auth import login_required
from core import bw_broker
from . import api_bp


@api_bp.route("/bw-broker", methods=["GET"])
@login_required
def bw_broker_status():
    return jsonify(bw_broker.snapshot())
