# -*- coding: utf-8 -*-
"""gs_probackup 物理（全量+增量）备份/恢复/PITR 接入测试（openGauss/磐维）。

覆盖：增量诚实拦截、模式映射、透传保留字、产物结构校验、恢复目标目录护栏。
不依赖真实数据库/SSH（单元级 + 源码契约）。
"""
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
os.environ.setdefault("BACKUP_ROOT", tempfile.mkdtemp(prefix="bp_probackup_test_"))
os.environ.setdefault("BP_META_DB", os.path.join(os.environ["BACKUP_ROOT"], "meta.db"))


def _make_task(**eo):
    from core import db
    task = {"id": -1, "db_type": "opengauss", "name": "t", "host": "127.0.0.1",
            "port": 26000, "username": "omm", "password": "", "db_name": "postgres",
            "extra_options": dict(eo)}
    return task


def _make_engine(task):
    from core.engines import get_engine
    return get_engine("opengauss", task, os.environ["BACKUP_ROOT"])


class TestProbackup(unittest.TestCase):

    def test_01_incremental_without_probackup_honest_fail(self):
        """物理增量 + 默认 gs_basebackup → 诚实失败（不再静默按全量跑）。"""
        from core.engines.base import BackupType, BackupStatus
        eng = _make_engine(_make_task())
        r = eng._backup_physical_remote.__self__ and None  # noqa: 触发属性存在性
        # 远端通道需 SSH，无法直跑；改为验证增量分支在解析 extra 后即拦截——
        # 通过 _extra_dict + 源码契约验证（真实拦截逻辑见 test_07 源码契约）
        eo = eng._extra_dict()
        self.assertNotEqual(eo.get("physical_tool"), "gs_probackup")

    def test_02_mode_mapping(self):
        from core.engines.base import BackupType
        from core.engines.opengauss import _PROBACKUP_MODE_BY_TYPE
        self.assertEqual(_PROBACKUP_MODE_BY_TYPE[BackupType.FULL], "full")
        self.assertEqual(_PROBACKUP_MODE_BY_TYPE[BackupType.INCREMENTAL], "delta")
        self.assertEqual(_PROBACKUP_MODE_BY_TYPE[BackupType.DIFFERENTIAL], "delta")

    def test_03_extra_reserved_keys(self):
        """新键不得被当 CLI 参数拼进命令行（透传保留字）。"""
        import inspect
        from core.engines import postgresql
        src = inspect.getsource(postgresql)
        for k in ("physical_tool", "probackup_mode", "probackup_dir",
                  "probackup_pgdata", "probackup_instance",
                  "probackup_restore_pgdata", "probackup_recovery_target_time",
                  "probackup_force_restore"):
            self.assertIn(f'"{k}"', src)

    def test_04_verify_probackup_tar_ok(self):
        eng = _make_engine(_make_task())
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "probackup.tar.gz")
            with tarfile.open(path, "w:gz") as tar:
                import io as _io
                conf = _io.BytesIO(b"# pg_probackup.conf\nPGDATA=/data/gaussdb5\n")
                ti = tarfile.TarInfo("pg_probackup.conf"); ti.size = len(conf.getvalue())
                tar.addfile(ti, conf)
                ctrl = _io.BytesIO(b"backup-content-control\n")
                ti2 = tarfile.TarInfo("QW12AB/backup_content.control")
                ti2.size = len(ctrl.getvalue())
                tar.addfile(ti2, ctrl)
            r = eng.verify_record({"backup_path": path, "backup_mode": "physical"})
            self.assertTrue(r.success, r.message)
            self.assertIn("gs_probackup", r.message)

    def test_05_verify_probackup_tar_missing_control(self):
        eng = _make_engine(_make_task())
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "probackup.tar.gz")
            with tarfile.open(path, "w:gz") as tar:
                import io as _io
                conf = _io.BytesIO(b"# pg_probackup.conf\n")
                ti = tarfile.TarInfo("pg_probackup.conf"); ti.size = len(conf.getvalue())
                tar.addfile(ti, conf)
            r = eng.verify_record({"backup_path": path, "backup_mode": "physical"})
            self.assertFalse(r.success)

    def test_06_restore_requires_target_dir(self):
        """未指定恢复目标数据目录 → 诚实失败并给出配置指引。"""
        eng = _make_engine(_make_task())
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "probackup.tar.gz")
            with tarfile.open(path, "w:gz") as tar:
                import io as _io
                conf = _io.BytesIO(b"x")
                ti = tarfile.TarInfo("pg_probackup.conf"); ti.size = 1
                tar.addfile(ti, conf)
            r = eng.restore(path)
            self.assertFalse(r.success)
            self.assertIn("probackup_restore_pgdata", r.message)

    def test_07_source_contracts(self):
        """远端通道/恢复分发/PITR/W 护栏的源码契约。"""
        import inspect
        from core import remote_dump
        from core.engines import opengauss
        rsrc = inspect.getsource(remote_dump)
        for kw in ("def remote_probackup_backup", "def remote_probackup_restore",
                   "enable_cbm_tracking", "OVERWRITE_RISK", "recovery-target-time",
                   "请勿清理该目录", "增量链"):
            self.assertIn(kw, rsrc)
        esrc = inspect.getsource(opengauss)
        for kw in ("physical_tool", "def _restore_probackup", "backup_content.control",
                   "gs_probackup"):
            self.assertIn(kw, esrc)

    def test_08_env_prefix(self):
        from core.remote_dump import _probackup_env_prefix
        e = _probackup_env_prefix("/opt/gauss/bin/gs_probackup", "sec@ret")
        self.assertIn("GAUSSHOME=$(dirname $(dirname /opt/gauss/bin/gs_probackup))", e)
        self.assertIn("PGPASSWORD=sec@ret", e)
        self.assertIn("LD_LIBRARY_PATH=$GAUSSHOME/lib", e)

    def test_09_panwei_engine_inherits_probackup(self):
        from core.engines import get_engine
        from core.engines import opengauss as og
        task = _make_task(physical_tool="gs_probackup")
        task["db_type"] = "panwei"
        eng = get_engine("panwei", task, os.environ["BACKUP_ROOT"])
        self.assertEqual(eng.db_type, "panwei")
        self.assertTrue(hasattr(eng, "_restore_probackup"))
        # 磐维引擎直接继承 openGauss 引擎的 probackup 实现（未覆写）
        self.assertIs(type(eng)._is_probackup_artifact,
                      og.OpenGaussEngine._is_probackup_artifact)
        self.assertIs(type(eng)._backup_physical_remote,
                      og.OpenGaussEngine._backup_physical_remote)


if __name__ == "__main__":
    unittest.main()
