# -*- coding: utf-8 -*-
"""Windows 客户端目录防护 API（防删除 / 防勒索，零安装）。

- ``GET  /api/win-protect/hosts``            已纳管的 Windows 主机
- ``GET  /api/win-protect/rules``            防护规则
- ``POST /api/win-protect/rules``            新增规则（可同时下发）
- ``DELETE /api/win-protect/rules/<id>``     删除规则（不自动解除，需显式解除）
- ``POST /api/win-protect/apply``            下发防护（body: host_id/paths/mode）
- ``POST /api/win-protect/remove``           解除防护
- ``POST /api/win-protect/check``            核查主机侧真实状态
"""
from __future__ import annotations

from flask import jsonify, request

from auth import login_required
from core import win_protect as wp
from core import ssh_hosts
from . import api_bp, contract

_PARAM = "AIDBM-1001"
_NOT_FOUND = "AIDBM-1004"
_EXEC = "AIDBM-5001"


def _body() -> dict:
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def _paths(b: dict) -> list[str]:
    raw = b.get("paths")
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw or "").splitlines() if x.strip()]


def _call(fn, *a, **kw):
    try:
        return jsonify({"ok": True, "data": fn(*a, **kw)})
    except wp.WinProtectError as e:
        return contract.error_response(_PARAM, str(e))
    except Exception as e:
        return contract.error_response(_EXEC, "执行失败: %s" % str(e)[:300])


@api_bp.route("/win-protect/hosts", methods=["GET"])
@login_required
def wp_hosts():
    try:
        rows = ssh_hosts.list_hosts()
    except Exception as e:
        return contract.error_response(_EXEC, "读取主机失败: %s" % str(e)[:200])
    items = [{"id": h.get("id"), "name": h.get("name"),
              "hostname": h.get("hostname")}
             for h in rows if (h.get("os_type") or "").lower() == "windows"]
    return jsonify({"ok": True, "items": items, "total": len(items)})


@api_bp.route("/win-protect/rules", methods=["GET"])
@login_required
def wp_rules():
    return jsonify({"ok": True, "items": wp.list_rules(),
                    "total": len(wp.list_rules())})


@api_bp.route("/win-protect/rules", methods=["POST"])
@login_required
def wp_add_rule():
    b = _body()
    if not b.get("host_id"):
        return contract.error_response(_PARAM, "缺少必填项: host_id")
    ps = _paths(b)
    if not ps:
        return contract.error_response(_PARAM, "缺少必填项: paths")
    try:
        ids = [wp.add_rule(int(b["host_id"]), p, b.get("mode") or "cfa",
                           b.get("allowed_apps") or []) for p in ps]
    except wp.WinProtectError as e:
        return contract.error_response(_PARAM, str(e))
    except Exception as e:
        return contract.error_response(_EXEC, "保存规则失败: %s" % str(e)[:300])
    out = {"ids": ids}
    if b.get("apply_now"):
        res = _call(wp.apply, int(b["host_id"]), ps, b.get("mode") or "cfa",
                    b.get("allowed_apps") or [])
        if res.status_code != 200:
            return res
        out["apply"] = res.get_json().get("data")
    return jsonify({"ok": True, "data": out}), 201


@api_bp.route("/win-protect/rules/<int:rid>", methods=["DELETE"])
@login_required
def wp_del_rule(rid):
    try:
        wp.delete_rule(rid)
    except Exception as e:
        return contract.error_response(_EXEC, "删除失败: %s" % str(e)[:200])
    return jsonify({"ok": True, "message": "规则已删除（主机侧防护需单独解除）"})


@api_bp.route("/win-protect/apply", methods=["POST"])
@login_required
def wp_apply():
    b = _body()
    if not b.get("host_id"):
        return contract.error_response(_PARAM, "缺少必填项: host_id")
    return _call(wp.apply, int(b["host_id"]), _paths(b),
                 b.get("mode") or "cfa", b.get("allowed_apps") or [])


@api_bp.route("/win-protect/remove", methods=["POST"])
@login_required
def wp_remove():
    b = _body()
    if not b.get("host_id"):
        return contract.error_response(_PARAM, "缺少必填项: host_id")
    return _call(wp.remove, int(b["host_id"]), _paths(b), b.get("mode") or "cfa")


@api_bp.route("/win-protect/check", methods=["POST"])
@login_required
def wp_check():
    b = _body()
    if not b.get("host_id"):
        return contract.error_response(_PARAM, "缺少必填项: host_id")
    return _call(wp.check, int(b["host_id"]), _paths(b))
