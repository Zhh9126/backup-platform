# -*- coding: utf-8 -*-
"""源端资源保护 / 离线负载周期学习 API。

配置写入需要 system.settings；把推荐窗口应用到任务需要 backup.task.manage。
采样与窗口查询只读，登录即可。
"""
from flask import request, jsonify

from auth import login_required, permission_required
from . import api_bp
from core import models, source_guard


@api_bp.route("/source-guard/config", methods=["GET"])
@login_required
def source_guard_config():
    return jsonify(source_guard.get_config())


@api_bp.route("/source-guard/config", methods=["POST"])
@login_required
@permission_required("system.settings")
def source_guard_config_save():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(source_guard.set_config(data))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/source-guard/sample/<int:task_id>", methods=["POST"])
@login_required
def source_guard_sample(task_id):
    """立即采样一次源端 CPU 并返回本任务的三层决策结论。"""
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    sample = source_guard.sample_cpu(task)
    verdict = source_guard.evaluate(task, force=True, sample=sample)
    return jsonify({"sample": sample, "verdict": verdict})


@api_bp.route("/source-guard/window", methods=["GET"])
@login_required
def source_guard_window():
    """按历史负载样本推荐最佳备份小时（纯离线统计）。"""
    raw = request.args.get("task_id")
    try:
        task_id = int(raw) if raw else None
    except (TypeError, ValueError):
        task_id = None
    try:
        days = int(request.args.get("days") or 30)
    except ValueError:
        days = 30
    return jsonify(source_guard.recommend_window(
        task_id=task_id, host=request.args.get("host"), days=days))


@api_bp.route("/source-guard/window/<int:task_id>/apply", methods=["POST"])
@login_required
@permission_required("backup.task.manage")
def source_guard_window_apply(task_id):
    """把推荐窗口写入任务 cron 并重载调度；样本不足如实拒绝。"""
    data = request.get_json(force=True, silent=True) or {}
    res = source_guard.apply_window(task_id, cron=data.get("cron"))
    return jsonify(res), (200 if res.get("ok") else 400)
