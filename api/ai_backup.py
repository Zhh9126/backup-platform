# -*- coding: utf-8 -*-
"""AI 工作负载保护计划 API（跨层一致性组）。"""
import json
import threading

from flask import request, jsonify
from auth import login_required
from core import ai_protection, db
from . import api_bp

# 进程内并发护栏：同一计划不允许并发运行（逐成员驱动备份会占用很久）
_RUNNING = set()
_RUN_LOCK = threading.Lock()


def _decorate(plan: dict) -> dict:
    plan = dict(plan)
    with _RUN_LOCK:
        plan["running"] = plan.get("id") in _RUNNING
    return plan


def _reload_sched():
    """计划增删改后重载调度（cron 周期执行随之注册/注销）。"""
    try:
        from core import scheduler as _sched
        _sched.reload_scheduler()
    except Exception as e:  # noqa: BLE001
        db.add_log("warning", "ai_plan", f"保护计划调度重载失败: {e}")


@api_bp.route("/ai-plans", methods=["GET"])
@login_required
def list_ai_plans():
    return jsonify({"plans": [_decorate(p) for p in ai_protection.list_plans()],
                    "layers": [{"key": k, "name": v}
                               for k, v in ai_protection.LAYERS]})


@api_bp.route("/ai-plans/<int:plan_id>", methods=["GET"])
@login_required
def get_ai_plan(plan_id):
    plan = ai_protection.get_plan(plan_id)
    if not plan:
        return jsonify({"error": f"保护计划不存在: {plan_id}"}), 404
    return jsonify(_decorate(plan))


@api_bp.route("/ai-plans", methods=["POST"])
@login_required
def create_ai_plan():
    from core import license as license_mod
    data = request.get_json(force=True, silent=True) or {}
    try:
        license_mod.check_quota("ai_plan")
        pid = ai_protection.create_plan(data)
        _reload_sched()
        return jsonify({"ok": True, "id": pid})
    except license_mod.QuotaExceededError as qe:
        return jsonify({"error": str(qe), "quota_exceeded": True}), 403
    except ai_protection.PlanError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/ai-plans/<int:plan_id>", methods=["PUT"])
@login_required
def update_ai_plan(plan_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        ok = ai_protection.update_plan(plan_id, data)
        _reload_sched()
        return jsonify({"ok": ok})
    except ai_protection.PlanError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/ai-plans/<int:plan_id>", methods=["DELETE"])
@login_required
def delete_ai_plan(plan_id):
    with _RUN_LOCK:
        if plan_id in _RUNNING:
            return jsonify({"error": "该计划正在运行中，请等待完成后再删除"}), 409
    ok = ai_protection.delete_plan(plan_id)
    _reload_sched()
    return jsonify({"ok": ok})


@api_bp.route("/ai-plans/<int:plan_id>/run", methods=["POST"])
@login_required
def run_ai_plan(plan_id):
    """运行保护计划（异步）。

    逐成员驱动备份在本机/远端可能耗时数分钟到数十分钟，同步执行会把 HTTP
    请求挂到超时（前端表现为"点了没反应"）。这里改为后台线程执行，
    结果写回计划的 last_result，前端轮询计划列表获取组恢复点报告。
    """
    data = request.get_json(force=True, silent=True) or {}
    plan = ai_protection.get_plan(plan_id)
    if not plan:
        return jsonify({"error": f"保护计划不存在: {plan_id}"}), 404
    with _RUN_LOCK:
        if plan_id in _RUNNING:
            return jsonify({"error": "该计划正在运行中，请稍候查看组恢复点报告",
                            "running": True}), 409
        _RUNNING.add(plan_id)

    def _worker():
        try:
            ai_protection.run_plan(plan_id, operator=data.get("operator") or "")
        except Exception as e:  # noqa: BLE001 - 后台线程异常必须落日志，不能静默
            db.add_log("error", "ai_plan",
                       f"AI 保护计划 {plan_id} 运行异常: {e}")
        finally:
            with _RUN_LOCK:
                _RUNNING.discard(plan_id)

    threading.Thread(target=_worker, daemon=True,
                     name=f"ai-plan-{plan_id}").start()
    return jsonify({"ok": True, "accepted": True, "plan_id": plan_id,
                    "message": "计划已开始运行（按顺序驱动成员备份），"
                               "完成后可在列表查看组恢复点报告"}), 202
