# -*- coding: utf-8 -*-
"""
T04：AI 告警任务级明细 + 数据验证 analyzer 单测（UX 反馈 20260801）。

运行方式（系统 Python 3.14，DEMO_MODE=on）：
    python -m pytest tests/test_ai_alert_taskdetail.py -q
    python tests/test_ai_alert_taskdetail.py

覆盖 design.md §7 T04 五条验收：
  1. ≥2 个任务各有失败记录时，analyze_backup_failure_risk() 的
     details.task_details 长度 ≥2，每项固定 9 键（8 字段 + suggestion），
     缺值填 null 而不省略键；
  2. details.evidence.record_ids 可对应到真实的 status='failed' 记录；
  3. run_all_checks() 返回 5 个 metric（含 verify_fail）；
  4. 篡改 success 记录的文件后，analyze_backup_verify_risk() 的
     risk_score 落入 high/critical（≥65）；
  5. 全部样本 checksum 为空时，verify_fail 不得误报 critical
     （L1 遇空 checksum 跳过判定，只计 unverified_ratio）。

隔离范式：沿用 tests/test_ai_alert.py 的 tempfile.mkdtemp + DEMO_MODE=on +
META_DB_PATH 真实 SQLite 临时库（不 mock）。额外在本模块内把
config.META_DB_PATH 切到独立库文件，避免与同进程其他测试模块共享
meta.db 造成的记录串扰（analyzer 是全库扫描型，必须独占数据）。
"""

