# -*- coding: utf-8 -*-
"""记录展示统一格式 + 业务系统字段化（v2）的单元测试。

v1 覆盖（保留）：
1. ``normalize_host_ip`` 对 4 种脏数据的归一化（含「本地」不被吞）。
2. ``config`` 中文映射（DB 类型含 file、备份方式三种）。
3. ``models.list_records`` 返回展示要素 + keyword 过滤。
4. ``models.list_restores`` 返回展示要素（含关联备份记录）+ keyword 过滤。

v2 新增覆盖：
5. ``models.compute_biz_label`` 的 R2 回退规则（4 种输入）。
6. ``TASK_FIELDS`` 白名单「写后读回」——防 biz_system 被静默丢弃（D-1）。
7. ``biz_label`` 在 _decorate / list_records / list_restores 三处均下发。
8. keyword 三字段并集搜索（name OR host OR biz_system）。
9. ``/api/records/enriched`` 契约含 biz_label 与 task_id。
10. 导出报表表头 13 列且与数据行对齐（D4）。
11. ``api.tasks._validate_biz_system`` 的必填 / 长度校验。
"""
import csv
import io
import os
import sys
import tempfile
import unittest

# ---------------- 0. 运行环境（导入 config 前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="rec_disp_test_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
db.init_schema()                               # noqa: E402

from flask import Flask                         # noqa: E402

import core.models as models                   # noqa: E402
import api.records as records_bp               # noqa: E402
import api.restore as restore_bp               # noqa: E402
import api.tasks as tasks_bp                   # noqa: E402

# 用于给受 @login_required 保护的视图函数提供请求上下文。
# 视图通过 functools.wraps 暴露 __wrapped__，可绕过鉴权直接测业务契约。
_FLASK_APP = Flask(__name__)


