# -*- coding: utf-8 -*-
"""虚拟机备份子系统（core.vm）对外门面。

职责边界：
* 本模块只做**编排 + 数据组装**（建任务、写恢复点、起作业线程）；
* 具体平台动作全部委托给 core/vm/providers/*；
* 调度、备份记录、告警等能力**复用平台既有体系**（把虚拟机当成一种受保护对象），
  不另起一套定时器或记录体系。

对外 API 由 api/vm.py 直接调用；UI 见 templates/vm.html。
"""
import json
import threading
import time
from datetime import datetime

import core.db as db
import core.models as models
from core.vm import journal
from core.vm.providers import PROVIDER_META, get_provider_class
from core.vm.types import VMProviderError
from core.vm.engine import VMBackupEngine

__all__ = [
    "PROVIDER_META", "VMBackupEngine", "list_providers", "provider_for",
    "list_hypervisors", "create_hypervisor", "update_hypervisor",
    "delete_hypervisor", "test_hypervisor", "discover_vms",
    "protect", "list_protected", "get_protected", "update_protected",
    "unprotect", "trigger_backup", "restore_in_place", "clone_from_rp",
    "verify_rp", "list_recovery_points", "delete_recovery_point",
    "list_jobs", "get_job", "delete_clone_target", "rpo_report", "plan_report",
    "VMProviderError",
]


# ---------------- Provider ----------------
def list_providers() -> list:
    return [dict(p) for p in PROVIDER_META]


def provider_for(hv_row: dict, logger=None):
    """构造 Provider 实例（传入的 hv_row 必须是**已解密**的）。"""
    cls = get_provider_class(hv_row.get("provider") or "")
    if not cls:
        raise VMProviderError("未知的虚拟化平台类型: %s" % (hv_row.get("provider") or "-"))
    return cls(hv_row, logger=logger)


def _hv_secret(hv_id: int) -> dict:
    hv = models.get_vm_hypervisor(int(hv_id), include_secret=True)
    if not hv:
        raise VMProviderError("虚拟化平台 %s 不存在" % hv_id)
    return hv


# ---------------- 虚拟化平台纳管 ----------------
def list_hypervisors() -> list:
    rows = models.list_vm_hypervisors()
    for r in rows:
        r["provider_name"] = next((p["name"] for p in PROVIDER_META
                                   if p["id"] == r.get("provider")), r.get("provider"))
    return rows


def create_hypervisor(data: dict) -> dict:
    pid = (data.get("provider") or "").strip()
    if not get_provider_class(pid):
        raise VMProviderError("不支持的虚拟化平台类型: %s" % pid)
    if not (data.get("name") or "").strip():
        raise VMProviderError("请填写虚拟化平台名称")
    if not (data.get("endpoint") or "").strip():
        raise VMProviderError("请填写连接地址（endpoint）")
    hv_id = models.create_vm_hypervisor(data)
    hv = _hv_secret(hv_id)
    ok, msg = None, ""
    try:
        ok, msg = provider_for(hv).connect()
    except Exception as e:
        ok, msg = False, str(e)[:300]
    models.update_vm_hypervisor(hv_id, {
        "status": "online" if ok else "offline",
        "last_check_at": db.now_iso(),
        "version": provider_version(hv_id) if ok else "",
    })
    return {"id": hv_id, "ok": bool(ok), "message": msg}


def provider_version(hv_id: int) -> str:
    try:
        return provider_for(_hv_secret(hv_id)).version() or ""
    except Exception:
        return ""


def update_hypervisor(hv_id: int, data: dict) -> dict:
    if "provider" in data and data["provider"] and not get_provider_class(data["provider"]):
        raise VMProviderError("不支持的虚拟化平台类型: %s" % data["provider"])
    models.update_vm_hypervisor(int(hv_id), data)
    return {"id": hv_id, "ok": True}


def delete_hypervisor(hv_id: int) -> dict:
    models.delete_vm_hypervisor(int(hv_id))
    return {"ok": True}


