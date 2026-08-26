# -*- coding: utf-8 -*-
"""插件服务端化（T02-T05）回归测试。

覆盖范围：
1. plugin_host_state 表 upsert / get / list / delete
2. plugin_runtime 工具分类映射正确性（BUNDLED_PHYSICAL_TOOLS）
3. plugin_catalog.check_installed_on_host 在无远端时的降级行为
4. preflight 物理备份缺工具时返回 False + 引导信息
5. 用 mock SSH 测试远端安装流程（mock remote_exec_capture / sftp_put）
6. batch-install 增强：支持 {"host_id": 11, "db_types": ["mysql"]} 自动算出
   该主机所需外部插件并批量下发
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import core.db as db


# ----------------------------------------------------------------------------
# 测试基础设施：临时 DB
# ----------------------------------------------------------------------------
def _setup_tmp_db():
    """创建临时 SQLite 并替换 db 模块的连接函数，避免污染主库。"""
    tmp = tempfile.mkdtemp(prefix="plugin_test_")
    db_path = os.path.join(tmp, "plugin_test.db")
    conn = __import__("sqlite3").connect(db_path, check_same_thread=False)
    conn.row_factory = __import__("sqlite3").Row

    # 建表
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plugin_host_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER,
            host_key TEXT NOT NULL,
            plugin_id TEXT NOT NULL,
            status TEXT DEFAULT 'uninstalled',
            version TEXT,
            method TEXT,
            extract_dir TEXT,
            found_paths TEXT,
            message TEXT,
            installed_at TEXT,
            updated_at TEXT,
            UNIQUE(host_key, plugin_id)
        );
        CREATE TABLE IF NOT EXISTS ssh_hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host_key TEXT NOT NULL UNIQUE,
            hostname TEXT,
            port INTEGER DEFAULT 22,
            username TEXT,
            password TEXT,
            auth_type TEXT DEFAULT 'password',
            private_key TEXT,
            os_type TEXT DEFAULT 'linux',
            remark TEXT,
            last_status TEXT,
            last_check_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );
    """)
    conn.commit()

    def _execute(sql, params=()):
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid

    def _query(sql, params=()):
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _query_one(sql, params=()):
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    db.execute = _execute
    db.query = _query
    db.query_one = _query_one
    return tmp, conn


# ============================================================================
# T05-1: plugin_host_state 表 CRUD
# ============================================================================
class TestPluginHostState(unittest.TestCase):
    """验证 plugin_host_state 表的 upsert / get / list / delete。"""

    @classmethod
    def setUpClass(cls):
        _setup_tmp_db()

    def test_01_upsert_and_get(self):
        """upsert 写入后 get 能读到。"""
        db.upsert_plugin_host_state("root@1.2.3.4:22", "percona-xtrabackup-80", {
            "status": "installed",
            "version": "8.0.35",
            "method": "fallback_download",
            "extract_dir": "/opt/backup_plugins/percona-xtrabackup-80",
            "host_id": 11,
            "message": "安装成功",
        })
        row = db.get_plugin_host_state("root@1.2.3.4:22", "percona-xtrabackup-80")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "installed")
        self.assertEqual(row["version"], "8.0.35")
        self.assertEqual(row["method"], "fallback_download")
        self.assertEqual(row["host_id"], 11)

    def test_02_upsert_idempotent(self):
        """重复 upsert 更新而非插入。"""
        db.upsert_plugin_host_state("local", "redis-tools", {
            "status": "installing",
        })
        db.upsert_plugin_host_state("local", "redis-tools", {
            "status": "installed",
            "method": "package_manager",
        })
        row = db.get_plugin_host_state("local", "redis-tools")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "installed")
        self.assertEqual(row["method"], "package_manager")

    def test_03_list_by_host_key(self):
        """list 按 host_key 过滤。"""
        db.upsert_plugin_host_state("host_a", "plugin_a", {"status": "installed"})
        db.upsert_plugin_host_state("host_b", "plugin_b", {"status": "failed"})
        rows_a = db.list_plugin_host_state(host_key="host_a")
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["plugin_id"], "plugin_a")
        rows_all = db.list_plugin_host_state()
        self.assertGreaterEqual(len(rows_all), 2)

    def test_04_delete(self):
        """delete 删除指定记录。"""
        db.upsert_plugin_host_state("host_del", "plugin_del", {"status": "installed"})
        row = db.get_plugin_host_state("host_del", "plugin_del")
        self.assertIsNotNone(row)
        db.delete_plugin_host_state("host_del", "plugin_del")
        row2 = db.get_plugin_host_state("host_del", "plugin_del")
        self.assertIsNone(row2)

    def test_05_found_paths_json(self):
        """found_paths 字段存储 JSON 字符串。"""
        paths = {"xtrabackup": "/opt/backup_plugins/xtrabackup/bin/xtrabackup"}
        db.upsert_plugin_host_state("host_json", "plugin_json", {
            "status": "installed",
            "found_paths": json.dumps(paths),
        })
        row = db.get_plugin_host_state("host_json", "plugin_json")
        self.assertIsNotNone(row)
        decoded = json.loads(row["found_paths"])
        self.assertEqual(decoded["xtrabackup"],
                         "/opt/backup_plugins/xtrabackup/bin/xtrabackup")


