# -*- coding: utf-8 -*-
"""克隆预检「目标端工具就绪」检查回归（2026-11-26 用户反馈后新增）。

现场症状：Oracle 克隆请求目标主机填了平台本机 127.0.0.1，提交后失败
「真实克隆失败: imp 客户端不可用，无法执行本地恢复」——错误发生在**拉起
之后**，用户既浪费时间也看不懂"要补什么"。

修复：预检（提交前、只读）就要回答"目标端需要什么"：

- 目标主机 = 平台本机 → 平台侧必须有对应导入客户端（impdp/imp、mysql、
  psql/pg_restore、dimp…），缺失即 fail 并给出两条处置路径；
- 目标主机 = 其他机器 → 走 SSH 通道由平台在目标机自动解析工具，只校验
  SSH 22 可达（数据库服务器零安装，符合产品红线）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_clone_pf/meta.db")
os.environ.setdefault("LOG_DIR", "/tmp/aidbm_test_clone_pf/logs")

from core import clone_preflight as CP  # noqa: E402


class ClonePreflightToolsTest(unittest.TestCase):

    # ------------------------------------------------------------------
    def test_local_tool_available_finds_platform_client(self):
        """平台侧已有的客户端应被解析到（PG 客户端在 /pgdb/pgsql/bin）。"""
        hit = CP.local_tool_available("psql")
        self.assertTrue(hit, "平台应有 psql（/pgdb/pgsql/bin）")
        self.assertTrue(os.path.isfile(hit))

    def test_local_tool_available_missing_returns_empty(self):
        self.assertEqual(CP.local_tool_available("definitely-not-a-tool-xyz"), "")

    def test_is_local_target(self):
        for h in ("127.0.0.1", "localhost", "::1", "", None):
            self.assertTrue(CP._is_local_target(h), h)
        self.assertFalse(CP._is_local_target("192.168.163.195"))

    # ------------------------------------------------------------------
    def _rec(self, db_type="oracle"):
        return {
            "id": 158, "db_type": db_type, "status": "success",
            "backup_path": "/tmp/does-not-matter-for-tool-check.dmp",
            "checksum": "",
        }

    def test_oracle_local_target_flags_missing_client(self):
        """Oracle + 平台本机：平台无 impdp/imp → 必须 fail 且带处置指引。"""
        rep = CP.preflight(self._rec(), target_host="127.0.0.1")
        tools = [c for c in rep["checks"] if c["item"] == "target_tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["level"], "fail")
        self.assertIn("impdp", tools[0]["message"] + tools[0].get("message", ""))
        self.assertFalse(rep["ok"])
        self.assertTrue(any("impdp" in e for e in rep["errors"]),
                       "errors 应明确指出缺少 impdp")

    def test_oracle_remote_target_requires_ssh(self):
        """Oracle + 远端：SSH 22 不可达时应明确失败（而不是等拉起后才炸）。"""
        rep = CP.preflight(self._rec(), target_host="192.0.2.77")
        tools = [c for c in rep["checks"] if c["item"] == "target_tools"]
        self.assertEqual(tools[0]["level"], "fail")
        self.assertFalse(rep["ok"])

    def test_mysql_local_target_tools_ok(self):
        """MySQL + 本机：平台侧 mysql 客户端就绪 → target_tools 通过。"""
        rep = CP.preflight(self._rec("mysql"), target_host="127.0.0.1")
        tools = [c for c in rep["checks"] if c["item"] == "target_tools"]
        self.assertEqual(tools[0]["level"], "pass")
        self.assertIn("mysql@", tools[0]["message"])

    def test_unknown_db_type_skips_tool_check(self):
        """未登记客户端要求的库型：只 warn/skip，不得误拦。"""
        rep = CP.preflight(self._rec("redis"), target_host="127.0.0.1")
        tools = [c for c in rep["checks"] if c["item"] == "target_tools"]
        self.assertEqual(tools[0]["level"], "pass")
        self.assertIn("无特定客户端要求", tools[0]["message"])


if __name__ == "__main__":
    unittest.main()