def test_hypervisor(payload: dict) -> dict:
    """测试连通性（未保存的配置也可测：直接构造临时 Provider）。"""
    pid = (payload.get("provider") or "").strip()
    if not get_provider_class(pid):
        return {"ok": False, "message": "不支持的虚拟化平台类型: %s" % pid}
    try:
        p = provider_for(payload)
        ok, msg = p.connect()
        return {"ok": bool(ok), "message": msg, "version": p.version() or ""}
    except Exception as e:
        return {"ok": False, "message": "连接异常: %s" % str(e)[:300]}


def discover_vms(hv_id: int) -> list:
    """资产发现：列出该平台上所有虚拟机（含磁盘、能力、排除原因）。"""
    hv = _hv_secret(hv_id)
    p = provider_for(hv)
    ok, msg = p.connect()
    if not ok:
        raise VMProviderError("连接失败: %s" % msg)
    vms = p.list_vms()
    caps = p.capabilities()
    out = []
    for vm in vms:
        d = vm.to_dict()
        d["provider_supports_incremental"] = caps.incremental
        d["excluded_disks"] = [
            {"key": k.key, "reason": k.exclude_reason}
            for k in vm.disks if k.excluded]
        out.append(d)
    models.update_vm_hypervisor(int(hv_id),
                                {"status": "online", "last_check_at": db.now_iso(),
                                 "version": p.version() or ""})
    return out


# ---------------- 保护（创建任务 + 受保护对象） ----------------
def protect(payload: dict) -> dict:
    """为一批虚拟机建立保护：一条保护任务 ↔ 一台虚拟机。

    复用平台既有调度：任务建在 backup_tasks(db_type='vm')，
    backup_type='incremental' 表示「由 Journal 决定全量/增量」，
    周期性触发由既有 scheduler 负责。
    """
    hv_id = int(payload.get("hypervisor_id") or 0)
    hv = _hv_secret(hv_id)
    refs = payload.get("vm_refs") or []
    if not refs:
        raise VMProviderError("请选择要保护的虚拟机")

    p = provider_for(hv)
    known = {str(v.ref): v for v in p.list_vms()}
    created, skipped = [], []
    for ref in refs:
        vm = known.get(str(ref))
        if not vm:
            skipped.append({"ref": ref, "reason": "平台上找不到该虚拟机"})
            continue
        exist = models.list_vm_protected(hypervisor_id=hv_id)
        if any(str(x.get("vm_ref")) == str(ref) for x in exist):
            skipped.append({"ref": ref, "reason": "已在保护中"})
            continue
        interval = int(payload.get("backup_interval_min") or 1440)
        task_id = models.create_task({
            "name": payload.get("task_name") or ("VM-%s" % (vm.name or ref)),
            "biz_system": payload.get("biz_system") or hv.get("name") or "虚拟机",
            "db_type": "vm",
            "host": hv.get("endpoint") or "",
            "port": None,
            "username": hv.get("username") or "",
            "db_name": str(ref),
            "backup_type": "incremental",   # 由 Journal 实际决策
            "backup_mode": "logical",
            "schedule_type": "interval",
            "interval_minutes": interval,
            "enabled": 1 if payload.get("enabled", 1) else 0,
            "retention_days": int(payload.get("retention_days") or 30),
            "retention_count": int(payload.get("retention_count") or 60),
            "extra_options": json.dumps({"vm_id": None}, ensure_ascii=False),
        })
        disk_n = len([d for d in vm.disks if not d.excluded])
        vm_id = models.create_vm_protected({
            "hypervisor_id": hv_id, "task_id": task_id, "vm_ref": str(ref),
            "vm_name": vm.name, "node": vm.node, "guest_os": vm.guest_os,
            "power_state": vm.power_state, "disk_count": disk_n,
            "total_size_bytes": vm.total_size_bytes,
            "enabled": 1 if payload.get("enabled", 1) else 0,
            "backup_interval_min": interval,
            "consistency": payload.get("consistency") or "crash",
            "rpo_target_min": int(payload.get("rpo_target_min") or interval),
            "retention_days": int(payload.get("retention_days") or 30),
            "retention_count": int(payload.get("retention_count") or 60),
            "extra_config": json.dumps({"provider": hv.get("provider")},
                                       ensure_ascii=False),
        })
        # 回填 vm_id 到任务 extra_options（引擎据此定位受保护对象）
        models.update_task(task_id, {"extra_options": json.dumps(
            {"vm_id": vm_id}, ensure_ascii=False)})
        created.append({"vm_id": vm_id, "task_id": task_id,
                        "vm_ref": str(ref), "vm_name": vm.name})
    return {"created": created, "skipped": skipped,
            "message": "已保护 %d 台，跳过 %d 台" % (len(created), len(skipped))}


