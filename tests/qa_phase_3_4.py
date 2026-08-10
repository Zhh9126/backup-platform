# -*- coding: utf-8 -*-
"""
QA 全量回归测试：容灾链路 HA + AI 预测告警（Phase 3）+ 演练制度化 + 数据价值挖掘（Phase 4）。

独立第二道防线：不依赖工程师自测，直接针对真实逻辑写用例、自己跑、独立判断。
覆盖 Phase 3 / Phase 4 全部新增能力，并对 Phase 0/1/2 做回归保护（重跑 qa_phase_0_1_2.py）。

运行方式（必须用系统 Python 3.14.3，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python tests/qa_phase_3_4.py   # 解释器需为系统 Python 3.14.3（含 APScheduler）

本脚本会：
- 使用临时 SQLite 元数据库与临时备份根目录，完全隔离，不污染生产数据；
- 通过 unittest 运行全部用例并打印通过率 X/Y；
- 对每个失败按「源码 Bug → 反馈工程师 / 测试 Bug → 自修」做路由判定。
"""

import os
import sys
import re
import json
import uuid
import shutil
import hashlib
import tempfile
import subprocess
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

# ---------------- 0. 运行环境（必须在导入 config / app 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"          # 强制仿真兜底，保证无客户端也能跑通
_TMP = tempfile.mkdtemp(prefix="qa_bk_p34_")
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

import core.disaster_link as disaster_link_mod  # noqa: E402
import core.ai_alert as ai_alert_mod            # noqa: E402
import core.drill as drill_mod                  # noqa: E402
import core.policy as policy_mod                # noqa: E402
import core.scheduler as scheduler_mod          # noqa: E402


