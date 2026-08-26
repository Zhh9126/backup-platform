# -*- coding: utf-8 -*-
"""
全局重删 API（参照鼎甲迪备白皮书 §2.4 全局重删）。

路由前缀: /api/dedup
- GET  /api/dedup/stats   全局重删统计（重删比 / 累计节省 / 唯一块数）
- POST /api/dedup/scan    对一个备份集产物做切片重删（手动触发，用于演示/回收）

说明：全局重删在备份写入路径（file / 仿真产物）自动调用 core.global_dedup，
本 API 仅暴露统计与手动扫描入口，不影响备份主流程。
"""
import os

from flask import request, jsonify

import core.models as models
from auth import login_required
from . import api_bp


@api_bp.route("/dedup/stats", methods=["GET"])
@login_required
def api_dedup_stats():
    from core import global_dedup as gd
    from core import db as _db
    s = gd.global_stats()
    s["saved_bytes_human"] = _db.human_size(s.get("saved_bytes", 0))
    return jsonify({"ok": True, "stats": s})


@api_bp.route("/dedup/scan", methods=["POST"])
@login_required
def api_dedup_scan():
    """对一个备份集的 object_key 物理文件做切片重删（演示/回收用）。"""
    from core import global_dedup as gd
    payload = request.get_json(silent=True) or {}
    set_id = payload.get("set_id")
    if not set_id:
        return jsonify({"ok": False, "error": "set_id required"}), 400
    bs = models.get_backup_set(set_id)
    if not bs:
        return jsonify({"ok": False, "error": "backup set not found"}), 404
    key = bs.get("object_key")
    if not key or not isinstance(key, str) or not os.path.isfile(key):
        return jsonify({"ok": False, "error": "object_key missing or not a file"}), 400
    res = gd.dedup_file(key, task_id=bs.get("task_id"), set_id=set_id)
    # 把节省量回写备份集
    if res.get("saved_bytes"):
        models.add_dedup_saved(set_id, int(res["saved_bytes"]))
    return jsonify({"ok": True, "set_id": set_id, "result": res,
                    "stats": gd.global_stats()})