import os
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="ai_alert_td_")
os.environ.setdefault("INSTANCE_DIR", os.path.join(_TMP, "instance"))
os.environ.setdefault("LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("BACKUP_ROOT", os.path.join(_TMP, "backups"))
os.environ.setdefault("META_DB_PATH", os.path.join(_TMP, "instance", "meta.db"))
os.environ["SCHEDULER_ENABLED"] = "false"
for _d in ("instance", "logs", "backups", "files"):
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                                   # noqa: E402
import core.db as db                                            # noqa: E402
import core.models as models                                    # noqa: E402
from core.ai_alert import (AIPredictor, RISK_LEVELS,            # noqa: E402
                           _level_from_score)

# 本模块独占的元数据库（与其他测试模块的 meta.db 物理隔离）
_MODULE_DB = os.path.join(_TMP, "instance", "taskdetail.db")
_FILE_DIR = os.path.join(_TMP, "files")

# design.md §8 约定：task_details 每项固定 8 字段 + suggestion，缺值填 null 不省略键
TASK_DETAIL_KEYS = {
    "task_id", "task_name", "db_type", "fail_7d", "fail_30d",
    "last_fail_at", "last_error", "task_risk_score", "suggestion",
}
EXPECTED_METRICS = {"backup_fail", "verify_fail", "storage_full",
                    "link_degraded", "drill_overdue"}
HIGH_FLOOR = RISK_LEVELS["high"][0]        # 65
CRITICAL_FLOOR = RISK_LEVELS["critical"][0]  # 85


def _iso_days_ago(days: float) -> str:
    """生成 N 天前的 ISO 8601 带时区时间戳（与 db.now_iso() 同格式）。"""
    ts = datetime.now(timezone.utc).astimezone() - timedelta(days=days)
    return ts.isoformat()


class _IsolatedDbCase(unittest.TestCase):
    """所有用例的公共基类：独占临时库 + 每个用例前清空业务表。"""

    @classmethod
    def setUpClass(cls):
        cls._prev_db_path = config.META_DB_PATH
        config.META_DB_PATH = _MODULE_DB
        db.init_schema()

    @classmethod
    def tearDownClass(cls):
        config.META_DB_PATH = cls._prev_db_path

    def setUp(self):
        # 逐用例重置，保证 analyzer 的全库扫描结果可预期（外键顺序：先子后父）
        db.execute("DELETE FROM backup_records")
        db.execute("DELETE FROM backup_tasks")
        db.execute("DELETE FROM alert_predictions")
        self.predictor = AIPredictor()

    # ---------------- 夹具工具 ----------------
    @staticmethod
    def _mk_task(name: str, db_type: str = "mysql") -> int:
        return models.create_task({
            "name": name, "db_type": db_type, "host": "127.0.0.1",
            "port": 3306, "username": "root", "password": "",
            "db_name": "demo", "backup_type": "full",
            "schedule_type": "manual", "enabled": 1, "demo_only": 1,
        })

    @staticmethod
    def _mk_record(task_id: int, status: str, days_ago: float = 0.0,
                   message: str = None, **extra) -> int:
        ts = _iso_days_ago(days_ago)
        data = {
            "task_id": task_id, "db_type": extra.pop("db_type", "mysql"),
            "backup_type": "full", "started_at": ts, "finished_at": ts,
            "duration_sec": 60, "status": status,
            "size_bytes": extra.pop("size_bytes", 1024),
            "is_simulated": extra.pop("is_simulated", 1),
            "message": message,
        }
        data.update(extra)
        return models.create_record(data)


# ==================== 1. backup_fail 任务级明细 ====================
class TestBackupFailTaskDetails(_IsolatedDbCase):
    """验收 1 & 2：details.task_details 结构与 details.evidence 可追溯。"""

    def _seed_two_failing_tasks(self) -> dict:
        """任务 A（连接被拒 4 连败）+ 任务 B（权限失败 3 次 + 2 次成功）。"""
        tid_a = self._mk_task("订单库全备-A", "mysql")
        tid_b = self._mk_task("用户库全备-B", "postgresql")
        ids_a, ids_b = [], []
        for i in range(4):
            ids_a.append(self._mk_record(
                tid_a, "failed", days_ago=i * 0.5,
                message="Connection refused: 无法连接源库 10.0.0.5:3306"))
        for i in range(3):
            ids_b.append(self._mk_record(
                tid_b, "failed", days_ago=i * 0.6 + 0.2,
                message="Access denied for user 'bak'@'%' 权限不足",
                db_type="postgresql"))
        for i in range(2):
            self._mk_record(tid_b, "success", days_ago=5 + i,
                            db_type="postgresql")
        return {"a": tid_a, "b": tid_b, "ids_a": ids_a, "ids_b": ids_b}

    def test_task_details_has_two_tasks_with_full_key_set(self):
        """≥2 个任务有失败记录 → task_details 长度 ≥2 且 9 个键一个不少。"""
        seed = self._seed_two_failing_tasks()
        result = self.predictor.analyze_backup_failure_risk()

        self.assertEqual(result["metric"], "backup_fail")
        details = result.get("details") or {}
        task_details = details.get("task_details")
        self.assertIsInstance(task_details, list, "details.task_details 应为 list")
        self.assertGreaterEqual(len(task_details), 2,
                                f"应至少含 2 个任务明细，实际 {len(task_details)}")

        for item in task_details:
            self.assertEqual(set(item.keys()), TASK_DETAIL_KEYS,
                             f"task_details 键集合不符（缺值应填 null 而非省略键）："
                             f"{sorted(item.keys())}")
            self.assertIsInstance(item["task_id"], int)
            self.assertIsInstance(item["fail_7d"], int)
            self.assertIsInstance(item["fail_30d"], int)
            self.assertIsInstance(item["task_risk_score"], (int, float))
            self.assertTrue(item["task_name"], "task_name 不应为空")
            self.assertTrue(item["suggestion"], "suggestion 不应为空")

        got_tasks = {d["task_id"] for d in task_details}
        self.assertIn(seed["a"], got_tasks)
        self.assertIn(seed["b"], got_tasks)

        by_id = {d["task_id"]: d for d in task_details}
        self.assertEqual(by_id[seed["a"]]["fail_7d"], 4)
        self.assertEqual(by_id[seed["a"]]["fail_30d"], 4)
        self.assertEqual(by_id[seed["b"]]["fail_7d"], 3)
        self.assertEqual(by_id[seed["b"]]["db_type"], "postgresql")

    def test_task_details_sorted_and_suggestion_mapped_by_keyword(self):
        """按 task_risk_score 倒序；建议动作由失败原因关键词映射。"""
        seed = self._seed_two_failing_tasks()
        details = self.predictor.analyze_backup_failure_risk()["details"]
        task_details = details["task_details"]

        scores = [d["task_risk_score"] for d in task_details]
        self.assertEqual(scores, sorted(scores, reverse=True),
                         "task_details 应按 task_risk_score 倒序")

        by_id = {d["task_id"]: d for d in task_details}
        self.assertEqual(by_id[seed["a"]]["suggestion"],
                         "检查源库端口可达性与网络策略")
        self.assertEqual(by_id[seed["b"]]["suggestion"],
                         "检查备份账号权限与凭据有效性")

    def test_missing_db_type_filled_with_null_not_omitted(self):
        """缺值填 null 不省略键：db_type 为空的任务，明细里应为 None 且键仍在。"""
        tid = self._mk_task("无类型任务-C", "mysql")
        db.execute("UPDATE backup_tasks SET db_type='' WHERE id=?", (tid,))
        for i in range(2):
            self._mk_record(tid, "failed", days_ago=i * 0.3, message=None)

        details = self.predictor.analyze_backup_failure_risk()["details"]
        by_id = {d["task_id"]: d for d in details["task_details"]}
        self.assertIn(tid, by_id)
        item = by_id[tid]
        self.assertIn("db_type", item, "db_type 键不得省略")
        self.assertIsNone(item["db_type"], "db_type 缺值应填 null")
        self.assertIn("last_error", item, "last_error 键不得省略")
        self.assertEqual(set(item.keys()), TASK_DETAIL_KEYS)

    def test_evidence_record_ids_point_to_real_failed_records(self):
        """验收 2：evidence.record_ids 每一项都能查到真实的失败记录。"""
        seed = self._seed_two_failing_tasks()
        details = self.predictor.analyze_backup_failure_risk()["details"]
        evidence = details.get("evidence")

        self.assertIsInstance(evidence, dict, "details.evidence 应为 dict")
        self.assertEqual(set(evidence.keys()), {"task_ids", "record_ids"},
                         "evidence 固定为 {task_ids, record_ids}")
        self.assertTrue(evidence["record_ids"], "record_ids 不应为空")

        expect_ids = set(seed["ids_a"] + seed["ids_b"])
        self.assertEqual(set(evidence["record_ids"]), expect_ids,
                         "record_ids 应恰为注入的失败记录 ID 集合")
        for rid in evidence["record_ids"]:
            row = db.query_one("SELECT * FROM backup_records WHERE id=?", (rid,))
            self.assertIsNotNone(row, f"record_id={rid} 在库中不存在")
            self.assertEqual(row["status"], "failed",
                             f"record_id={rid} 应为 failed 记录")
        self.assertEqual(set(evidence["task_ids"]),
                         {d["task_id"] for d in details["task_details"]})

    def test_evidence_lives_in_details_not_basis(self):
        """机器可读 ID 只进 details；basis 恒为人类可读 list[str]。"""
        self._seed_two_failing_tasks()
        result = self.predictor.analyze_backup_failure_risk()
        self.assertIsInstance(result["basis"], list)
        for line in result["basis"]:
            self.assertIsInstance(line, str, "basis 每项必须是字符串")
        self.assertNotIn("evidence", result, "evidence 不得挂在返回体顶层")
        self.assertIn("evidence", result["details"])


# ==================== 2. run_all_checks 5 个 metric ====================
class TestRunAllChecksMetrics(_IsolatedDbCase):
    """验收 3：run_all_checks() 返回 5 个 metric（含新增 verify_fail）。"""

    def test_run_all_checks_returns_five_metrics(self):
        tid = self._mk_task("全量分析任务", "mysql")
        for i in range(3):
            self._mk_record(tid, "failed", days_ago=i * 0.4, message="模拟失败")
        self._mk_record(tid, "success", days_ago=1.0)

        summary = self.predictor.run_all_checks()
        self.assertFalse(summary.get("skipped"), "不应跳过分析")
        results = summary.get("results") or []
        metrics = [r.get("metric") for r in results]

        self.assertEqual(len(results), 5,
                         f"应返回 5 个 metric 结果，实际 {len(results)}: {metrics}")
        self.assertEqual(set(metrics), EXPECTED_METRICS,
                         f"metric 集合不符: {metrics}")
        self.assertIn("verify_fail", metrics, "缺少新增的 verify_fail metric")
        for r in results:
            self.assertIn("risk_score", r)
            self.assertIn("risk_level", r)
            self.assertIn(r["risk_level"], RISK_LEVELS)
            self.assertEqual(r["risk_level"], _level_from_score(r["risk_score"]),
                             f"{r['metric']} 的 risk_level 应由 _level_from_score 判级")

    def test_verify_fail_config_subtable_present(self):
        """verify_fail 子配置齐全（design §2 A2 接线清单第 1 项）。"""
        cfg = self.predictor.get_config().get("verify_fail")
        self.assertIsInstance(cfg, dict, "配置应含 verify_fail 子表")
        for key in ("l1_enabled", "l2_enabled", "l3_enabled",
                    "verify_sample_limit", "verify_max_file_mb",
                    "unverified_ratio_warn", "stale_days"):
            self.assertIn(key, cfg, f"verify_fail 子配置缺少 {key}")


# ==================== 3. verify_fail 篡改检测 ====================
class TestVerifyRiskTampered(_IsolatedDbCase):
    """验收 4：文件被篡改（sha256 与落库 checksum 不符）→ 风险 ≥ high。"""

    def _mk_real_backup_file(self, name: str, body: bytes) -> str:
        path = os.path.join(_FILE_DIR, name)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def test_tampered_file_raises_verify_risk_to_high(self):
        tid = self._mk_task("核心库全备-可校验", "mysql")
        path = self._mk_real_backup_file(
            "tampered.sql",
            b"-- MySQL dump\nCREATE TABLE t(id INT);\nINSERT INTO t VALUES(1);\n")
        good_checksum = db.sha256_file(path)
        self.assertTrue(good_checksum, "sha256_file 应返回非空摘要")

        rid = self._mk_record(tid, "success", days_ago=0.1, is_simulated=0,
                              backup_path=path, checksum=good_checksum,
                              size_bytes=os.path.getsize(path))
        db.execute("UPDATE backup_records SET verified=1, verify_msg='' WHERE id=?",
                   (rid,))

        # —— 篡改：追加内容使实际 sha256 与落库 checksum 不再一致 ——
        with open(path, "ab") as f:
            f.write(b"INSERT INTO t VALUES(999); -- tampered\n")
        self.assertNotEqual(db.sha256_file(path), good_checksum,
                            "篡改后 sha256 应发生变化（夹具自检）")

        result = self.predictor.analyze_backup_verify_risk()
        self.assertEqual(result["metric"], "verify_fail")
        self.assertFalse(result.get("empty"), "检出篡改时不应返回空结果")
        self.assertGreaterEqual(
            result["risk_score"], HIGH_FLOOR,
            f"篡改文件应至少 high（≥{HIGH_FLOOR}），实际 {result['risk_score']}")
        self.assertIn(result["risk_level"], ("high", "critical"),
                      f"risk_level 应为 high/critical，实际 {result['risk_level']}")
        self.assertEqual(result["risk_level"],
                         _level_from_score(result["risk_score"]),
                         "必须走 _level_from_score 判级")

        layers = result["details"]["layers"]
        self.assertGreaterEqual(layers["l1"]["failed"], 1,
                                "L1 完整性应记录 1 条失败")
        task_details = result["details"]["task_details"]
        self.assertTrue(task_details, "篡改任务应出现在 task_details")
        for item in task_details:
            self.assertEqual(set(item.keys()), TASK_DETAIL_KEYS,
                             "verify_fail 的 task_details 必须与 backup_fail 同构")
        by_id = {d["task_id"]: d for d in task_details}
        self.assertIn(tid, by_id)
        self.assertGreaterEqual(by_id[tid]["task_risk_score"], HIGH_FLOOR)

    def test_intact_file_not_flagged_as_l1_failure(self):
        """未被篡改的文件不得判 L1 失败（避免反向误报）。"""
        tid = self._mk_task("核心库全备-完好", "mysql")
        path = self._mk_real_backup_file(
            "intact.sql", b"-- MySQL dump\nCREATE TABLE ok(id INT);\n")
        rid = self._mk_record(tid, "success", days_ago=0.1, is_simulated=0,
                              backup_path=path, checksum=db.sha256_file(path),
                              size_bytes=os.path.getsize(path))
        db.execute("UPDATE backup_records SET verified=1, verify_msg='' WHERE id=?",
                   (rid,))

        result = self.predictor.analyze_backup_verify_risk()
        layers = (result.get("details") or {}).get("layers")
        if layers:  # 非 empty 结果才有 layers
            self.assertEqual(layers["l1"]["failed"], 0,
                             "完好文件不应计入 L1 失败")
        self.assertLess(result["risk_score"], CRITICAL_FLOOR,
                        "完好文件不应触发 critical")


# ==================== 4. checksum 全空不误报 ====================
class TestVerifyRiskEmptyChecksum(_IsolatedDbCase):
    """验收 5：全部样本 checksum 为空时不得误报 critical。"""

    def test_all_checksum_empty_does_not_report_critical(self):
        tid = self._mk_task("存量无校验和任务", "mysql")
        # 注入超过 verify_sample_limit(20) 条记录，确保抽样池全部为空 checksum
        for i in range(25):
            self._mk_record(tid, "success", days_ago=i * 0.1,
                            is_simulated=1, checksum=None, backup_path="")

        result = self.predictor.analyze_backup_verify_risk()
        self.assertEqual(result["metric"], "verify_fail")
        self.assertNotEqual(result["risk_level"], "critical",
                            "checksum 全空不得误报 critical")
        self.assertLess(result["risk_score"], CRITICAL_FLOOR,
                        f"风险分应低于 critical 门槛 {CRITICAL_FLOOR}，"
                        f"实际 {result['risk_score']}")

        details = result.get("details") or {}
        layers = details.get("layers")
        self.assertIsNotNone(layers, "非空结果应含 layers")
        self.assertEqual(layers["l1"]["failed"], 0,
                         "L1 遇空 checksum 应跳过判定而非判失败")
        self.assertGreater(layers["l1"]["skipped"], 0,
                           "空 checksum 的记录应计入 l1.skipped")
        self.assertEqual(details.get("no_checksum_count"),
                         details.get("sample_count"),
                         "全部样本都应计入 no_checksum_count")
        self.assertGreaterEqual(details.get("unverified_ratio", 0), 0.3,
                                "未校验占比应被统计")
        self.assertTrue(
            any("回填" in b for b in result.get("basis", [])),
            f"basis 应提示回填校验和，实际: {result.get('basis')}")

    def test_verified_records_without_checksum_still_no_critical(self):
        """已标记 verified=1 但无 checksum：既不误报，也不因 stale 触发 critical。"""
        tid = self._mk_task("已校验但无校验和", "mysql")
        for i in range(22):
            rid = self._mk_record(tid, "success", days_ago=i * 0.1,
                                  is_simulated=1, checksum=None, backup_path="")
            db.execute(
                "UPDATE backup_records SET verified=1, verify_msg='' WHERE id=?",
                (rid,))

        result = self.predictor.analyze_backup_verify_risk()
        self.assertNotEqual(result.get("risk_level"), "critical",
                            "无 checksum 但已校验的记录不得判 critical")
        layers = (result.get("details") or {}).get("layers")
        if layers:
            self.assertEqual(layers["l1"]["failed"], 0)


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
