# -*- coding: utf-8 -*-
"""防勒索态势与处置 API。

诚实边界：离线环境无病毒库，检测是启发式（体积突变/熵值/结构嗅探）
+ sha256 完整性基线，不做病毒查杀；suspect 是「需人工复核」而非确诊。
"""
from flask import jsonify, request

from auth import login_required, permission_required
from core import ransomware_guard as guard
from . import api_bp


def _operator() -> str:
    from auth import _current_user_dict
    u = _current_user_dict()
    return u.get("username") or u.get("display_name") or "unknown"


@api_bp.route("/ransomware/summary", methods=["GET"])
@login_required
def ransomware_summary():
    return jsonify(guard.summary())


@api_bp.route("/ransomware/config", methods=["GET", "POST"])
@login_required
@permission_required("security.manage")
def ransomware_config():
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        return jsonify({"ok": True, "config": guard.set_config(body)})
    return jsonify({"config": guard.get_config(),
                    "defaults": guard.DEFAULTS})


@api_bp.route("/ransomware/events", methods=["GET"])
@login_required
def ransomware_events():
    return jsonify({"items": guard.events(request.args.get("limit", 50))})


@api_bp.route("/ransomware/fs-capability", methods=["GET"])
@login_required
def ransomware_fs_capability():
    return jsonify(guard.fs_immutable_capability())


@api_bp.route("/ransomware/scan/<int:record_id>", methods=["POST"])
@login_required
def ransomware_scan(record_id):
    """重扫单条记录：结论落库，suspect 按配置自动隔离。"""
    try:
        return jsonify(guard.scan_record(record_id, operator=_operator()))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"检测失败: {e}"}), 500


@api_bp.route("/ransomware/scan-recent", methods=["POST"])
@login_required
def ransomware_scan_recent():
    """补扫安全门上线前的存量备份（默认最近 100 条未检测记录）。"""
    body = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(body.get("limit") or 100)
    except (TypeError, ValueError):
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify(guard.scan_recent(limit, operator=_operator()))


@api_bp.route("/ransomware/quarantine/<int:record_id>", methods=["POST"])
@login_required
@permission_required("security.manage")
def ransomware_quarantine(record_id):
    body = request.get_json(force=True, silent=True) or {}
    reason = (body.get("reason") or "管理员手工隔离").strip()
    try:
        return jsonify({"ok": True, **guard.quarantine_record(
            record_id, reason=reason, operator=_operator())})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"隔离失败: {e}"}), 500


@api_bp.route("/ransomware/release/<int:record_id>", methods=["POST"])
@login_required
@permission_required("security.manage")
def ransomware_release(record_id):
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": True, **guard.release_quarantine(
            record_id, reason=(body.get("reason") or "").strip(),
            operator=_operator())})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"解除隔离失败: {e}"}), 500
