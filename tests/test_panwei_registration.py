# -*- coding: utf-8 -*-
"""磐维（PanWeiDB）类型注册契约测试。

仿 test_neo4j_registration.py：验证 panwei 在引擎注册表、config 三表、
PG 族通道（remote_dump/logical_full/dump_format/native_conn/probe）中
完整注册，且关键行为继承自 openGauss（gs_* 工具族、-W 显式密码、
库名位置参数）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
os.environ.setdefault("BACKUP_ROOT", tempfile.mkdtemp(prefix="bp_panwei_test_"))
os.environ.setdefault("BP_META_DB", os.path.join(
    os.environ["BACKUP_ROOT"], "meta.db"))


class TestPanweiRegistration(unittest.TestCase):
    """panwei 注册一致性：注册表 / config / PG 族通道。"""

    def test_01_engine_registered(self):
        from core.engines import (ENGINE_REGISTRY, ENGINE_DISPLAY,
                                  supported_types, PanweiEngine)
        self.assertIs(ENGINE_REGISTRY.get("panwei"), PanweiEngine)
        self.assertIn("panwei", supported_types())
        self.assertIn("panwei", ENGINE_DISPLAY)
        self.assertEqual(ENGINE_DISPLAY["panwei"], "PanWeiDB（磐维）")

    def test_02_config_tables(self):
        import config
        self.assertEqual(config.DEFAULT_PORTS.get("panwei"), 12800)
        self.assertIn("panwei", config.DB_DISPLAY_NAMES)

    def test_03_engine_meta(self):
        from core.engines import engine_meta_map
        meta = engine_meta_map().get("panwei") or {}
        self.assertEqual(meta.get("display_name"), "PanWeiDB（磐维）")
        self.assertEqual(meta.get("default_port"), 12800)
        # 诚实展示：磐维未注册同步插件
        self.assertFalse(meta.get("supports_sync"))
        self.assertTrue(meta.get("supports_full_instance"))
        self.assertIn("physical", meta.get("backup_modes") or [])

    def test_04_get_engine_instance(self):
        from core.engines import get_engine
        task = {"id": -1, "db_type": "panwei", "name": "t", "host": "127.0.0.1",
                "port": 12800, "username": "omm", "password": "",
                "db_name": "postgres", "extra_options": {}}
        eng = get_engine("panwei", task, os.environ["BACKUP_ROOT"])
        self.assertEqual(eng.db_type, "panwei")
        # openGauss 系行为继承：gs_* 工具族 / 库名位置参数 / -W 显式密码
        self.assertTrue(getattr(eng, "_dump_dbname_positional"))
        self.assertTrue(getattr(eng, "_pass_via_w"))
        self.assertEqual(getattr(eng, "_phys_tool"), "gs_basebackup")
        self.assertEqual(getattr(eng, "_dump_db_type"), "panwei")

    def test_05_pg_family_channels(self):
        from core import remote_dump, logical_full, dump_format
        cfg = remote_dump._PG_FAMILY_TOOLING.get("panwei") or {}
        self.assertTrue(cfg.get("use_w_password"))
        self.assertTrue(cfg.get("dbname_positional"))
        self.assertTrue(cfg.get("set_lib_path"))
        self.assertEqual(cfg.get("dump_tool"), "gs_dump")
        self.assertEqual(cfg.get("check_user"), "omm")
        self.assertIn("panwei", logical_full.PG_FAMILY)
        self.assertIn("panwei", logical_full.SYSTEM_DBS)
        self.assertIn("panwei", logical_full.TOOLING)
        self.assertIn("panwei", dump_format._PG_FAMILY)
        fmts = dump_format.supported_formats("panwei")
        self.assertTrue(fmts)
        self.assertEqual(dump_format.supported_formats("panwei"),
                         dump_format.supported_formats("opengauss"))

    def test_06_native_conn_and_probe(self):
        from core import native_conn, probe
        self.assertIn("panwei", native_conn.NATIVE_CONFIG)
        self.assertIn("panwei", native_conn.NATIVE_DB_TYPES)
        self.assertEqual(native_conn.DEFAULT_PORTS.get("panwei"), 12800)
        self.assertIn("panwei", probe._PROBES)

    def test_07_dispatch_paths(self):
        """remote_dump 分发/物理备份 -W 分支覆盖 panwei（源码级契约）。"""
        import inspect
        from core import remote_dump, cross_host
        src = inspect.getsource(remote_dump)
        self.assertIn('("opengauss", "panwei")', src)
        csrc = inspect.getsource(cross_host)
        self.assertIn('("opengauss", "panwei")', csrc)

    def test_08_meta_api_includes_panwei(self):
        """/api/meta 的 db_types 含 panwei（前端下拉数据源）。"""
        from api.system import build_meta
        meta = build_meta()
        self.assertIn("panwei", meta.get("db_types") or [])
        self.assertEqual((meta.get("display_names") or {}).get("panwei"),
                         "PanWeiDB（磐维）")
        # sync_types 不含 panwei（未注册同步插件，诚实）
        self.assertNotIn("panwei", meta.get("sync_types") or [])


if __name__ == "__main__":
    unittest.main()