def list_protected() -> list:
    rows = models.list_vm_protected()
    for r in rows:
        rps = models.list_vm_recovery_points(r["id"], limit=1)
        r["last_rp"] = (rps[0].get("pit_at") if rps else "") or r.get("last_rp_at") or ""
        r["rp_count"] = len(models.list_vm_recovery_points(r["id"], limit=1000))
    return rows


def get_protected(vm_id: int) -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    rows = models.list_vm_recovery_points(vm["id"])
    vm["rps"] = rows
    try:
        hv = _hv_secret(vm["hypervisor_id"])
        caps = provider_for(hv).capabilities()
        vm["caps"] = caps.to_dict()
    except Exception as e:
        vm["caps"] = {"notes": "能力探测失败: %s" % str(e)[:120]}
    return vm


def update_protected(vm_id: int, data: dict) -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    models.update_vm_protected(int(vm_id), data)
    if vm.get("task_id"):
        patch = {}
        if "backup_interval_min" in data:
            patch["interval_minutes"] = int(data["backup_interval_min"] or 0)
        if "enabled" in data:
            patch["enabled"] = 1 if data["enabled"] else 0
        if "retention_days" in data:
            patch["retention_days"] = int(data["retention_days"] or 0)
        if "retention_count" in data:
            patch["retention_count"] = int(data["retention_count"] or 0)
        if patch:
            models.update_task(vm["task_id"], patch)
    return {"ok": True}


def unprotect(vm_id: int) -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if vm and vm.get("task_id"):
        try:
            models.delete_task(vm["task_id"])
        except Exception:
            pass
    models.delete_vm_protected(int(vm_id))
    return {"ok": True}


# ---------------- 备份触发 ----------------
def trigger_backup(vm_id: int, backup_type: str = "incremental") -> dict:
    """人工触发一次虚拟机备份（走平台既有调度器入口）。"""
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    if not vm.get("task_id"):
        raise VMProviderError("该虚拟机未关联备份任务")
    from core import scheduler
    task = models.get_task(vm["task_id"], include_secret=True)
    from core.engines import get_engine
    import config
    engine = get_engine("vm", task, config.BACKUP_ROOT, None)
    from core.engines.base import BackupType
    bt = BackupType(backup_type) if backup_type in ("full", "incremental") \
        else BackupType.INCREMENTAL
    res = engine.run_backup(bt)
    return {"ok": bool(res.success), "message": res.message,
            "artifact": res.backup_path or "", "size": res.size_bytes}


# ---------------- 还原 / 克隆 / 验证（异步作业） ----------------
def _task_for(vm_row: dict) -> dict:
    return models.get_task(vm_row["task_id"], include_secret=True)


def _engine_for(vm_row: dict):
    from core.engines import get_engine
    import config
    return get_engine("vm", _task_for(vm_row), config.BACKUP_ROOT, None)


