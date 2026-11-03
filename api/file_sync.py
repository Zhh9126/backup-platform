# -*- coding: utf-8 -*-
"""文件推送式实时同步 API（P1-4）：

- GET    /api/file-sync             任务列表
- POST   /api/file-sync             新建（name/ssh_host_id/src_paths/excludes/
                                    interval_sec/keep_versions/enabled）
- PUT    /api/file-sync/<id>        更新
- DELETE /api/file-sync/<id>        删除
- POST   /api/file-sync/<id>/run    立即同步一轮
- GET    /api/file-sync/<id>/versions  版本目录列表（新→旧）
"""
from flask import jsonify, request

from auth import login_required
from core import file_sync
from . import api_bp


@api_bp.route("/file-sync", methods=["GET"])
@login_required
def api_fs_list():
    return jsonify({"ok": True, "tasks": file_sync.list_tasks()})


@api_bp.route("/file-sync", methods=["POST"])
@login_required
def api_fs_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        tid = file_sync.create_task(data)
    except file_sync.FileSyncError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": tid}), 201


@api_bp.route("/file-sync/<int:task_id>", methods=["PUT"])
@login_required
def api_fs_update(task_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        file_sync.update_task(task_id, data)
    except file_sync.FileSyncError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@api_bp.route("/file-sync/<int:task_id>", methods=["DELETE"])
@login_required
def api_fs_delete(task_id):
    if not file_sync.get_task(task_id):
        return jsonify({"error": "任务不存在"}), 404
    file_sync.delete_task(task_id)
    return jsonify({"ok": True})


@api_bp.route("/file-sync/<int:task_id>/run", methods=["POST"])
@login_required
def api_fs_run(task_id):
    try:
        result = file_sync.sync_once(task_id)
    except file_sync.FileSyncError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "同步失败: %s" % e}), 500
    return jsonify({"ok": result.get("status") != "failed", "result": result})


@api_bp.route("/file-sync/<int:task_id>/versions", methods=["GET"])
@login_required
def api_fs_versions(task_id):
    if not file_sync.get_task(task_id):
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"ok": True, "versions": file_sync.versions_of(task_id)})
