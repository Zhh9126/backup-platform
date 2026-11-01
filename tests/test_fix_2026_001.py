# -*- coding: utf-8 -*-
"""FIX-2026-001 回归：P3 日志脱敏（连接串/建用户口令）与 P2 纳管唯一性下沉。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_fix/meta.db")
os.makedirs("/tmp/aidbm_test_fix", exist_ok=True)

from core.logging_setup import mask_secrets, mask_command  # noqa: E402


class TestMaskSecretsP3(unittest.TestCase):
    """导出日志明文密码脱敏（P3）——保留审计要素，仅掩码密码值。"""

    def test_userid_plain(self):
        s = mask_secrets("expdp USERID=system/oracle@127.0.0.1:1521/orcl SCHEMAS=APP")
        self.assertNotIn("oracle@127.0.0.1", s.replace("***@127.0.0.1", ""))
        self.assertIn("USERID=system/***@127.0.0.1:1521/orcl", s)
        self.assertIn("SCHEMAS=APP", s)  # 审计要素保留

    def test_userid_dm_quoted_at_password(self):
        # 达梦密码含 @ 的官方引号写法
        s = mask_secrets('dexp USERID="SYSDBA/\\"Ceshi@5235\\"@localhost:5236" FILE=a')
        self.assertNotIn("Ceshi@5235", s)
        self.assertIn("SYSDBA/***", s)

    def test_disql_connstr(self):
        # 达梦真实形态：disql "SYSDBA/\"密码\"@host:port"（引号内密码含 @）
        s = mask_secrets('disql "SYSDBA/\\"Ceshi@5235\\"@localhost:5236"')
        self.assertNotIn("Ceshi@5235", s)
        self.assertIn("disql", s)
        self.assertIn("@localhost:5236", s)

    def test_conn_plain(self):
        s = mask_secrets("conn app_user/Pw123@//db1:1521/svc")
        self.assertNotIn("Pw123@//db1", s)

    def test_identified_by(self):
        s = mask_secrets('CREATE USER "APP" IDENTIFIED BY "S3cret!" ')
        self.assertNotIn("S3cret!", s)
        self.assertIn("IDENTIFIED BY", s)

    def test_audit_elements_retained(self):
        line = "USERID=system/oracle@db1:1521/orcl TABLES=T1"
        s = mask_secrets(line)
        self.assertIn("system", s)      # 导出用户可见
        self.assertIn("db1:1521/orcl", s)  # 目标库可见
        self.assertIn("TABLES=T1", s)   # 导出对象可见

    def test_mask_command_list(self):
        s = mask_command(["dimp", "USERID=SYSDBA/Ceshi@5235@localhost:5236",
                          "FILE=x.dmp"])
        self.assertNotIn("Ceshi@5235", s)
        self.assertIn("x.dmp", s)


class TestHostsUniquenessP2(unittest.TestCase):
    """纳管唯一性下沉：同 IP 不同库型可共存，同连接同库型拒绝（P2）。"""

    @classmethod
    def setUpClass(cls):
        import core.db as db
        db.init_schema()
        from core import ssh_hosts as sh
        cls.sh = sh
        # 清理本测试的历史记录（隔离 meta 库内按名称清理）
        for h in sh.list_hosts():
            if str(h.get("name") or "").startswith("fixp2-"):
                sh.delete_host(h["id"])

    def tearDown(self):
        for h in self.sh.list_hosts():
            if str(h.get("name") or "").startswith("fixp2-"):
                self.sh.delete_host(h["id"])

    def _mk(self, name, db_type):
        return self.sh.create_host({
            "name": name, "hostname": "192.168.201.201", "port": 22,
            "username": "root", "auth_type": "key",
            "db_type": db_type, "os_type": "linux"})

    def test_same_ip_different_dbtype_ok(self):
        """同一 IP 先纳管 Oracle 再纳管达梦 → 应成功（核心验收）"""
        id1 = self._mk("fixp2-ora", "oracle")
        id2 = self._mk("fixp2-dm", "dameng")
        self.assertTrue(id1 and id2 and id1 != id2)

    def test_same_ip_same_dbtype_rejected(self):
        self._mk("fixp2-ora", "oracle")
        with self.assertRaises(ValueError) as ctx:
            self._mk("fixp2-ora2", "oracle")
        self.assertIn("同类型", str(ctx.exception))

    def test_generic_conflict_with_generic(self):
        self._mk("fixp2-gen", "")
        with self.assertRaises(ValueError):
            self._mk("fixp2-gen2", "")

    def test_generic_and_typed_coexist(self):
        a = self._mk("fixp2-gen", "")
        b = self._mk("fixp2-ora", "oracle")
        self.assertTrue(a and b)


if __name__ == "__main__":
    unittest.main()