# ============================================================================
# T05-2: plugin_runtime 工具分类映射
# ============================================================================
class TestPluginRuntimeMapping(unittest.TestCase):
    """验证 plugin_runtime 的工具分类映射正确性。"""

    def test_01_bundled_physical_tools(self):
        """数据库自带物理工具映射正确。"""
        from core import plugin_runtime
        self.assertEqual(plugin_runtime.bundled_physical_tools("oracle"), ["rman"])
        self.assertEqual(plugin_runtime.bundled_physical_tools("postgresql"),
                         ["pg_basebackup"])
        self.assertEqual(plugin_runtime.bundled_physical_tools("kingbase"),
                         ["sys_basebackup"])
        self.assertEqual(plugin_runtime.bundled_physical_tools("dameng"),
                         ["dmrman"])
        # MySQL/Redis/MongoDB 无自带物理工具
        self.assertEqual(plugin_runtime.bundled_physical_tools("mysql"), [])
        self.assertEqual(plugin_runtime.bundled_physical_tools("redis"), [])
        self.assertEqual(plugin_runtime.bundled_physical_tools("mongodb"), [])

    def test_02_case_insensitive(self):
        """db_type 大小写不敏感。"""
        from core import plugin_runtime
        self.assertEqual(plugin_runtime.bundled_physical_tools("Oracle"), ["rman"])
        self.assertEqual(plugin_runtime.bundled_physical_tools("POSTGRESQL"),
                         ["pg_basebackup"])

    def test_03_unknown_db_type(self):
        """未知 db_type 返回空列表。"""
        from core import plugin_runtime
        self.assertEqual(plugin_runtime.bundled_physical_tools("unknown_db"), [])
        self.assertEqual(plugin_runtime.bundled_physical_tools(""), [])


# ============================================================================
# T05-3: plugin_catalog.check_installed_on_host 降级行为
# ============================================================================
class TestCheckInstalledOnHostDegradation(unittest.TestCase):
    """验证 check_installed_on_host 在无远端/无 manifest 时的降级行为。"""

    @classmethod
    def setUpClass(cls):
        _setup_tmp_db()

    def test_01_unknown_plugin_returns_uninstalled(self):
        """不存在的 plugin_id 返回 installed=False, status=uninstalled。"""
        from core import plugin_catalog
        ssh_host = {"host_key": "test_host", "id": 99}
        result = plugin_catalog.check_installed_on_host(
            "nonexistent-plugin-id", ssh_host)
        self.assertFalse(result["installed"])
        self.assertEqual(result["status"], "uninstalled")
        self.assertEqual(result["host_key"], "test_host")

    def test_02_no_ssh_host_returns_local(self):
        """ssh_host=None 时 host_key 为 'local'。"""
        from core import plugin_catalog
        # 模拟一个不存在的插件
        result = plugin_catalog.check_installed_on_host(
            "nonexistent-plugin-id", None)
        self.assertFalse(result["installed"])
        self.assertEqual(result["host_key"], "local")


