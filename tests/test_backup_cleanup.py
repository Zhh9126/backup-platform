# -*- coding: utf-8 -*-
"""备份产物定期清理（core.backup_cleanup）+ 组合任务编辑修复的回归自测。

A. 定期清理引擎（真实文件 / 真实 SQLite，不用任何模拟数据）：
   1. 按保留天数判定过期（超过 retention_days 的成功备份进入清理清单）。
   2. 按保留份数判定过期（超过 retention_count 的第 N 份进入清理清单）。
   3. keep_min 兜底：每个任务最近 N 份成功备份无条件保留，清理清单永不含最新一份。
   4. 未配置保留策略（retention_days 与 retention_count 都为空/0）的任务不动。
   5. 受管备份目录之外的产物不被删除（只标记，防误删）。
   6. dry_run 不落任何改动（文件还在、状态不变）。
   7. 真实执行：产物文件被删，记录落 expired_cleanup 且 backup_path 清空，
      统计的 deleted_files / freed_bytes 与实际一致。
   8. 幽灵记录（产物文件已不在磁盘）只标记，不虚报可释放空间。

B. 组合 / 增量任务「保存点了没反应」根因回归（静态层，防止代码回退）：
   9. BKP.$ 对缺失元素返回的代理对象，querySelectorAll 必须返回数组，
      历史上 undefined.forEach 抛 TypeError 直接打断 openTaskModal。
   10. 混合调度回填必须走真实 DOM 的 getElementById（不用代理对象），
      且候选 ID 覆盖 HTML 里真实的 t_incremental_days / t_incremental_time。
   11. 打开编辑弹窗的入口必须包 try/catch + toast（失败可见，不静默）。
"""
import datetime
import os
import re
import sys
import tempfile
import unittest

# ---------------- 0. 隔离运行：临时元数据库 + 临时备份根目录 ----------------
_TMP = os.path.join(tempfile.mkdtemp(prefix="bp_cleanup_test_"), "work")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
_BACKUP_ROOT = os.path.join(_TMP, "backups")
os.makedirs(_BACKUP_ROOT, exist_ok=True)
config.BACKUP_ROOT = _BACKUP_ROOT
db.init_schema()                                # noqa: E402

import pytest                                   # noqa: E402
import core.models as models                    # noqa: E402
from core import backup_cleanup as bc           # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cleanup_config():
    """每个用例前把运行时 config 指回本模块固化的临时库/备份根。

    config.META_DB_PATH / BACKUP_ROOT 是运行时读取的模块属性，pytest 同进程
    收集多个测试模块后会被后导入者改写；不改产品代码，只消除跨模块污染。
    """
    config.META_DB_PATH = _MODULE_DB
    config.BACKUP_ROOT = _BACKUP_ROOT
    yield


def _mk_task(name, retention_days=7, retention_count=0):
    return models.create_task({
        "name": name, "biz_system": "清理自测", "db_type": "mysql",
        "host": "127.0.0.1", "port": 3306, "username": "root",
        "password": "x", "db_name": "x", "backup_type": "full",
        "backup_mode": "logical", "schedule_type": "none",
        "retention_days": retention_days, "retention_count": retention_count,
        "enabled": 0,
    })


def _mk_record(task_id, days_ago, size=1024, subdir="t1", root=None):
    """造一条成功备份记录 + 一个真实产物文件，返回 (path, record_id)。"""
    d = os.path.join(root or _BACKUP_ROOT, subdir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "rec_%s.sql" % str(days_ago).replace(".", "_"))
    with open(path, "wb") as f:
        f.write(b"x" * size)
    ts = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    rid = db.execute(
        "INSERT INTO backup_records (task_id, status, backup_path, size_bytes, "
        "finished_at, started_at) VALUES (?,?,?,?,?,?)",
        (task_id, "success", path, size, ts, ts))
    return path, rid


