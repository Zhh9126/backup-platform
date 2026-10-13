# -*- coding: utf-8 -*-
"""数据库 OS 用户标准变量（db_os_user）单测。

背景：客户环境里数据库软件的安装属主用户可能与默认不同（如 Oracle 不叫
oracle、达梦不叫 dmdba、openGauss 不叫 omm）。此前 `su - oracle` /
`su - dmdba` 等写死在各引擎中，换环境即失效。现收敛为标准变量：

    任务 extra_options.db_os_user > ssh_hosts.db_os_user > 引擎
    tool_check_user 声明 > 库型默认（DB_OS_USER_DEFAULTS）

本文件同时做静态契约回归：引擎源码中不允许再出现写死的
`su - oracle` / `su - dmdba` / `su - omm`（注释除外）。
"""
import os
import sys
import unittest

# 与其他测试隔离：先固化环境再 import core（同目录测试模块会互相覆盖 os.environ）
_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
_TMP_DIR = os.path.join(_HERE, ".tmp_db_os_user")
os.makedirs(_TMP_DIR, exist_ok=True)
os.environ["BACKUP_ROOT"] = os.path.join(_TMP_DIR, "backups")
os.environ.setdefault("META_DB_PATH", os.path.join(_TMP_DIR, "meta.db"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db  # noqa: E402
db.init_schema()
from core.remote_dump import resolve_db_os_user, DB_OS_USER_DEFAULTS  # noqa: E402


class TestResolveDbOsUser(unittest.TestCase):
    def test_defaults_by_db_type(self):
        self.assertEqual(resolve_db_os_user(None, None, db_type="oracle"), "oracle")
        self.assertEqual(resolve_db_os_user(None, None, db_type="dameng"), "dmdba")
        self.assertEqual(resolve_db_os_user(None, None, db_type="opengauss"), "omm")
        self.assertEqual(resolve_db_os_user(None, None, db_type="panwei"), "omm")
        self.assertEqual(resolve_db_os_user(None, None, db_type="kingbase"), "kingbase")
        self.assertEqual(resolve_db_os_user(None, None, db_type="postgresql"), "postgres")

    def test_unknown_db_type_falls_back_root(self):
        self.assertEqual(resolve_db_os_user(None, None, db_type="mysql"),
                         DB_OS_USER_DEFAULTS.get("mysql", "root"))
        self.assertEqual(resolve_db_os_user(None, None, db_type=""), "root")
        self.assertEqual(resolve_db_os_user(None, None, db_type="weird_db"), "root")

    def test_host_level_overrides_default(self):
        host = {"db_os_user": "oraapp"}
        self.assertEqual(resolve_db_os_user(None, host, db_type="oracle"), "oraapp")

    def test_task_level_overrides_host_and_default(self):
        task = {"db_type": "oracle", "extra_options": {"db_os_user": "oratask"}}
        host = {"db_os_user": "orahost"}
        self.assertEqual(resolve_db_os_user(task, host), "oratask")
        # extra_options 为 JSON 字符串时同样生效
        task2 = {"db_type": "dameng",
                 "extra_options": '{"db_os_user": "dmtask"}'}
        self.assertEqual(resolve_db_os_user(task2, {"db_os_user": "dmhost"}),
                         "dmtask")

    def test_engine_attr_used_when_no_task_host_override(self):
        class _E:
            tool_check_user = "customuser"
        self.assertEqual(resolve_db_os_user(None, {}, db_type="oracle",
                                            engine=_E()), "customuser")
        # 主机级覆盖仍然优先于引擎声明
        self.assertEqual(resolve_db_os_user(None, {"db_os_user": "h"},
                                            db_type="oracle", engine=_E()), "h")
        # 任务级最高
        t = {"extra_options": {"db_os_user": "t"}}
        self.assertEqual(resolve_db_os_user(t, {"db_os_user": "h"},
                                            db_type="oracle", engine=_E()), "t")

    def test_task_db_type_inferred_from_task(self):
        self.assertEqual(resolve_db_os_user({"db_type": "opengauss"}, None), "omm")

    def test_invalid_extra_json_is_ignored(self):
        task = {"db_type": "oracle", "extra_options": "{bad json"}
        self.assertEqual(resolve_db_os_user(task, None), "oracle")


class TestHostFieldPersistence(unittest.TestCase):
    def test_ssh_hosts_roundtrip(self):
        hid = db.execute(
            "INSERT INTO ssh_hosts (name, host_key, hostname, port, username,"
            " password, auth_type, os_type, db_os_user, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t-osuser", f"t-osuser-{id(self)}@127.0.0.1:22", "127.0.0.1",
             22, "root", "", "password", "linux", "oraapp",
             db.now_iso(), db.now_iso()))
        try:
            from core import ssh_hosts
            h = ssh_hosts.get_host(hid)
            self.assertEqual(h.get("db_os_user"), "oraapp")
            ssh_hosts.update_host(hid, {"db_os_user": "oraapp2"})
            self.assertEqual(ssh_hosts.get_host(hid).get("db_os_user"),
                             "oraapp2")
        finally:
            db.execute("DELETE FROM ssh_hosts WHERE id=?", (hid,))

    def test_column_in_schema(self):
        cols = {r["name"] for r in db.query(
            "SELECT name FROM pragma_table_info('ssh_hosts')")}
        self.assertIn("db_os_user", cols)


class TestEngineIntegration(unittest.TestCase):
    def _engine(self, db_type, task):
        from core import engines
        return engines.ENGINE_REGISTRY[db_type](
            task, os.path.join(_TMP_DIR, "storage"))

    def test_oracle_su_user_via_host_override(self):
        eng = self._engine("oracle", {"id": 1, "db_type": "oracle",
                                      "name": "t", "host": "h", "port": 1521})
        self.assertEqual(eng._resolve_check_user({"db_os_user": "myora"}),
                         "myora")
        self.assertEqual(eng._resolve_check_user({}), "oracle")

    def test_dameng_su_user_via_task_override(self):
        eng = self._engine("dameng", {"id": 1, "db_type": "dameng",
                                      "name": "t", "host": "h", "port": 5236,
                                      "extra_options": {"db_os_user": "dmg"}})
        self.assertEqual(eng._resolve_check_user({}), "dmg")


class TestNoHardcodedSuUser(unittest.TestCase):
    """静态契约：引擎/跨主机/克隆源码不得再写死 su 用户（注释行除外）。"""

    RULES = [
        ("core/engines/oracle.py", "su - oracle"),
        ("core/engines/dameng.py", "su - dmdba"),
        ("core/engines/opengauss.py", "su - omm"),
        ("core/cross_host.py", "su - oracle"),
        ("core/cross_host.py", "su - dmdba"),
        ("core/restore_extras_clone.py", "su - oracle"),
    ]

    def test_no_hardcoded_su_user_in_code(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad = []
        for rel, needle in self.RULES:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    s = line.strip()
                    if needle in s and not s.startswith("#") \
                            and not s.lstrip("- ").startswith(needle):
                        bad.append(f"{rel}:{i}: {s.strip()[:80]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
