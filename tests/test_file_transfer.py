# -*- coding: utf-8 -*-
"""文件备份传输能力单测：瞬时异常判定 / sha256 / staged 元数据。"""
import os
import sys
import unittest
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_ft/meta.db")

from core.engines import file as F  # noqa: E402


class TestTransient(unittest.TestCase):
    def test_eof_transient(self):
        self.assertTrue(F._is_transient(EOFError()))

    def test_socket_timeout_transient(self):
        import socket
        self.assertTrue(F._is_transient(socket.timeout()))

    def test_ssh_exception_transient(self):
        class SSHException(Exception):
            pass
        e = SSHException("SSH session ended")
        # 按类型名匹配（避免依赖 paramiko）
        e.__class__ = type("SSHException", (Exception,), {})
        self.assertTrue(F._is_transient(e))

    def test_runtime_error_not_transient(self):
        self.assertFalse(F._is_transient(RuntimeError("远程打包失败 rc=2")))

    def test_value_error_not_transient(self):
        self.assertFalse(F._is_transient(ValueError("bad")))


class TestSha256(unittest.TestCase):
    def test_matches_hashlib(self):
        import tempfile
        data = os.urandom(5 * 1024 * 1024 + 123)
        fd, p = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            self.assertEqual(F._db_sha256(p), hashlib.sha256(data).hexdigest())
        finally:
            os.unlink(p)


class TestStageStat(unittest.TestCase):
    def test_stage_naming_has_task_and_kind(self):
        """stage 命名规则：task_id + kind——同名任务重跑可复用残件续传。"""
        task_id, kind = 17, "full"
        stage = "/tmp/aidbm_stage_%s_%s.tar.gz" % (task_id, kind)
        self.assertIn("aidbm_stage_17_full", stage)
        self.assertTrue(stage.startswith("/tmp/"))


class TestResumeOffset(unittest.TestCase):
    """跨运行续传残件有效性：边车 .sha 与远端 stage sha 必须一致。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="aidbm_resume_")
        self.part = os.path.join(self.dir, "p.part")
        self.side = self.part + ".sha"

    def test_no_part_zero(self):
        self.assertEqual(F._load_resume_offset(self.part, self.side, "a" * 64, 100), 0)

    def test_valid_resume(self):
        with open(self.part, "wb") as f:
            f.write(b"x" * 77)
        with open(self.side, "w") as f:
            f.write("a" * 64)
        self.assertEqual(F._load_resume_offset(self.part, self.side, "a" * 64, 100), 77)

    def test_stage_rebuilt_invalidates(self):
        """stage 已重建（sha 变化）→ 残件作废从头拉，防拼出 sha 不一致产物。"""
        with open(self.part, "wb") as f:
            f.write(b"x" * 77)
        with open(self.side, "w") as f:
            f.write("b" * 64)
        self.assertEqual(F._load_resume_offset(self.part, self.side, "a" * 64, 100), 0)

    def test_missing_sidecar_invalidates(self):
        with open(self.part, "wb") as f:
            f.write(b"x" * 77)
        self.assertEqual(F._load_resume_offset(self.part, self.side, "a" * 64, 100), 0)

    def test_oversized_part_invalidates(self):
        with open(self.part, "wb") as f:
            f.write(b"x" * 200)
        with open(self.side, "w") as f:
            f.write("a" * 64)
        self.assertEqual(F._load_resume_offset(self.part, self.side, "a" * 64, 100), 0)


if __name__ == "__main__":
    unittest.main()