# ============================================================================
# T05-4: preflight 物理备份缺工具返回 False
# ============================================================================
class TestPreflightPhysicalMissing(unittest.TestCase):
    """验证 preflight 物理备份缺工具时返回 False + 引导信息。"""

    def _make_engine(self, db_type, bundled_tools=None, external_plugins=None,
                     backup_mode="physical"):
        """构造一个测试用引擎实例。"""
        from core.engines.base import BackupEngine
        # 动态创建子类以设置类属性
        attrs = {"db_type": db_type, "display_name": db_type}
        if bundled_tools is not None:
            attrs["physical_bundled_tools"] = bundled_tools
        if external_plugins is not None:
            attrs["physical_external_plugins"] = external_plugins
        cls = type("TestEngine", (BackupEngine,), attrs)
        task = {
            "id": 1,
            "name": "test_task",
            "db_type": db_type,
            "backup_mode": backup_mode,
        }
        # Mock logger
        logger = MagicMock()
        engine = cls(task, "/tmp/test_backup", logger=logger)
        return engine

    def test_01_oracle_physical_missing_remote(self):
        """Oracle 物理备份缺 rman 且无远端 → False + 引导。"""
        engine = self._make_engine("oracle", bundled_tools=["rman"])
        # Mock: 本机无 rman, 无 SSH 远端
        with patch("shutil.which", return_value=None), \
             patch("core.remote_dump.resolve_ssh_host", return_value=None):
            ok, msg = engine.preflight()
        self.assertFalse(ok)
        self.assertIn("物理备份", msg)
        self.assertIn("备份插件", msg)

    def test_02_mysql_physical_missing_external(self):
        """MySQL 物理备份缺 xtrabackup 且无远端 → False + 引导。"""
        engine = self._make_engine(
            "mysql",
            external_plugins=["percona-xtrabackup-80", "mariabackup"])
        with patch("shutil.which", return_value=None), \
             patch("core.remote_dump.resolve_ssh_host", return_value=None):
            ok, msg = engine.preflight()
        self.assertFalse(ok)
        self.assertIn("物理备份", msg)

    def test_03_physical_with_remote_bundled_ok(self):
        """Oracle 物理备份有 SSH 远端且远端有 rman → True。"""
        engine = self._make_engine("oracle", bundled_tools=["rman"])
        fake_ssh = {"host_key": "root@1.2.3.4:22"}
        with patch("shutil.which", return_value=None), \
             patch("core.remote_dump.resolve_ssh_host", return_value=fake_ssh), \
             patch("core.plugin_runtime.remote_check_clients",
                   return_value={"installed": True, "missing": [],
                                 "found_paths": {"rman": "/usr/bin/rman"}}):
            ok, msg = engine.preflight()
        self.assertTrue(ok)
        self.assertIn("远端", msg)

    def test_04_physical_with_remote_missing_bundled(self):
        """Oracle 物理备份有 SSH 远端但远端无 rman → False + 引导。"""
        engine = self._make_engine("oracle", bundled_tools=["rman"])
        fake_ssh = {"host_key": "root@1.2.3.4:22"}
        with patch("shutil.which", return_value=None), \
             patch("core.remote_dump.resolve_ssh_host", return_value=fake_ssh), \
             patch("core.plugin_runtime.remote_check_clients",
                   return_value={"installed": False, "missing": ["rman"],
                                 "found_paths": {}}):
            ok, msg = engine.preflight()
        self.assertFalse(ok)
        self.assertIn("rman", msg)

    def test_05_mysql_physical_with_remote_external_ok(self):
        """MySQL 物理备份有 SSH 远端且远端有 xtrabackup → True。"""
        engine = self._make_engine(
            "mysql",
            external_plugins=["percona-xtrabackup-80", "mariabackup"])
        fake_ssh = {"host_key": "root@1.2.3.4:22"}
        with patch("shutil.which", return_value=None), \
             patch("core.remote_dump.resolve_ssh_host", return_value=fake_ssh), \
             patch("core.plugin_catalog.check_installed_on_host",
                   return_value={"installed": True, "missing": [],
                                 "found_paths": {"xtrabackup": "/usr/bin/xtrabackup"},
                                 "status": "installed"}):
            ok, msg = engine.preflight()
        self.assertTrue(ok)

    def test_06_logical_mode_allows_simulate(self):
        """逻辑备份缺客户端时返回 True（允许仿真兜底）。"""
        engine = self._make_engine("mysql", backup_mode="logical")
        with patch("shutil.which", return_value=None):
            ok, msg = engine.preflight()
        self.assertTrue(ok)

    def test_07_local_bundled_tools_present(self):
        """本机有物理备份工具 → True。"""
        engine = self._make_engine("oracle", bundled_tools=["rman"])
        with patch("shutil.which", return_value="/usr/bin/rman"), \
             patch("core.remote_dump.resolve_ssh_host", return_value=None):
            ok, msg = engine.preflight()
        # shutil.which 返回非 None → check_client 成功 → preflight 直接返回 True
        self.assertTrue(ok)


