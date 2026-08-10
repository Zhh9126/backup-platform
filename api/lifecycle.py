# -*- coding: utf-8 -*-
"""
冷热分级生命周期 API：状态概览 / 策略配置 / 手动触发。

路由前缀: /api/lifecycle（通过共享 api_bp 注册）
- GET  /api/lifecycle          状态概览（各级备份集计数 / 容量 / 配置）
- POST /api/lifecycle/config   保存生命周期策略（年龄 / 容量阈值 + 开关）
- POST /api/lifecycle/run      手动触发一次生命周期流转
"""
from flask import request, jsonify

import core.models as models
from auth import login_required
from . import api_bp


def _engine():
    from core import lifecycle as lifecycle_engine
    return lifecycle_engine.LifecycleEngine()


@api_bp.route("/lifecycle", methods=["GET"])
@login_required
def api_lifecycle_status():
    engine = _engine()
    return jsonify({"ok": True, "config": engine.get_config(),
                    "status": engine.get_status()})


@api_bp.route("/lifecycle/config", methods=["POST"])
@login_required
def api_lifecycle_save_config():
    data = request.get_json(silent=True) or {}
    engine = _engine()
    cfg = engine.save_config(data)
    return jsonify({"ok": True, "config": cfg})


@api_bp.route("/lifecycle/run", methods=["POST"])
@login_required
def api_lifecycle_run():
    engine = _engine()
    summary = engine.run_once()
    return jsonify({"ok": True, "summary": summary})