def _start_job(vm_id: int, rp_id: int, mode: str, spec: dict, operator: str = ""):
    job_id = models.create_vm_job({
        "vm_id": vm_id, "rp_id": rp_id, "mode": mode,
        "target_name": (spec or {}).get("target_name") or "",
        "target_node": (spec or {}).get("target_node") or "",
        "isolate_network": (spec or {}).get("isolate_network", 1),
        "auto_start": (spec or {}).get("auto_start", 1),
        "ttl_hours": (spec or {}).get("ttl_hours", 0),
        "status": "running", "started_at": db.now_iso(), "operator": operator,
    })

    def _work():
        vm = models.get_vm_protected(int(vm_id))
        try:
            engine = _engine_for(vm)
            if mode == "verify":
                rp = models.get_vm_recovery_point(int(rp_id))
                res = engine.verify_record({"backup_path": rp.get("artifact_path")},
                                           spec or {})
                status = "ready" if res.success else "failed"
                models.update_vm_job(job_id, {
                    "status": status, "progress": 100,
                    "message": res.message, "finished_at": db.now_iso()})
                return
            rp = models.get_vm_recovery_point(int(rp_id))
            res = engine.restore(rp.get("artifact_path") or "",
                                 job_id=job_id,
                                 mode=("clone" if mode == "clone" else "restore_in_place"),
                                 **{k: v for k, v in (spec or {}).items()
                                    if k in ("target_name", "target_node",
                                             "isolate_network", "network_name",
                                             "auto_start", "live", "regenerate_mac",
                                             "ttl_hours")})
            # 引擎已按 job_id 回填作业状态，这里只兜底补齐派生对象引用
            extra = {}
            if getattr(res, "target_ref", ""):
                extra["target_ref"] = res.target_ref
            models.update_vm_job(job_id, dict({
                "status": "ready" if res.success else "failed", "progress": 100,
                "message": res.message, "finished_at": db.now_iso()}, **extra))
        except Exception as e:
            models.update_vm_job(job_id, {
                "status": "failed", "message": "作业异常: %s" % str(e)[:300],
                "finished_at": db.now_iso()})

    threading.Thread(target=_work, name="vm-job-%s" % job_id, daemon=True).start()
    return job_id


def restore_in_place(vm_id: int, rp_id: int, operator: str = "") -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    if not models.get_vm_recovery_point(int(rp_id)):
        raise VMProviderError("恢复点 %s 不存在" % rp_id)
    return {"job_id": _start_job(vm_id, rp_id, "restore_in_place", {}, operator),
            "status": "running",
            "message": "原位还原作业已提交（将覆盖原虚拟机，请确认业务已停止）"}


def clone_from_rp(vm_id: int, rp_id: int, spec: dict, operator: str = "") -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    if not models.get_vm_recovery_point(int(rp_id)):
        raise VMProviderError("恢复点 %s 不存在" % rp_id)
    spec = spec or {}
    if int(spec.get("ttl_hours") or 0) <= 0:
        spec["ttl_hours"] = int(spec.get("ttl_hours") or 0)
    return {"job_id": _start_job(vm_id, rp_id, "clone", spec, operator),
            "status": "running",
            "message": "克隆作业已提交：将在隔离网络中生成一台新虚拟机"}


def verify_rp(vm_id: int, rp_id: int, spec: dict = None, operator: str = "") -> dict:
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    return {"job_id": _start_job(vm_id, rp_id, "verify", spec or {}, operator),
            "status": "running",
            "message": "自动恢复验证已提交（隔离网络拉起 → 健康检查 → 自动销毁）"}


def list_jobs(limit: int = 100) -> list:
    return models.list_vm_jobs(limit=limit)


def get_job(job_id: int) -> dict:
    job = models.get_vm_job(int(job_id))
    if not job:
        raise VMProviderError("作业 %s 不存在" % job_id)
    return job


def delete_clone_target(job_id: int) -> dict:
    """销毁某次克隆/验证产生的目标 VM（到期回收 / 手动回收）。"""
    job = models.get_vm_job(int(job_id))
    if not job:
        raise VMProviderError("作业 %s 不存在" % job_id)
    if not job.get("target_ref"):
        return {"ok": False, "message": "该作业没有可销毁的目标 VM"}
    vm = models.get_vm_protected(job["vm_id"])
    hv = _hv_secret(vm["hypervisor_id"])
    p = provider_for(hv)
    ok, msg = p.delete_vm(job["target_ref"], job.get("target_node") or "")
    models.update_vm_job(int(job_id), {"status": "deleted",
                                       "message": msg, "finished_at": db.now_iso()})
    return {"ok": bool(ok), "message": msg}


