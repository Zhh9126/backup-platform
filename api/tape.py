# -*- coding: utf-8 -*-
"""磁带库 / 可移动介质管理 API。

- GET  /api/tape/devices               设备发现（lsscsi / sysfs / udev）+ 容器接入建议
- GET  /api/tape/summary               介质与写入目录统计
- GET  /api/tape/status                各磁带目标连接状态（原有）
- GET  /api/tape/files                 带内成员清单（原有）
- POST /api/tape/restore               带内文件回迁（原有）
- POST /api/tape/<id>/test             写入前置检查（原有）
- GET  /api/tape/library/<id>          mtx status：驱动器/槽位/条码 + 台账同步
- POST /api/tape/library/<id>/load    装载/卸载/出库转移（机械手，显式操作）
- GET  /api/tape/media                 介质台账
- POST /api/tape/media/<id>/action    出库/归库/退役/写保护/WORM 标记
- GET  /api/tape/jobs                  写入目录（含 checksum / 校验状态）
- POST /api/tape/verify                显式写后校验（回读比对 sha256）

真实硬件不可用时如实返回错误，不仿真设备、不伪造机械手动作。
"""
import json
from flask import request, jsonify

import core.db as db
from auth import login_required, permission_required
from core import tape_library as tl
from core.storage_backends import get_backend
from . import api_bp


def _get_tape_target(target_id: int) -> dict:
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (target_id,))
    if not row or row["type"] != "tape":
        return None
    return dict(row)


def _extra(t: dict) -> dict:
    raw = t.get("extra_options") or t.get("config_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _changer(t: dict) -> tl.TapeChanger:
    dev = (_extra(t).get("changer_device") or "").strip()
    if not dev:
        raise ValueError("该磁带目标未配置 changer_device（/dev/sgN 或 /dev/tape/by-id/...）")
    return tl.TapeChanger(dev)


def _operator() -> str:
    from auth import _current_user_dict
    u = _current_user_dict()
    return u.get("username") or u.get("display_name") or "unknown"


# ------------------------- 设备发现 / 接入检查 -------------------------

@api_bp.route("/tape/devices", methods=["GET"])
@login_required
def api_tape_devices():
    return jsonify(tl.discover())


@api_bp.route("/tape/summary", methods=["GET"])
@login_required
def api_tape_summary():
    target_id = request.args.get("target_id", type=int)
    return jsonify(tl.media_summary(target_id))


# ------------------------- 原有状态 / 清单 / 回迁 -------------------------

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
                        "used_bytes": backend.get_used_space(),
                        "changer_device": _extra(t).get("changer_device") or ""})
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
    return jsonify({"target": t["name"], "files": backend.list_files(),
                    "jobs": tl.job_list(target_id, 100)})


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


# ------------------------- 带库机械手 -------------------------

@api_bp.route("/tape/library/<int:target_id>", methods=["GET"])
@login_required
def api_tape_library(target_id):
    t = _get_tape_target(target_id)
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    try:
        st = _changer(t).status()
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e), "changer_device": _extra(t).get("changer_device") or ""}), 400
    if not st.get("ok"):
        return jsonify({"error": st.get("err") or "mtx status 失败",
                        "out": st.get("out")}), 400
    sync = tl.sync_inventory(target_id, st["library"])
    return jsonify({"ok": True, "library": st["library"],
                    "media_sync": sync, "media": tl.media_list(target_id)})