class TestBackupCleanup(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM backup_records")
        db.execute("DELETE FROM backup_tasks")

    # -------- 1/3. 按天判定 + keep_min 兜底 --------
    def test_plan_by_days_and_count(self):
        tid = _mk_task("按天+按份数", retention_days=3, retention_count=2)
        for d in (20, 10, 5, 1, 0):
            _mk_record(tid, d, size=512)
        p = bc.build_plan(task_id=tid, keep_min=1)[0]
        self.assertEqual(p["total"], 5)
        self.assertEqual(p["clean_count"], 3, "应按天数清掉 3 条并保住最新 1 条")
        self.assertEqual(p["keep"], 2)
        # keep_min 加大 → 少清（兜底更强）
        self.assertEqual(bc.build_plan(task_id=tid, keep_min=3)[0]["clean_count"], 2)

    def test_keep_min_never_kills_last_backup(self):
        tid = _mk_task("只剩一份", retention_days=1, retention_count=1)
        _mk_record(tid, 30, size=256)
        self.assertEqual(bc.build_plan(task_id=tid, keep_min=1), [],
                         "keep_min 必须保住最后一个可用备份")

    # -------- 2. 只配份数 --------
    def test_plan_by_count_only(self):
        tid = _mk_task("只配份数", retention_days=None, retention_count=2)
        for d in (9, 8, 7, 6):
            _mk_record(tid, d, size=128, subdir="t_cnt")
        p = bc.build_plan(task_id=tid, keep_min=1)[0]
        self.assertEqual(p["clean_count"], 2, "超出的第 3/4 份应被清理")
        self.assertEqual(p["keep"], 2)

    # -------- 4. 无保留策略不动 --------
    def test_skip_task_without_retention_policy(self):
        tid = _mk_task("无保留策略", retention_days=0, retention_count=0)
        _mk_record(tid, 400, size=2048, subdir="t_nopolicy")
        self.assertEqual(bc.build_plan(task_id=tid), [],
                         "未配置保留策略的任务不得被自动清理")

    # -------- 5. 受管目录外不删 --------
    def test_outside_managed_root_not_deleted(self):
        outside = os.path.join(_TMP, "outside_root")
        os.makedirs(outside, exist_ok=True)
        tid = _mk_task("越界产物", retention_days=1)
        path, _ = _mk_record(tid, 30, size=64, subdir="", root=outside)
        self.assertEqual(bc._remove_artifact(path), (False, "outside"))
        self.assertTrue(os.path.isfile(path), "受管目录外的文件必须原样保留")
        self.assertFalse(bc._is_managed_path(path))

    # -------- 6. dry_run --------
    def test_dry_run_changes_nothing(self):
        tid = _mk_task("dry-run", retention_days=1)
        paths = [_mk_record(tid, d, size=100, subdir="t_dry")[0] for d in (9, 0)]
        rep = bc.run_cleanup(task_id=tid, dry_run=True)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["records"], 1)
        self.assertTrue(all(os.path.isfile(p) for p in paths), "dry-run 不得删文件")
        newest = db.query_one("SELECT status FROM backup_records WHERE task_id=? "
                              "ORDER BY id DESC", (tid,))
        self.assertEqual(newest["status"], "success", "dry-run 不得改记录状态")

    # -------- 7. 真实执行 --------
    def test_real_cleanup_deletes_and_marks(self):
        tid = _mk_task("真实清理", retention_days=2)
        old_path, old_id = _mk_record(tid, 15, size=2048, subdir="t_real")
        new_path, _ = _mk_record(tid, 0, size=2048, subdir="t_real")
        rep = bc.run_cleanup(task_id=tid, dry_run=False)
        self.assertFalse(os.path.isfile(old_path), "过期产物必须被真实删除")
        self.assertTrue(os.path.isfile(new_path), "最新备份必须保留")
        self.assertEqual(rep["deleted_files"], 1)
        self.assertEqual(rep["freed_bytes"], 2048)
        old = db.query_one("SELECT * FROM backup_records WHERE id=?", (old_id,))
        self.assertEqual(old["status"], bc.STATUS_EXPIRED)
        self.assertEqual(old["backup_path"], "", "不得留下指向已删文件的路径")
        self.assertIn("定期清理", old["message"] or "", "必须留下可审计的原因")
        cfg = bc.get_config()
        self.assertEqual(cfg["last_summary"].get("deleted_files"), 1)
        self.assertTrue(cfg["last_run_at"])

    # -------- 8. 幽灵记录不虚报空间 --------
    def test_missing_artifact_counted_as_ghost(self):
        tid = _mk_task("幽灵记录", retention_days=1)
        _mk_record(tid, 20, size=4096, subdir="t_ghost")
        for r in db.query("SELECT backup_path FROM backup_records WHERE task_id=?", (tid,)):
            if r["backup_path"] and os.path.isfile(r["backup_path"]):
                os.remove(r["backup_path"])
        p = bc.build_plan(task_id=tid, keep_min=0)[0]
        self.assertEqual(p["free_bytes"], 0, "文件已不在时不得虚报可释放空间")
        self.assertEqual(p["ghost_bytes"], 4096)


# ---------------- B. 组合任务「编辑无响应」根因的静态回归保护 ----------------
class TestMixedEditUiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = open(os.path.join(PROJECT_ROOT, "static", "js", "app.js"),
                          encoding="utf-8").read()
        cls.core_js = open(os.path.join(PROJECT_ROOT, "static", "js", "bkp-core.js"),
                           encoding="utf-8").read()
        cls.tasks_html = open(os.path.join(PROJECT_ROOT, "templates", "tasks.html"),
                              encoding="utf-8").read()

    def test_missing_element_proxy_returns_array(self):
        idx = self.core_js.index("BKP.$")
        self.assertIn("return new Proxy", self.core_js[idx:idx + 400])
        seg = self.core_js[idx:self.core_js.index("return t[p]", idx)]
        self.assertIn('p === "querySelectorAll"', seg)
        self.assertIn("return function(){ return []; }", seg)

    def test_mixed_fill_uses_real_dom_lookup(self):
        seg = self.app_js[self.app_js.index("组合调度：按天勾选"):][:3200]
        self.assertIn("function _firstRealEl", seg)
        self.assertIn("document.getElementById", seg)
        self.assertNotIn('$(prefix + "_days")', seg,
                         "不得再用 BKP.$ 拼前缀（缺失元素返回代理对象）")

    def test_candidate_ids_match_html(self):
        self.assertIn('replace(/_inc$/, "_incremental")', self.app_js)
        html_ids = set(re.findall(r'id="([^"]+)"', self.tasks_html))
        for need in ("t_full_days", "t_incremental_days", "t_incremental_time",
                     "t_full_time", "t_ssh_host", "cleanupBtn"):
            self.assertIn(need, html_ids, "HTML 缺少元素 #%s" % need)

    def test_open_task_modal_has_guard(self):
        seg = self.app_js[self.app_js.index("function openTaskModal"):][:1400]
        self.assertIn("try {", seg)
        self.assertIn("catch (e)", seg)
        self.assertIn("toast(", seg, "打开失败必须给用户可见提示")
        self.assertIn("_openTaskModalInner", seg)

    def test_edit_task_reports_failure(self):
        seg = self.app_js[self.app_js.index("window.editTask"):][:700]
        self.assertIn("catch (e)", seg)
        self.assertIn("toast(", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
