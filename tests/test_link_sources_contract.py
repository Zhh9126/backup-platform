# -*- coding: utf-8 -*-
"""
B1 / G1 回归单测（UX 反馈 20260801 验收遗留缺陷）。

运行方式（系统 Python，DEMO_MODE=on）：
    python -m pytest tests/test_link_sources_contract.py -q
    python tests/test_link_sources_contract.py

覆盖 verification.md §4 两项缺陷的修复契约：
  B1（P0 · 数据源契约）
    1. GET /api/disaster-links/sources 返回设计 §2 D 约定的扁平 items 数组，
       且 items == sources.sync_task + sources.rt_task；
    2. 向后兼容：kinds / sources / total 三个旧键不得删除，total == len(items)；
    3. 每个数据源项都带非空 status（前端状态徽章直接读取）；
    4. rt 源项必带 rpo_sec 键：有实时运行态时为实际 RPO 秒数，
       无运行态时为 None（键存在但取值为空，不得省略键）；
    5. 端点仍受登录态保护（未登录 → 401）。
  G1（P2 · 守护 stopped 兜底提示）
    6. /rt-timeline 页面含常驻提示条 #rtStoppedHint 与「启动守护」按钮；
    7. app.js 的 rtSubmitCreate() 成功分支已探测守护状态并按 running 分支提示。

隔离范式：沿用 tests/test_ai_alert_taskdetail.py 的 tempfile.mkdtemp +
DEMO_MODE=on + META_DB_PATH 真实 SQLite 临时库（不 mock），并在本模块内把
config.META_DB_PATH 切到独立库文件，避免与同进程其他测试模块串扰。
"""

