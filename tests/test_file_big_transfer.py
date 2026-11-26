# -*- coding: utf-8 -*-
"""大文件备份传输可靠性（2026-11-26，用户现场 20GB 任务 10m43s 失败回归）。

用户现场：192.168.163.247 全量文件备份 20GB，10m43s 后
「文件备份失败：SSH transport dead」。根因链：
  1) 远端打包扛在一个阻塞 SSH 会话里，会话被中间设备掐断（长会话时长上限），
     tar 被 SIGHUP 杀掉；
  2) "SSH transport dead" 不被判瞬时 -> 不重试；
  3) 重试用的还是池里那具死连接，从未重建。

守住四条修复：瞬时判定 / 死连接透明重建 / 分离式打包（断连不影响远端 tar）
+ 完成标记 / sha 超时随大小缩放。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_ft_big/meta.db")
os.environ.setdefault("LOG_DIR", "/tmp/aidbm_test_ft_big/logs")

from core.engines import file as F  # noqa: E402


class TestTransportDeadIsTransient(unittest.TestCase):
    def test_transport_dead_is_transient(self):
        self.assertTrue(F._is_transient(RuntimeError("SSH transport dead")))

    def test_transport_generic_is_transient(self):
        self.assertTrue(F._is_transient(RuntimeError("ssh transport closed")))

    def test_real_pack_failure_still_not_transient(self):
        self.assertFalse(F._is_transient(
            RuntimeError("远端打包落盘失败 rc=2: tar: error")))
        self.assertFalse(F._is_transient(
            RuntimeError("STAGED_ENOSPC: 远端落盘空间不足")))


class _FakeTransport:
    def __init__(self, alive):
        self._alive = alive

    def is_active(self):
        return self._alive

    def send_ignore(self):
        if not self._alive:
            raise OSError("dead")


class _FakeClient:
    def __init__(self, alive=True):
        self._t = _FakeTransport(alive)
        self.closed = False

    def get_transport(self):
        return self._t

    def close(self):
        self.closed = True


class TestEnsureAlive(unittest.TestCase):
    def test_live_client_returned_as_is(self):
        c = _FakeClient(True)
        self.assertIs(F._ensure_alive(c, ""), c)

    def test_dead_client_without_host_key_returned_as_is(self):
        c = _FakeClient(False)
        self.assertIs(F._ensure_alive(c, ""), c)

    def test_dead_client_rebuilt_from_pool(self):
        """host_key 给定 + 池里有活连接 -> 换新连接（不攥尸体）。"""
        orig = F._ssh_pool.get("tester@127.0.0.1:22")
        F._ssh_pool["tester@127.0.0.1:22"] = _FakeClient(True)
        try:
            dead = _FakeClient(False)
            got = F._ensure_alive(dead, "tester@127.0.0.1:22")
            self.assertIsNot(got, dead)
            self.assertTrue(dead.closed)
        finally:
            if orig is None:
                F._ssh_pool.pop("tester@127.0.0.1:22", None)
            else:
                F._ssh_pool["tester@127.0.0.1:22"] = orig


class TestShaTimeoutScaling(unittest.TestCase):
    def test_small_file_minimum(self):
        self.assertEqual(F._sha_timeout_for(0), 600)

    def test_20gb_gets_minutes_not_120s(self):
        t = F._sha_timeout_for(20 * 1024 ** 3)
        self.assertGreaterEqual(t, 600)
        self.assertLessEqual(t, 7200)


if __name__ == "__main__":
    unittest.main()
