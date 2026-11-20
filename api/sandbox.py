# -*- coding: utf-8 -*-
"""净室演练沙箱能力探测（只读）。

GET /api/sandbox/capability  宿主是否具备 netns / KVM / NBD / iSCSI，
                             以及平台当前演练实际处于哪一级隔离。
"""
from flask import jsonify

from auth import login_required
from core import sandbox_probe
from . import api_bp


@api_bp.route("/sandbox/capability", methods=["GET"])
@login_required
def sandbox_capability():
    return jsonify(sandbox_probe.probe_sandbox_readiness())
