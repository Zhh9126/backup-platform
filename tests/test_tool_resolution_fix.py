# -*- coding: utf-8 -*-
"""工具解析三修复的防回退单测（2026-10-13 四库型深度 E2E 暴露）。

修复点：
1. base._ensure_local_clients_on_path 兜底失效——_LOCAL_TOOL_FALLBACK_GLOBS
   只扫 /opt 两级，漏掉 /pgdb/pgsql/bin 等自定义根目录，本机物理备份预检
   误报"缺少 pg_basebackup"（_resolve_local_tool 本可找到）；
2. remote_dump.remote_has_tool 带 check_user 时只做 su 探测、失败即 False，
   不落 find/glob 兜底——金仓等工具不在属主用户 profile PATH 时预检必挂；
3. dameng._restore_logical_remote 硬编码裸 `dimp`——root 登录 shell 无
   dimp → rc=127（解析出的 dimp_bin 未被使用）。
"""
import os
import stat
import sys
import unittest

os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HERE, ".tmp_tool_res")
os.makedirs(_TMP, exist_ok=True)
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ.setdefault("META_DB_PATH", os.path.join(_TMP, "meta.db"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db  # noqa: E402
db.init_schema()
from core.engines import get_engine  # noqa: E402
from core import remote_dump  # noqa: E402


class TestEnsureLocalClientsFallback(unittest.TestCase):
    def test_falls_back_to_resolve_local_tool(self):
        """PATH 与窄 glob 都找不到时，_resolve_local_tool 兜底必须生效。"""
        fake_bin = os.path.join(_TMP, "fakepgbin")
        os.makedirs(fake_bin, exist_ok=True)
        tool = os.path.join(fake_bin, "pg_basebackup")
        with open(tool, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)

        eng = get_engine("postgresql",
                         {"id": 1, "db_type": "postgresql", "name": "t",
                          "host": "127.0.0.1", "port": 5785,
                          "username": "postgres", "password": "x",
                          "db_name": "d"},
                         os.path.join(_TMP, "backups"))
        # 任务级 tool_path 指向假 bin 目录 → _resolve_local_tool 第一优先级命中
        eng.task = dict(eng.task)
        eng.task["extra_options"] = {"tool_path": fake_bin}
        still, hits = eng._ensure_local_clients_on_path(["pg_basebackup"])
        self.assertEqual(still, [])
        self.assertTrue(any(h.startswith("pg_basebackup@") for h in hits))

    def test_real_pgdb_path_resolvable(self):
        """回归本机实况：/pgdb/pgsql/bin 的 pg_basebackup 必须可解析
        （预检此前因窄 glob 误报缺失）。环境无该目录时跳过。"""
        if not os.path.isfile("/pgdb/pgsql/bin/pg_basebackup"):
            self.skipTest("本机无 /pgdb/pgsql/bin")
        eng = get_engine("postgresql",
                         {"id": 1, "db_type": "postgresql", "name": "t",
                          "host": "127.0.0.1", "port": 5785,
                          "username": "postgres", "password": "x",
                          "db_name": "d"},
                         os.path.join(_TMP, "backups"))
        still, _ = eng._ensure_local_clients_on_path(["pg_basebackup"])
        self.assertEqual(still, [])


class TestRemoteHasToolFallback(unittest.TestCase):
    def test_source_uses_resolve_remote_tool_fallback(self):
        """remote_has_tool 的 check_user 分支在 su 探测失败后必须复用
        resolve_remote_tool 完整解析（防回退静态契约）。"""
        import inspect
        src = inspect.getsource(remote_dump.remote_has_tool)
        self.assertIn("resolve_remote_tool", src)


class TestDamengRestoreUsesResolvedDimp(unittest.TestCase):
    def test_remote_restore_cmd_uses_dimp_bin(self):
        """达梦远程恢复命令必须使用解析出的 dimp 绝对路径（防回退：
        曾硬编码裸 `dimp` 导致 root shell rc=127）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "core", "engines", "dameng.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("cmd_parts = [dimp_bin or \"dimp\"", src)
        # 本机 dimp 分支同样要带 TABLE_EXISTS_ACTION=REPLACE（存量表覆盖语义）
        self.assertGreaterEqual(src.count("TABLE_EXISTS_ACTION=REPLACE"), 2)
        self.assertNotIn('"IGNORE=Y"', src)  # IGNORE=Y 与 REPLACE 互斥，禁止回加


if __name__ == "__main__":
    unittest.main()
