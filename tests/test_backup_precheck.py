# -*- coding: utf-8 -*-
"""备份/恢复预检（backup_precheck）单元测试。"""
import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

_TMP = tempfile.mkdtemp(prefix="precheck_test_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.path.join(_TMP, "backups"), exist_ok=True)

import core.db as db  # noqa: E402
db.init_schema()
from core.backup_precheck import (  # noqa: E402
    precheck_backup, precheck_restore, _summarize,
)


def _mk_task(db_type, **kw):
    data = dict(name="precheck-t", db_type=db_type, host="127.0.0.1", port=1,
                username="u", password="p", db_name="d", backup_mode="logical",
                enabled=0)
    data.update(kw)
    tid = db.execute(
        "INSERT INTO backup_tasks ({}) VALUES ({})".format(
            ",".join(data.keys()), ",".join(["?"] * len(data))),
        tuple(data.values()))
    return tid


class TestBackupPrecheck(unittest.TestCase):
    def test_unreachable_source_and_tools(self):
        """不可达源：源库连通不通过（warn），工具按本机实际情况判定。"""
        tid = _mk_task("mysql", host="127.0.0.1", port=1)  # 不可达端口
        from core import models
        t = models.get_task(tid, include_secret=True)
        res = precheck_backup(t)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertIn(items["source_connectivity"], ("warn", "fail"))
        # 工具检查只反映本机 PATH 是否有所需客户端（平台机可能装了
        # mysqldump 也可能没装），不做硬断言，只要求结果存在
        self.assertIn(items["backup_tools"], ("pass", "fail"))

    def test_missing_tools_fail(self):
        """本机没有 oracle 客户端：backup_tools 应阻断（fail）。"""
        import shutil
        if shutil.which("impdp") or shutil.which("expdp"):
            self.skipTest("本机装了 oracle 客户端，无法构造缺失场景")
        tid = _mk_task("oracle", host="127.0.0.1", port=1521)
        from core import models
        t = models.get_task(tid, include_secret=True)
        res = precheck_backup(t)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertEqual(items["backup_tools"], "fail")

    def test_missing_config_fail(self):
        tid = _mk_task("mysql", host="", port=None, username="")
        from core import models
        t = models.get_task(tid, include_secret=True)
        res = precheck_backup(t)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertEqual(items["task_config"], "fail")
        self.assertFalse(res["ok"])

    def test_disk_space_present(self):
        tid = _mk_task("mysql", host="127.0.0.1", port=1)
        from core import models
        t = models.get_task(tid, include_secret=True)
        res = precheck_backup(t)
        self.assertIn("disk_space", {i["item"] for i in res["items"]})


class TestRestorePrecheck(unittest.TestCase):
    def _mk_record(self, path, checksum=None):
        tid = _mk_task("mysql")
        rid = db.execute(
            "INSERT INTO backup_records (task_id, backup_type, status, "
            "backup_path, size_bytes, checksum, started_at, finished_at) "
            "VALUES (?, 'full', 'success', ?, 1000, ?, ?, ?)",
            (tid, path, checksum, db.now_iso(), db.now_iso()))
        return db.query_one("SELECT * FROM backup_records WHERE id=?", (rid,))

    def test_gzip_artifact_pass(self):
        p = os.path.join(_TMP, "bk.sql.gz")
        with gzip.open(p, "wb") as g:
            g.write(b"-- INSERT INTO t VALUES (1);\n" * 100)
        from core import db as _db
        rec = self._mk_record(p, checksum=_db.sha256_file(p))
        res = precheck_restore(rec)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertEqual(items["artifact"], "pass")
        self.assertEqual(items["integrity"], "pass")
        self.assertEqual(items["structure"], "pass")
        self.assertTrue(res["ok"], str(res))

    def test_bad_checksum_fail(self):
        p = os.path.join(_TMP, "bk2.sql.gz")
        with gzip.open(p, "wb") as g:
            g.write(b"data")
        rec = self._mk_record(p, checksum="0" * 64)
        res = precheck_restore(rec)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertEqual(items["integrity"], "fail")
        self.assertFalse(res["ok"])

    def test_missing_artifact_fail(self):
        rec = self._mk_record(os.path.join(_TMP, "ghost.bin"))
        res = precheck_restore(rec)
        self.assertEqual(res["level"], "fail")
        self.assertFalse(res["ok"])

    def test_empty_artifact_fail(self):
        p = os.path.join(_TMP, "empty.sql")
        open(p, "wb").close()
        rec = self._mk_record(p)
        res = precheck_restore(rec)
        self.assertEqual(res["level"], "fail")

    def test_unreachable_target_host(self):
        p = os.path.join(_TMP, "bk3.sql.gz")
        with gzip.open(p, "wb") as g:
            g.write(b"data")
        rec = self._mk_record(p)
        host = {"id": -1, "host_key": "u@203.0.113.99:22", "hostname": "203.0.113.99",
                "port": 22, "username": "u", "password": "x", "auth_type": "password"}
        res = precheck_restore(rec, target_host_info=host)
        items = {i["item"]: i["level"] for i in res["items"]}
        self.assertEqual(items["target_channel"], "fail")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
