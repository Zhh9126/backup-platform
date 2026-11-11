# -*- coding: utf-8 -*-
"""无备份目录扫描恢复 / Oracle 实例发现 API。

scan 为只读探测（EXTRA_EXEMPT 豁免），pull 会拉回数据需 backup.task.manage；
oracle-inventory 为 GET，仅需登录。
"""
import json

from flask import request, jsonify

from auth import login_required
from . import api_bp
from core import models, oracle_discover, scan_fs


def _task(data: dict) -> dict:
    """支持按 task_id 复用任务凭据，或直接传主机 + SSH 凭据。"""
    import core.db as db
    tid = data.get("task_id")
    if tid:
        try:
            task = models.get_task(int(tid), include_secret=True)
        except (TypeError, ValueError):
            task = None
        if not task:
            raise ValueError("任务不存在")
        return task
    host = str(data.get("host") or "").strip()
    if not host:
        raise ValueError("需要 task_id 或 host")
    cred = {"host": host, "port": int(data.get("ssh_port") or 22),
            "username": str(data.get("ssh_user") or "root")}
    if data.get("ssh_password"):
        cred["password"] = db.encrypt_secret(str(data["ssh_password"]))
    return {"id": 0, "host": host,
            "extra_options": json.dumps({"ssh_cred": cred})}


@api_bp.route("/scan-fs/scan", methods=["POST"])
@login_required
def scan_fs_scan():
    """无备份目录时只读扫描远端文件系统，返回可恢复对象清单。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        task = _task(data)
        return jsonify(scan_fs.scan_host(
            task, roots=data.get("roots"),
            max_files=int(data.get("max_files") or 5000)))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/scan-fs/pull", methods=["POST"])
@login_required
def scan_fs_pull():
    """按扫描清单把选定对象经 SFTP 拉回平台暂存目录。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        task = _task(data)
        return jsonify(scan_fs.pull_objects(
            task, paths=data.get("paths") or [],
            dest_dir=str(data.get("dest_dir") or ""),
            max_bytes=int(data.get("max_bytes") or 2 * 1024 ** 3)))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/tasks/<int:task_id>/oracle-inventory", methods=["GET"])
@login_required
def oracle_inventory(task_id):
    """发现 Oracle 实例 / ORACLE_HOME / PDB 清单（11g 如实标注不支持 PDB）。"""
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    try:
        return jsonify(oracle_discover.discover(task))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
