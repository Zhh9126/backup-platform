# -*- coding: utf-8 -*-
"""
自动合成全量 API：手动触发 / 状态概览。

落实 CDM "系统内自动合成全量"：永远增量 → 定期合成全量，
中间增量副本由 lifecycle 按 chain_status='merged' 回收。

路由前缀: /api/synthesize（通过共享 api_bp 注册）
- GET  /api/synthesize         概览（配置 + 各任务可合并增量链数）
- POST /api/synthesize/run      手动触发一次自动合成全量
"""
from flask import request, jsonify

import core.models as models
from auth import login_required
from . import api_bp


def _engine():
    from core import synthesize as synthesize_engine
    return synthesize_engine


@api_bp.route("/synthesize", methods=["GET"])
@login_required
def api_synthesize_status():
    syn = _engine()
    cfg = syn._load_config()
    # 统计存在可合并增量链的任务数
    tasks = models.list_tasks(enabled=True) or []
    mergeable = 0
    for t in tasks:
        sets = models.list_backup_sets(task_id=t["id"])
        bases = [s for s in sets
                 if s.get("set_type") in ("full", "synthetic_full")]
        for base in bases:
            inc = sum(1 for s in sets
                      if s.get("parent_set_id") == base["id"]
                      and s.get("set_type") == "incremental")
            if inc >= int(cfg.get("min_incremental", 2) or 2):
                mergeable += 1
                break
    return jsonify({"ok": True, "config": cfg, "mergeable_tasks": mergeable,
                    "total_tasks": len(tasks)})


@api_bp.route("/synthesize/run", methods=["POST"])
@login_required
def api_synthesize_run():
    syn = _engine()
    result = syn.run_auto_synthesis()
    return jsonify({"ok": True, "result": result})
