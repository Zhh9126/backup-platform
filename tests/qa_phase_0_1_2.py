# -*- coding: utf-8 -*-
"""
QA 全量回归测试：银行级「三位一体」架构优化 Phase 0 + Phase 1 + Phase 2。

独立第二道防线：不依赖工程师自测，直接针对真实逻辑写用例、自己跑、独立判断。

运行方式（必须用系统 Python 3.14.3，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python3.14 tests/qa_phase_0_1_2.py

本脚本会：
- 使用临时 SQLite 元数据库与临时备份根目录，完全隔离，不污染生产数据；
- 通过 unittest 运行全部用例并打印通过率 X/Y；
- 覆盖：Phase 0 保护策略 / Phase 1 合成全量+调度+去重+生命周期 /
  Phase 2 迁移+克隆+ITSM+异构转换 / API 端点回归。

路由判定规则（见文末报告）：失败源于源码 Bug → 反馈工程师；源于测试 Bug → 自修。
"""

import os
import sys
import uuid
import tempfile
import shutil
import unittest
from datetime import datetime, timezone, timedelta

# ---------------- 0. 运行环境（必须在导入 config / app 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"          # 强制仿真兜底，保证无客户端也能跑通
_TMP = tempfile.mkdtemp(prefix="qa_bk_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                  # noqa: E402  (env 已就绪)
import core.db as db                           # noqa: E402
db.init_schema()                               # noqa: E402

import core.models as models                   # noqa: E402
from app import app as flask_app                # noqa: E402  (触发 create_app -> init_schema)

import core.policy as policy_mod               # noqa: E402
import core.engines as engines_mod             # noqa: E402
from core.engines import (                     # noqa: E402
    synthesize_full_for_task,
    get_adapter_tier,
)
import core.scheduler as scheduler_mod         # noqa: E402
import core.lifecycle as lifecycle_mod         # noqa: E402
import core.migration as migration_mod         # noqa: E402
import core.clone_service as clone_mod         # noqa: E402
import core.itsm as itsm_mod                   # noqa: E402
import core.hetero_convert as hetero_mod       # noqa: E402
from core.storage_backends import (            # noqa: E402
    LocalStorageBackend,
    get_backend,
)


# ---------------- 1. 测试基础设施 ----------------
def clear_all():
    """清空所有业务表，保证每个用例相互独立。"""
    tables = [
        "backup_sets", "backup_records", "restore_records", "protection_policies",
        "backup_tasks", "migration_plans", "clone_requests", "vdb_instances",
        "itsm_tickets", "hetero_jobs", "system_config", "storage_targets",
        "ssh_hosts", "sync_tasks", "sync_records", "inspection_records",
        "drills", "deployments", "system_logs",
    ]
    for t in tables:
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass


class QABase(unittest.TestCase):
    def setUp(self):
        clear_all()
        self.client = flask_app.test_client()
        self.anon = flask_app.test_client()
        # 登录（默认 admin/admin123，见 config）
        r = self.client.post("/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 200, "登录应成功")

    def _task(self, db_type="mysql", **kw):
        base = {
            "name": kw.pop("name", "qa_" + uuid.uuid4().hex[:8]),
            "db_type": db_type,
            "host": "127.0.0.1",
            "port": {"mysql": 3306, "postgresql": 5432, "oracle": 1521,
                     "kingbase": 54321, "dameng": 5236}.get(db_type, 3306),
            "username": "u", "password": "p",
        }
        base.update(kw)
        return models.create_task(base)

    def _record(self, task_id, db_type="mysql", status="success", **kw):
        data = {"task_id": task_id, "db_type": db_type, "backup_type": "full",
                "status": status, "size_bytes": 10, "backup_path": "/tmp/qa.sim"}
        data.update(kw)
        return models.create_record(data)


# ================= Phase 0：保护策略模型 + 适配层契约 =================
class TestPhase0Policy(QABase):

    def test_resolve_core_defaults(self):
        pol = policy_mod.policy_service.resolve({"protection_level": "core"})
        self.assertEqual(pol["rpo_target_min"], 0)
        self.assertEqual(pol["rto_target_min"], 15)
        self.assertEqual(pol["backup_strategy"]["parallel"], 4)

    def test_resolve_important_defaults(self):
        pol = policy_mod.policy_service.resolve({"protection_level": "important"})
        self.assertEqual(pol["rpo_target_min"], 15)
        self.assertEqual(pol["rto_target_min"], 60)
        self.assertEqual(pol["backup_strategy"]["parallel"], 2)

    def test_resolve_general_defaults(self):
        pol = policy_mod.policy_service.resolve({"protection_level": "general"})
        self.assertEqual(pol["rpo_target_min"], 240)
        self.assertEqual(pol["rto_target_min"], 240)
        self.assertEqual(pol["backup_strategy"]["parallel"], 1)

    def test_resolve_rpo_rto_three_levels_differ(self):
        rpo_core, _ = policy_mod.policy_service.resolve_rpo_rto({"protection_level": "core"})
        rpo_imp, _ = policy_mod.policy_service.resolve_rpo_rto({"protection_level": "important"})
        rpo_gen, _ = policy_mod.policy_service.resolve_rpo_rto({"protection_level": "general"})
        self.assertNotEqual(rpo_core, rpo_imp)
        self.assertNotEqual(rpo_imp, rpo_gen)
        self.assertLess(rpo_core, rpo_gen)

    def test_resolve_explicit_policy_has_priority(self):
        pid = models.create_protection_policy({
            "name": "custom", "level": "general",
            "rpo_target_min": 42, "rto_target_min": 99,
        })
        pol = policy_mod.policy_service.resolve({"policy_id": pid, "protection_level": "core"})
        # 显式绑定策略优先于默认 core
        self.assertEqual(pol["rpo_target_min"], 42)
        self.assertEqual(pol["rto_target_min"], 99)

    def test_resolve_fallback_when_level_invalid(self):
        pol = policy_mod.policy_service.resolve({"protection_level": "bogus"})
        self.assertEqual(pol["level"], "general")

    def test_get_adapter_tier_core_self(self):
        for dt in ("oracle", "kingbase", "dameng"):
            self.assertEqual(get_adapter_tier(dt), "core_self", dt)

    def test_get_adapter_tier_peripheral_api(self):
        for dt in ("mysql", "mariadb", "postgresql", "redis", "mongodb", "file"):
            self.assertEqual(get_adapter_tier(dt), "peripheral_api", dt)

    def test_get_adapter_tier_unknown_defaults_peripheral(self):
        self.assertEqual(get_adapter_tier("not_a_real_db"), "peripheral_api")

    def test_policy_models_crud(self):
        pid = models.create_protection_policy({
            "name": "pol_crud", "level": "core",
            "rpo_target_min": 5, "rto_target_min": 10})
        self.assertTrue(pid > 0)
        self.assertIsNotNone(models.get_protection_policy(pid))
        self.assertEqual(len(models.list_protection_policies()), 1)
        models.update_protection_policy(pid, {"rpo_target_min": 7})
        self.assertEqual(models.get_protection_policy(pid)["rpo_target_min"], 7)
        models.delete_protection_policy(pid)
        self.assertIsNone(models.get_protection_policy(pid))

    def test_policy_api_crud(self):
        r = self.client.post("/api/policy", json={"name": "api_pol", "level": "core"})
        self.assertEqual(r.status_code, 201)
        pid = r.get_json()["id"]
        r = self.client.get(f"/api/policy/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["level"], "core")
        r = self.client.put(f"/api/policy/{pid}", json={"rpo_target_min": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/policy/{pid}").get_json()["rpo_target_min"], 3)
        r = self.client.delete(f"/api/policy/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/policy/{pid}").status_code, 404)

    def test_policy_api_validation(self):
        r = self.client.post("/api/policy", json={"level": "core"})  # 缺 name
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/policy", json={"name": "x", "level": "invalid"})
        self.assertEqual(r.status_code, 400)

    def test_bind_policy_sets_derived_columns(self):
        # 显式给出 core 级 RPO/RTO（与 ProtectionPolicyService 默认一致），
        # 验证绑定后任务派生列被正确写入。
        pid = models.create_protection_policy({
            "name": "core_pol", "level": "core",
            "rpo_target_min": 0, "rto_target_min": 15})
        tid = self._task("mysql", policy_id=pid)
        task = models.get_task(tid)
        self.assertEqual(task["protection_level"], "core")
        self.assertEqual(task["adapter_tier"], "peripheral_api")  # mysql=外围
        self.assertEqual(task["rpo_target_min"], 0)
        self.assertEqual(task["rto_target_min"], 15)

    def test_bind_policy_oracle_is_core_self(self):
        pid = models.create_protection_policy({"name": "core_pol2", "level": "core"})
        tid = self._task("oracle", policy_id=pid)
        task = models.get_task(tid)
        self.assertEqual(task["adapter_tier"], "core_self")  # oracle=核心自研

    def test_unbind_policy_clears_columns(self):
        pid = models.create_protection_policy({"name": "core_pol3", "level": "core"})
        tid = self._task("mysql", policy_id=pid)
        models.unbind_policy_from_tasks([tid])
        task = models.get_task(tid)
        self.assertIsNone(task["protection_level"])
        self.assertIsNone(task["adapter_tier"])
        self.assertIsNone(task["rpo_target_min"])
        self.assertIsNone(task["rto_target_min"])


# ================= Phase 1：合成全量 + 调度 + 去重 + 生命周期 =================
class TestPhase1SynthesisSchedulerDedup(QABase):

    def test_backup_set_crud_and_three_types(self):
        tid = self._task()
        rid = self._record(tid)
        for st in ("full", "incremental", "synthetic_full"):
            sid = models.create_backup_set({
                "task_id": tid, "record_id": rid, "set_type": st,
                "storage_tier": 1, "object_key": f"{st}.sim"})
            self.assertTrue(sid > 0)
        all_sets = models.list_backup_sets(task_id=tid)
        self.assertEqual(len(all_sets), 3)
        self.assertEqual(len(models.list_backup_sets(set_type="synthetic_full")), 1)

    def test_synthesize_full_for_task(self):
        tid = self._task()
        rid = self._record(tid)
        full_id = models.create_backup_set({
            "task_id": tid, "record_id": rid, "set_type": "full",
            "storage_tier": 1, "object_key": "/tmp/qa_full.sim", "size_bytes": 100})
        models.create_backup_set({
            "task_id": tid, "record_id": rid, "set_type": "incremental",
            "storage_tier": 1, "object_key": "/tmp/qa_inc.sim",
            "parent_set_id": full_id, "size_bytes": 50})
        new_ids = synthesize_full_for_task(tid)
        self.assertTrue(len(new_ids) >= 1, "应生成至少一条 synthetic_full")
        syn = models.get_backup_set(new_ids[0])
        self.assertEqual(syn["set_type"], "synthetic_full")
        self.assertEqual(syn["parent_set_id"], full_id)

    def test_synthesize_full_no_incremental_noop(self):
        tid = self._task()
        rid = self._record(tid)
        models.create_backup_set({
            "task_id": tid, "record_id": rid, "set_type": "full",
            "storage_tier": 1, "object_key": "/tmp/qa_full2.sim"})
        # 无增量链，不产生合成全量
        self.assertEqual(synthesize_full_for_task(tid), [])

    def test_scheduler_defaults(self):
        cc = scheduler_mod._ConcurrencyController()
        cc._ensure()
        self.assertEqual(cc._limit, 2)  # 缺省保守并发上限
        bw = scheduler_mod._BandwidthGovernor()
        bw._ensure()
        self.assertEqual(bw._cap, 0.0)  # 缺省不限速
        self.assertFalse(scheduler_mod._in_peak_window())  # 未配置避峰窗口

    def test_scheduler_peak_window_logic(self):
        db.set_system_config("peak_hours", "09:00-18:00")
        self.assertTrue(scheduler_mod._in_peak_window(datetime(2026, 1, 1, 10, 30)))
        self.assertFalse(scheduler_mod._in_peak_window(datetime(2026, 1, 1, 21, 0)))
        # 跨午夜窗口
        db.set_system_config("peak_hours", "22:00-06:00")
        self.assertTrue(scheduler_mod._in_peak_window(datetime(2026, 1, 1, 23, 0)))
        self.assertFalse(scheduler_mod._in_peak_window(datetime(2026, 1, 1, 12, 0)))

    def test_run_task_demo_completes(self):
        tid = self._task()
        rec = scheduler_mod.run_task_now(tid)
        self.assertIsNotNone(rec, "DEMO 下任务应产出备份记录")
        self.assertIn(rec["status"], ("success", "simulated"))
        self.assertTrue(rec["id"] > 0)

    def test_dedup_second_save_skips_disk_and_accumulates(self):
        dedup_dir = tempfile.mkdtemp(prefix="qa_dedup_")
        backend = LocalStorageBackend({"endpoint": dedup_dir}, db.get_logger("qa-dedup"))
        content = b"IDENTICAL_BACKUP_PAYLOAD_FOR_DEDUP_2026"
        p1 = os.path.join(dedup_dir, "_srcA.bin")
        p2 = os.path.join(dedup_dir, "_srcB.bin")
        with open(p1, "wb") as f:
            f.write(content)
        with open(p2, "wb") as f:
            f.write(content)
        # 第一次保存（尚无备份集，应真实落盘）
        self.assertTrue(backend.save_file(p1, "obj_A", dedup=True))
        self.assertTrue(os.path.exists(os.path.join(dedup_dir, "obj_A")))
        # 登记该内容的备份集（模拟正常流程：保存后登记 set + checksum）
        chk = db.sha256_file(p1)
        set_id = models.create_backup_set({
            "task_id": 1, "record_id": 1, "set_type": "full", "storage_tier": 1,
            "object_key": "obj_A", "size_bytes": len(content), "checksum": chk})
        # 第二次保存相同内容（不同 object_key）→ 去重命中，不落盘
        self.assertTrue(backend.save_file(p2, "obj_B", dedup=True))
        self.assertFalse(os.path.exists(os.path.join(dedup_dir, "obj_B")),
                          "去重命中后不应重复落盘")
        bs = models.get_backup_set(set_id)
        self.assertEqual(bs["dedup_saved_bytes"], len(content),
                         "去重节省量应累加")

    def test_non_dedup_path_writes_file(self):
        d = tempfile.mkdtemp(prefix="qa_nodedup_")
        backend = LocalStorageBackend({"endpoint": d}, db.get_logger("qa-nodedup"))
        p = os.path.join(d, "_src.bin")
        with open(p, "wb") as f:
            f.write(b"plain content no dedup")
        self.assertTrue(backend.save_file(p, "obj_X", dedup=False))
        self.assertTrue(os.path.exists(os.path.join(d, "obj_X")))


class TestPhase1Lifecycle(QABase):

    def _old_iso(self, days):
        return (datetime.now(timezone.utc).astimezone() - timedelta(days=days)).isoformat(timespec="seconds")

    def test_lifecycle_get_config_defaults(self):
        cfg = lifecycle_mod.LifecycleEngine().get_config()
        self.assertIn("enabled", cfg)
        self.assertIn("l1_to_l2_days", cfg)
        self.assertIn("l2_to_l3_days", cfg)
        self.assertIn("retention_days", cfg)
        self.assertTrue(cfg["enabled"])

    def test_lifecycle_save_config_coerces_types(self):
        eng = lifecycle_mod.LifecycleEngine()
        cfg = eng.save_config({"l1_to_l2_days": "9", "capacity_threshold_pct": "70.5",
                               "enabled": 1, "enable_expiry": "1"})
        self.assertEqual(cfg["l1_to_l2_days"], 9)
        self.assertEqual(cfg["capacity_threshold_pct"], 70.5)
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["enable_expiry"])

    def test_lifecycle_get_status_shape(self):
        tid = self._task()
        rid = self._record(tid)
        models.create_backup_set({"task_id": tid, "record_id": rid,
                                  "set_type": "full", "storage_tier": 1,
                                  "object_key": "s.sim"})
        st = lifecycle_mod.LifecycleEngine().get_status()
        self.assertIn("tiers", st)
        self.assertIn("set_types", st)
        self.assertIn("total_sets", st)
        self.assertIn("config", st)
        self.assertEqual(st["total_sets"], 1)

    def test_lifecycle_run_once_no_exception(self):
        tid = self._task()
        rid = self._record(tid)
        models.create_backup_set({"task_id": tid, "record_id": rid,
                                  "set_type": "full", "storage_tier": 1,
                                  "object_key": "s.sim"})
        summary = lifecycle_mod.LifecycleEngine().run_once()
        for k in ("moved", "expired", "errors", "total_sets", "run_at"):
            self.assertIn(k, summary)

    def test_lifecycle_expiry_branch(self):
        # 三级（tier=3）且超龄的备份集应被到期清理。
        # 说明：models.create_backup_set 会强制 created_at=now（不允许回填），
        # 故此处按“构造数据”直接用 SQL 插入一条 100 天前的备份集以验证年龄阈值分支。
        tid = self._task()
        rid = self._record(tid)
        db.execute(
            "INSERT INTO backup_sets(task_id, record_id, set_type, storage_tier, "
            "object_key, created_at) VALUES (?,?,?,?,?,?)",
            (tid, rid, "full", 3, "old3.sim", self._old_iso(100)))
        summary = lifecycle_mod.LifecycleEngine().run_once()
        self.assertGreaterEqual(summary["expired"], 1)

    def test_lifecycle_capacity_branch(self):
        # 容量阈值设为 0% → 任意 L1 集触发容量下沉分支（capacity_forced 累加）
        eng = lifecycle_mod.LifecycleEngine()
        eng.save_config({"capacity_threshold_pct": 0.0, "l1_to_l2_days": 7})
        tid = self._task()
        rid = self._record(tid)
        models.create_backup_set({"task_id": tid, "record_id": rid,
                                  "set_type": "full", "storage_tier": 1,
                                  "object_key": "fresh.sim",
                                  "created_at": self._old_iso(0)})
        summary = eng.run_once()
        self.assertGreaterEqual(summary["capacity_forced"], 1)


# ================= Phase 2：迁移 + 克隆 + ITSM + 异构转换 =================
class TestPhase2MigrationCloneITSMHetero(QABase):

    def test_migration_pre_mid_post_flow(self):
        tid = self._task()
        eng = migration_mod.MigrationPlan()
        plan = eng.start_pre(tid)
        self.assertEqual(plan["stage"], "pre")
        self.assertIsNotNone(plan["golden_backup_record_id"])
        res = eng.verify_golden(plan["id"])
        self.assertTrue(res["verified"])
        plan = eng.start_mid(tid)
        self.assertEqual(plan["stage"], "mid")
        plan = eng.start_post(tid, old_retention_days=30)
        self.assertEqual(plan["stage"], "post")
        self.assertEqual(plan["old_retention_days"], 30)

    def test_migration_api_flow(self):
        tid = self._task()
        r = self.client.post("/api/migration", json={"task_id": tid, "stage": "pre"})
        self.assertEqual(r.status_code, 201)
        pid = r.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/migration/{pid}/verify").status_code, 200)
        self.assertEqual(self.client.post("/api/migration", json={"task_id": tid,
                         "stage": "mid"}).status_code, 201)
        r = self.client.post("/api/migration", json={"task_id": tid, "stage": "post",
                            "old_retention_days": 15})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["plan"]["old_retention_days"], 15)

    def test_migration_api_invalid_stage(self):
        tid = self._task()
        r = self.client.post("/api/migration", json={"task_id": tid, "stage": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_clone_request_to_ready_then_destroy(self):
        tid = self._task()
        rid = self._record(tid)
        req = clone_mod.clone_service.request_clone(rid, "test", "dev1")
        self.assertEqual(req["status"], "pending")
        self.assertIsNotNone(req["itsm_ticket_id"])
        req = clone_mod.clone_service.approve_clone(req["id"])
        self.assertEqual(req["status"], "ready")
        self.assertIsNotNone(req["vdb_instance_id"])
        req = clone_mod.clone_service.destroy_clone(req["id"])
        self.assertEqual(req["status"], "deleted")

    def test_clone_reject_path(self):
        tid = self._task()
        rid = self._record(tid)
        req = clone_mod.clone_service.request_clone(rid, "test", "dev2")
        req = clone_mod.clone_service.reject_clone(req["id"])
        self.assertEqual(req["status"], "rejected")

    def test_clone_api_approve_reject_destroy(self):
        tid = self._task()
        rid = self._record(tid)
        r = self.client.post("/api/clone", json={"source_record_id": rid,
                            "target_env": "dev", "requested_by": "qa"})
        self.assertEqual(r.status_code, 201)
        cid = r.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/clone/{cid}/approve").status_code, 200)
        self.assertEqual(self.client.post(f"/api/clone/{cid}/destroy").status_code, 200)
        # reject 路径
        r2 = self.client.post("/api/clone", json={"source_record_id": rid,
                             "target_env": "stg", "requested_by": "qa"})
        cid2 = r2.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/clone/{cid2}/reject").status_code, 200)
        self.assertEqual(self.client.get(f"/api/clone/{cid2}").get_json()["status"], "rejected")

    def test_itsm_factory_internal_and_degrade(self):
        ad = itsm_mod.get_itsm_adapter("internal")
        self.assertEqual(ad.system, "internal")
        # dingtalk 未配置真实凭证 → 降级本地工单，不抛异常
        ad2 = itsm_mod.get_itsm_adapter("dingtalk")
        t = ad2.create_ticket("clone", 1, {"x": 1})
        self.assertIsNotNone(t)
        # 未知 system → 降级 internal
        ad3 = itsm_mod.get_itsm_adapter("nope_sys")
        self.assertEqual(ad3.system, "internal")

    def test_itsm_api_tickets_and_config(self):
        self.assertEqual(self.client.get("/api/itsm/tickets").status_code, 200)
        r = self.client.get("/api/itsm/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.get_json()["system"], ("internal", "dingtalk", "servicenow"))

    def test_itsm_approve_cascades_to_clone(self):
        tid = self._task()
        rid = self._record(tid)
        req = clone_mod.clone_service.request_clone(rid, "test", "qa")
        ticket_id = req["itsm_ticket_id"]
        # 通过 ITSM 审批接口回写 → 克隆应被级联审批为 ready
        r = self.client.post(f"/api/itsm/ticket/{ticket_id}/approve",
                             json={"by": "admin"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/clone/{req['id']}").get_json()["status"],
                         "ready")

    def test_hetero_convert_oracle_kingbase(self):
        job_id = hetero_mod.hetero_convert.convert("oracle", "kingbase")
        job = models.get_hetero_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertTrue(os.path.exists(job["result_path"]), "应生成转换产物 manifest")

    def test_hetero_invalid_src_dst(self):
        with self.assertRaises(ValueError):
            hetero_mod.hetero_convert.convert("mysql", "kingbase")   # 源仅支持 oracle
        with self.assertRaises(ValueError):
            hetero_mod.hetero_convert.convert("oracle", "postgresql")  # 目标不支持


# ================= API 端点回归（已登录 / 未登录） =================
class TestApiRegression(QABase):

    def test_pages_return_200_logged_in(self):
        for p in ("/protection", "/storage", "/migration", "/clone"):
            r = self.client.get(p)
            self.assertEqual(r.status_code, 200, f"页面 {p} 应 200")

    def test_api_endpoints_return_200(self):
        for ep in ("/api/policy", "/api/lifecycle", "/api/migration",
                   "/api/clone", "/api/itsm/tickets"):
            r = self.client.get(ep)
            self.assertEqual(r.status_code, 200, f"端点 {ep} 应 200")

    def test_policy_endpoints_regression(self):
        # Phase 0 端点仍正常（回归保护）
        self.assertEqual(self.client.get("/api/policy").status_code, 200)

    def test_unauthenticated_api_returns_401(self):
        r = self.anon.get("/api/policy")
        self.assertEqual(r.status_code, 401)


# ---------------- 2. 运行入口（打印通过率 X/Y） ----------------
if __name__ == "__main__":
    import unittest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    passed = total - failures - errors
    print("\n" + "=" * 64)
    print(f"QA 通过率 = {passed}/{total}  (失败={failures}, 错误={errors})")
    print("=" * 64)
    # 清理临时目录
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:
        pass
    sys.exit(0 if (failures == 0 and errors == 0) else 1)
