# -*- coding: utf-8 -*-
"""克隆范围回归：整库 / 整实例 / 物理归档识别 与库白名单解析（2026-11-14）。

背景：克隆服务定位是「整库 VDB 克隆」。此前把全实例多库 tar 当单个 SQL
灌进一个库，直接报 `ASCII '\0' appeared in the statement`；物理归档也会
被错误地当逻辑产物处理。这里锁定三条契约：
1. 产物范围识别（single / full_instance / physical）；
2. 库白名单解析（JSON / 逗号串 / 空=全部）；
3. 物理归档克隆必须被明确拒绝并给出可执行建议。
"""
import io
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

from core.clone_service import CloneService  # noqa: E402


def _make_tar(path, members: dict, compress=True):
    mode = "w:gz" if compress else "w"
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    return path


class TestArchiveScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clone_scope_")

    def test_single_logical_file(self):
        p = os.path.join(self.tmp, "x__full.sql.zst")
        open(p, "wb").write(b"somedata")
        self.assertEqual(CloneService._archive_scope(p), "single")

    def test_full_instance_archive(self):
        p = os.path.join(self.tmp, "full.tar.gz")
        _make_tar(p, {"manifest.json": "{\"databases\":[\"a\",\"b\"]}",
                      "dbs/a.sql": "CREATE TABLE a.t1(id int);",
                      "dbs/b.sql": "CREATE TABLE b.t1(id int);"})
        self.assertEqual(CloneService._archive_scope(p), "full_instance")

    def test_physical_xtrabackup_archive(self):
        p = os.path.join(self.tmp, "xb.tar.gz")
        _make_tar(p, {"xtrabackup_checkpoints": "backup_type = full-backuped",
                      "ibdata1": b"\x00\x01"})
        self.assertEqual(CloneService._archive_scope(p), "physical")

    def test_pg_base_archive_is_physical(self):
        p = os.path.join(self.tmp, "base.tar.gz")
        _make_tar(p, {"base.tar": b"\x00", "PG_VERSION": "16"})
        self.assertEqual(CloneService._archive_scope(p), "physical")


class TestParseDatabases(unittest.TestCase):
    def test_empty_means_all(self):
        self.assertEqual(CloneService._parse_databases(None), [])
        self.assertEqual(CloneService._parse_databases(""), [])

    def test_json_array(self):
        self.assertEqual(CloneService._parse_databases('["a","b"]'), ["a", "b"])

    def test_comma_string(self):
        self.assertEqual(CloneService._parse_databases("a, b，c"), ["a", "b", "c"])

    def test_list_passthrough(self):
        self.assertEqual(CloneService._parse_databases(["a", " b "]), ["a", "b"])


class TestPhysicalRejected(unittest.TestCase):
    def test_physical_archive_raises_with_hint(self):
        """物理归档走逻辑克隆必须抛错且给建议（不能静默灌进库）。"""
        tmp = tempfile.mkdtemp(prefix="clone_phys_")
        p = _make_tar(os.path.join(tmp, "xb.tar.gz"),
                      {"xtrabackup_checkpoints": "x", "ibdata1": b"\x00"})

        svc = CloneService.__new__(CloneService)   # 不初始化 logger/db
        import logging
        svc.logger = logging.getLogger("test.clone")
        from unittest import mock
        rec = {"id": 1, "db_type": "mysql", "backup_path": p, "task_id": 1,
               "backup_type": "full"}
        req = {"id": 9, "source_record_id": 1, "target_host": "127.0.0.1",
               "target_password": "", "note": ""}
        with mock.patch("core.clone_service.models.get_record", return_value=rec), \
             mock.patch.object(CloneService, "_parse_mode",
                               return_value=(("logical", ""), "")), \
             mock.patch("config.DEMO_MODE", "off"):
            with self.assertRaises(RuntimeError) as ctx:
                svc._launch_vdb(req)
        self.assertIn("物理备份归档", str(ctx.exception))
        self.assertIn("数据恢复", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
