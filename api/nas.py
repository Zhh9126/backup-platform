# -*- coding: utf-8 -*-
"""NAS 阵列快照差量适配：厂商清单 + 连通/认证探测（只探测）。

- GET  /api/nas/vendors   支持的阵列与所需 API
- POST /api/nas/probe     探测地址/端口/账号（不读业务数据）
变更清单（snapshot diff）需真机对接，接口见 core.nas_diff.list_snapshot_changes。
"""
from flask import jsonify, request

from auth import login_required
from core import nas_diff
from . import api_bp


@api_bp.route("/nas/vendors", methods=["GET"])
@login_required
def nas_vendors():
    return jsonify({"ok": True, "vendors": nas_diff.vendors(),
                    "diff_implemented": False})


@api_bp.route("/nas/probe", methods=["POST"])
@login_required
def nas_probe():
    p = request.get_json(silent=True) or {}
    res = nas_diff.probe(
        p.get("vendor"), p.get("host"), p.get("port"),
        username=p.get("username") or "", password=p.get("password") or "",
        verify_tls=bool(p.get("verify_tls")), timeout=int(p.get("timeout") or 8))
    return jsonify(res), (200 if res.get("ok") else 400)
