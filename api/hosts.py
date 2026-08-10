# -*- coding: utf-8 -*-
"""SSH 主机纳管 API：增删改查 + 连接测试。"""
from flask import request, jsonify

from auth import login_required
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
    hid = ssh_hosts.create_host(data)
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
    if not ssh_hosts.get_host(host_id):
        return jsonify({"error": "主机不存在"}), 404
    ssh_hosts.delete_host(host_id)
    return jsonify({"ok": True})


@api_bp.route("/hosts/<int:host_id>/test", methods=["POST"])
@login_required
def test_host(host_id):
    if not ssh_hosts.get_host(host_id):
        return jsonify({"error": "主机不存在"}), 404
    return jsonify(ssh_hosts.test_connection(host_id))