# ---------------- 1. 测试基础设施 ----------------
def clear_all():
    """清空所有业务表，保证每个用例相互独立（含 Phase 3/4 新增表）。"""
    tables = [
        "backup_sets", "backup_records", "restore_records", "protection_policies",
        "backup_tasks", "migration_plans", "clone_requests", "vdb_instances",
        "itsm_tickets", "hetero_jobs", "system_config", "storage_targets",
        "ssh_hosts", "sync_tasks", "sync_records", "inspection_records",
        "drills", "deployments", "system_logs",
        "disaster_links", "alert_predictions", "anonymized_exports",
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

    def _link(self, **kw):
        base = {"name": "link_" + uuid.uuid4().hex[:6], "primary_site": "A",
                "dr_site": "B", "status": "standby"}
        base.update(kw)
        return models.create_disaster_link(base)

    def _finished_drill(self, task_id, rto=120.0, rpo=30.0, **kw):
        d = {"name": "d_" + uuid.uuid4().hex[:6], "task_id": task_id,
             "drill_type": "full_recovery"}
        d.update(kw)
        did = models.create_drill(d)
        models.update_drill(did, {
            "status": "success", "finished_at": db.now_iso(),
            "rto_actual_sec": rto, "rpo_actual_sec": rpo, "score": 90,
            "report": json.dumps({"score": 90})})
        return did


# ================= Phase 3：容灾链路 HA =================
class TestPhase3DisasterLink(QABase):

    def test_model_crud(self):
        lid = self._link(name="主备链路1")
        self.assertTrue(lid > 0)
        link = models.get_disaster_link(lid)
        self.assertEqual(link["name"], "主备链路1")
        self.assertEqual(link["status"], "standby")
        # route_policy 缺省自动填充双专线默认策略
        self.assertIsInstance(link["route_policy"], list)
        self.assertGreaterEqual(len(link["route_policy"]), 2)

        # 列表
        self.assertEqual(len(models.list_disaster_links()), 1)
        # 更新
        models.update_disaster_link(lid, {"status": "active", "note": "x"})
        self.assertEqual(models.get_disaster_link(lid)["status"], "active")
        # 删除
        models.delete_disaster_link(lid)
        self.assertIsNone(models.get_disaster_link(lid))

    def test_route_policy_roundtrip(self):
        rp = [{"provider": "移动", "endpoint": "10.0.0.1:3306",
                "priority": 1, "enabled": True, "latency_ms": 9},
               {"provider": "电信", "endpoint": "10.0.0.2:3306",
                "priority": 2, "enabled": False, "latency_ms": 20}]
        lid = self._link(route_policy=rp)
        link = models.get_disaster_link(lid)
        self.assertEqual(link["route_policy"][0]["provider"], "移动")
        # update 序列化
        models.update_disaster_link(lid, {"route_policy": rp})
        self.assertEqual(models.get_disaster_link(lid)["route_policy"][1]["enabled"], False)

    def test_select_route_demo_ok(self):
        lid = self._link()
        eng = disaster_link_mod.DisasterLinkEngine()
        res = eng.select_route(lid)
        self.assertTrue(res["ok"])
        self.assertIn("selected", res)
        self.assertIn("candidates", res)
        self.assertGreaterEqual(res["selected"]["latency_ms"], 1.0)
        # 选中的应是优先级最高的可用专线
        usable = [c for c in res["candidates"] if c["enabled"]]
        expected = min(usable, key=lambda c: (c["priority"], c["latency_ms"]))
        self.assertEqual(res["selected"]["provider"], expected["provider"])

    def test_select_route_no_route_policy_no_crash(self):
        # 边界：无专线配置（空 route_policy）时不应抛异常，应优雅返回错误
        lid = self._link(route_policy=[])
        eng = disaster_link_mod.DisasterLinkEngine()
        res = eng.select_route(lid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "无可用专线")
        # 不存在的链路也不崩
        res2 = eng.select_route(999999)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error"], "容灾链路不存在")

    def test_fill_log_gap_invariants(self):
        lid = self._link()
        eng = disaster_link_mod.DisasterLinkEngine()
        res = eng.fill_log_gap(lid)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["primary_lsn"], res["dr_lsn"] >= 0)
        self.assertEqual(res["gap_lsn"], res["primary_lsn"] - res["dr_lsn"])
        self.assertIn(res["result"], ("no_gap", "filled"))
        self.assertEqual(res["result"], "filled" if res["gap_lsn"] > 0 else "no_gap")

    def test_fill_log_gap_sets_filling_status(self):
        lid = self._link(status="active")
        eng = disaster_link_mod.DisasterLinkEngine()
        # 固定随机种子，确保 gap>0，触发补传分支
        with mock.patch("core.disaster_link.random.randint", return_value=5_000_000), \
             mock.patch("core.disaster_link.random.uniform", return_value=2.0):
            res = eng.fill_log_gap(lid)
        self.assertEqual(res["result"], "filled")
        self.assertEqual(models.get_disaster_link(lid)["status"], "filling")

    def test_run_consistency_check_levels(self):
        lid = self._link(status="filling")
        eng = disaster_link_mod.DisasterLinkEngine()
        # 多次调用覆盖 pass/warn/fail 分布（DEMO 随机）
        seen = set()
        for _ in range(40):
            res = eng.run_consistency_check(lid)
            self.assertTrue(res["ok"])
            self.assertIn(res["result"], ("pass", "warn", "fail"))
            self.assertIn("match_rate", res)
            self.assertIn("sample_checksum_hit", res)
            seen.add(res["result"])
        self.assertIn(res["result"], ("pass", "warn", "fail"))
        # 写入 last_consistency_check 与 consistency_result
        link = models.get_disaster_link(lid)
        self.assertIsNotNone(link["last_consistency_check"])
        self.assertIn(link["consistency_result"], ("pass", "warn", "fail"))

    def test_get_link_status_and_list(self):
        lid = self._link()
        eng = disaster_link_mod.DisasterLinkEngine()
        eng.select_route(lid)
        st = eng.get_link_status(lid)
        self.assertTrue(st["ok"])
        self.assertIsNotNone(st["last_route"])
        self.assertEqual(len(eng.list_links()), 1)

    def test_api_link_crud_and_engine_endpoints(self):
        # 创建
        r = self.client.post("/api/disaster-links", json={"name": "api_link"})
        self.assertEqual(r.status_code, 201)
        lid = r.get_json()["id"]
        # 列表
        self.assertEqual(self.client.get("/api/disaster-links").status_code, 200)
        # 详情
        self.assertEqual(self.client.get(f"/api/disaster-links/{lid}").status_code, 200)
        # 选路 / 填补 / 一致性
        self.assertEqual(
            self.client.post(f"/api/disaster-links/{lid}/select-route").status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/disaster-links/{lid}/fill-gap").status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/disaster-links/{lid}/check-consistency").status_code, 200)
        # 更新
        self.assertEqual(
            self.client.put(f"/api/disaster-links/{lid}", json={"status": "active"}).status_code, 200)
        # 删除
        self.assertEqual(
            self.client.delete(f"/api/disaster-links/{lid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/disaster-links/{lid}").status_code, 404)


