# -*- coding: utf-8 -*-
"""恢复点安全检测（backup_anomaly）单元测试。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import gzip
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

_TMP = tempfile.mkdtemp(prefix="anomaly_test_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")

import core.db as db  # noqa: E402
db.init_schema()
from core.backup_anomaly import check_record, check_record_by_id, sample_entropy  # noqa: E402


def _mk(path, size=0, random_bytes=False):
    with open(path, "wb") as f:
        if random_bytes:
            f.write(os.urandom(size))
        else:
            f.write(b"x" * size)
    return path


class TestEntropy(unittest.TestCase):
    def test_random_full_entropy(self):
        p = _mk(os.path.join(_TMP, "rnd.bin"), 200_000, random_bytes=True)
        self.assertGreater(sample_entropy(p), 7.95)

    def test_plain_low_entropy(self):
        p = _mk(os.path.join(_TMP, "plain.bin"), 200_000)
        self.assertLess(sample_entropy(p), 0.1)


class TestCheckRecord(unittest.TestCase):
    def _task(self):
        return db.execute(
            "INSERT INTO backup_tasks (name, db_type, host, port, username, "
            "password, db_name, backup_mode, created_at, updated_at) "
            "VALUES ('anomaly-t','mysql','h',3306,'u',?,'d','logical',?,?)",
            (db.encrypt_secret("p"), db.now_iso(), db.now_iso()))

    def _rec(self, path):
        tid = self._task()
        return db.execute(
            "INSERT INTO backup_records (task_id, backup_type, status, "
            "backup_path, size_bytes, started_at, finished_at) "
            "VALUES (?, 'full', 'success', ?, 100000, ?, ?)",
            (tid, path, db.now_iso(), db.now_iso()))

    def test_gzip_artifact_clean(self):
        p = os.path.join(_TMP, "bk1.tar.gz")
        with gzip.open(p, "wb") as g:
            g.write(b"-- INSERT INTO t VALUES (1)\n" * 4096)
        rid = self._rec(p)
        res = check_record_by_id(rid)
        self.assertEqual(res["level"], "clean", json.dumps(res, ensure_ascii=False))
        items = {c["item"]: c["level"] for c in res["checks"]}
        self.assertEqual(items["structure"], "clean")
        self.assertEqual(items["entropy"], "unknown")  # 压缩豁免

    def test_randomized_artifact_suspect(self):
        p = _mk(os.path.join(_TMP, "bk2.sql"), 300_000, random_bytes=True)
        rid = self._rec(p)
        res = check_record_by_id(rid)
        levels = {c["item"]: c["level"] for c in res["checks"]}
        self.assertEqual(levels["entropy"], "suspect")
        self.assertEqual(res["level"], "suspect")

    def test_volume_drift_suspect(self):
        # 同任务先建 4 条 5MB 基线记录，当前记录 0.1MB → 体积突变
        tid = self._task()
        base_p = _mk(os.path.join(_TMP, "base1.bin"), 5_000_000)
        for i in range(4):
            db.execute(
                "INSERT INTO backup_records (task_id, backup_type, status, "
                "backup_path, size_bytes, started_at, finished_at) "
                "VALUES (?, 'full', 'success', ?, 5000000, ?, ?)",
                (tid, base_p, db.now_iso(), db.now_iso()))
        p = _mk(os.path.join(_TMP, "shrunk.sql"), 100_000)
        rid = db.execute(
            "INSERT INTO backup_records (task_id, backup_type, status, "
            "backup_path, size_bytes, started_at, finished_at) "
            "VALUES (?, 'full', 'success', ?, 100000, ?, ?)",
            (tid, p, db.now_iso(), db.now_iso()))
        res = check_record_by_id(rid)
        items = {c["item"]: c["level"] for c in res["checks"]}
        self.assertEqual(items["volume"], "suspect")

    def test_missing_artifact_unknown(self):
        rid = self._rec(os.path.join(_TMP, "ghost_not_exist.sql"))
        res = check_record_by_id(rid)
        self.assertEqual(res["level"], "unknown")

    def test_suspect_writes_audit_log(self):
        p = _mk(os.path.join(_TMP, "bk3.sql"), 300_000, random_bytes=True)
        rid = self._rec(p)
        n0 = db.query_one("SELECT COUNT(*) AS n FROM system_logs WHERE source='backup_anomaly'")
        check_record_by_id(rid)
        n1 = db.query_one("SELECT COUNT(*) AS n FROM system_logs WHERE source='backup_anomaly'")
        self.assertGreater((n1["n"] if n1 else 0), (n0["n"] if n0 else 0))


class TestApiContract(unittest.TestCase):
    def test_route_registered(self):
        import inspect
        from api import records as R
        src = inspect.getsource(R)
        self.assertIn("/records/<int:record_id>/security-check", src)
        self.assertIn("check_record_by_id", src)


if __name__ == "__main__":
    unittest.main()
