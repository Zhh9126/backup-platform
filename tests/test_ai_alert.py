# -*- coding: utf-8 -*-
"""
AI 预测告警透明化测试。

运行方式（必须用系统 Python 3.14.3，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python tests/test_ai_alert.py

覆盖验收标准：
  1. AIPredictor().run_all_checks() 生成的预测记录含非空 predicted_content 与 basis；
  2. list_alert_predictions 返回这两个字段（basis 为 list）；
  3. 配置 GET→POST→GET 往返一致；
  4. _l1_usage() 修正后不再将 local(L3) 误作 L1。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="ai_alert_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"
os.makedirs(os.environ["INSTANCE_DIR"], exist_ok=True)
os.makedirs(os.environ["LOG_DIR"], exist_ok=True)
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402

db.init_schema()

import core.models as models                    # noqa: E402
from core.ai_alert import AIPredictor, _level_from_score   # noqa: E402


# ============================ 1. 预测透明化 ============================
class TestPredictionTransparency(unittest.TestCase):
    """验收 1 & 2：预测记录含 predicted_content 与 basis（list[str]）。"""

    @classmethod
    def setUpClass(cls):
        # 确保有备份记录（供 backup_fail 分析器使用）
        cls.task_id = models.create_task({
            "name": "测试任务-AI", "db_type": "mysql", "host": "127.0.0.1",
            "port": 3306, "username": "root", "password": "",
            "db_name": "demo", "backup_type": "full", "schedule_type": "manual",
            "enabled": 1, "demo_only": 1,
        })
        # 插入几条失败记录让 backup_fail 分析器有数据可分析
        now = db.now_iso()
        for i in range(5):
            models.create_record({
                "task_id": cls.task_id, "db_type": "mysql", "backup_type": "full",
                "started_at": now, "finished_at": now, "duration_sec": 120,
                "status": "failed", "size_bytes": 0, "is_simulated": 1,
                "message": "模拟失败",
            })
        # 插入演练记录让 drill 分析器有数据
        models.create_drill({
            "name": "测试演练", "task_id": cls.task_id, "drill_type": "full_recovery",
            "status": "done", "rto_actual_sec": 1200, "rpo_actual_sec": 20000,
            "triggered_by": "test",
        })

    def test_run_all_checks_produces_transparency_fields(self):
        """run_all_checks() 生成的预测记录含 predicted_content 和 basis。"""
        predictor = AIPredictor()
        summary = predictor.run_all_checks()
        self.assertFalse(summary.get("skipped"), "不应跳过分析")

        # 获取最新预测记录
        raw_preds = models.list_alert_predictions(limit=20)
        # DEMO 环境可能有非空预测
        has_non_empty = False
        for p in raw_preds:
            parsed = models._ap_to_dict(p)
            # basis 必须是 list（_ap_to_dict 已处理）
            self.assertIsInstance(parsed.get("basis", []), list,
                                 f"basis 应为 list，实际: {type(parsed.get('basis'))}")
            # predicted_content 必须是 str
            self.assertIsInstance(parsed.get("predicted_content", ""), str,
                                 f"predicted_content 应为 str，实际: {type(parsed.get('predicted_content'))}")
            if parsed.get("risk_level") != "low" and not parsed.get("empty"):
                has_non_empty = True
                self.assertTrue(parsed["predicted_content"],
                                f"非空预测应有 predicted_content，metric={parsed['metric']}")
                self.assertTrue(len(parsed["basis"]) > 0,
                                f"非空预测应有 basis 条目，metric={parsed['metric']}")
        # 至少有1条非空预测（backup_fail 因为有5条失败记录）
        self.assertTrue(has_non_empty, "应有至少1条非空预测记录")

    def test_each_analyzer_returns_transparency_fields(self):
        """每个分析器直接返回 predicted_content 和 basis。"""
        predictor = AIPredictor()
        analyzers = {
            "backup_fail": predictor.analyze_backup_failure_risk,
            "storage_full": predictor.analyze_storage_risk,
            "link_degraded": predictor.analyze_link_health,
            "drill_overdue": predictor.analyze_drill_compliance,
        }
        for metric, fn in analyzers.items():
            result = fn()
            self.assertIn("predicted_content", result,
                          f"{metric} 分析器应返回 predicted_content")
            self.assertIn("basis", result,
                          f"{metric} 分析器应返回 basis")
            self.assertIsInstance(result["predicted_content"], str)
            self.assertIsInstance(result["basis"], list)
            # 非空预测（score>0）必须有内容和依据
            if result.get("risk_score", 0) > 0 and not result.get("empty"):
                self.assertTrue(result["predicted_content"],
                                f"{metric} 非空预测应有内容")
                self.assertTrue(len(result["basis"]) > 0,
                                f"{metric} 非空预测应有依据")

    def test_list_predictions_returns_basis_as_list(self):
        """list_alert_predictions 返回 basis 为 list[str]（经 _ap_to_dict 解析）。"""
        predictor = AIPredictor()
        # 先确保有预测数据
        predictor.run_all_checks()
        raw_preds = models.list_alert_predictions(limit=50)
        for p in raw_preds:
            # models.list_alert_predictions 返回原始 dict，basis 可能是 JSON str
            # 但 API 路由会经过 _ap_to_dict，这里直接验证 _ap_to_dict 行为
            parsed = models._ap_to_dict(p)
            self.assertIsInstance(parsed["basis"], list,
                                 f"_ap_to_dict 应将 basis 解析为 list")

    def test_create_and_read_prediction_roundtrip(self):
        """create_alert_prediction 写入 predicted_content/basis，读取后一致。"""
        basis_input = ["依据1：失败率 40%", "依据2：连续失败 3 次"]
        content_input = "预测未来一段时间内备份失败概率上升"
        pred_id = models.create_alert_prediction({
            "metric": "backup_fail",
            "risk_score": 75.0,
            "risk_level": "high",
            "details": {"note": "测试"},
            "predicted_content": content_input,
            "basis": basis_input,
        })
        row = db.query_one("SELECT * FROM alert_predictions WHERE id=?", (pred_id,))
        parsed = models._ap_to_dict(row)
        self.assertEqual(parsed["predicted_content"], content_input)
        self.assertEqual(parsed["basis"], basis_input)


# ============================ 2. 配置往返 ============================
class TestConfigRoundTrip(unittest.TestCase):
    """验收 3：GET→POST→GET 配置一致。"""

    def test_config_round_trip(self):
        predictor = AIPredictor()
        # GET 原始配置
        original = predictor.get_config()
        # POST 修改
        new_data = {
            "enabled": True,
            "min_risk_level_to_record": "high",
            "notify_on": "critical",
            "ai_alert_interval_hours": 12,
        }
        predictor.save_config(new_data)
        # GET 再次读取
        after = predictor.get_config()
        self.assertEqual(after["enabled"], True)
        self.assertEqual(after["min_risk_level_to_record"], "high")
        self.assertEqual(after["notify_on"], "critical")
        self.assertEqual(after["ai_alert_interval_hours"], 12)
        # 恢复默认
        predictor.save_config({
            "enabled": original.get("enabled", True),
            "min_risk_level_to_record": original.get("min_risk_level_to_record", "medium"),
            "notify_on": original.get("notify_on", "critical"),
            "ai_alert_interval_hours": original.get("ai_alert_interval_hours", 6),
        })


# ============================ 3. _l1_usage 层级修正 ============================
class TestL1UsageCorrection(unittest.TestCase):
    """验收 4：_l1_usage() 不误把 local(L3) 当 L1。"""

    def test_l1_usage_returns_label_not_confusing_l3_as_l1(self):
        """_l1_usage 返回的 label 应为 'L1(MinIO)' 或 'L1(暂存回退)'，不含 'local(L3)'。"""
        predictor = AIPredictor()
        l1_info = predictor._l1_usage()
        if l1_info and not l1_info.get("error"):
            label = l1_info.get("label", "")
            # label 不应为空
            self.assertTrue(label, "_l1_usage 应返回 label 字段")
            # label 不应含 "L3" 或 "local" 误标
            self.assertNotIn("L3", label, "_l1_usage label 不应含 L3（误把 local 当 L1）")
            # label 应为 L1(MinIO) 或 L1(暂存回退)
            self.assertTrue(label.startswith("L1"),
                            f"_l1_usage label 应以 L1 开头，实际: {label}")

    def test_l1_usage_fallback_on_local_path_labeled_as_staging(self):
        """DEMO_MODE 下无 MinIO 目标，回退到暂存目录但正确标注。"""
        # 确保 DEMO 环境无 MinIO 目标
        minio_targets = db.query(
            "SELECT * FROM storage_targets WHERE type='minio' AND enabled=1")
        if not minio_targets:
            predictor = AIPredictor()
            l1_info = predictor._l1_usage()
            if l1_info and not l1_info.get("error"):
                self.assertEqual(l1_info.get("label"), "L1(暂存回退)",
                                 "无 MinIO 目标时 label 应为 L1(暂存回退)")

    def test_l1_usage_minio_with_used_pct(self):
        """有 MinIO 目标且 extra_options 含 used_pct 时，应返回 MinIO 用量。"""
        # 清除现有 MinIO 目标（如有）
        for t in db.query("SELECT id FROM storage_targets WHERE type='minio'"):
            db.execute("DELETE FROM storage_targets WHERE id=?", (t["id"],))
        # 插入一个含 used_pct 的 MinIO 目标
        db.execute(
            "INSERT INTO storage_targets(name, type, tier, endpoint, access_key, secret_key, "
            "bucket, enabled, is_default, extra_options) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("测试MinIO", "minio", 1, "localhost:9000", "minioadmin", "minioadmin",
             "backup", 1, 1,
             json.dumps({"used_pct": 72.3, "total_bytes": 100000000000,
                         "used_bytes": 72300000000})))
        predictor = AIPredictor()
        l1_info = predictor._l1_usage()
        if l1_info and not l1_info.get("error"):
            self.assertEqual(l1_info.get("label"), "L1(MinIO)")
            self.assertAlmostEqual(l1_info.get("used_percent", 0), 72.3, places=1)
        # 清理
        db.execute("DELETE FROM storage_targets WHERE name='测试MinIO'")


# ============================ 4. 数据库迁移 ============================
class TestSchemaMigration(unittest.TestCase):
    """验证 predicted_content 和 basis 列已存在。"""

    def test_new_columns_exist(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(alert_predictions)")}
        self.assertIn("predicted_content", cols, "缺少列 predicted_content")
        self.assertIn("basis", cols, "缺少列 basis")

    def test_init_schema_idempotent(self):
        """在已有库上重复迁移不报错、不丢数据。"""
        pred_id = models.create_alert_prediction({
            "metric": "backup_fail", "risk_score": 50.0, "risk_level": "medium",
            "details": {"note": "幂等测试"},
            "predicted_content": "测试内容",
            "basis": ["依据1"],
        })
        db.init_schema()
        db.init_schema()
        row = db.query_one("SELECT * FROM alert_predictions WHERE id=?", (pred_id,))
        parsed = models._ap_to_dict(row)
        self.assertEqual(parsed["predicted_content"], "测试内容")


def _main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n通过率: {total - failed}/{total}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    code = 1
    try:
        code = _main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
