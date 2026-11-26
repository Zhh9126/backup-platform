# -*- coding: utf-8 -*-
"""WAL 流式接收客户端的**版本择优**与英文报错中文化（2026-11-26 回归）。

现场症状（实时任务 418 / PG 16.9 源库）::

    pg_receivewal: error: incompatible server version 16.9;
    client does not support streaming from server versions newer than 14.12

根因：平台侧 pg_receivewal 只有 14.12（/pgdb/pgsql/bin），而旧版
``_resolve_client()`` 按目录模板顺序取"第一个命中"——永远拿到 14.12，
哪怕平台上另有更高版本客户端；且英文报错直接糊在 error.log 里，现场看不懂。

修复三件套：
  1. 族优先 + 同族内版本择优（族顺序保证金仓仍用 sys_receivewal）；
  2. LD_LIBRARY_PATH 跟随所选客户端自身的 libpq（避免 ABI 串味）；
  3. 客户端 stderr 英文报错 → 中文可执行提示。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_pgwal_ver/meta.db")
os.environ.setdefault("LOG_DIR", "/tmp/aidbm_test_pgwal_ver/logs")

from core.cdc.pg_wal import PostgresWALDaemon  # noqa: E402


class _FakeClient:
    """模拟一个客户端可执行文件（--version 输出可指定）。"""

    def __init__(self, path: str, version: str):
        self.path = path
        self.version = version

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __call__(self, cmd, **kw):
        raise AssertionError("不应真正执行客户端")


class PgWalClientVersionTest(unittest.TestCase):

    def setUp(self):
        self._cache = dict(PostgresWALDaemon._CLIENT_VERSION_CACHE)

    def tearDown(self):
        PostgresWALDaemon._CLIENT_VERSION_CACHE.clear()
        PostgresWALDaemon._CLIENT_VERSION_CACHE.update(self._cache)

    # ------------------------------------------------------------------
    # 1. 版本探测
    # ------------------------------------------------------------------
    def test_client_version_parses_pg_output(self):
        import subprocess as _sp

        def fake_run(cmd, **kw):
            class _R:
                stdout = "pg_receivewal (PostgreSQL) 16.9\n"
                stderr = ""
            return _R()

        orig = _sp.run
        _sp.run = fake_run
        try:
            PostgresWALDaemon._CLIENT_VERSION_CACHE.clear()
            ver = PostgresWALDaemon._client_version("/nonexistent/pg_receivewal")
        finally:
            _sp.run = orig
        self.assertEqual(ver, (16, 9))

    def test_client_version_unknown_returns_zero(self):
        PostgresWALDaemon._CLIENT_VERSION_CACHE.clear()
        ver = PostgresWALDaemon._client_version("/definitely/not/exist")
        self.assertEqual(ver, (0, 0))

    # ------------------------------------------------------------------
    # 2. 选取规则：族优先 + 同族版本择优
    # ------------------------------------------------------------------
    def test_resolve_prefers_family_order_over_higher_version(self):
        """金仓必须用 sys_receivewal，哪怕 PG 的 pg_receivewal 版本更高。"""
        from core.cdc.kingbase_wal import KingbaseWALDaemon

        cands = ["/opt/kb/bin/sys_receivewal", "/pgdb/pgsql/bin/pg_receivewal"]
        versions = {"/opt/kb/bin/sys_receivewal": (9, 0),
                    "/pgdb/pgsql/bin/pg_receivewal": (14, 12)}

        cls = KingbaseWALDaemon
        orig_cands = cls._client_candidates
        orig_ver = cls._client_version

        def fake_cands(c):
            return list(cands)

        def fake_ver(c, path):
            return versions.get(path, (0, 0))

        cls._client_candidates = classmethod(fake_cands)
        cls._client_version = classmethod(fake_ver)
        try:
            name, reason = cls._resolve_client()
        finally:
            cls._client_candidates = orig_cands
            cls._client_version = orig_ver
        self.assertEqual(name, "/opt/kb/bin/sys_receivewal")
        self.assertEqual(reason, "")

    def test_resolve_picks_highest_version_within_family(self):
        """同族多套安装时取主版本最高的一套（14.12 vs 16.9 → 选 16.9）。"""
        cands = ["/pgdb/pgsql/bin/pg_receivewal", "/opt/database/bin/pg_receivewal"]
        versions = {"/pgdb/pgsql/bin/pg_receivewal": (14, 12),
                    "/opt/database/bin/pg_receivewal": (16, 9)}

        cls = PostgresWALDaemon
        orig_cands = cls._client_candidates
        orig_ver = cls._client_version

        cls._client_candidates = classmethod(lambda c: list(cands))
        cls._client_version = classmethod(lambda c, p: versions.get(p, (0, 0)))
        try:
            name, reason = cls._resolve_client()
        finally:
            cls._client_candidates = orig_cands
            cls._client_version = orig_ver
        self.assertEqual(name, "/opt/database/bin/pg_receivewal")
        self.assertEqual(reason, "")

    def test_resolve_missing_client_reason(self):
        cls = PostgresWALDaemon
        orig_cands = cls._client_candidates
        cls._client_candidates = classmethod(lambda c: [])
        try:
            name, reason = cls._resolve_client()
        finally:
            cls._client_candidates = orig_cands
        self.assertEqual(name, "")
        self.assertIn("缺少客户端工具", reason)

    # ------------------------------------------------------------------
    # 3. libpq 目录跟随客户端
    # ------------------------------------------------------------------
    def test_client_lib_dir_follows_client_path(self):
        d = PostgresWALDaemon._client_lib_dir("/pgdb/pgsql/bin/pg_receivewal")
        self.assertEqual(d, "/pgdb/pgsql/lib")

    def test_client_lib_dir_fallback_when_unknown(self):
        self.assertEqual(PostgresWALDaemon._client_lib_dir(""), "/pgdb/pgsql/lib")

    def test_client_lib_dir_falls_back_when_sibling_lib_unloadable(self):
        """同目录树下的 libpq 存在但**加载不了**时，必须回落平台自带 libpq。

        现场教训：把 PG16 的 libpq（需 GLIBC_2.25）拷到 CentOS7 平台，
        目录与文件都在，但客户端根本起不来；而 PG16 的 pg_receivewal 配
        平台 libpq 14 完全可用。
        """
        import tempfile
        base = tempfile.mkdtemp(prefix="pgwal_lib_")
        bindir = os.path.join(base, "bin")
        libdir = os.path.join(base, "lib")
        os.makedirs(bindir)
        os.makedirs(libdir)
        client = os.path.join(bindir, "pg_receivewal")
        with open(client, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 1\n")   # 故意失败：模拟 ABI 不兼容
        os.chmod(client, 0o755)
        with open(os.path.join(libdir, "libpq.so.5"), "w", encoding="utf-8") as fh:
            fh.write("not a real library\n")
        PostgresWALDaemon._CLIENT_LIB_CACHE.pop(client, None)
        try:
            got = PostgresWALDaemon._client_lib_dir(client)
        finally:
            PostgresWALDaemon._CLIENT_LIB_CACHE.pop(client, None)
        self.assertEqual(got, "/pgdb/pgsql/lib")

    def test_client_lib_dir_accepts_loadable_sibling_lib(self):
        """同目录树下的 libpq 可加载时，优先用它（版本对齐）。"""
        import tempfile
        base = tempfile.mkdtemp(prefix="pgwal_lib2_")
        bindir = os.path.join(base, "bin")
        libdir = os.path.join(base, "lib")
        os.makedirs(bindir)
        os.makedirs(libdir)
        client = os.path.join(bindir, "pg_receivewal")
        with open(client, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho 'pg_receivewal (PostgreSQL) 16.9'\n")
        os.chmod(client, 0o755)
        with open(os.path.join(libdir, "libpq.so.5"), "w", encoding="utf-8") as fh:
            fh.write("stub\n")
        PostgresWALDaemon._CLIENT_LIB_CACHE.pop(client, None)
        try:
            got = PostgresWALDaemon._client_lib_dir(client)
        finally:
            PostgresWALDaemon._CLIENT_LIB_CACHE.pop(client, None)
        self.assertEqual(got, libdir)

    # ------------------------------------------------------------------
    # 4. 英文报错中文化
    # ------------------------------------------------------------------
    def test_translate_incompatible_server_version(self):
        raw = ("pg_receivewal: error: incompatible server version 16.9; "
               "client does not support streaming from server versions newer than 14.12")
        out = PostgresWALDaemon._translate_stderr(raw)
        self.assertIn("16.9", out)
        self.assertIn("14.12", out)
        self.assertIn("平台侧", out)

    def test_translate_scram_libpq_too_old(self):
        raw = ("pg_receivewal: error: SCRAM authentication requires libpq "
               "version 10 or above")
        out = PostgresWALDaemon._translate_stderr(raw)
        self.assertIn("libpq", out)
        self.assertIn("平台侧", out)

    def test_translate_unknown_passthrough(self):
        raw = "pg_receivewal: error: something-else"
        self.assertEqual(PostgresWALDaemon._translate_stderr(raw), raw)

    def test_translate_empty(self):
        self.assertEqual(PostgresWALDaemon._translate_stderr(""), "")


if __name__ == "__main__":
    unittest.main()