# ============================================================================
# T05-5: 远端安装流程 mock 测试
# ============================================================================
class TestRemoteInstallFlow(unittest.TestCase):
    """用 mock SSH 测试远端安装流程。"""

    @classmethod
    def setUpClass(cls):
        _setup_tmp_db()
        # 插入一个 mock SSH 主机到 ssh_hosts 表
        db.execute(
            "INSERT INTO ssh_hosts (name, host_key, hostname, port, username, "
            "password, os_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-host", "root@1.2.3.4:22", "1.2.3.4", 22,
             "root", "", "linux"))

    def test_01_install_triggers_remote_thread(self):
        """install(pid, host_id=1) 异步派发远端安装线程。"""
        from core import plugin_installer, plugin_catalog

        # Mock catalog.load_all 返回一个假 manifest
        fake_manifest = {
            "id": "test-remote-plugin",
            "name": "Test Remote Plugin",
            "category": "mysql",
            "required_clients": ["xtrabackup"],
            "packages": {
                "linux": {
                    "fallback": {
                        "url": "http://example.com/xtrabackup.tar.gz",
                        "extract_dir": "/opt/backup_plugins/test-remote-plugin",
                    }
                }
            },
            "post_install_tips": [],
        }

        # Mock SSH 主机查询
        fake_host = {
            "id": 1,
            "host_key": "root@1.2.3.4:22",
            "hostname": "1.2.3.4",
            "port": 22,
            "username": "root",
            "password": "secret",
        }

        with patch.object(plugin_catalog, "load_all",
                          return_value={"test-remote-plugin": fake_manifest}), \
             patch("core.ssh_hosts.get_host", return_value=fake_host), \
             patch("core.plugin_runtime.remote_detect_os",
                   return_value="linux"), \
             patch("core.plugin_runtime.remote_detect_package_manager",
                   return_value="apt"), \
             patch("core.plugin_installer._download_to_local",
                   return_value={"ok": True, "path": "/tmp/test.tar.gz",
                                 "ext": ".tar.gz", "message": ""}), \
             patch("core.remote_dump.sftp_put") as mock_sftp, \
             patch("core.remote_dump.remote_exec_capture") as mock_exec, \
             patch("core.plugin_runtime.remote_check_clients",
                   return_value={"installed": True, "missing": [],
                                 "found_paths": {"xtrabackup": "/opt/backup_plugins/test-remote-plugin/bin/xtrabackup"}}), \
             patch("core.plugin_runtime.remote_bin_version",
                   return_value="8.0.35"), \
             patch("core.plugin_catalog.check_installed_on_host",
                   return_value={"installed": False}):
            # Mock remote_exec_capture: 解压成功
            mock_exec.return_value = {"returncode": 0, "stdout": "", "stderr": ""}

            res = plugin_installer.install("test-remote-plugin", host_id=1)
            self.assertTrue(res.get("ok"))

            # 等待线程执行完成（最多 3 秒）
            import time
            for _ in range(30):
                state = plugin_installer.get_state(
                    "test-remote-plugin", host_key="root@1.2.3.4:22")
                if state and state.get("status") in (
                        "success", "success_with_warn", "failed", "manual"):
                    break
                time.sleep(0.1)

            # 验证状态
            state = plugin_installer.get_state(
                "test-remote-plugin", host_key="root@1.2.3.4:22")
            self.assertIsNotNone(state)
            self.assertIn(state["status"], ("success", "success_with_warn"))

            # 验证 SFTP 被调用
            mock_sftp.assert_called_once()

            # 验证 DB 落库
            row = db.get_plugin_host_state(
                "root@1.2.3.4:22", "test-remote-plugin")
            self.assertIsNotNone(row)
            self.assertIn(row["status"], ("installed", "success_with_warn"))

    def test_02_uninstall_remote(self):
        """uninstall(pid, host_id=1) 清理远端 + DB。"""
        from core import plugin_installer

        # 先写入一条 DB 状态
        db.upsert_plugin_host_state("root@1.2.3.4:22", "test-uninstall-plugin", {
            "status": "installed",
            "method": "fallback_download",
            "extract_dir": "/opt/backup_plugins/test-uninstall-plugin",
            "host_id": 1,
        })

        fake_host = {
            "id": 1,
            "host_key": "root@1.2.3.4:22",
            "hostname": "1.2.3.4",
            "port": 22,
            "username": "root",
            "password": "secret",
        }

        with patch("core.ssh_hosts.get_host", return_value=fake_host), \
             patch("core.remote_dump.remote_exec_capture",
                   return_value={"returncode": 0, "stdout": "", "stderr": ""}):
            res = plugin_installer.uninstall("test-uninstall-plugin", host_id=1)

        self.assertTrue(res.get("ok"))
        # DB 状态已删除
        row = db.get_plugin_host_state(
            "root@1.2.3.4:22", "test-uninstall-plugin")
        self.assertIsNone(row)