# ================= Phase 3：AI 预测告警 =================
class TestPhase3AIAlert(QABase):

    def _make_failed_records(self, n=5):
        tid = self._task()
        for _ in range(n):
            self._record(tid, status="failed")
        return tid

    def test_run_all_checks_generates_predictions(self):
        # 构造 backup_fail（连续失败 → critical）与 storage_full（L1 高占用 → critical）
        self._make_failed_records(5)
        with mock.patch.object(ai_alert_mod.AIPredictor, "_l1_usage",
                               return_value={"used_percent": 96.0, "path": "/"}):
            summary = ai_alert_mod.AIPredictor().run_all_checks()
        self.assertGreaterEqual(summary["recorded"], 2, "应至少记录 2 类风险")
        preds = models.list_alert_predictions()
        levels = {(p["metric"], p["risk_level"]) for p in preds}
        self.assertTrue(any(m == "backup_fail" for m, lv in levels
                           if lv in ("medium", "high", "critical")),
                        "backup_fail 应达到 medium 以上")
        self.assertTrue(any(m == "storage_full" for m, lv in levels
                           if lv in ("medium", "high", "critical")),
                        "storage_full 应达到 medium 以上")

    def test_critical_triggers_notifier_path(self):
        # critical 级别应自动触发 notifier（DEMO 不真实外发但代码路径执行）
        self._make_failed_records(5)
        with mock.patch.object(ai_alert_mod.AIPredictor, "_l1_usage",
                               return_value={"used_percent": 96.0, "path": "/"}):
            summary = ai_alert_mod.AIPredictor().run_all_checks()
        self.assertGreaterEqual(summary["critical_fired"], 1, "应至少触发 1 次 critical 通知")
        # _fire_critical 写入系统日志，证明 notifier 代码路径已执行且不崩
        log = db.query_one(
            "SELECT * FROM system_logs WHERE source='ai_alert' "
            "AND message LIKE 'critical 风险触发通知%' ORDER BY id DESC")
        self.assertIsNotNone(log, "critical 通知代码路径应已执行（系统日志存在）")

    def test_analyze_drill_compliance_overdue(self):
        # 最近一次演练超过 90 天 → overdue
        did = models.create_drill({"name": "old_drill", "task_id": self._task()})
        old = (datetime.now(timezone.utc).astimezone() - timedelta(days=100)).isoformat(timespec="seconds")
        models.update_drill(did, {"status": "success", "finished_at": old,
                                  "rto_actual_sec": 0, "rpo_actual_sec": 0})
        res = ai_alert_mod.AIPredictor().analyze_drill_compliance()
        self.assertFalse(res.get("empty"))
        self.assertEqual(res["metric"], "drill_overdue")
        self.assertIn(res["risk_level"], ("medium", "high", "critical"))
        self.assertGreaterEqual(res["details"]["overdue_days"], 90)

    def test_analyze_drill_compliance_not_overdue(self):
        did = models.create_drill({"name": "fresh_drill", "task_id": self._task()})
        models.update_drill(did, {"status": "success", "finished_at": db.now_iso(),
                                  "rto_actual_sec": 0, "rpo_actual_sec": 0})
        res = ai_alert_mod.AIPredictor().analyze_drill_compliance()
        # 完全合规（未逾期、无 RTO/RPO 超标）→ 空指标（与备份/存储分析一致的"无风险"语义）
        self.assertTrue(res.get("empty"))
        self.assertEqual(res["metric"], "drill_overdue")

    def test_analyze_drill_compliance_custom_interval(self):
        # 自定义 interval_days=5：3 天前不逾期（合规=空指标），10 天前逾期（暴露 overdue_days）
        ai_alert_mod.AIPredictor().save_config({"drill_overdue": {"interval_days": 5}})
        d3 = models.create_drill({"name": "d3", "task_id": self._task()})
        models.update_drill(d3, {"status": "success",
                                 "finished_at": (datetime.now(timezone.utc).astimezone()
                                                 - timedelta(days=3)).isoformat(timespec="seconds"),
                                 "rto_actual_sec": 0, "rpo_actual_sec": 0})
        self.assertTrue(ai_alert_mod.AIPredictor().analyze_drill_compliance().get("empty"))
        d10 = models.create_drill({"name": "d10", "task_id": self._task()})
        models.update_drill(d10, {"status": "success",
                                  "finished_at": (datetime.now(timezone.utc).astimezone()
                                                  - timedelta(days=10)).isoformat(timespec="seconds"),
                                  "rto_actual_sec": 0, "rpo_actual_sec": 0})
        r = ai_alert_mod.AIPredictor().analyze_drill_compliance()
        self.assertFalse(r.get("empty"))
        self.assertGreaterEqual(r["details"]["overdue_days"], 5)

    def test_analyze_storage_risk_l1_thresholds(self):
        pred = ai_alert_mod.AIPredictor()
        # >95% → critical
        with mock.patch.object(pred, "_l1_usage", return_value={"used_percent": 96.0}):
            self.assertEqual(pred.analyze_storage_risk()["risk_level"], "critical")
        # 85%<=x<95% → high（>=65 <85）
        with mock.patch.object(pred, "_l1_usage", return_value={"used_percent": 90.0}):
            self.assertEqual(pred.analyze_storage_risk()["risk_level"], "high")
        # 70%<=x<85% → medium（>=40 <65）
        with mock.patch.object(pred, "_l1_usage", return_value={"used_percent": 70.0}):
            self.assertEqual(pred.analyze_storage_risk()["risk_level"], "medium")
        # <70% 且无其他信号 → 空指标（"存储用量正常"）
        with mock.patch.object(pred, "_l1_usage", return_value={"used_percent": 50.0}):
            self.assertTrue(pred.analyze_storage_risk().get("empty"))

    def test_analyze_storage_risk_bucket_branch(self):
        # 通过 storage_targets 的 extra_options.used_pct 触发预警/临界/趋势分支（与 L1 同逻辑）
        db.execute(
            "INSERT INTO storage_targets(name,type,enabled,extra_options) "
            "VALUES('b1','minio',1,?)",
            (json.dumps({"used_pct": 96.0, "growth_per_day_pct": 5.0}),))
        with mock.patch.object(ai_alert_mod.AIPredictor, "_l1_usage",
                               return_value={"used_percent": 10.0}):
            res = ai_alert_mod.AIPredictor().analyze_storage_risk()
        self.assertEqual(res["risk_level"], "critical")
        kinds = {s["kind"] for s in res["details"]["signals"]}
        self.assertIn("critical", kinds)
        self.assertIn("forecast", kinds)

    def test_analyze_link_health_with_failed_link(self):
        # 链路一致性校验 fail → 中等以上风险
        self._link(consistency_result="fail")
        res = ai_alert_mod.AIPredictor().analyze_link_health()
        self.assertIn(res["risk_level"], ("medium", "high", "critical"))
        self.assertEqual(res["metric"], "link_degraded")
        # 无链路 → 空指标
        clear_all()
        self.assertTrue(ai_alert_mod.AIPredictor().analyze_link_health().get("empty"))

    def test_predict_with_model_rule_engine_and_skeleton(self):
        self._make_failed_records(4)
        pred = ai_alert_mod.AIPredictor()
        # 无 model_uri → 走规则引擎
        r1 = pred.predict_with_model("backup_fail", {})
        self.assertEqual(r1["metric"], "backup_fail")
        self.assertIn(r1["risk_level"], ("low", "medium", "high", "critical"))
        # 有 model_uri → 调用外部 ML 骨架，不崩，返回 low
        r2 = pred.predict_with_model("backup_fail", {}, model_uri="http://ml.example/p")
        self.assertEqual(r2["model_uri"], "http://ml.example/p")
        self.assertEqual(r2["risk_level"], "low")
        self.assertEqual(r2["risk_score"], 0.0)
        # 未知指标且无 model_uri → 空指标不崩
        r3 = pred.predict_with_model("unknown_metric", {})
        self.assertTrue(r3.get("empty"))

    def test_get_prediction_stats(self):
        self._make_failed_records(5)
        with mock.patch.object(ai_alert_mod.AIPredictor, "_l1_usage",
                               return_value={"used_percent": 96.0}):
            ai_alert_mod.AIPredictor().run_all_checks()
        stats = ai_alert_mod.AIPredictor().get_prediction_stats(days=7)
        for k in ("window_days", "by_metric", "latest", "trend"):
            self.assertIn(k, stats)
        self.assertEqual(stats["window_days"], 7)
        self.assertIn("backup_fail", stats["by_metric"])

    def test_get_recent_predictions(self):
        self._make_failed_records(3)
        with mock.patch.object(ai_alert_mod.AIPredictor, "_l1_usage",
                               return_value={"used_percent": 96.0}):
            ai_alert_mod.AIPredictor().run_all_checks()
        preds = ai_alert_mod.AIPredictor().get_recent_predictions(limit=10)
        self.assertGreaterEqual(len(preds), 1)
        self.assertIn("metric", preds[0])


