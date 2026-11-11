# -*- coding: utf-8 -*-
"""虚拟机备份引擎（db_type="vm"）——把「虚拟机」作为受保护对象接入平台统一体系。

为什么不另起一套调度
--------------------
平台已有：任务调度（APScheduler）、备份记录、备份集链、存储分层、生命周期、
告警、WebSocket 进度。虚拟机备份只需实现 BackupEngine 契约（backup /
restore / verify_record / synthesize_full），即可复用全部既有能力，
「恢复点」额外落到 vm_recovery_points 表承载 PITR 语义（parent_rp_id 链）。

核心流程
--------
backup:
    复原 ctx → Provider 探活 → 能力声明 → journal.plan_next 决策
    （全量 / 增量，增量不支持则诚实回退全量）→ 数据面拉回 → 落 ShA256
    → 写恢复点 + 更新受保护 VM 的 last_change_token / RPO 状态
    → 可选：自动恢复验证（SureBackup 式）

restore:
    按记录找到恢复点 → journal.resolve_chain 得到 [full, inc...]
    → 校验每个产物都在 → Provider 原位还原 / 克隆为新 VM

synthesize_full:
    仓库侧合成全量（Veeam 式）：不占用生产 IOPS，得到可直接挂载的新基座。
"""
import hashlib
import os
import time

import core.db as db
import core.models as models
from core.engines.base import (
    BackupEngine, BackupStatus, BackupResult,
)
from core.engines import BackupType  # noqa: F401  （对外暴露，便于上层引用）
from core.vm import journal, verify as vmverify
from core.vm.providers import get_provider_class
from core.vm.types import CloneSpec, VMProviderError


