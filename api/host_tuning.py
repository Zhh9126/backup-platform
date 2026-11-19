# -*- coding: utf-8 -*-
"""备份服务器内核/IO 调优诊断接口（只读）。

GET /api/host-tuning            诊断报告（sysctl / IO 调度器 / io_uring / 挂载 / 磁盘）
GET /api/host-tuning/recommend  需调整项的可执行命令清单（平台不代执行）

权限沿用 storage 模块映射（backup.storage.manage）。
"""
from flask import jsonify

from auth import login_required
from core import host_tuning
from . import api_bp


@api_bp.route("/host-tuning", methods=["GET"])
@login_required
def host_tuning_report():
    return jsonify(host_tuning.inspect())


@api_bp.route("/host-tuning/recommend", methods=["GET"])
@login_required
def host_tuning_recommend():
    """列出需要调整的项及其命令（供运维在维护窗口执行）。"""
    rep = host_tuning.inspect()
    cmds = [{"item": c["item"], "actual": c.get("actual"),
             "recommend": c.get("recommend"), "cmd": c.get("apply_cmd")}
            for c in rep["checks"]
            if c.get("ok") is False and c.get("apply_cmd")]
    return jsonify({"count": len(cmds), "commands": cmds,
                    "note": "平台只做只读检查，不会自动修改系统参数；"
                            "请在维护窗口按需执行并评估对同机业务的影响"})