@api_bp.route("/tape/library/<int:target_id>/load", methods=["POST"])
@login_required
@permission_required("backup.storage.manage")
def api_tape_changer_action(target_id):
    """机械手显式操作：{op: load|unload|inventory|transfer_to_ie|transfer_from_ie,
    slot, drive, ie_slot}。任何操作都记审计日志。"""
    t = _get_tape_target(target_id)
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    data = request.get_json(force=True, silent=True) or {}
    op = (data.get("op") or "").strip()
    try:
        ch = _changer(t)
        slot = int(data.get("slot") or 0)
        drive = int(data.get("drive") or 0)
        if op == "load":
            res = ch.load(slot, drive)
        elif op == "unload":
            res = ch.unload(slot, drive)
        elif op == "inventory":
            res = ch.inventory()
        elif op == "transfer_to_ie":
            res = ch.transfer_to_ie(slot, int(data.get("ie_slot") or 0))
        elif op == "transfer_from_ie":
            res = ch.transfer_from_ie(int(data.get("ie_slot") or 0), slot)
        else:
            return jsonify({"error": "op 必须是 load/unload/inventory/transfer_to_ie/transfer_from_ie"}), 400
    except (ValueError, RuntimeError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    db.add_log("WARNING", "tape_library",
               f"机械手操作 target={target_id} op={op} slot={data.get('slot')} "
               f"drive={data.get('drive')} ok={res.get('ok')} by={_operator()}")
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("err") or "机械手命令失败",
                        "out": res.get("out")}), 400
    return jsonify({"ok": True, "out": res.get("out")})


# ------------------------- 介质台账 / 生命周期 -------------------------

@api_bp.route("/tape/media", methods=["GET"])
@login_required
def api_tape_media():
    return jsonify({"items": tl.media_list(
        request.args.get("target_id", type=int),
        request.args.get("state"))})


@api_bp.route("/tape/media/<int:media_id>/action", methods=["POST"])
@login_required
@permission_required("backup.storage.manage")
def api_tape_media_action(media_id):
    """介质状态机：offsite(出库离场保管) / return(归库) / retire(退役) /
    protect / unprotect(写保护) / worm(WORM 标记)。"""
    data = request.get_json(force=True, silent=True) or {}
    action = (data.get("action") or "").strip()
    note = (data.get("note") or "").strip()
    maps = {
        "offsite": {"state": "offsite", "offsite_since": tl.now_iso()},
        "return": {"state": "in_library", "offsite_since": None},
        "retire": {"state": "retired"},
        "protect": {"write_protected": 1},
        "unprotect": {"write_protected": 0},
        "worm": {"worm": 1, "write_protected": 1},
    }
    if action not in maps:
        return jsonify({"error": "不支持的介质操作: " + action}), 400
    if action == "offsite" and not note:
        return jsonify({"error": "出库必须填写去向/经办人（审计要求）"}), 400
    fields = dict(maps[action])
    if note:
        fields["note"] = note
    try:
        row = tl.media_update(media_id, **fields)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    db.add_log("WARNING", "tape_library",
               f"介质 #{media_id} 条码 {row.get('barcode')} 操作={action} "
               f"note={note} by={_operator()}")
    return jsonify({"ok": True, "media": row})


# ------------------------- 写入目录 / 写后校验 -------------------------

@api_bp.route("/tape/jobs", methods=["GET"])
@login_required
def api_tape_jobs():
    return jsonify({"items": tl.job_list(
        request.args.get("target_id", type=int),
        request.args.get("limit", 100))})


@api_bp.route("/tape/verify", methods=["POST"])
@login_required
@permission_required("backup.storage.manage")
def api_tape_verify():
    """显式写后校验：回读带内对象并与写入目录里的 sha256 比对。"""
    data = request.get_json(force=True, silent=True) or {}
    t = _get_tape_target(int(data.get("target_id") or 0))
    if not t:
        return jsonify({"error": "磁带目标不存在"}), 404
    key = (data.get("object_key") or "").strip()
    if not key:
        return jsonify({"error": "object_key 必填"}), 400
    backend = get_backend("tape", t)
    res = backend.verify_object(key, expected_checksum=data.get("checksum"))
    db.add_log("WARNING" if not res.get("ok") else "INFO", "tape_library",
               f"写后校验 target={t['id']} object_key={key} ok={res.get('ok')} "
               f"by={_operator()}")
    return jsonify(res), (200 if res.get("ok") else 400)