# ================= Phase 4：演练制度化 =================
class TestPhase4Drill(QABase):

    def test_drill_schedule_save_read_models(self):
        cfg = drill_mod.save_drill_schedule({"enabled": True, "frequency": "monthly",
                                             "target_task_ids": [7, 8]})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["frequency"], "monthly")
        self.assertEqual(cfg["target_task_ids"], [7, 8])
        # 读取
        got = drill_mod.get_drill_schedule()
        self.assertEqual(got["enabled"], True)
        self.assertEqual(got["target_task_ids"], [7, 8])
        # 缺省返回
        clear_all()
        d = drill_mod.get_drill_schedule()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["frequency"], "quarterly")

    def test_run_scheduled_drill_demo(self):
        # 准备一个带真实成功备份文件的任务
        tmpf = os.path.join(_TMP, "drill_src.sim")
        with open(tmpf, "w") as f:
            f.write("backup-payload")
        tid = self._task()
        self._record(tid, status="success", backup_path=tmpf, size_bytes=13)
        drill_mod.save_drill_schedule({"enabled": True, "target_task_ids": [tid]})
        summary = drill_mod.run_scheduled_drill(force=True)
        self.assertTrue(summary["ok"])
        self.assertEqual(len(summary["ran"]), 1)
        did = summary["ran"][0]
        drill = models.get_drill(did)
        self.assertIsNotNone(drill)
        self.assertIn(drill["status"], ("success", "failed"))  # 至少完成了一次演练
        self.assertIsNotNone(drill["finished_at"])
        # next_run 被推进
        self.assertIsNotNone(drill_mod.get_drill_schedule().get("next_run"))

    def test_run_scheduled_drill_disabled_skips(self):
        drill_mod.save_drill_schedule({"enabled": False})
        summary = drill_mod.run_scheduled_drill()
        self.assertTrue(summary.get("skipped"))

    def test_get_trend_negative_cleaned(self):
        tid = self._task()
        # 注入一条失真（负值）RTO/RPO 的旧演练，验证趋势清洗
        did = self._finished_drill(tid)
        models.update_drill(did, {"rto_actual_sec": -5, "rpo_actual_sec": -10,
                                  "finished_at": db.now_iso()})
        # 再注入一条正常
        self._finished_drill(tid, rto=120.0, rpo=30.0)
        trend = drill_mod.get_trend(task_id=tid, days=90)
        self.assertGreaterEqual(len(trend["points"]), 2)
        for p in trend["points"]:
            self.assertFalse(p["rto"] is not None and p["rto"] < 0, "RTO 不应为负")
            self.assertFalse(p["rpo"] is not None and p["rpo"] < 0, "RPO 不应为负")
        # summary 聚合值非负
        self.assertGreaterEqual(trend["summary"]["avg_rto"] or 0, 0)

    def test_get_baseline_vs_policy_target(self):
        pid = models.create_protection_policy({"name": "bl_pol", "level": "important",
                                               "rpo_target_min": 30, "rto_target_min": 60})
        tid = self._task(policy_id=pid)
        task = models.get_task(tid)
        self.assertEqual(task["protection_level"], "important")
        # 构造若干已完成演练，使基线有数据
        self._finished_drill(tid, rto=200.0, rpo=50.0)
        self._finished_drill(tid, rto=300.0, rpo=80.0)
        base = drill_mod.get_baseline(tid)
        self.assertTrue(base["ok"])
        # 目标取自保护策略 *60（秒）
        self.assertEqual(base["rpo_target_sec"], 30 * 60)
        self.assertEqual(base["rto_target_sec"], 60 * 60)
        self.assertEqual(base["task_name"], task["name"])
        self.assertEqual(base["protection_level"], "important")
        self.assertIn("baseline", base)
        self.assertIn("verdict", base)
        self.assertIn("rto", base["verdict"])
        self.assertIn("rpo", base["verdict"])

    def test_get_baseline_task_not_found(self):
        base = drill_mod.get_baseline(999999)
        self.assertFalse(base["ok"])
        self.assertIn("error", base)

    def test_scheduler_registers_drill_schedule_job(self):
        # 启动调度器（APScheduler），验证 drill_schedule 周期检查 job 已注册
        config.SCHEDULER_ENABLED = True
        try:
            sched = scheduler_mod.start_scheduler()
            self.assertIsNotNone(sched, "调度器应成功启动")
            job_ids = [j.id for j in sched.get_jobs()]
            self.assertIn("drill_schedule", job_ids,
                          "drill_schedule 周期检查 job 应被注册")
        finally:
            scheduler_mod.stop_scheduler()
            config.SCHEDULER_ENABLED = False


