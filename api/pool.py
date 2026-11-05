# -*- coding: utf-8 -*-
"""池复制两地三中心 API（P1-3）：

- GET    /api/pool/chains                       链列表（含站点与最近结果）
- POST   /api/pool/chains                       新建链
- GET    /api/pool/chains/<id>                  链详情
- PUT    /api/pool/chains/<id>                  更新（站点/模式/策略/启停）
- DELETE /api/pool/chains/<id>                  删除
- POST   /api/pool/chains/<id>/run              手动执行一次 {record_id}
- GET    /api/pool/replicas?record_id=          备份记录副本分布视图
- POST   /api/pool/fetch                        异地取回 {record_id, log_id}
"""
from flask import jsonify, request

import core.db as db
from auth import login_required, verify_code_gate
from core import pool_replication
from . import api_bp


@api_bp.route("/pool/chains", methods=["GET"])
@login_required
def api_pool_chains():
    return jsonify({"ok": True, "chains": pool_replication.list_chains()})


@api_bp.route("/pool/chains", methods=["POST"])
@login_required
def api_pool_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        cid = pool_replication.create_chain(data)
    except pool_replication.PoolError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": cid}), 201


@api_bp.route("/pool/chains/<int:chain_id>", methods=["GET"])
@login_required
def api_pool_get(chain_id):
    chain = pool_replication.get_chain(chain_id)
    if not chain:
        return jsonify({"error": "链不存在"}), 404
    return jsonify({"ok": True, "chain": chain})


@api_bp.route("/pool/chains/<int:chain_id>", methods=["PUT"])
@login_required
def api_pool_update(chain_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        pool_replication.update_chain(chain_id, data)
    except pool_replication.PoolError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@api_bp.route("/pool/chains/<int:chain_id>", methods=["DELETE"])
@login_required
def api_pool_delete(chain_id):
    if not pool_replication.get_chain(chain_id):
        return jsonify({"error": "链不存在"}), 404
    pool_replication.delete_chain(chain_id)
    return jsonify({"ok": True})


@api_bp.route("/pool/chains/<int:chain_id>/run", methods=["POST"])
@login_required
def api_pool_run(chain_id):
    """手动执行一次链复制（对指定备份记录）。

    backup_path 缺省时自动从备份记录解析产物路径（2026-11-04 巡检发现：
    只传 record_id 必然报"备份产物不存在: "——调用方不应被要求自己查库取路径）。
    """
    data = request.get_json(force=True, silent=True) or {}
    record_id = data.get("record_id")
    if not record_id:
        return jsonify({"error": "record_id 必填"}), 400
    backup_path = data.get("backup_path") or ""
    if not backup_path:
        from core import db as _db
        rec = _db.query_one(
            "SELECT backup_path FROM backup_records WHERE id=?", (int(record_id),))
        backup_path = (rec or {}).get("backup_path") or ""
    try:
        result = pool_replication.run_chain(
            chain_id, int(record_id), backup_path)
    except pool_replication.PoolError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "池复制执行失败: %s" % e}), 500
    return jsonify({"ok": True, "result": result})


@api_bp.route("/pool/replicas", methods=["GET"])
@login_required
def api_pool_replicas():
    try:
        view = pool_replication.replica_view(
            int(request.args.get("record_id") or 0))
    except pool_replication.PoolError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True, **view})


@api_bp.route("/pool/fetch", methods=["POST"])
@login_required
def api_pool_fetch():
    """异地取回：从池站点拉副本回平台恢复区（写入恢复目录供恢复选择）。"""
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("record_id") or not data.get("log_id"):
        return jsonify({"error": "record_id 与 log_id 必填"}), 400
    try:
        result = pool_replication.fetch_back(
            int(data["record_id"]), int(data["log_id"]))
    except pool_replication.PoolError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "取回失败: %s" % e}), 500
    return jsonify({"ok": result["ok"], "result": result})
