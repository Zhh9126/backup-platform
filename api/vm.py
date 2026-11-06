# -*- coding: utf-8 -*-
"""虚拟机备份 REST API（路由前缀 /api/vm）。

设计原则：**所有能力都如实暴露**——前端据此禁用不支持的按钮，而不是让用户
点了才发现不支持。Provider 抛 VMProviderError 一律转 400 + 可读原因。

路由清单
- GET    /api/vm/providers                 支持的虚拟化平台类型
- GET    /api/vm/hypervisors               纳管的虚拟化平台列表
- POST   /api/vm/hypervisors               新增并探活
- PUT    /api/vm/hypervisors/<id>          修改
- DELETE /api/vm/hypervisors/<id>          删除
- POST   /api/vm/hypervisors/test          连通性测试（未保存也能测）
- GET    /api/vm/hypervisors/<id>/vms      资产发现（列出该平台上的虚拟机）
- POST   /api/vm/protect                   为一台/多台虚拟机建立保护
- GET    /api/vm/protected                 受保护虚拟机列表
- GET    /api/vm/protected/<id>            详情（含恢复点、能力）
- PUT    /api/vm/protected/<id>            修改保护策略
- DELETE /api/vm/protected/<id>            取消保护
- POST   /api/vm/protected/<id>/backup     立即备份（full|incremental）
- GET    /api/vm/recovery-points           恢复点列表（可传 vm_id）
- DELETE /api/vm/recovery-points/<id>      删除恢复点（有依赖时拒绝）
- POST   /api/vm/recovery-points/<id>/restore   原位还原
- POST   /api/vm/recovery-points/<id>/clone     克隆为新 VM（隔离网络/TTL）
- POST   /api/vm/recovery-points/<id>/verify    自动恢复验证（SureBackup 式）
- GET    /api/vm/jobs                      作业列表
- GET    /api/vm/jobs/<id>                 作业详情（前端轮询）
- POST   /api/vm/jobs/<id>/destroy         销毁作业产生的目标 VM
- POST   /api/vm/jobs/reap                 TTL 到期回收
- GET    /api/vm/rpo                       RPO 达成报告（SLA 视角）
- GET    /api/vm/plan/<vm_id>              下次备份决策与 RPO 状况
"""
from flask import request, jsonify

from auth import login_required
from . import api_bp

_vmr = None


def _svc():
    global _vmr
    if _vmr is None:
        import core.vm as _m
        _vmr = _m
    return _vmr


def _err(e: Exception, code: int = 400):
    """把业务异常转成 JSON 错误（400 表示「请求/环境不支持」，500 才是平台故障）。"""
    from core.vm.types import VMProviderError
    if isinstance(e, VMProviderError):
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), code


def _json():
    return request.get_json(silent=True) or {}


# ---------------- Provider ----------------
@api_bp.route("/vm/providers", methods=["GET"])
@login_required
def api_vm_providers():
    return jsonify(_svc().list_providers())


# ---------------- 虚拟化平台纳管 ----------------
@api_bp.route("/vm/hypervisors", methods=["GET"])
@login_required
def api_vm_hypervisors():
    return jsonify(_svc().list_hypervisors())


@api_bp.route("/vm/hypervisors", methods=["POST"])
@login_required
def api_vm_hypervisor_create():
    try:
        return jsonify(_svc().create_hypervisor(_json())), 201
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/hypervisors/test", methods=["POST"])
@login_required
def api_vm_hypervisor_test():
    try:
        return jsonify(_svc().test_hypervisor(_json()))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/hypervisors/<int:hv_id>", methods=["PUT"])
@login_required
def api_vm_hypervisor_update(hv_id):
    try:
        return jsonify(_svc().update_hypervisor(hv_id, _json()))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/hypervisors/<int:hv_id>", methods=["DELETE"])