# ================= Phase 4：数据价值挖掘（脱敏导出） =================
class TestPhase4DataMining(QABase):

    def _source_record(self):
        tid = self._task()
        return self._record(tid, status="success")

    def test_export_anonymized_creates_file_and_record(self):
        from core import data_mining as dm
        rid = self._source_record()
        res = dm.DataMiner().export_anonymized(rid)
        self.assertIn("id", res)
        self.assertIn("file_path", res)
        self.assertEqual(res["row_count"], 50)
        self.assertTrue(os.path.isfile(res["file_path"]))
        exp = models.get_anonymized_export(res["id"])
        self.assertIsNotNone(exp)
        self.assertEqual(exp["source_record_id"], rid)

    def test_export_nonexistent_record_raises(self):
        from core import data_mining as dm
        with self.assertRaises(ValueError):
            dm.DataMiner().export_anonymized(999999)

    def test_mask_phone_pattern(self):
        from core import data_mining as dm
        rid = self._source_record()
        res = dm.DataMiner().export_anonymized(
            rid, columns=["phone", "email", "name", "id_card", "bank_card"],
            mask_rules={"phone": "mask", "email": "hash", "name": "fake",
                        "id_card": "mask", "bank_card": "mask"})
        # 读取 CSV 校验脱敏结果
        with open(res["file_path"], "r", encoding="utf-8-sig", newline="") as f:
            import csv
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 50)
        for row in rows:
            # 手机号被脱敏为 138****1234 形态（前3 + **** + 后4）
            self.assertRegex(row["phone"], r"^1\d{2}\*{4}\d{4}$",
                             f"手机号脱敏形态错误: {row['phone']}")
            # email 走 hash：16 位 hex，且不可逆（不等于原值）
            self.assertRegex(row["email"], r"^[0-9a-f]{16}$")
            self.assertNotEqual(row["email"], "user@example.com")
            # 身份证脱敏
            self.assertRegex(row["id_card"], r"^\d{6}\*{4}\d{4}$")
            # 银行卡脱敏含 ****
            self.assertIn("****", row["bank_card"])

    def test_hash_rule_irreversible(self):
        from core import data_mining as dm
        h = dm.DataMiner._mask_hash("secret123")
        self.assertEqual(h, hashlib.sha256(b"secret123").hexdigest()[:16])
        self.assertNotEqual(h, "secret123")  # 单向
        self.assertNotEqual(dm.DataMiner._mask_hash("a"), dm.DataMiner._mask_hash("b"))

    def test_drop_rule_removes_column(self):
        from core import data_mining as dm
        rid = self._source_record()
        res = dm.DataMiner().export_anonymized(
            rid, columns=["phone", "email"], mask_rules={"email": "drop"})
        # email 被 drop → 不出现在导出列
        self.assertIn("phone", res["columns"])
        self.assertNotIn("email", res["columns"])
        with open(res["file_path"], "r", encoding="utf-8-sig", newline="") as f:
            import csv
            header = next(csv.reader(f))
        self.assertIn("phone", header)
        self.assertNotIn("email", header)

    def test_list_download_delete_api(self):
        rid = self._source_record()
        # export
        r = self.client.post("/api/datamining/export",
                             json={"source_record_id": rid,
                                   "columns": ["phone", "email"],
                                   "mask_rules": {"phone": "mask", "email": "hash"}})
        self.assertEqual(r.status_code, 201)
        eid = r.get_json()["id"]
        # list
        lst = self.client.get("/api/datamining/exports")
        self.assertEqual(lst.status_code, 200)
        ids = [x["id"] for x in lst.get_json()]
        self.assertIn(eid, ids)
        # download
        dl = self.client.get(f"/api/datamining/exports/{eid}/download")
        self.assertEqual(dl.status_code, 200)
        self.assertIn(b"phone", dl.data)  # CSV 头含列名
        # delete
        de = self.client.delete(f"/api/datamining/exports/{eid}")
        self.assertEqual(de.status_code, 200)
        self.assertFalse(models.get_anonymized_export(eid))


