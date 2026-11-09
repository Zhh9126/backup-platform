# -*- coding: utf-8 -*-
"""异地容灾出站拉取 REST API（云侧推对象存储 + 平台出站拉取）。

包格式与摆渡收件箱完全一致，详见 ``core/remote_pull.py`` 模块说明。
"""
from flask import jsonify, request

from auth import login_required
from core import remote_pull as rp
from . import api_bp


def _payload():
    return request.get_json(force=True, silent=True) or {}


def _err(e, status=400):
    return jsonify({"error": str(e).strip() or type(e).__name__}), status


def _source_or_404(sid):
    s = rp.get_source(sid)
    if not s:
        return None
    return s


@api_bp.route("/remote-pull/sources", methods=["GET"])
@login_required
def rp_list_sources():
    return jsonify(rp.list_sources(include_secret=False))


@api_bp.route("/remote-pull/sources", methods=["POST"])
@login_required
def rp_create_source():
    try:
        sid = rp.create_source(_payload())
    except rp.RemotePullError as e:
        return _err(e)
    return jsonify({"id": sid, "ok": True}), 201


@api_bp.route("/remote-pull/sources/<int:sid>", methods=["GET"])
@login_required
def rp_get_source(sid):
    s = _source_or_404(sid)
    return jsonify(s) if s else (jsonify({"error": "来源不存在"}), 404)


@api_bp.route("/remote-pull/sources/<int:sid>", methods=["PUT"])
@login_required
def rp_update_source(sid):
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    try:
        rp.update_source(sid, _payload())
    except rp.RemotePullError as e:
        return _err(e)
    return jsonify({"ok": True})


@api_bp.route("/remote-pull/sources/<int:sid>", methods=["DELETE"])
@login_required
def rp_delete_source(sid):
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    rp.delete_source(sid)
    return jsonify({"ok": True})


@api_bp.route("/remote-pull/test", methods=["POST"])
@login_required
def rp_test_unsaved():
    """未保存先测：body 是一份来源配置（secret_key 明文，仅本次请求）。"""
    data = _payload()
    if data.get("id"):
        return jsonify(rp.test_source(int(data["id"])))
    out = rp.test_source(dict(data))
    return (jsonify(out), 200) if out.get("ok") else (jsonify(out), 400)


@api_bp.route("/remote-pull/sources/<int:sid>/test", methods=["POST"])
@login_required
def rp_test_source(sid):
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    out = rp.test_source(sid)
    return (jsonify(out), 200) if out.get("ok") else (jsonify(out), 400)


@api_bp.route("/remote-pull/sources/<int:sid>/preview", methods=["GET"])
@login_required
def rp_preview(sid):
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    try:
        return jsonify(rp.preview(sid, limit=request.args.get("limit") or 100))
    except Exception as e:
        return _err(e)


@api_bp.route("/remote-pull/sources/<int:sid>/pull", methods=["POST"])
@login_required
def rp_pull(sid):
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    data = _payload()
    try:
        out = rp.pull_source(sid, dry_run=bool(data.get("dry_run")),
                             limit=int(data.get("limit") or 50))
        return jsonify({"ok": True, **out})
    except Exception as e:
        return _err(e)


@api_bp.route("/remote-pull/sources/<int:sid>/push/<int:record_id>", methods=["POST"])
@login_required
def rp_push_record(sid, record_id):
    """云侧推送参考实现：把本地备份记录打成摆渡包推到远端对象存储。"""
    if not _source_or_404(sid):
        return jsonify({"error": "来源不存在"}), 404
    try:
        return jsonify({"ok": True, **rp.push_record(sid, record_id)}), 201
    except rp.RemotePullError as e:
        return _err(e)
    except Exception as e:
        return _err(e)


@api_bp.route("/remote-pull/logs", methods=["GET"])
@login_required
def rp_logs():
    sid = request.args.get("source_id")
    return jsonify(rp.list_logs(int(sid) if sid else None,
                                limit=request.args.get("limit") or 100))


@api_bp.route("/remote-pull/scenarios", methods=["GET"])
@login_required
def rp_scenarios():
    return jsonify({"ok": True, **rp.scenario_note()})