import os
import re
import sys
import shutil
import tempfile
import unittest

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="link_src_")
os.environ.setdefault("INSTANCE_DIR", os.path.join(_TMP, "instance"))
os.environ.setdefault("LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("BACKUP_ROOT", os.path.join(_TMP, "backups"))
os.environ.setdefault("META_DB_PATH", os.path.join(_TMP, "instance", "meta.db"))
os.environ["SCHEDULER_ENABLED"] = "false"
for _d in ("instance", "logs", "backups"):
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                    # noqa: E402
import core.db as db             # noqa: E402
import core.models as models     # noqa: E402

# 本模块独占的元数据库（与其他测试模块的 meta.db 物理隔离）
_MODULE_DB = os.path.join(_TMP, "instance", "link_sources.db")
_APP_JS = os.path.join(PROJECT_ROOT, "static", "js", "app.js")

# 设计 §2 D 约定：每个数据源项必须具备的公共键
COMMON_KEYS = {"kind", "id", "name", "primary_site", "dr_site", "db_type", "status"}
# 向后兼容：这三个旧键不得从响应体中删除
LEGACY_KEYS = {"kinds", "sources", "total"}


class _SourcesCase(unittest.TestCase):
    """公共基类：独占临时库 + 已登录的 Flask test_client。"""

    @classmethod
    def setUpClass(cls):
        cls._prev_db_path = config.META_DB_PATH
        config.META_DB_PATH = _MODULE_DB
        db.init_schema()
        from app import create_app          # 延迟导入：确保库路径已切换
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        config.META_DB_PATH = cls._prev_db_path

    def setUp(self):
        # 逐用例重置（外键顺序：先子后父）
        db.execute("DELETE FROM rt_capture_state")
        db.execute("DELETE FROM disaster_links")
        db.execute("DELETE FROM backup_records")
        db.execute("DELETE FROM backup_tasks")
        db.execute("DELETE FROM sync_tasks")
        self.client = self.app.test_client()
        self._login(self.client)

    # ---------------- 夹具工具 ----------------
    def _login(self, client) -> None:
        resp = client.post("/login",
                           json={"username": config.WEB_USERNAME,
                                 "password": config.WEB_PASSWORD},
                           content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    @staticmethod
    def _mk_sync_task(name: str = "北京→上海 订单库同步") -> int:
        return models.create_sync_task({
            "name": name, "source_type": "manual",
            "src_db_type": "mysql", "src_host": "10.10.0.5", "src_port": 3306,
            "src_db_name": "orders", "src_username": "root", "src_password": "p@ss",
            "tgt_db_type": "mysql", "tgt_host": "10.20.0.1", "tgt_port": 3306,
            "tgt_db_name": "orders", "tgt_username": "root", "tgt_password": "p@ss",
            "sync_mode": "full", "enabled": 1,
        })

    @staticmethod
    def _mk_rt_task(name: str = "核心交易库") -> int:
        task_id = models.create_task({
            "name": name, "db_type": "mysql", "host": "10.10.0.9", "port": 3306,
            "username": "root", "password": "", "db_name": "trade",
            "backup_type": "full", "schedule_type": "manual", "enabled": 1,
            "demo_only": 1, "rt_enabled": 1, "rt_mode": "db_cdc",
        })
        db.execute("UPDATE backup_tasks SET rt_enabled=1, rt_mode='db_cdc' WHERE id=?",
                   (int(task_id),))
        return int(task_id)

    def _get_sources(self) -> dict:
        resp = self.client.get("/api/disaster-links/sources")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, dict)
        self.assertTrue(body.get("ok"))
        return body


# ==================== B1：/sources 扁平 items 契约 ====================
class TestLinkSourcesContract(_SourcesCase):
    """验收 B1-1/2/3：扁平 items + 旧键兼容 + status 非空。"""

    def test_items_is_flat_array_of_all_sources(self):
        """items 必须为扁平数组，等于 sync 分组 + rt 分组的顺序拼接。"""
        self._mk_sync_task()
        self._mk_rt_task()
        body = self._get_sources()

        self.assertIn("items", body, "响应缺少设计 §2 D 约定的扁平 items 数组")
        items = body["items"]
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 2)

        groups = body.get("sources") or {}
        expected = list(groups.get("sync_task") or []) + list(groups.get("rt_task") or [])
        self.assertEqual(items, expected)
        self.assertEqual([i["kind"] for i in items], ["sync_task", "rt_task"])

    def test_legacy_keys_preserved(self):
        """kinds / sources / total 三个旧键保持不变，total 与 items 长度一致。"""
        self._mk_sync_task()
        self._mk_rt_task()
        body = self._get_sources()

        self.assertTrue(LEGACY_KEYS.issubset(set(body.keys())))
        self.assertEqual(body["total"], len(body["items"]))
        self.assertIn("sync_task", body["sources"])
        self.assertIn("rt_task", body["sources"])
        self.assertIn("manual", body["kinds"])

    def test_every_item_has_non_empty_status(self):
        """每项都带非空 status —— 否则前端状态徽章恒显示「-」。"""
        self._mk_sync_task()
        self._mk_rt_task()
        for item in self._get_sources()["items"]:
            self.assertTrue(COMMON_KEYS.issubset(set(item.keys())),
                            f"数据源项缺少必备键：{COMMON_KEYS - set(item.keys())}")
            self.assertTrue(str(item.get("status") or "").strip(),
                            f"数据源 {item.get('kind')}#{item.get('id')} 的 status 为空")
            # 旧字段保留，避免既有调用方回归
            self.assertIn("last_status", item)

    def test_empty_db_returns_empty_items(self):
        """无任何数据源时 items 为空数组（前端空态判定依赖该语义）。"""
        body = self._get_sources()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)

    def test_sources_requires_login(self):
        """端点仍受登录态保护：未登录 → 401。"""
        anon = self.app.test_client()
        resp = anon.get("/api/disaster-links/sources")
        self.assertEqual(resp.status_code, 401)