# ================= API 端点回归（已登录） =================
class TestApiRegressionPhase34(QABase):

    def test_new_endpoints_return_200(self):
        # 准备一个任务用于 baseline/trend 相关端点
        tid = self._task()
        self._record(tid, status="success")
        endpoints = [
            "/api/disaster-links",
            "/api/alerts/predictions",
            "/api/alerts/stats",
            "/api/alerts/config",
            "/api/drills/trend",
            f"/api/drills/baseline?task_id={tid}",
            "/api/drills/schedule",
            "/api/datamining/exports",
        ]
        for ep in endpoints:
            r = self.client.get(ep)
            self.assertEqual(r.status_code, 200, f"端点 {ep} 应 200")

    def test_alerts_run_and_config_post(self):
        r = self.client.post("/api/alerts/run")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("ok"))
        r = self.client.post("/api/alerts/config",
                             json={"enabled": True, "min_risk_level_to_record": "low"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["config"]["enabled"])

    def test_drill_export_post_endpoints(self):
        rid = self._record(self._task(), status="success")
        r = self.client.post("/api/datamining/export",
                             json={"source_record_id": rid})
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/drills/schedule/run")
        self.assertEqual(r.status_code, 200)

    def test_new_pages_return_200(self):
        for p in ("/dr-link", "/alert", "/drills", "/datamining"):
            r = self.client.get(p)
            self.assertEqual(r.status_code, 200, f"页面 {p} 应 200")


# ================= 回归保护：Phase 0/1/2 未被破坏 =================
class TestRegressionPhase012(QABase):

    def test_core_endpoints_still_200(self):
        # 关键业务端点仍可用（回归基线）
        for ep in ("/api/policy", "/api/lifecycle", "/api/migration", "/api/clone"):
            r = self.client.get(ep)
            self.assertEqual(r.status_code, 200, f"回归端点 {ep} 应 200")

    def test_policy_resolve_business_logic_intact(self):
        # Phase 0 关键业务逻辑仍成立：分级 RPO/RTO 解析
        rpo_core, _ = policy_mod.policy_service.resolve_rpo_rto({"protection_level": "core"})
        rpo_gen, _ = policy_mod.policy_service.resolve_rpo_rto({"protection_level": "general"})
        self.assertLess(rpo_core, rpo_gen)

    def test_baseline_suite_passes(self):
        # 重跑 tests/qa_phase_0_1_2.py，确认 Phase 0/1/2 全量回归通过
        py = sys.executable
        env = dict(os.environ)
        env["DEMO_MODE"] = "on"
        env["SCHEDULER_ENABLED"] = "false"
        env["PYTHONPATH"] = PROJECT_ROOT
        proc = subprocess.run(
            [py, os.path.join(PROJECT_ROOT, "tests", "qa_phase_0_1_2.py")],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         "Phase 0/1/2 基线测试应全部通过；"
                         f"stdout=\n{proc.stdout[-2000:]}\n"
                         f"stderr=\n{proc.stderr[-2000:]}")


# ---------------- 2. 运行入口（打印通过率 X/Y） ----------------
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    passed = total - failures - errors
    print("\n" + "=" * 64)
    print(f"QA Phase 3+4 通过率 = {passed}/{total}  (失败={failures}, 错误={errors})")
    print("=" * 64)
    # 清理临时目录
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:
        pass
    sys.exit(0 if (failures == 0 and errors == 0) else 1)
