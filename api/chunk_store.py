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
    # 容器模式（Append-Only 64MB 封箱）单独统计：块都在容器里，blob 目录为空
    try:
        from core import container_store
        ct = container_store.stats()
        ct["physical_human"] = _db.human_size(ct.get("physical_bytes", 0))
        s["containers"] = ct
    except Exception as exc:  # noqa: BLE001
        s["containers"] = {"error": str(exc)}
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
    mode = str(payload.get("mode") or "blob").lower()
    if mode not in ("blob", "container"):
        return jsonify({"ok": False, "error": "mode 只能是 blob 或 container"}), 400
    try:
        if mode == "container":
            # Append-Only 容器封箱：块追加进 64MB 容器，显著减少小文件
            from core import container_store
            rep = container_store.store_file(path)
        else:
            rep = chunk_store.store_file(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "写入块存储失败: %s" % exc}), 500
    rep["mode"] = mode
    # 回写清单 ID：原产物日后被保留策略清理时，恢复入口可自动回填
    try:
        db.execute("UPDATE backup_records SET chunk_manifest_id=? WHERE id=?",
                   (rep["manifest_id"], int(rid)))
    except Exception as exc:  # noqa: BLE001
        rep["warning"] = "块已写入但回写清单 ID 失败: %s" % exc
    rep["record_id"] = int(rid)
    return jsonify(rep)


@api_bp.route("/chunk-store/have", methods=["POST"])
@login_required
def chunk_store_have():
    """指纹探路应答：对端告诉源端「这些块我已有哪些」（返回位图）。

    跨站点复制时由目标端调用（或目标端部署同款平台，源端直接请求）。
    单批上限 16384 个指纹（约 512KB），超出请分批。
    """
    payload = request.get_json(silent=True) or {}
    fps = payload.get("fps") or []
    if not isinstance(fps, list):
        return jsonify({"ok": False, "error": "fps 必须是数组"}), 400
    if len(fps) > 16384:
        return jsonify({"ok": False,
                        "error": "单批最多 16384 个指纹，请分批探路"}), 400
    from core import block_transfer
    # 本端「已有」= 块存储中实际存在的块
    have = [str(fp) for fp in fps if chunk_store.has_block(str(fp))]
    res = block_transfer.have_bitmap(have, [str(x) for x in fps])
    res["ok"] = True
    res["blocks_on_disk"] = len(have)
    return jsonify(res)


@api_bp.route("/chunk-store/transfer-plan", methods=["POST"])
@login_required
def chunk_store_transfer_plan():
    """生成增量传输计划（两份清单 → 缺失块区间 + 节省率）。"""
    from core import block_transfer
    payload = request.get_json(silent=True) or {}
    src = payload.get("source_manifest")
    tgt = payload.get("target_manifest")
    if not src or not tgt:
        return jsonify({"ok": False,
                        "error": "source_manifest 与 target_manifest 必填"}), 400
    try:
        plan = block_transfer.plan_transfer(src, tgt,
                                           batch=int(payload.get("batch") or block_transfer.BATCH))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "清单不存在"}), 404
    plan["transfer_human"] = db.human_size(plan.get("transfer_comp_bytes", 0))
    plan["total_human"] = db.human_size(plan.get("target_total_comp_bytes", 0))
    return jsonify(plan)


@api_bp.route("/chunk-store/apply-bitmap", methods=["POST"])
@login_required
def chunk_store_apply_bitmap():
    """按对端返回的位图生成区间计划（真实跨站点路径）。"""
    from core import block_transfer
    payload = request.get_json(silent=True) or {}
    mid = payload.get("target_manifest")
    bitmap = payload.get("bitmap")
    if not mid or bitmap is None:
        return jsonify({"ok": False,
                        "error": "target_manifest 与 bitmap 必填"}), 400
    try:
        man = chunk_store.load_manifest(mid)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "清单不存在"}), 404
    fps = [c["fp"] for c in man["chunks"]]
    src_map = {c["fp"]: c for c in man["chunks"]}
    try:
        plan = block_transfer.apply_bitmap(fps, str(bitmap), src_map)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    plan["ok"] = True
    plan["transfer_human"] = db.human_size(plan.get("transfer_comp_bytes", 0))
    return jsonify(plan)


@api_bp.route("/chunk-store/gc", methods=["POST"])
@permission_required("system.settings")
def chunk_store_gc():
    """回收未被任何清单引用的块（默认 dry_run 只报数，确认后才实删）。"""
    payload = request.get_json(silent=True) or {}
    dry = payload.get("dry_run", True)
    dry = bool(dry) if not isinstance(dry, str) else dry.lower() in ("1", "true", "yes")
    res = chunk_store.gc(dry_run=dry)
    res["reclaimable_human"] = db.human_size(res.get("reclaimable_bytes", 0))
    return jsonify(res)


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