# ==================== B1：rt 源 rpo_sec 字段 ====================
class TestRtSourceRpoField(_SourcesCase):
    """验收 B1-4：rt 源必带 rpo_sec 键，取自实时运行态 rpo_actual_sec。"""

    def _rt_item(self) -> dict:
        rt_items = [i for i in self._get_sources()["items"] if i["kind"] == "rt_task"]
        self.assertEqual(len(rt_items), 1)
        return rt_items[0]

    def test_rpo_sec_from_rt_capture_state(self):
        """有实时运行态时，rpo_sec == rt_capture_state.rpo_actual_sec。"""
        task_id = self._mk_rt_task()
        models.upsert_rt_state(task_id, {"rpo_actual_sec": 42, "health": "green",
                                         "daemon_status": "running"})
        item = self._rt_item()
        self.assertIn("rpo_sec", item)
        self.assertEqual(item["rpo_sec"], 42)

    def test_rpo_sec_is_none_without_state(self):
        """无实时运行态时 rpo_sec 为 None，但键必须存在（不得省略）。"""
        self._mk_rt_task()
        item = self._rt_item()
        self.assertIn("rpo_sec", item)
        self.assertIsNone(item["rpo_sec"])

    def test_rt_status_falls_back_to_never(self):
        """rt 源从未运行时 status 兜底为 never，不返回空串。"""
        self._mk_rt_task()
        self.assertEqual(self._rt_item()["status"], "never")

    def test_rpo_sec_helper_tolerates_bad_values(self):
        """_rt_rpo_sec 对缺行 / 空值 / 非法值 / 负值一律降级为 None。"""
        from api.link import _rt_rpo_sec
        self.assertIsNone(_rt_rpo_sec(0, {}))
        self.assertIsNone(_rt_rpo_sec(0, {"rpo_actual_sec": None}))
        self.assertIsNone(_rt_rpo_sec(0, {"rpo_actual_sec": ""}))
        self.assertIsNone(_rt_rpo_sec(0, {"rpo_actual_sec": "abc"}))
        self.assertIsNone(_rt_rpo_sec(0, {"rpo_actual_sec": -1}))
        self.assertEqual(_rt_rpo_sec(0, {"rpo_actual_sec": 7}), 7)


# ==================== G1：守护 stopped 兜底提示 ====================
class TestDaemonStoppedHint(_SourcesCase):
    """验收 G1-6/7：模板提示条 + 创建成功后的守护态探测分支。"""

    def test_rt_timeline_page_has_stopped_hint(self):
        """/rt-timeline 含常驻提示条与「启动守护」按钮，且默认隐藏。"""
        resp = self.client.get("/rt-timeline")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('id="rtStoppedHint"', body)
        self.assertIn('id="rtStoppedHintStartBtn"', body)
        self.assertIn("守护进程未启动，实时保护暂不产生恢复点", body)
        self.assertIn("启动守护", body)
        hint = re.search(r'<div[^>]*id="rtStoppedHint"', body)
        self.assertIsNotNone(hint)
        self.assertIn("d-none", hint.group(0), "提示条应默认隐藏，由前端按守护态显形")

    def test_rt_submit_create_probes_daemon_status(self):
        """rtSubmitCreate() 成功分支必须探测 /api/rt/status 并按 running 分支提示。"""
        with open(_APP_JS, "r", encoding="utf-8") as fh:
            source = fh.read()
        start = source.find("async function rtSubmitCreate()")
        self.assertGreater(start, 0, "未找到 rtSubmitCreate()")
        end = source.find("async function rtLoadTimeline()", start)
        self.assertGreater(end, start)
        block = source[start:end]

        self.assertIn("rtProbeDaemonRunning()", block, "创建成功后未探测守护状态")
        self.assertIn("daemonRunning === false", block, "缺少 running=false 的降级分支")
        self.assertIn("守护进程未启动", block, "未给出「守护未启动」降级提示")
        self.assertIn('"warning"', block, "stopped 分支应降级为 warning 提示")
        self.assertIn("已开启实时保护，正在加载时间轴…", block, "running 分支应保持原成功提示")
        self.assertIn("rtSyncStoppedHint(", block, "未联动常驻提示条显形")

        # 探测函数确实打 /api/rt/status；启动按钮复用同一启动逻辑
        self.assertIn('api("GET", "/api/rt/status")', source)
        self.assertIn("async function rtStartDaemon()", source)
        self.assertIn('rtStoppedHintStartBtn', source)


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
