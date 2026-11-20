# -*- coding: utf-8 -*-
"""虚拟机备份「编排」端到端契约测试。

test_vm_backup.py 验证的是「登记与决策」层（表、引擎解析、能力声明、
增量链决策、API 端点）；本文件验证的是**真正执行**那一层：
用桩 Provider 把 PVE / libvirt / ESXi / Hyper-V 的差异屏蔽掉，跑通

    protect → backup(full) → backup(incremental) → restore / clone / verify
    → 目标 VM 回收（TTL reap / 手动销毁）→ RPO & 计划报告

真实环境不一定随时有虚拟化平台，但编排逻辑（产物落盘、sha256、增量父链、
作业状态流转、TTL 回收、依赖保护）必须始终成立，否则一上生产就是事故。
桩 Provider 只做「文件系统模拟」：全量/增量各产生一个不同内容的真实文件，
size/sha256 都由平台自己算，不允许假成功。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="vm_orch_")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
_REMOTE = os.path.join(_TMP, "remote")          # 模拟「数据源侧」目录
os.makedirs(_REMOTE, exist_ok=True)
os.makedirs(os.path.dirname(os.environ["META_DB_PATH"]), exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                    # noqa: E402
import core.db as db                             # noqa: E402
import core.models as models                     # noqa: E402
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()

from core.vm.base import VMProvider              # noqa: E402
from core.vm.types import (                      # noqa: E402
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, VMDisk, VMInfo,
)

VM_REF = "100"


# --------------------------------------------------------------------------
# 桩 Provider：文件系统级模拟，能力声明为「支持增量 + 支持克隆」
# --------------------------------------------------------------------------
class FakeProvider(VMProvider):
    provider_id = "pve"
    display_name = "Fake PVE"
    deleted = []          # 记录被销毁的目标 VM

    def __init__(self, hypervisor, logger=None):
        super().__init__(hypervisor, logger=logger)
        self.calls = {"full": 0, "incremental": 0, "restore": 0, "clone": 0}

    # ---------------- 基础 ----------------
    def connect(self):
        return True, "桩平台已连接"

    def version(self):
        return "fake-8.1.4"

    def capabilities(self):
        return ProviderCaps(
            incremental=True, change_tracking=True, instant_restore=False,
            clone_to_new_vm=True, file_restore=False, snapshot=True,
            consistency_levels=["crash", "fs"], notes="桩 Provider：全能力声明")

    def list_vms(self):
        return [VMInfo(
            ref=VM_REF, name="fake-vm", node="node1", guest_os="linux",
            power_state="running", cpu=2, memory_mb=2048,
            disks=[VMDisk(key="virtio0", path=os.path.join(_REMOTE, "disk.qcow2"),
                          size_bytes=64 << 20, format="qcow2",
                          change_tracking=True)],
            change_tracking=True)]

    def get_vm(self, ref):
        return VMInfo(ref=str(ref), name="any", node="node1",
                      power_state="running")

    # ---------------- 备份 ----------------
    def _write(self, name: str, kind: str) -> str:
        path = os.path.join(_REMOTE, name)
        body = ("%s|%s|%s\n" % (kind, name, time.time())).encode("utf-8") * 512
        with open(path, "wb") as f:
            f.write(body)
        return path

    def backup_full(self, vm_ref, work_dir, consistency="crash"):
        self.calls["full"] += 1
        n = self.calls["full"]
        path = self._write("full-%d.tar.gz" % n, "FULL")
        return BackupArtifact(
            path=path, kind="full", size_bytes=os.path.getsize(path),
            change_token="tok-full-%d" % n, parent_token="",
            consistency=consistency, disks=["virtio0"])

    def backup_incremental(self, vm_ref, work_dir, parent_token,
                           consistency="crash"):
        self.calls["incremental"] += 1
        n = self.calls["incremental"]
        path = self._write("inc-%d.tar.gz" % n, "INC")
        return BackupArtifact(
            path=path, kind="incremental", size_bytes=os.path.getsize(path),
            change_token="tok-inc-%d" % n, parent_token=parent_token,
            consistency=consistency, disks=["virtio0"])

    def download_artifact(self, artifact, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        shutil.copyfile(artifact.path, local_path)
        with open(local_path, "rb") as f:
            data = f.read()
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def upload_artifact(self, local_path, remote_dir):
        os.makedirs(remote_dir or _REMOTE, exist_ok=True)
        dst = os.path.join(remote_dir or _REMOTE, os.path.basename(local_path))
        shutil.copyfile(local_path, dst)
        return dst

    def cleanup_artifact(self, artifact):
        try:
            os.remove(artifact.path)
        except OSError:
            pass

    # ---------------- 恢复 / 克隆 / 运行时 ----------------
    def restore_in_place(self, vm_ref, artifacts):
        self.calls["restore"] += 1
        return True, "已原位还原（链上 %d 个产物）" % len(artifacts)

    def clone_to_new_vm(self, vm_ref, artifacts, spec):
        self.calls["clone"] += 1
        return CloneResult(ok=True, target_ref="900", target_name="clone-1",
                           ip="10.0.0.9", message="克隆成功")

    def delete_vm(self, ref, node=""):
        self.deleted.append(str(ref))
        return True, "已销毁 %s" % ref

    def power_state(self, vm_ref, node=""):
        return "running"

    def start_vm(self, vm_ref, node=""):
        return True, "started"

    def stop_vm(self, vm_ref, node="", force=False):
        return True, "stopped"

    def guest_ip(self, vm_ref, node=""):
        return "10.0.0.9"


class TestVMOrchestration(unittest.TestCase):
    """protect → backup → restore / clone / verify → reap 全链路。"""

    def setUp(self):
        FakeProvider.deleted = []
        import core.vm as vmface
        import core.vm.engine as vmengine
        self._orig_provider = vmface.get_provider_class
        self._orig_engine_provider = vmengine.get_provider_class
        vmface.get_provider_class = lambda _pid: FakeProvider
        vmengine.get_provider_class = lambda _pid: FakeProvider
        self.vm = vmface

        self.hv_id = models.create_vm_hypervisor({
            "name": "ut-hv", "provider": "pve", "endpoint": "https://fake:8006",
            "username": "root@pam", "password": "x",
            "extra_config": json.dumps({"work_dir": _REMOTE}),
        })
        res = vmface.protect({
            "hypervisor_id": self.hv_id, "vm_refs": [VM_REF],
            "backup_interval_min": 60, "rpo_target_min": 60,
            "biz_system": "虚拟机保护",
        })
        self.assertTrue(res["created"], msg=res)
        self.vm_id = res["created"][0]["vm_id"]
        self.task_id = res["created"][0]["task_id"]

    def tearDown(self):
        import core.vm as vmface
        import core.vm.engine as vmengine
        vmface.get_provider_class = self._orig_provider
        vmengine.get_provider_class = self._orig_engine_provider
        try:
            models.delete_vm_protected(self.vm_id)
            models.delete_vm_hypervisor(self.hv_id)
        except Exception:
            pass

    # ---------------- 工具 ----------------
    def _wait_job(self, job_id: int, timeout: float = 15.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            job = models.get_vm_job(job_id)
            if job and job.get("status") in ("ready", "failed", "deleted"):
                return job
            time.sleep(0.1)
        return models.get_vm_job(job_id) or {}

    def _backup(self, btype="full"):
        from core.engines import get_engine
        from core.engines.base import BackupType
        task = models.get_task(self.task_id, include_secret=True)
        eng = get_engine("vm", task, config.BACKUP_ROOT, None)
        return eng.run_backup(BackupType(btype))

    # ---------------- 用例 ----------------
    def test_01_protect_binds_task_and_vm(self):
        vm = models.get_vm_protected(self.vm_id)
        self.assertEqual(vm["vm_ref"], VM_REF)
        self.assertEqual(vm["task_id"], self.task_id)
        task = models.get_task(self.task_id)
        self.assertEqual(task["db_type"], "vm")
        self.assertEqual(json.loads(task["extra_options"] or "{}")["vm_id"],
                         self.vm_id)

    def test_02_full_backup_real_artifact(self):
        res = self._backup("full")
        self.assertTrue(res.success, msg=res.message)
        self.assertTrue(res.backup_path and os.path.exists(res.backup_path))
        self.assertGreater(res.size_bytes, 0)
        self.assertEqual(len(res.checksum), 64)
        with open(res.backup_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), res.checksum)
        rps = models.list_vm_recovery_points(self.vm_id)
        self.assertEqual(len(rps), 1)
        self.assertEqual(rps[0]["rp_type"], "full")
        self.assertIsNone(rps[0].get("parent_rp_id"))
        self.assertTrue(rps[0]["change_token"])

    def test_03_incremental_links_to_base(self):
        full = self._backup("full")
        inc = self._backup("incremental")
        self.assertTrue(full.success and inc.success, msg=inc.message)
        rows = models.list_vm_recovery_points(self.vm_id)
        base = [r for r in rows if r["rp_type"] == "full"][0]
        child = [r for r in rows if r["rp_type"] == "incremental"][0]
        self.assertEqual(child["parent_rp_id"], base["id"])
        self.assertEqual(child["parent_token"], base["change_token"])
        self.assertNotEqual(child["artifact_path"], base["artifact_path"])
        self.assertTrue(os.path.exists(child["artifact_path"]))

    def test_04_restore_job(self):
        self._backup("full")
        rp = models.list_vm_recovery_points(self.vm_id)[0]
        out = self.vm.restore_in_place(self.vm_id, rp["id"])
        job = self._wait_job(out["job_id"])
        self.assertEqual(job.get("status"), "ready", msg=job.get("message"))
        self.assertEqual(job.get("mode"), "restore_in_place")

    def test_05_clone_job_and_destroy(self):
        self._backup("full")
        rp = models.list_vm_recovery_points(self.vm_id)[0]
        out = self.vm.clone_from_rp(self.vm_id, rp["id"],
                                    {"target_name": "ut-clone",
                                     "isolate_network": True})
        job = self._wait_job(out["job_id"])
        self.assertEqual(job.get("status"), "ready", msg=job.get("message"))
        self.assertTrue(job.get("target_ref"), msg=job)
        res = self.vm.delete_clone_target(job["id"])
        self.assertTrue(res["ok"], msg=res)
        self.assertIn("900", FakeProvider.deleted)

    def test_06_verify_job_passes_and_cleans_up(self):
        self._backup("full")
        rp = models.list_vm_recovery_points(self.vm_id)[0]
        out = self.vm.verify_rp(self.vm_id, rp["id"],
                                {"health_checks": ["power_on"]})
        job = self._wait_job(out["job_id"])
        self.assertEqual(job.get("status"), "ready", msg=job.get("message"))
        # 验证用的隔离 VM 必须被销毁，不能留在生产网络里
        self.assertTrue(FakeProvider.deleted, msg="验证 VM 未被回收")
        self.assertEqual(models.get_vm_recovery_point(rp["id"])["verified"], 1)

    def test_07_reap_expired_clone(self):
        self._backup("full")
        rp = models.list_vm_recovery_points(self.vm_id)[0]
        out = self.vm.clone_from_rp(self.vm_id, rp["id"], {"ttl_hours": 1})
        job = self._wait_job(out["job_id"])
        self.assertEqual(job.get("status"), "ready")
        # 把完成时间往前挪 2 小时，模拟 TTL 到期
        old = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - 7200))
        models.update_vm_job(job["id"], {"finished_at": old, "ttl_hours": 1})
        res = self.vm.reap_expired_clones()
        self.assertIn(job["id"], res["reaped"], msg=res)
        self.assertEqual(models.get_vm_job(job["id"])["status"], "deleted")

    def test_08_delete_recovery_point_blocked_by_dependents(self):
        self._backup("full")
        self._backup("incremental")
        rows = models.list_vm_recovery_points(self.vm_id)
        base = [r for r in rows if r["rp_type"] == "full"][0]
        child = [r for r in rows if r["rp_type"] == "incremental"][0]
        with self.assertRaises(Exception):
            self.vm.delete_recovery_point(base["id"])
        self.vm.delete_recovery_point(child["id"])
        self.vm.delete_recovery_point(base["id"])
        self.assertEqual(models.list_vm_recovery_points(self.vm_id), [])

    def test_09_plan_and_rpo_report(self):
        self._backup("full")
        plan = self.vm.plan_report(self.vm_id)
        self.assertTrue(plan["plan"]["level"])
        self.assertIn("incremental", plan["caps"])
        self.assertTrue(plan["policy"])
        rpo = self.vm.rpo_report(self.vm_id)
        self.assertEqual(len(rpo), 1)
        self.assertTrue(rpo[0]["ok"], msg=rpo)
        self.assertEqual(rpo[0]["rp_count"], 1)


    def test_10_auto_verify_repushes_complete_valid_chain(self):
        from unittest.mock import patch
        from core.vm.types import VerifyResult
        models.update_vm_hypervisor(self.hv_id, {
            "extra_config": json.dumps({"work_dir": _REMOTE, "verify_after_backup": True})})
        lengths = []
        def verify(provider, remotes, spec, **kwargs):
            lengths.append(len(remotes))
            self.assertTrue(all(os.path.isfile(path) for path in remotes))
            self.assertTrue(spec.isolate_network)
            return VerifyResult(ok=True, message="contract read passed")
        with patch("core.vm.engine.vmverify.run_verify", side_effect=verify):
            full = self._backup("full")
            child = self._backup("incremental")
        self.assertTrue(full.success and child.success)
        self.assertTrue(full.verified and child.verified)
        self.assertEqual(lengths, [1, 2])

    def test_11_download_checksum_lie_does_not_publish_recovery_point(self):
        from unittest.mock import patch
        original = FakeProvider.download_artifact
        def damaged(provider, artifact, local_path):
            result = original(provider, artifact, local_path)
            result["sha256"] = "0" * 64
            return result
        with patch.object(FakeProvider, "download_artifact", damaged):
            result = self._backup("full")
        self.assertFalse(result.success)
        self.assertEqual(models.list_vm_recovery_points(self.vm_id), [])

    def test_12_consistency_downgrade_is_rejected(self):
        from unittest.mock import patch
        models.update_vm_protected(self.vm_id, {"consistency": "fs"})
        original = FakeProvider.backup_full
        def downgraded(provider, *args, **kwargs):
            result = original(provider, *args, **kwargs)
            result.consistency = "crash"
            return result
        with patch.object(FakeProvider, "backup_full", downgraded):
            result = self._backup("full")
        self.assertFalse(result.success)
        self.assertIn("一致性", result.message)
        self.assertEqual(models.list_vm_recovery_points(self.vm_id), [])


if __name__ == "__main__":
    unittest.main()
