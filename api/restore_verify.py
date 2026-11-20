# -*- coding: utf-8 -*-
"""恢复校验策略与恢复测试报告 API。"""
from flask import request, jsonify

from . import api_bp, contract
from core import models
from core.restore_verify import run_restore_verify_policy
from auth import login_required, permission_required


@api_bp.route("/restore-verify-policies", methods=["GET"])
@login_required
def list_policies():
    """列出恢复校验策略。"""
    task_id = request.args.get("task_id", type=int)
    policies = models.list_restore_verify_policies(task_id=task_id)
    return jsonify({"success": True, "data": policies})


@api_bp.route("/restore-verify-policies", methods=["POST"])
@permission_required("restore.verify.manage")
def create_policy():
    """创建恢复校验策略。"""
    data = request.get_json(force=True, silent=True) or {}
    required = ["task_id"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"success": False, "message": f"缺少必填字段: {missing}"}), 400
    policy_id = models.create_restore_verify_policy(data)
    return jsonify({"success": True, "data": {"id": policy_id}}), 201


@api_bp.route("/restore-verify-policies/<int:policy_id>", methods=["GET"])
@login_required
def get_policy(policy_id: int):
    """获取单个恢复校验策略。"""
    policy = models.get_restore_verify_policy(policy_id)
    if not policy:
        return jsonify({"success": False, "message": "策略不存在"}), 404
    return jsonify({"success": True, "data": policy})


@api_bp.route("/restore-verify-policies/<int:policy_id>", methods=["PUT"])
@permission_required("restore.verify.manage")
def update_policy(policy_id: int):
    """更新恢复校验策略。"""
    data = request.get_json(force=True, silent=True) or {}
    ok = models.update_restore_verify_policy(policy_id, data)
    if not ok:
        return jsonify({"success": False, "message": "没有可更新内容"}), 400
    return jsonify({"success": True, "data": {"id": policy_id}})


@api_bp.route("/restore-verify-policies/<int:policy_id>", methods=["DELETE"])
@permission_required("restore.verify.manage")
def delete_policy(policy_id: int):
    """删除恢复校验策略及其测试报告。"""
    models.delete_restore_verify_policy(policy_id)
    return jsonify({"success": True, "message": "已删除"})


@api_bp.route("/restore-verify-policies/<int:policy_id>/test", methods=["POST"])
@permission_required("restore.verify.manage")
def run_policy_test(policy_id: int):
    """立即执行一次恢复校验。"""
    if not models.get_restore_verify_policy(policy_id):
        return jsonify({"success": False, "message": "策略不存在"}), 404
    data = request.get_json(silent=True) or {}
    clone_id = data.get("clone_request_id")
    try:
        clone_id = int(clone_id) if clone_id is not None else None
        result = run_restore_verify_policy(policy_id, clone_request_id=clone_id)
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 409 if "正在执行" in str(exc) else 400
    return jsonify({"success": result["success"], "data": result})


@api_bp.route("/restore-verify-policies/<int:policy_id>/reports", methods=["GET"])
@login_required
def list_policy_reports(policy_id: int):
    """获取某策略的测试报告。"""
    reports = models.list_restore_test_reports(policy_id=policy_id)
    return jsonify({"success": True, "data": reports})


@api_bp.route("/restore-test-reports", methods=["GET"])
@login_required
def list_reports():
    """列出恢复测试报告，可选 task_id 过滤。"""
    task_id = request.args.get("task_id", type=int)
    default_size = 100 if request.path.startswith(contract.V1_PREFIX) else 200
    _page, size, offset = contract.pagination_args(default_size=default_size,
                                                   max_size=500)
    reports = models.list_restore_test_reports(task_id=task_id, limit=size + offset)
    page_rows = reports[offset:offset + size]
    return contract.list_response(page_rows, total=len(reports),
                                  legacy={"success": True, "data": reports})


@api_bp.route("/restore-test-reports/<int:report_id>/clean", methods=["POST"])
@permission_required("restore.verify.manage")
def clean_report(report_id: int):
    """标记测试报告为已清理。"""
    models.update_restore_test_report(report_id, {"cleaned": 1})
    return jsonify({"success": True, "message": "已标记清理"})


@api_bp.route("/restore-verify-stats", methods=["GET"])
@login_required
def stats():
    """恢复校验仪表盘 KPI。"""
    return jsonify({"success": True, "data": models.get_restore_verify_stats()})
