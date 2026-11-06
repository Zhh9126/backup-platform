# -*- coding: utf-8 -*-
"""SSH 主机纳管 API：增删改查 + 连接测试。"""
from flask import request, jsonify

from auth import login_required, verify_code_gate
from core import ssh_hosts
from . import api_bp


@api_bp.route("/hosts", methods=["GET"])
@login_required
def list_hosts():
    return jsonify(ssh_hosts.list_hosts(include_secret=False))


@api_bp.route("/hosts", methods=["POST"])
@login_required
def create_host():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "主机名称必填"}), 400
    if not data.get("hostname"):
        return jsonify({"error": "主机地址必填"}), 400
    try:
        hid = ssh_hosts.create_host(data)
    except ValueError as e:
        # 重复纳管：精确到「连接 + 数据库类型」的提示（FIX-2026-001 P2）
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": hid, "ok": True}), 201


@api_bp.route("/hosts/<int:host_id>", methods=["GET"])
@login_required
def get_host(host_id):
    h = ssh_hosts.get_host(host_id, include_secret=False)
    if not h:
        return jsonify({"error": "主机不存在"}), 404
    return jsonify(h)


@api_bp.route("/hosts/<int:host_id>", methods=["PUT"])
@login_required
def update_host(host_id):
    data = request.get_json(force=True, silent=True) or {}
    if not ssh_hosts.get_host(host_id):
        return jsonify({"error": "主机不存在"}), 404
    ssh_hosts.update_host(host_id, data)
    return jsonify({"ok": True})


@api_bp.route("/hosts/<int:host_id>", methods=["DELETE"])
@login_required
def delete_host(host_id):
    # P0-2：移除纳管主机属高危操作，开关开启时需验证码
    gate = verify_code_gate(request.get_json(silent=True) or {})
    if gate:
        return jsonify(gate[0]), gate[1]
    if not ssh_hosts.get_host(host_id):
        return jsonify({"error": "主机不存在"}), 404
    ssh_hosts.delete_host(host_id)
    return jsonify({"ok": True})


@api_bp.route("/hosts/<int:host_id>/protect", methods=["POST"])
@login_required
def protect_host(host_id):
    """资源受保护标记（P0-2）：受保护主机禁止作为恢复/复制/演练目标。"""
    data = request.get_json(silent=True) or {}
    if not ssh_hosts.set_protected(host_id, bool(data.get("protected")),
                                   data.get("note") or ""):
        return jsonify({"error": "主机不存在"}), 404
    return jsonify({"ok": True, "protected": bool(data.get("protected"))})


@api_bp.route("/hosts/<int:host_id>/test", methods=["POST"])
@login_required
def test_host(host_id):
    if not ssh_hosts.get_host(host_id):
        return jsonify({"error": "主机不存在"}), 404
    return jsonify(ssh_hosts.test_connection(host_id))

@api_bp.route("/hosts/<int:host_id>/list-shares", methods=["POST"])
@login_required
def list_host_shares(host_id):
    """枚举 NAS（群晖 DSM/通用 Linux）的共享目录。

    纯 SSH 只读枚举（零 Agent 零依赖）：遍历 /volume* 一级目录，
    过滤 @ 开头系统目录与隐藏目录/配额文件，返回可直接用作文件备份源路径的清单。
    """
    import re as _re
    h = ssh_hosts.get_host(host_id, include_secret=True)
    if not h:
        return jsonify({"error": f"主机不存在: {host_id}"}), 404
    host_key = f"{h.get('username')}@{h.get('hostname')}:{h.get('port') or 22}"
    try:
        from core.engines.file import _get_ssh_client
        client = _get_ssh_client(host_key,
                                 password=h.get("password") or None,
                                 private_key=h.get("private_key") or None)
        # /volume1,/volume2,... DSM 共享目录所在卷；@sync-conflict 等由前缀过滤
        cmd = ("for v in /volume*/ ; do [ -d \"$v\" ] || continue; "
               "for d in \"$v\"*/ ; do [ -d \"$d\" ] && echo \"$d\"; done; done")
        _, stdout, _ = client.exec_command(cmd, timeout=20)
        out = stdout.read().decode("utf-8", "replace")
        shares = []
        for line in out.splitlines():
            p = line.strip().rstrip("/")
            if not p:
                continue
            name = p.rsplit("/", 1)[-1]
            # 过滤 DSM 系统目录与隐藏/配额文件
            if name.startswith("@") or name.startswith(".") or name.startswith("aquota"):
                continue
            shares.append(p)
        shares.sort()
        return jsonify({"ok": True, "host": h.get("name"), "shares": shares,
                        "count": len(shares),
                        "note": "已过滤 @ 开头的 DSM 系统目录与隐藏目录；可按需删减后作为备份源"})
    except Exception as e:
        return jsonify({"error": f"枚举共享目录失败: {str(e) or type(e).__name__}"}), 400