class VMBackupEngine(BackupEngine):
    """虚拟机备份/恢复引擎。

    任务（backup_tasks, db_type='vm'）与受保护 VM（vm_protected）一一对应：
    task.db_name 存 VM 标识（vm_ref），hypervisor 凭据存于 vm_hypervisors 表。
    """

    db_type = "vm"
    display_name = "虚拟机"
    adapter_tier = "peripheral_api"
    required_clients = []       # 控制面/数据面都走 API 或 SSH，无本地客户端依赖

    # ---------------- 上下文 ----------------
    def _ctx(self):
        """取出 受保护VM + hypervisor(已解密) + provider 实例。"""
        extra = self._parse_task_extra()
        vm_id = extra.get("vm_id") or self.task.get("extra_options", {}) \
            if isinstance(self.task.get("extra_options"), dict) else None
        if not isinstance(extra, dict):
            extra = {}
        vm_id = extra.get("vm_id") or self.extra.get("vm_id")
        vm = None
        if vm_id:
            vm = models.get_vm_protected(int(vm_id))
        if not vm:
            vm = models.get_vm_protected_by_task(self.task.get("id"))
        if not vm:
            return None, "任务未关联受保护的虚拟机（请在「虚拟机保护」中创建，勿手工建 db_type=vm 的任务）"

        hv = models.get_vm_hypervisor(vm["hypervisor_id"], include_secret=True)
        if not hv:
            return None, "关联虚拟化平台 %s 不存在" % vm.get("hypervisor_id")
        cls = get_provider_class(hv.get("provider") or "")
        if not cls:
            return None, "未知的虚拟化平台类型: %s" % (hv.get("provider") or "-")
        provider = cls(hv, logger=self.logger)
        return {"vm": vm, "hv": hv, "provider": provider}, ""

    def _ctx_or_fail(self, default=None):
        ctx, err = self._ctx()
        if err:
            return None, (default or BackupResult(
                success=False, status=BackupStatus.FAILED, message=err))
        return ctx, None

    @staticmethod
    def _hv_extra(hv: dict) -> dict:
        import json
        raw = hv.get("extra_config")
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    # ---------------- 前置检查 ----------------
    def preflight(self) -> tuple:
        ctx, err = self._ctx()
        if err:
            return False, err
        provider = ctx["provider"]
        ok, msg = provider.connect()
        if not ok:
            return False, "虚拟化平台连接失败: %s" % msg
        try:
            caps = provider.capabilities()
        except Exception as e:
            return False, "能力探测失败: %s" % str(e)[:200]
        level = (ctx["vm"].get("consistency") or "crash")
        if level not in (caps.consistency_levels or ["crash"]):
            return False, ("该平台不支持 %s 一致性（可选：%s）"
                           % (level, "/".join(caps.consistency_levels)))
        return True, "ok（%s）%s" % (msg, caps.notes or "")

    # ---------------- 备份 ----------------
    def backup(self, backup_type) -> BackupResult:
        ctx, res = self._ctx_or_fail()
        if res:
            return res
        vm, hv, provider = ctx["vm"], ctx["hv"], ctx["provider"]
        extra_cfg = self._hv_extra(hv)
        policy = journal.merge_policy(extra_cfg)
        rows = models.list_vm_recovery_points(vm["id"])
        bt_value = getattr(backup_type, "value", str(backup_type or "full"))

        ok, msg = provider.connect()
        if not ok:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="虚拟化平台连接失败: %s" % msg)
        caps = provider.capabilities()

        plan = journal.plan_next(rows, caps.to_dict(), policy)
        force_full = bt_value == "full"

        # ① 周期性合成全量（先于本次备份执行：用上一个基座的链合成新基座）
        if plan.get("after_synth") and not force_full:
            try:
                base_rows = models.list_vm_recovery_points(vm["id"])
                last = journal.last_full_in_chain(base_rows)
                if last:
                    self._synthesize(vm, provider, journal.resolve_chain(
                        base_rows, last["id"]))
                    rows = models.list_vm_recovery_points(vm["id"])
                    plan = journal.plan_next(rows, caps.to_dict(), policy)
            except Exception as e:
                self.logger.warning("合成全量失败（继续按常规备份）: %s", e)

        level = "full" if force_full else plan["level"]
        consistency = vm.get("consistency") or "crash"
        work_dir = str(extra_cfg.get("work_dir") or "")
        last_rp = rows[0] if rows else None
        parent_token = (last_rp or {}).get("change_token") or ""

        # ② 执行备份（不支持增量时诚实回退全量，不伪造增量）
        note_msgs = []
        if level == "incremental" and not force_full:
            try:
                art = provider.backup_incremental(vm["vm_ref"], work_dir,
                                                  parent_token, consistency)
            except (VMProviderError, NotImplementedError) as e:
                note_msgs.append("增量不可用，已回退全量：%s" % str(e)[:200])
                art = provider.backup_full(vm["vm_ref"], work_dir, consistency)
        else:
            note_msgs.append(plan.get("reason") or "按计划执行全量")
            art = provider.backup_full(vm["vm_ref"], work_dir, consistency)

        # ③ 数据面拉回平台并计算真实 size/sha256
        ts = self._timestamp()
        local_name = "%s_%s_%s" % (vm.get("vm_name") or "vm",
                                   art.kind, ts)
        ext = os.path.splitext(art.path)[1] or ".img"
        local_path = os.path.join(self._output_dir(), local_name + ext)
        dl = provider.download_artifact(art, local_path)
        size = int(dl.get("size") or 0) or self._file_size(local_path)
        sha = dl.get("sha256") or self._sha256(local_path)
        try:
            provider.cleanup_artifact(art)
        except Exception:
            pass

        # ④ 写恢复点（增量挂到上一个恢复点；全量独立成链首）
        rp_id = models.create_vm_recovery_point({
            "vm_id": vm["id"],
            "task_id": self.task.get("id"),
            "parent_rp_id": (last_rp or {}).get("id") if art.kind == "incremental" else None,
            "rp_type": "full" if art.kind == "full" else "incremental",
            "pit_at": db.now_iso(),
            "consistency": art.consistency or consistency,
            "change_token": art.change_token or "",
            "parent_token": art.parent_token or parent_token,
            "artifact_path": local_path,
            "remote_path": art.path,
            "size_bytes": size,
            "checksum": sha,
        })
        models.update_vm_protected(vm["id"], {
            "last_change_token": art.change_token or parent_token,
            "last_rp_at": db.now_iso(),
            "last_status": "success",
            "last_message": (plan.get("reason") or "")[:200],
        })

        message = "虚拟机备份完成：%s（%s，%.1fMB）" % (
            art.kind, vm.get("vm_name"), size / 1048576.0)
        if note_msgs:
            message += "；" + "；".join(note_msgs)[:300]

        result = BackupResult(
            success=True, status=BackupStatus.SUCCESS, backup_path=local_path,
            size_bytes=size, checksum=sha, message=message,
            detail_log="\n".join([plan.get("reason") or "", message]),
        )

        # ⑤ 自动恢复验证（可选，默认按 VM 配置）
        if extra_cfg.get("verify_after_backup"):
            try:
                spec = CloneSpec(
                    target_name="%s_verify_%s" % (vm.get("vm_name"), ts),
                    isolate_network=True, auto_start=True, live=False)
                spec.network_name = extra_cfg.get("isolate_network") or ""
                vres = vmverify.run_verify(
                    provider, [art.path], spec,
                    checks=(extra_cfg.get("health_checks") or None),
                    node=vm.get("node") or "", logger=self.logger)
                models.update_vm_recovery_point(
                    rp_id, {"verified": 1 if vres.ok else 0,
                            "verify_msg": vres.message[:500]})
                result.verified = bool(vres.ok)
                result.verify_msg = vres.message
            except Exception as e:
                result.verify_msg = "自动恢复验证异常: %s" % str(e)[:200]
        return result

    # ---------------- 恢复 ----------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        ctx, res = self._ctx_or_fail()
        if res:
            return res
        vm, provider = ctx["vm"], ctx["provider"]
        mode = kwargs.get("mode") or "restore_in_place"
        # 上层已经建好作业记录时走「回填」，避免同一作业被记两次账：
        # 否则 UI 轮询的那条永远停在 running，且拿不到克隆出的 target_ref
        # （target_ref 缺失 → 「销毁目标 VM / TTL 自动回收」全部失效）。
        job_id = kwargs.get("job_id")
        target = None

        def _settle(status: str, message: str, **patch):
            data = {"status": status, "progress": 100, "message": message,
                    "finished_at": db.now_iso()}
            data.update(patch)
            if job_id:
                models.update_vm_job(int(job_id), data)
            else:
                models.create_vm_job(dict({"vm_id": vm["id"],
                                           "rp_id": (target or {}).get("id"),
                                           "mode": mode}, **data))

        rows = models.list_vm_recovery_points(vm["id"])
        for r in rows:
            if r.get("artifact_path") == backup_path:
                target = r
                break
        if not target:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="未找到对应的虚拟机恢复点: %s" % backup_path)
        try:
            chain = journal.resolve_chain(rows, target["id"])
        except ValueError as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="恢复点链不可用: %s" % e)

        ok, msg = provider.connect()
        if not ok:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="虚拟化平台连接失败: %s" % msg)

        try:
            if mode == "clone":
                spec = CloneSpec(
                    target_name=kwargs.get("target_name")
                    or "%s_clone_%s" % (vm.get("vm_name"), self._timestamp()),
                    target_node=kwargs.get("target_node") or "",
                    isolate_network=bool(kwargs.get("isolate_network", True)),
                    network_name=kwargs.get("network_name") or "",
                    auto_start=bool(kwargs.get("auto_start", True)),
                    live=bool(kwargs.get("live", False)),
                    regenerate_mac=bool(kwargs.get("regenerate_mac", True)),
                    ttl_hours=int(kwargs.get("ttl_hours") or 0))
                remotes = self._ensure_remote(provider, chain)
                cres = provider.clone_to_new_vm(vm["vm_ref"], remotes, spec)
                if cres.ok:
                    _settle("ready", cres.message,
                            target_ref=cres.target_ref or "",
                            target_name=cres.target_name or "",
                            isolate_network=spec.isolate_network,
                            auto_start=spec.auto_start,
                            ttl_hours=spec.ttl_hours,
                            result_json=_json_dump(cres.to_dict()))
                else:
                    _settle("failed", cres.message or "克隆失败")
                # 作业结束：清理回推到数据源侧的产物副本（平台侧永远保留原件）
                self._cleanup_pushed(provider, remotes)
                return BackupResult(success=cres.ok,
                                    status=BackupStatus.SUCCESS if cres.ok
                                    else BackupStatus.FAILED,
                                    message=cres.message,
                                    detail_log=cres.message)
            rchain = self._ensure_remote(provider, chain)
            ok, message = provider.restore_in_place(vm["vm_ref"], rchain)
            _settle("ready" if ok else "failed", message)
            self._cleanup_pushed(provider, rchain)
            return BackupResult(success=bool(ok),
                                status=BackupStatus.SUCCESS if ok
                                else BackupStatus.FAILED,
                                message=message)
        except (VMProviderError, NotImplementedError) as e:
            _msg = "虚拟机还原失败: %s" % str(e)[:300]
            try:
                _settle("failed", _msg)
            except Exception:
                pass
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=_msg)

    def _ensure_remote(self, provider, chain: list) -> list:
        """确保链上每个恢复点都有「数据源侧可读」的路径。

        恢复/克隆/验证的数据面都在 Hypervisor 侧执行，所以产物必须在那里可见。
        平台侧永远保留落盘副本（且已校验 sha256），这里只在远端副本被清理过
        时才回推一份（幂等覆盖），返回远端路径列表。
        """
        out = []
        for rp in chain:
            local = rp.get("artifact_path") or ""
            remote = rp.get("remote_path") or ""
            if local and os.path.exists(local):
                # 远端可能已清理 → 推回一份（幂等：同名覆盖）
                remote = provider.upload_artifact(
                    local, os.path.dirname(remote) or provider.extra.get("work_dir")
                    or "/var/tmp/vmbk")
            if not remote:
                raise VMProviderError("恢复点 %s 产物不可达（本地与远端均缺失）" % rp["id"])
            out.append(remote)
        return out

    @staticmethod
    def _cleanup_pushed(provider, remotes: list) -> None:
        """恢复/克隆/验证作业结束后，清理 _ensure_remote 回推的数据源侧副本。

        只删回推产生的临时副本；平台侧落盘原件不受影响。逐个容错，
        清理失败不影响作业结论（下次同名覆盖，不累积）。
        """
        from core.vm.types import BackupArtifact
        for p in remotes or []:
            try:
                provider.cleanup_artifact(BackupArtifact(path=p))
            except Exception:
                continue

    # ---------------- 合成全量 ----------------
    def synthesize_full(self) -> BackupResult:
        ctx, res = self._ctx_or_fail()
        if res:
            return res
        vm, provider = ctx["vm"], ctx["provider"]
        rows = models.list_vm_recovery_points(vm["id"])
        last = journal.last_full_in_chain(rows)
        if not last:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="尚无全量基座，无法合成")
        try:
            chain = journal.resolve_chain(rows, last["id"])
            art = self._synthesize(vm, provider, chain)
        except (VMProviderError, ValueError, NotImplementedError) as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="合成全量失败: %s" % str(e)[:300])
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=art.get("artifact_path") or "",
                            message="已在仓库侧合成新的全量基座")

    def _synthesize(self, vm: dict, provider, chain: list) -> dict:
        """执行一次合成全量并落库为 synthetic_full 恢复点。"""
        remotes = self._ensure_remote(provider, chain)
        art = provider.synthesize(list(remotes), "synth_%s" % self._timestamp())
        local_path = os.path.join(self._output_dir(),
                                  "%s_synth_%s.tar.gz" % (vm.get("vm_name"),
                                                          self._timestamp()))
        dl = provider.download_artifact(art, local_path)
        size = int(dl.get("size") or 0) or self._file_size(local_path)
        sha = dl.get("sha256") or self._sha256(local_path)
        try:
            provider.cleanup_artifact(art)
        except Exception:
            pass
        rp_id = models.create_vm_recovery_point({
            "vm_id": vm["id"], "task_id": self.task.get("id"),
            "parent_rp_id": None, "rp_type": "synthetic_full",
            "pit_at": db.now_iso(),
            "consistency": (chain[-1].get("consistency") or "crash"),
            "change_token": (chain[-1].get("change_token") or ""),
            "artifact_path": local_path, "remote_path": art.path,
            "size_bytes": size, "checksum": sha,
        })
        return {"artifact_path": local_path, "rp_id": rp_id, "size": size}

    # ---------------- 恢复校验（复用既有恢复校验页） ----------------
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        ctx, res = self._ctx_or_fail()
        if res:
            return res
        vm, provider = ctx["vm"], ctx["provider"]
        rows = models.list_vm_recovery_points(vm["id"])
        target = None
        for r in rows:
            if r.get("artifact_path") == (record.get("backup_path") or ""):
                target = r
                break
        if not target:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="未找到对应恢复点，无法做恢复验证")
        options = options or {}
        try:
            chain = journal.resolve_chain(rows, target["id"])
            remotes = self._ensure_remote(provider, chain)
        except (ValueError, VMProviderError) as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="恢复点链不可用: %s" % e)
        spec = CloneSpec(
            target_name="%s_verify_%s" % (vm.get("vm_name"), self._timestamp()),
            isolate_network=True, auto_start=True,
            network_name=self._hv_extra(ctx["hv"]).get("isolate_network") or "")
        vres = vmverify.run_verify(
            provider, remotes, spec, checks=(options.get("health_checks") or None),
            node=vm.get("node") or "", logger=self.logger,
            cleanup=bool(options.get("cleanup", True)))
        models.update_vm_recovery_point(
            target["id"], {"verified": 1 if vres.ok else 0,
                           "verify_msg": vres.message[:500]})
        return BackupResult(success=bool(vres.ok),
                            status=BackupStatus.SUCCESS if vres.ok
                            else BackupStatus.FAILED,
                            message=vres.message, verified=bool(vres.ok),
                            verify_msg=vres.message,
                            detail_log="\n".join(
                                "%s: %s" % (c.name, c.message) for c in vres.checks))

    # ---------------- 工具 ----------------
    @staticmethod
    def _file_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except Exception:
            return 0

    @staticmethod
    def _sha256(path: str, chunk: int = 4 << 20) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(chunk), b""):
                    h.update(blk)
            return h.hexdigest()
        except Exception:
            return ""


def _json_dump(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return "{}"


__all__ = ["VMBackupEngine"]
