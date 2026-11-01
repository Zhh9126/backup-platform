# -*- coding: utf-8 -*-
"""远端暂存目录（FIX-2026-001 延伸）：任务级可配 + 空间预算检查。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_stage/meta.db")
os.makedirs("/tmp/aidbm_test_stage", exist_ok=True)

from core import remote_dump as rd  # noqa: E402


class TestTaskStageDir(unittest.TestCase):
    def _task(self, extra):
        return {"id": 1, "extra_options": extra}

    def test_default(self):
        self.assertTrue(rd.task_stage_dir(self._task("{}")).endswith("bk_stage"))

    def test_extra_dict(self):
        self.assertEqual(rd.task_stage_dir(
            self._task({"backup_stage_dir": "/data/st"})), "/data/st")

    def test_extra_json_str(self):
        self.assertEqual(rd.task_stage_dir(
            self._task('{"backup_stage_dir":"/data/st"}')), "/data/st")

    def test_fixed_path_uses_task_stage(self):
        p = rd.fixed_remote_path(self._task('{"backup_stage_dir":"/data/st"}'),
                                 "mysql", "full.tar")
        self.assertTrue(p.startswith("/data/st/fixed/"))
        self.assertIn("t1_mysql_full.tar", p)


class TestEnsureStageOk(unittest.TestCase):
    """df/stat 解析与预算判定（fake _ssh_exec_pipe）。"""

    def setUp(self):
        import core.db as db
        db.init_schema()
        from core import models
        self.tid = models.create_task({
            "name": "e2e_stage", "db_type": "mysql", "host": "127.0.0.1",
            "username": "root", "password": "", "db_name": "x",
            "backup_type": "full", "schedule_type": "manual",
            "retention_days": 7, "enabled": 0})
        # 清掉本测试的历史成功记录，避免互相污染
        db.execute("DELETE FROM backup_records WHERE task_id=?", (self.tid,))

    def _seed(self, size):
        import core.db as db
        db.execute(
            "INSERT INTO backup_records (task_id, backup_type, status, "
            "size_bytes, backup_path, started_at, finished_at) VALUES "
            "(?, 'full', 'success', ?, '/tmp/x.tar.gz', datetime('now'), "
            "datetime('now'))", (self.tid, size))

    def _fake_exec(self, free_bytes, dev_root="42", dev_dir="42"):
        df_line = (f"/dev/sda1 100000000000 10000000000 {free_bytes} "
                   f"11% /")
        def fake(client, cmd, timeout=30, **kw):
            return (df_line.encode() + b"\n" + f"{dev_root}\n{dev_dir}\n".encode(),
                    b"", 0)
        return fake

    def _client(self):
        return object()   # fake exec 不使用

    def test_ok_writable_other_fs(self):
        self._seed(0)   # 无历史大小 → 不做预算拦截
        with mock.patch("core.engines.file._ssh_exec_pipe",
                        self._fake_exec(500 * 1024 ** 3, "42", "43")):
            w = rd.ensure_remote_stage_ok(self._client(), {"id": self.tid},
                                          "/data/st", label="t")
        self.assertEqual(w, "")

    def test_same_fs_warns(self):
        self._seed(0)
        with mock.patch("core.engines.file._ssh_exec_pipe",
                        self._fake_exec(500 * 1024 ** 3, "42", "42")):
            w = rd.ensure_remote_stage_ok(self._client(), {"id": self.tid},
                                          "/data/st")
        self.assertIn("根分区", w)

    def test_low_budget_fails(self):
        # 上次成功备份 10GB → 预算 15GB，空闲 10GB → 拦截
        self._seed(10 * 1024 ** 3)
        with mock.patch("core.engines.file._ssh_exec_pipe",
                        self._fake_exec(10 * 1024 ** 3, "42", "43")):
            with self.assertRaises(RuntimeError) as ctx:
                rd.ensure_remote_stage_ok(self._client(), {"id": self.tid},
                                          "/data/st")
        self.assertIn("空闲空间不足", str(ctx.exception))
        self.assertIn("backup_stage_dir", str(ctx.exception))

    def test_low_free_absolute_warn(self):
        self._seed(0)
        with mock.patch("core.engines.file._ssh_exec_pipe",
                        self._fake_exec(500 * 1024 ** 3, "42", "43")):
            w = rd.ensure_remote_stage_ok(self._client(), {"id": self.tid},
                                          "/data/st")
        self.assertEqual(w, "")


if __name__ == "__main__":
    unittest.main()