def _iso(days_ago: float = 0.0) -> str:
    import datetime
    base = datetime.datetime(2026, 8, 1, 9, 0, 0)
    return (base - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


class TestNormalizeHostIp(unittest.TestCase):
    def test_strip_user_and_port(self):
        self.assertEqual(models.normalize_host_ip("root@192.168.220.150:22"),
                         "192.168.220.150")

    def test_plain_ip(self):
        self.assertEqual(models.normalize_host_ip("192.168.220.150"),
                         "192.168.220.150")

    def test_localhost_preserved(self):
        # 关键：旧正则会把「本地」吞成空串，这里必须原样保留
        self.assertEqual(models.normalize_host_ip("本地"), "本地")

    def test_local_ip(self):
        self.assertEqual(models.normalize_host_ip("127.0.0.1"), "127.0.0.1")

    def test_empty(self):
        self.assertEqual(models.normalize_host_ip(""), "-")
        self.assertEqual(models.normalize_host_ip(None), "-")


class TestConfigMappings(unittest.TestCase):
    def test_db_display_has_file(self):
        self.assertEqual(config.DB_DISPLAY_NAMES.get("file"), "文件")
        self.assertEqual(config.DB_DISPLAY_NAMES.get("mysql"), "MySQL")

    def test_backup_type_display(self):
        self.assertEqual(config.BACKUP_TYPE_DISPLAY_NAMES.get("full"), "全量")
        self.assertEqual(config.BACKUP_TYPE_DISPLAY_NAMES.get("incremental"), "增量")
        self.assertEqual(config.BACKUP_TYPE_DISPLAY_NAMES.get("differential"), "差异")


# ============================================================
# v2：R2 回退规则（compute_biz_label）
# ============================================================
class TestComputeBizLabel(unittest.TestCase):
    """规则 R2 的四种输入组合（设计 §4.1.1 表格逐行对应）。"""

    def test_biz_system_wins(self):
        self.assertEqual(
            models.compute_biz_label("OA 办公系统", "mysql-增量-v2"), "OA 办公系统")

    def test_none_falls_back_to_name(self):
        # 存量任务上线首日 100% 走此分支
        self.assertEqual(models.compute_biz_label(None, "OA"), "OA")

    def test_blank_falls_back_to_name(self):
        self.assertEqual(models.compute_biz_label("", "phase2-demo-mysql"),
                         "phase2-demo-mysql")
        self.assertEqual(models.compute_biz_label("   ", "x"), "x")

    def test_both_empty_returns_placeholder(self):
        # 兜底：永不返回空串 / undefined
        self.assertEqual(models.compute_biz_label(None, None), "-")
        self.assertEqual(models.compute_biz_label("", "   "), "-")

    def test_result_is_trimmed(self):
        self.assertEqual(models.compute_biz_label("  OA  ", "n"), "OA")

    def test_never_returns_empty(self):
        for a in (None, "", "  ", "OA"):
            for b in (None, "", "  ", "任务名"):
                self.assertTrue(models.compute_biz_label(a, b),
                                f"空返回值: biz_system={a!r} name={b!r}")


# ============================================================
# v2：Schema 迁移
# ============================================================
class TestSchemaMigration(unittest.TestCase):
    def test_column_exists(self):
        cols = [r["name"] for r in db.query("PRAGMA table_info(backup_tasks)")]
        self.assertIn("biz_system", cols)

    def test_init_schema_idempotent(self):
        # 重复执行不得抛异常（ALTER 重复列被 except 吞掉）
        db.init_schema()
        db.init_schema()
        cols = [r["name"] for r in db.query("PRAGMA table_info(backup_tasks)")]
        self.assertEqual(cols.count("biz_system"), 1)


# ============================================================
# v2：TASK_FIELDS 白名单「写后读回」（D-1 硬失败）
# ============================================================
class TestTaskFieldsWhitelist(unittest.TestCase):
    """漏加白名单时 biz_system 会被静默丢弃（无异常、无日志、HTTP 200），
    只能靠写后读回把它变成硬失败。"""

    def test_biz_system_in_whitelist(self):
        self.assertIn("biz_system", models.TASK_FIELDS)

    def test_create_then_read_back(self):
        tid = models.create_task({
            "name": "wl-create", "biz_system": "白名单验证系统",
            "db_type": "mysql", "host": "10.0.0.1", "port": 3306,
            "enabled": 1, "demo_only": 1,
        })
        task = models.get_task(tid)
        self.assertEqual(task["biz_system"], "白名单验证系统")
        self.assertEqual(task["biz_label"], "白名单验证系统")

    def test_update_then_read_back(self):
        tid = models.create_task({
            "name": "wl-update", "biz_system": "旧系统",
            "db_type": "mysql", "host": "10.0.0.2", "port": 3306,
            "enabled": 1, "demo_only": 1,
        })
        models.update_task(tid, {"biz_system": "新系统"})
        self.assertEqual(models.get_task(tid)["biz_system"], "新系统")

    def test_create_without_biz_system_is_null(self):
        tid = models.create_task({
            "name": "wl-null", "db_type": "mysql", "host": "10.0.0.3",
            "port": 3306, "enabled": 1, "demo_only": 1,
        })
        task = models.get_task(tid)
        self.assertIn(task["biz_system"], (None, ""))
        # R2 回退：展示值等于任务名
        self.assertEqual(task["biz_label"], "wl-null")


# ============================================================
# 共享种子数据（模块级只种一次，避免各测试类重复种植干扰搜索断言）
# ============================================================
_SEED = {}


def _seed():
    """种植测试数据并返回 id 字典。重复调用直接返回缓存。"""
    if _SEED:
        return _SEED

    # A：存量形态——biz_system 为空，展示走 R2 回退到 name
    _SEED["tid_a"] = models.create_task({
        "name": "订单库全备", "db_type": "mysql", "host": "root@192.168.220.150:22",
        "port": 3306, "username": "root", "password": "", "db_name": "demo",
        "backup_type": "full", "schedule_type": "manual", "enabled": 1, "demo_only": 1,
    })
    # B：存量形态 + 本地主机
    _SEED["tid_b"] = models.create_task({
        "name": "本地文件备份", "db_type": "file", "host": "本地",
        "port": 0, "username": "", "password": "", "db_name": "",
        "backup_type": "incremental", "schedule_type": "manual", "enabled": 1, "demo_only": 1,
    })
    # C：已填业务系统 + 任务名与业务系统不同（「改名后」场景，验证搜索超集语义）
    _SEED["tid_c"] = models.create_task({
        "name": "phase2-demo-mysql", "biz_system": "OA办公系统",
        "db_type": "mysql", "host": "root@10.20.30.40:3306",
        "port": 3306, "username": "root", "password": "", "db_name": "oa",
        "backup_type": "full", "schedule_type": "manual", "enabled": 1, "demo_only": 1,
    })

    _SEED["rid_a"] = models.create_record({
        "task_id": _SEED["tid_a"], "db_type": "mysql", "backup_type": "full",
        "started_at": _iso(1), "finished_at": _iso(1), "duration_sec": 60,
        "status": "success", "size_bytes": 1024, "is_simulated": 1, "message": "",
    })
    _SEED["rid_b"] = models.create_record({
        "task_id": _SEED["tid_b"], "db_type": "file", "backup_type": "incremental",
        "started_at": _iso(2), "finished_at": _iso(2), "duration_sec": 30,
        "status": "success", "size_bytes": 2048, "is_simulated": 1, "message": "",
    })
    _SEED["rid_c"] = models.create_record({
        "task_id": _SEED["tid_c"], "db_type": "mysql", "backup_type": "full",
        "started_at": _iso(3), "finished_at": _iso(3), "duration_sec": 90,
        "status": "success", "size_bytes": 4096, "is_simulated": 1, "message": "",
    })

    _SEED["rrid"] = models.create_restore({
        "task_id": _SEED["tid_a"], "record_id": _SEED["rid_a"],
        "target_host": "192.168.220.200", "target_db": "demo2",
        "started_at": _iso(0), "finished_at": _iso(0),
        "status": "success", "message": "", "operator": "tester",
    })
    _SEED["rrid_c"] = models.create_restore({
        "task_id": _SEED["tid_c"], "record_id": _SEED["rid_c"],
        "target_host": "10.20.30.41", "target_db": "oa2",
        "started_at": _iso(0), "finished_at": _iso(0),
        "status": "success", "message": "", "operator": "tester",
    })
    return _SEED


class _SeedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = _seed()
        for k, v in s.items():
            setattr(cls, k, v)


class TestListRecords(_SeedCase):
    def test_elements_joined(self):
        rows = models.list_records(limit=500)
        by_id = {r["id"]: r for r in rows}
        a = by_id[self.rid_a]
        self.assertEqual(a["task_name"], "订单库全备")
        self.assertEqual(a["host_ip"], "192.168.220.150")
        self.assertEqual(a["db_type_display"], "MySQL")
        self.assertEqual(a["backup_type_display"], "全量")  # 保留下发，供导出使用
        b = by_id[self.rid_b]
        self.assertEqual(b["task_name"], "本地文件备份")
        self.assertEqual(b["host_ip"], "本地")  # 不被吞
        self.assertEqual(b["db_type_display"], "文件")
        self.assertEqual(b["backup_type_display"], "增量")

    def test_keyword_by_name(self):
        rows = models.list_records(keyword="订单库", limit=500)
        self.assertTrue(any(r["id"] == self.rid_a for r in rows))
        self.assertFalse(any(r["id"] == self.rid_b for r in rows))

    def test_keyword_by_host(self):
        rows = models.list_records(keyword="192.168.220.150", limit=500)
        self.assertTrue(any(r["id"] == self.rid_a for r in rows))


class TestListRestores(_SeedCase):
    def test_elements_joined(self):
        rows = models.list_restores(limit=200)
        rr = next(r for r in rows if r["id"] == self.rrid)
        self.assertEqual(rr["task_name"], "订单库全备")
        self.assertEqual(rr["host_ip"], "192.168.220.150")
        self.assertEqual(rr["db_type_display"], "MySQL")
        self.assertEqual(rr["backup_type_display"], "全量")

    def test_keyword_by_name(self):
        rows = models.list_restores(keyword="订单库", limit=200)
        self.assertTrue(any(r["id"] == self.rrid for r in rows))


# ============================================================
# v2：biz_label 三处下发
# ============================================================
class TestBizLabelInRows(_SeedCase):
    def test_decorate_emits_biz_label(self):
        legacy = models.get_task(self.tid_a)
        self.assertEqual(legacy["biz_label"], "订单库全备")   # R2 回退
        filled = models.get_task(self.tid_c)
        self.assertEqual(filled["biz_label"], "OA办公系统")   # 原始值优先

    def test_list_tasks_emits_biz_label(self):
        tasks = {t["id"]: t for t in models.list_tasks()}
        self.assertEqual(tasks[self.tid_c]["biz_label"], "OA办公系统")
        self.assertEqual(tasks[self.tid_a]["biz_label"], "订单库全备")

    def test_list_records_emits_biz_label(self):
        by_id = {r["id"]: r for r in models.list_records(limit=500)}
        self.assertEqual(by_id[self.rid_a]["biz_label"], "订单库全备")
        self.assertEqual(by_id[self.rid_c]["biz_label"], "OA办公系统")
        # 任务名与业务系统解耦：task_name 保持原值不被覆盖
        self.assertEqual(by_id[self.rid_c]["task_name"], "phase2-demo-mysql")

    def test_list_restores_emits_biz_label(self):
        by_id = {r["id"]: r for r in models.list_restores(limit=200)}
        self.assertEqual(by_id[self.rrid]["biz_label"], "订单库全备")
        self.assertEqual(by_id[self.rrid_c]["biz_label"], "OA办公系统")

    def test_biz_label_never_empty(self):
        for r in models.list_records(limit=500):
            self.assertTrue(r.get("biz_label"), f"记录 {r['id']} 的 biz_label 为空")
        for r in models.list_restores(limit=200):
            self.assertTrue(r.get("biz_label"), f"恢复 {r['id']} 的 biz_label 为空")


# ============================================================
# v2：三字段并集搜索
# ============================================================
class TestKeywordThreeFields(_SeedCase):
    def test_records_hit_by_biz_system(self):
        rows = models.list_records(keyword="OA办公", limit=500)
        self.assertTrue(any(r["id"] == self.rid_c for r in rows))

    def test_records_hit_by_old_name(self):
        # 刻意保留的超集行为：记得旧任务名仍能搜到，命中项展示业务系统名
        rows = models.list_records(keyword="phase2", limit=500)
        hit = next(r for r in rows if r["id"] == self.rid_c)
        self.assertEqual(hit["biz_label"], "OA办公系统")

    def test_records_hit_by_host(self):
        rows = models.list_records(keyword="10.20.30.40", limit=500)
        self.assertTrue(any(r["id"] == self.rid_c for r in rows))

    def test_records_no_false_positive(self):
        rows = models.list_records(keyword="绝不存在的关键字xyz", limit=500)
        self.assertEqual(rows, [])

    def test_records_empty_keyword_returns_all(self):
        allrows = models.list_records(limit=500)
        ids = {r["id"] for r in allrows}
        for rid in (self.rid_a, self.rid_b, self.rid_c):
            self.assertIn(rid, ids)

    def test_restores_hit_by_biz_system(self):
        rows = models.list_restores(keyword="OA办公", limit=200)
        self.assertTrue(any(r["id"] == self.rrid_c for r in rows))

    def test_restores_hit_by_old_name(self):
        rows = models.list_restores(keyword="phase2", limit=200)
        self.assertTrue(any(r["id"] == self.rrid_c for r in rows))

    def test_restores_hit_by_host(self):
        rows = models.list_restores(keyword="10.20.30.40", limit=200)
        self.assertTrue(any(r["id"] == self.rrid_c for r in rows))

    def test_null_biz_system_not_matched_wrongly(self):
        # NULL LIKE '%kw%' 为假值，不得让 biz_system 为空的行误命中
        rows = models.list_records(keyword="OA办公", limit=500)
        self.assertFalse(any(r["id"] == self.rid_a for r in rows))


# ============================================================
# v2：/api/records/enriched 契约
# ============================================================
class TestEnrichedContract(_SeedCase):
    def _fetch(self):
        with _FLASK_APP.test_request_context("/api/records/enriched"):
            resp = restore_bp.list_records_enriched.__wrapped__()
            return resp.get_json()

    def test_every_item_has_biz_label_and_task_id(self):
        data = self._fetch()
        self.assertTrue(data, "enriched 返回空，无法验证契约")
        for item in data:
            self.assertIn("biz_label", item)
            self.assertIn("task_id", item)
            self.assertTrue(item["biz_label"], f"记录 {item['id']} biz_label 为空")

    def test_biz_label_values(self):
        by_id = {i["id"]: i for i in self._fetch()}
        self.assertEqual(by_id[self.rid_a]["biz_label"], "订单库全备")
        self.assertEqual(by_id[self.rid_c]["biz_label"], "OA办公系统")

    def test_task_id_matches_source_task(self):
        by_id = {i["id"]: i for i in self._fetch()}
        self.assertEqual(by_id[self.rid_c]["task_id"], self.tid_c)


# ============================================================
# v2：导出表头对齐（D4）
# ============================================================
class TestExportHeaderAlignment(_SeedCase):
    EXPECTED_HEADERS = ["ID", "任务ID", "业务系统", "类型", "备份方式", "开始时间",
                        "完成时间", "耗时(s)", "状态", "大小", "路径", "校验和", "备注"]

    def _export_rows(self):
        with _FLASK_APP.test_request_context("/api/records/export?format=csv"):
            resp = records_bp.export_records.__wrapped__()
            text = resp.get_data().decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_header_is_13_columns(self):
        rows = self._export_rows()
        header = next(r for r in rows if r and r[0] == "ID")
        self.assertEqual(len(header), 13)
        self.assertEqual(header, self.EXPECTED_HEADERS)

    def test_biz_system_at_index_2_and_backup_type_kept_at_4(self):
        rows = self._export_rows()
        header = next(r for r in rows if r and r[0] == "ID")
        self.assertEqual(header[2], "业务系统")
        self.assertEqual(header[4], "备份方式")  # D4：导出保留备份方式

    def test_data_rows_align_with_header(self):
        rows = self._export_rows()
        idx = next(i for i, r in enumerate(rows) if r and r[0] == "ID")
        header = rows[idx]
        data_rows = [r for r in rows[idx + 1:] if r]
        self.assertTrue(data_rows, "导出无数据行，无法验证对齐")
        for r in data_rows:
            self.assertEqual(len(r), len(header),
                             f"数据行列数 {len(r)} != 表头 {len(header)}: {r}")

    def test_biz_label_value_in_data_row(self):
        rows = self._export_rows()
        idx = next(i for i, r in enumerate(rows) if r and r[0] == "ID")
        data_rows = [r for r in rows[idx + 1:] if r]
        by_id = {r[0]: r for r in data_rows}
        self.assertEqual(by_id[str(self.rid_c)][2], "OA办公系统")
        self.assertEqual(by_id[str(self.rid_a)][2], "订单库全备")  # R2 回退


# ============================================================
# v2：写入通道校验
# ============================================================
class TestValidateBizSystem(unittest.TestCase):
    def test_required_rejects_empty(self):
        self.assertEqual(tasks_bp._validate_biz_system(None), "业务系统为必填")
        self.assertEqual(tasks_bp._validate_biz_system(""), "业务系统为必填")
        self.assertEqual(tasks_bp._validate_biz_system("   "), "业务系统为必填")

    def test_optional_allows_empty(self):
        self.assertIsNone(tasks_bp._validate_biz_system(None, required=False))
        self.assertIsNone(tasks_bp._validate_biz_system("  ", required=False))

    def test_length_boundary(self):
        self.assertIsNone(tasks_bp._validate_biz_system("好" * 64))
        self.assertIsNotNone(tasks_bp._validate_biz_system("好" * 65))
        # 长度按字符数计（中文按 1 计，设计 §10 A2）
        self.assertIsNone(tasks_bp._validate_biz_system("x" * 64))
        self.assertIsNotNone(tasks_bp._validate_biz_system("x" * 65))

    def test_valid_value(self):
        self.assertIsNone(tasks_bp._validate_biz_system("OA 办公系统"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
