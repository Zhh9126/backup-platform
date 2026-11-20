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


@api_bp.route("/dedup/cutters", methods=["POST"])
@login_required
def api_dedup_cutters():
    """三种切分方式对比（固定块 / FastCDC / 8KB 块对齐）。

    请求同 estimate（task_id 或 record_ids）。用于判断某类产物该用哪种分块：
    块级备份（RMAN 片/数据文件）→ aligned；逻辑导出/压缩包 → cdc。
    """
    import config
    from core import db as _db
    from core import fastcdc

    payload = request.get_json(silent=True) or {}
    rec_ids = payload.get("record_ids") or []
    task_id = payload.get("task_id")

    def _rooted(p):
        try:
            root = os.path.realpath(getattr(config, "BACKUP_ROOT", "") or "/")
            return os.path.realpath(p).startswith(root)
        except OSError:
            return False

    def _read(rid):
        row = _db.query_one("SELECT id, backup_path FROM backup_records WHERE id=?",
                            (rid,))
        if not row or not row.get("backup_path"):
            return None, "记录 %s 无产物路径" % rid
        p = row["backup_path"]
        if not os.path.isfile(p):
            return None, "记录 %s 的产物已不存在" % rid
        if not _rooted(p):
            return None, "记录 %s 的产物不在备份根目录内，拒绝读取" % rid
        return p, None

    if not rec_ids:
        if not task_id:
            return jsonify({"ok": False, "error": "task_id 或 record_ids 必填"}), 400
        rows = _db.query(
            "SELECT id FROM backup_records WHERE task_id=? AND status='success' "
            "AND size_bytes>0 AND backup_path IS NOT NULL ORDER BY id DESC LIMIT 2",
            (int(task_id),))
        rec_ids = [r["id"] for r in rows]
        if len(rec_ids) < 2:
            return jsonify({"ok": False, "error": "成功记录不足 2 条"}), 400
    if len(rec_ids) != 2:
        return jsonify({"ok": False, "error": "record_ids 需要正好 2 个"}), 400

    pa, err = _read(rec_ids[0])
    if err:
        return jsonify({"ok": False, "error": err}), 400
    pb, err = _read(rec_ids[1])
    if err:
        return jsonify({"ok": False, "error": err}), 400

    cap = int(payload.get("max_bytes") or 64 * 1024 * 1024)
    try:
        with open(pa, "rb") as f:
            da = f.read(cap)
        with open(pb, "rb") as f:
            db_data = f.read(cap)
    except OSError as exc:
        return jsonify({"ok": False, "error": "读取产物失败: %s" % exc}), 500

    # 缺省参数交给 compare_cutters 自适应（小产物用 4MB 平均块会被整块吞掉）
    rep = fastcdc.compare_cutters(
        da, db_data,
        min_size=int(payload["min_size"]) if payload.get("min_size") else None,
        avg_size=int(payload["avg_size"]) if payload.get("avg_size") else None,
        max_size=int(payload["max_size"]) if payload.get("max_size") else None)
    rep["ok"] = True
    rep["record_ids"] = rec_ids
    return jsonify(rep)


@api_bp.route("/dedup/estimate", methods=["POST"])
@login_required
def api_dedup_estimate():
    """测算「固定分块 vs FastCDC 可变长分块」在两版产物间的可复用率。

    请求：``{"task_id": N}``（自动取该任务最近两条成功记录）
    或 ``{"record_ids": [a, b]}``（显式指定，a 为基线）。
    只读取 BACKUP_ROOT 内的产物，单文件扫描上限 128MB。
    """
    import config
    from core import db as _db
    from core import fastcdc

    payload = request.get_json(silent=True) or {}
    rec_ids = payload.get("record_ids") or []
    task_id = payload.get("task_id")

    def _rooted(p):
        try:
            root = os.path.realpath(getattr(config, "BACKUP_ROOT", "") or "/")
            return os.path.realpath(p).startswith(root)
        except OSError:
            return False

    def _path_of(rid):
        row = _db.query_one("SELECT id, backup_path, size_bytes, task_id, "
                            "status FROM backup_records WHERE id=?", (rid,))
        if not row or not row.get("backup_path"):
            return None, "记录 %s 无产物路径" % rid
        p = row["backup_path"]
        if not os.path.isfile(p):
            return None, "记录 %s 的产物已不存在（可能已清理）" % rid
        if not _rooted(p):
            return None, "记录 %s 的产物不在备份根目录内，拒绝读取" % rid
        return p, None

    if not rec_ids:
        if not task_id:
            return jsonify({"ok": False, "error": "task_id 或 record_ids 必填"}), 400
        rows = _db.query(
            "SELECT id FROM backup_records WHERE task_id=? AND status='success' "
            "AND size_bytes>0 AND backup_path IS NOT NULL "
            "ORDER BY id DESC LIMIT 2", (int(task_id),))
        rec_ids = [r["id"] for r in rows]
        if len(rec_ids) < 2:
            return jsonify({"ok": False,
                            "error": "该任务成功记录不足 2 条，无法测算"}), 400

    if len(rec_ids) != 2:
        return jsonify({"ok": False, "error": "record_ids 需要正好 2 个"}), 400

    pa, err_a = _path_of(rec_ids[0])
    if err_a:
        return jsonify({"ok": False, "error": err_a}), 400
    pb, err_b = _path_of(rec_ids[1])
    if err_b:
        return jsonify({"ok": False, "error": err_b}), 400

    try:
        rep = fastcdc.estimate_reuse(pa, pb,
                                     max_bytes=int(payload.get("max_bytes") or
                                                   128 * 1024 * 1024))
    except OSError as exc:
        return jsonify({"ok": False, "error": "读取产物失败: %s" % exc}), 500

    rep["ok"] = True
    rep["record_ids"] = [rec_ids[0], rec_ids[1]]
    rep["note"] = ("cdc_reuse 高于 fixed_reuse 说明可变长分块跨版本更稳；"
                   "压缩产物（.gz/.zst）因字节整体移位，固定块命中率通常极低")
    return jsonify(rep)
