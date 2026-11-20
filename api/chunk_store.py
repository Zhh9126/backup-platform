# -*- coding: utf-8 -*-
"""分块压缩内容寻址存储 API（PB 级方案 §3.1 / §4）。

- GET  /api/chunk-store/stats               存储现状（块数/物理占用/清单数）
- POST /api/chunk-store/estimate            量化某个产物「整包压缩 vs 分块压缩」
- POST /api/chunk-store/store               把某条备份记录产物真正写入块存储
- POST /api/chunk-store/restore             从块存储还原并校验（内容级证明）
- GET  /api/chunk-store/manifests           最近清单

只读取 BACKUP_ROOT 内的产物，写入只落在块存储目录（不触碰原产物）。
"""
import os

from flask import jsonify, request

import config
from auth import login_required, permission_required
import core.db as db
from core import chunk_store
from . import api_bp


def _record_path(record_id):
    row = db.query_one("SELECT id, backup_path, size_bytes FROM backup_records "
                       "WHERE id=?", (record_id,))
    if not row:
        return None, "备份记录不存在", 404
    p = row.get("backup_path")
    if not p or not os.path.isfile(p):
        return None, "该记录的产物不存在（可能已清理）", 400
    try:
        root = os.path.realpath(getattr(config, "BACKUP_ROOT", "") or "/")
        if not os.path.realpath(p).startswith(root):
            return None, "产物不在备份根目录内，拒绝读取", 400
    except OSError:
        return None, "路径解析失败", 400
    return p, None, 200


@api_bp.route("/chunk-store/stats", methods=["GET"])
@login_required
def chunk_store_stats():
    from core import db as _db
    s = chunk_store.stats()
    s["physical_human"] = _db.human_size(s.get("physical_bytes", 0))
    return jsonify({"ok": True, "stats": s})


@api_bp.route("/chunk-store/manifests", methods=["GET"])
@login_required
def chunk_store_manifests():
    return jsonify({"ok": True, "items": chunk_store.list_manifests(limit=50)})


@api_bp.route("/chunk-store/estimate", methods=["POST"])
@login_required
def chunk_store_estimate():
    """对比整包压缩与分块压缩的体积（不写盘）。"""
    payload = request.get_json(silent=True) or {}
    rid = payload.get("record_id")
    if not rid:
        return jsonify({"ok": False, "error": "record_id 必填"}), 400
    path, err, code = _record_path(int(rid))
    if err:
        return jsonify({"ok": False, "error": err}), code
    try:
        est = chunk_store.estimate_vs_whole(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "测算失败: %s" % exc}), 500
    est["ok"] = True
    est["record_id"] = int(rid)
    est["verdict"] = (
        "分块压缩额外开销 %.1f%%：换来跨版本只传变化块的能力"
        % est["overhead_pct"])
    return jsonify(est)


@api_bp.route("/chunk-store/store", methods=["POST"])
@permission_required("system.settings")
def chunk_store_store():
    """把一条备份记录的产物写入块存储（幂等：同内容块只写一次）。"""
    payload = request.get_json(silent=True) or {}
    rid = payload.get("record_id")
    if not rid:
        return jsonify({"ok": False, "error": "record_id 必填"}), 400
    path, err, code = _record_path(int(rid))
    if err:
        return jsonify({"ok": False, "error": err}), code
    try:
        rep = chunk_store.store_file(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "写入块存储失败: %s" % exc}), 500
    # 回写清单 ID：原产物日后被保留策略清理时，恢复入口可自动回填
    try:
        db.execute("UPDATE backup_records SET chunk_manifest_id=? WHERE id=?",
                   (rep["manifest_id"], int(rid)))
    except Exception as exc:  # noqa: BLE001
        rep["warning"] = "块已写入但回写清单 ID 失败: %s" % exc
    rep["record_id"] = int(rid)
    return jsonify(rep)


@api_bp.route("/chunk-store/restore", methods=["POST"])
@login_required
def chunk_store_restore():
    """从块存储还原到指定路径并做 sha256 校验（证明内容级可靠）。"""
    payload = request.get_json(silent=True) or {}
    mid = payload.get("manifest_id")
    out = payload.get("out_path")
    if not mid or not out:
        return jsonify({"ok": False, "error": "manifest_id 与 out_path 必填"}), 400
    try:
        res = chunk_store.restore_file(mid, out)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "manifest 不存在"}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "还原失败: %s" % exc}), 500
    return jsonify(res)