def reap_expired_clones() -> dict:
    """TTL 到期的克隆/验证 VM 自动回收（平台生命周期侧的常规任务）。"""
    done, failed = [], []
    for job in models.list_vm_jobs(limit=500):
        if job.get("status") != "ready" or not job.get("ttl_hours"):
            continue
        if not job.get("target_ref"):
            continue
        finished = job.get("finished_at") or job.get("created_at") or ""
        # 平台时间戳是 ISO8601 带时区（db.now_iso()），不能用 strptime 硬解析，
        # 否则 TTL 永远算不出「已到期」，克隆 VM 会无限期残留。
        dt = journal.parse_dt(finished)
        if not dt:
            continue
        age_h = (datetime.now() - dt).total_seconds() / 3600.0
        if age_h < float(job["ttl_hours"]):
            continue
        try:
            res = delete_clone_target(job["id"])
            (done if res.get("ok") else failed).append(job["id"])
        except Exception:
            failed.append(job["id"])
    return {"reaped": done, "failed": failed}


# ---------------- 恢复点 / 报告 ----------------
def list_recovery_points(vm_id: int = None, limit: int = 200) -> list:
    return models.list_vm_recovery_points(vm_id, limit=limit)


def delete_recovery_point(rp_id: int) -> dict:
    rp = models.get_vm_recovery_point(int(rp_id))
    if not rp:
        raise VMProviderError("恢复点 %s 不存在" % rp_id)
    dependents = [r for r in models.list_vm_recovery_points(rp["vm_id"], limit=1000)
                  if int(r.get("parent_rp_id") or 0) == int(rp_id)]
    if dependents:
        raise VMProviderError(
            "该恢复点被 %d 个增量依赖，删除会导致链上恢复点无法恢复；"
            "请先删除其后的增量或执行合成全量" % len(dependents))
    models.delete_vm_recovery_point(int(rp_id))
    return {"ok": True}


def rpo_report(vm_id: int = None) -> list:
    """RPO 达成情况（SLA 视角），用于首页/看板与告警。"""
    out = []
    vms = [models.get_vm_protected(int(vm_id))] if vm_id else models.list_vm_protected()
    for vm in vms:
        if not vm:
            continue
        rows = models.list_vm_recovery_points(vm["id"], limit=1000)
        st = journal.rpo_state(rows, vm.get("rpo_target_min") or 1440)
        out.append({"vm_id": vm["id"], "vm_name": vm.get("vm_name"),
                    "hypervisor_name": vm.get("hypervisor_name"),
                    "rp_count": len(rows), **st})
    return out


def plan_report(vm_id: int) -> dict:
    """下次备份决策（为什么会全量/增量）——可解释性，避免「黑盒策略」。"""
    vm = models.get_vm_protected(int(vm_id))
    if not vm:
        raise VMProviderError("受保护虚拟机 %s 不存在" % vm_id)
    hv = _hv_secret(vm["hypervisor_id"])
    import json as _json
    extra = hv.get("extra_config")
    if isinstance(extra, str) and extra.strip():
        try:
            extra = _json.loads(extra)
        except Exception:
            extra = {}
    policy = journal.merge_policy(extra or {})
    rows = models.list_vm_recovery_points(vm["id"], limit=1000)
    caps = provider_for(hv).capabilities()
    return {"policy": policy, "caps": caps.to_dict(),
            "plan": journal.plan_next(rows, caps.to_dict(), policy),
            "rpo": journal.rpo_state(rows, vm.get("rpo_target_min") or 1440),
            "expiring": [r["id"] for r in journal.plan_expiry(rows, policy)]}
