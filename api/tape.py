# -*- coding: utf-8 -*-
"""磁带库（D2T）API。

路由前缀: /api/tape
- GET  /api/tape/status            磁带目标状态（设备 mt status / 模拟带列表）
- GET  /api/tape/files?target_id=  带内成员清单
- POST /api/tape/restore           从磁带回迁文件到本地目录（供恢复流程使用）
- POST /api/tape/<id>/test         连接/写入测试
"""
from flask import request, jsonify

import core.db as db
from auth import login_required
from core.storage_backends import get_backend
from . import api_bp


def _get_tape_target(target_id: int) -> dict:
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (target_id,))
    if not row or row["type"] != "tape":
        return None
    return dict(row)


@api_bp.route("/tape/status", methods=["GET"])
@login_required
def api_tape_status():
    targets = db.query("SELECT * FROM storage_targets WHERE type='tape' ORDER BY id")
    out = []
    for t in targets:
        t = dict(t)
        try:
            backend = get_backend("tape", t)
            ok, msg = backend.test_connection()
            out.append({"id": t["id"], "name": t["name"], "endpoint": t.get("endpoint"),
                        "ok": ok, "message": msg,
                        "used_bytes": backend.get_used_space()})
        except Exception as e:
            out.append({"id": t["id"], "name": t.get("name"),
                        "ok": False, "message": str(e)})
    return jsonify(out)


@api_bp.route("/tape/files", methods=["GET"])
@login_required
def api_tape_files():
    target_id = request.args.get("target_id", type=int)
    t = _get_tape_target(target_id)
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    backend = get_backend("tape", t)
    return jsonify({"target": t["name"], "files": backend.list_files()})


@api_bp.route("/tape/restore", methods=["POST"])
@login_required
def api_tape_restore():
    """从磁带回迁文件：{target_id, object_key, dest_path}。"""
    data = request.get_json(silent=True) or {}
    t = _get_tape_target(int(data.get("target_id") or 0))
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    object_key = data.get("object_key")
    dest_path = data.get("dest_path")
    if not object_key or not dest_path:
        return jsonify({"error": "object_key 与 dest_path 必填"}), 400
    backend = get_backend("tape", t)
    res = backend.get_file(object_key, dest_path=dest_path)
    if res:
        return jsonify({"ok": True, "message": f"已回迁到 {dest_path}"})
    return jsonify({"ok": False, "error": "带内未找到该文件"}), 404


@api_bp.route("/tape/<int:target_id>/test", methods=["POST"])
@login_required
def api_tape_test(target_id):
    t = _get_tape_target(target_id)
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    backend = get_backend("tape", t)
    ok, msg = backend.test_connection()
    return jsonify({"ok": ok, "message": msg})