@login_required
def api_vm_hypervisor_delete(hv_id):
    try:
        return jsonify(_svc().delete_hypervisor(hv_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/hypervisors/<int:hv_id>/vms", methods=["GET"])
@login_required
def api_vm_hypervisor_vms(hv_id):
    try:
        return jsonify(_svc().discover_vms(hv_id))
    except Exception as e:
        return _err(e)


# ---------------- 保护对象 ----------------
@api_bp.route("/vm/protect", methods=["POST"])
@login_required
def api_vm_protect():
    from core import license as license_mod
    try:
        license_mod.check_quota("vm")
        return jsonify(_svc().protect(_json())), 201
    except license_mod.QuotaExceededError as qe:
        return jsonify({"error": str(qe), "quota_exceeded": True}), 403
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/protected", methods=["GET"])
@login_required
def api_vm_protected():
    return jsonify(_svc().list_protected())


@api_bp.route("/vm/protected/<int:vm_id>", methods=["GET"])
@login_required
def api_vm_protected_detail(vm_id):
    try:
        return jsonify(_svc().get_protected(vm_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/protected/<int:vm_id>", methods=["PUT"])
@login_required
def api_vm_protected_update(vm_id):
    try:
        return jsonify(_svc().update_protected(vm_id, _json()))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/protected/<int:vm_id>", methods=["DELETE"])
@login_required
def api_vm_protected_delete(vm_id):
    try:
        return jsonify(_svc().unprotect(vm_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/protected/<int:vm_id>/backup", methods=["POST"])
@login_required
def api_vm_backup(vm_id):
    try:
        data = _json()
        return jsonify(_svc().trigger_backup(
            vm_id, (data.get("backup_type") or "incremental")))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/plan/<int:vm_id>", methods=["GET"])
@login_required
def api_vm_plan(vm_id):
    try:
        return jsonify(_svc().plan_report(vm_id))
    except Exception as e:
        return _err(e)


# ---------------- 恢复点 ----------------
@api_bp.route("/vm/recovery-points", methods=["GET"])
@login_required
def api_vm_recovery_points():
    vm_id = request.args.get("vm_id")
    limit = int(request.args.get("limit") or 200)
    return jsonify(_svc().list_recovery_points(int(vm_id) if vm_id else None,
                                               limit=limit))


@api_bp.route("/vm/recovery-points/<int:rp_id>", methods=["DELETE"])
@login_required
def api_vm_rp_delete(rp_id):
    try:
        return jsonify(_svc().delete_recovery_point(rp_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/recovery-points/<int:rp_id>/restore", methods=["POST"])
@login_required
def api_vm_rp_restore(rp_id):
    try:
        data = _json()
        import core.models as models
        rp = models.get_vm_recovery_point(int(rp_id))
        if not rp:
            return jsonify({"ok": False, "error": "恢复点不存在"}), 404
        return jsonify(_svc().restore_in_place(
            rp["vm_id"], rp_id, operator=data.get("operator") or "")), 202
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/recovery-points/<int:rp_id>/clone", methods=["POST"])
@login_required
def api_vm_rp_clone(rp_id):
    try:
        data = _json()
        import core.models as models
        rp = models.get_vm_recovery_point(int(rp_id))
        if not rp:
            return jsonify({"ok": False, "error": "恢复点不存在"}), 404
        spec = {
            "target_name": data.get("target_name") or "",
            "target_node": data.get("target_node") or "",
            "isolate_network": data.get("isolate_network", True),
            "network_name": data.get("network_name") or "",
            "auto_start": data.get("auto_start", True),
            "regenerate_mac": data.get("regenerate_mac", True),
            "live": data.get("live", False),
            "ttl_hours": int(data.get("ttl_hours") or 0),
        }
        return jsonify(_svc().clone_from_rp(
            rp["vm_id"], rp_id, spec, operator=data.get("operator") or "")), 202
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/recovery-points/<int:rp_id>/verify", methods=["POST"])
@login_required
def api_vm_rp_verify(rp_id):
    try:
        data = _json()
        import core.models as models
        rp = models.get_vm_recovery_point(int(rp_id))
        if not rp:
            return jsonify({"ok": False, "error": "恢复点不存在"}), 404
        return jsonify(_svc().verify_rp(
            rp["vm_id"], rp_id, data, operator=data.get("operator") or "")), 202
    except Exception as e:
        return _err(e)


# ---------------- 作业 ----------------
@api_bp.route("/vm/jobs", methods=["GET"])
@login_required
def api_vm_jobs():
    return jsonify(_svc().list_jobs(limit=int(request.args.get("limit") or 100)))


@api_bp.route("/vm/jobs/<int:job_id>", methods=["GET"])
@login_required
def api_vm_job(job_id):
    try:
        return jsonify(_svc().get_job(job_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/jobs/<int:job_id>/destroy", methods=["POST"])
@login_required
def api_vm_job_destroy(job_id):
    try:
        return jsonify(_svc().delete_clone_target(job_id))
    except Exception as e:
        return _err(e)


@api_bp.route("/vm/jobs/reap", methods=["POST"])
@login_required
def api_vm_job_reap():
    try:
        return jsonify(_svc().reap_expired_clones())
    except Exception as e:
        return _err(e)


# ---------------- RPO / SLA ----------------
@api_bp.route("/vm/rpo", methods=["GET"])
@login_required
def api_vm_rpo():
    vm_id = request.args.get("vm_id")
    try:
        return jsonify(_svc().rpo_report(int(vm_id) if vm_id else None))
    except Exception as e:
        return _err(e)