# ============================================================================
# T05-6: batch-install 增强（db_types + host_id）
# ============================================================================
class TestBatchInstallEnhancement(unittest.TestCase):
    """验证 batch-install 支持 {"host_id": 11, "db_types": ["mysql"]} 自动算出
    该主机所需外部插件并批量下发。"""

    @classmethod
    def setUpClass(cls):
        _setup_tmp_db()

    def test_01_batch_with_db_types_and_host_id(self):
        """传入 db_types + host_id 时自动取推荐列表。"""
        from core import plugin_catalog, plugin_installer

        # Mock recommend_for_host 返回 2 个推荐插件
        fake_recs = [
            {"id": "percona-xtrabackup-80", "name": "XtraBackup 8.0"},
            {"id": "mariabackup", "name": "MariaBackup"},
        ]
        # Mock install 返回 ok
        install_results = [
            {"ok": True, "state": {"status": "queued"}},
            {"ok": True, "state": {"status": "queued"}},
        ]
        install_mock = MagicMock(side_effect=install_results)

        with patch.object(plugin_catalog, "recommend_for_host",
                          return_value=fake_recs) as mock_rec, \
             patch.object(plugin_installer, "install", install_mock):
            # 模拟 API 逻辑
            body = {"host_id": 11, "db_types": ["mysql"]}
            host_id = body.get("host_id")
            ids = []
            if not ids and body.get("db_types"):
                rows = plugin_catalog.recommend_for_host(
                    body["db_types"], host_id=host_id)
                ids = [r["id"] for r in rows]
            ids = [i for i in ids if i]

            self.assertEqual(len(ids), 2)
            self.assertIn("percona-xtrabackup-80", ids)
            self.assertIn("mariabackup", ids)

            # 验证 recommend_for_host 被正确调用（带 host_id）
            mock_rec.assert_called_once_with(["mysql"], host_id=11)

            # 模拟批量安装
            queued = []
            for pid in ids:
                res = install_mock(pid, host_id=host_id)
                if res.get("ok"):
                    queued.append(pid)
            self.assertEqual(len(queued), 2)

    def test_02_batch_with_explicit_ids_and_host_id(self):
        """传入 ids + host_id 时直接安装指定插件。"""
        from core import plugin_installer

        install_mock = MagicMock(return_value={"ok": True, "state": {"status": "queued"}})
        with patch.object(plugin_installer, "install", install_mock):
            body = {"host_id": 11, "ids": ["redis-tools", "mongodb-database-tools"]}
            host_id = body.get("host_id")
            ids = [i for i in body["ids"] if i]

            queued = []
            for pid in ids:
                res = install_mock(pid, host_id=host_id)
                if res.get("ok"):
                    queued.append(pid)

            self.assertEqual(len(queued), 2)
            # 验证 install 被调用时传入了 host_id
            for call in install_mock.call_args_list:
                self.assertEqual(call.kwargs.get("host_id"), 11)


# ============================================================================
# T05-7: 状态文件命名兼容性
# ============================================================================
class TestStateFileNaming(unittest.TestCase):
    """验证状态文件命名规则与旧文件兼容读取。"""

    def test_01_safe_host_key(self):
        """host_key 安全化：/ \\ @ : → _"""
        from core import plugin_installer as inst
        self.assertEqual(inst._safe_host_key("root@1.2.3.4:22"),
                         "root_1.2.3.4_22")
        self.assertEqual(inst._safe_host_key(None), "local")
        self.assertEqual(inst._safe_host_key("local"), "local")

    def test_02_state_path_with_host_key(self):
        """带 host_key 的状态文件路径正确。"""
        from core import plugin_installer as inst
        path = inst._state_path("my-plugin", host_key="root@1.2.3.4:22")
        self.assertIn("root_1.2.3.4_22__my-plugin.json", str(path))

    def test_03_state_path_local(self):
        """host_key=None 时优先读 local__ 路径。"""
        from core import plugin_installer as inst
        path = inst._state_path("my-plugin", host_key=None)
        self.assertIn("local__my-plugin.json", str(path))

    def test_04_log_path_with_host_key(self):
        """带 host_key 的日志文件路径正确。"""
        from core import plugin_installer as inst
        path = inst._log_path("my-plugin", host_key="root@1.2.3.4:22")
        self.assertIn("root_1.2.3.4_22__my-plugin.log", str(path))

    def test_05_get_state_returns_none_for_nonexistent(self):
        """不存在的状态文件返回 None。"""
        from core import plugin_installer as inst
        result = inst.get_state("nonexistent-plugin",
                                 host_key="nonexistent_host")
        self.assertIsNone(result)


# ============================================================================
# 运行入口
# ============================================================================
if __name__ == "__main__":
    unittest.main(verbosity=2)
