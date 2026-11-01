# -*- coding: utf-8 -*-
"""运行中任务协作式取消（FIX-2026-001 用户反馈）：注册/停止/块边界语义。"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_cancel/meta.db")
os.makedirs("/tmp/aidbm_test_cancel", exist_ok=True)

from core import cancel  # noqa: E402


class TestCancelRegistry(unittest.TestCase):
    def test_register_cancel_flow(self):
        key = "t999:r1"
        cancel.register(key)
        token = cancel.attach(key)
        try:
            self.assertFalse(cancel.is_cancelled())
            self.assertTrue(cancel.cancel(key))
            self.assertTrue(cancel.is_cancelled())
            with self.assertRaises(cancel.CancelledError):
                cancel.check()
        finally:
            cancel.finish(key)
            cancel.detach(token)
        # 结束后事件清理：is_cancelled 恢复 False
        self.assertFalse(cancel.is_cancelled())
        self.assertFalse(cancel.cancel(key))

    def test_isolated_contexts(self):
        """两个线程上下文互不影响（ContextVar 隔离）。"""
        k1, k2 = "t801:r1", "t802:r1"
        cancel.register(k1)
        cancel.register(k2)
        results = {}

        def worker(k, out):
            tok = cancel.attach(k)
            try:
                if k == k2:
                    cancel.cancel(k2)
                out[k] = cancel.is_cancelled()
            finally:
                cancel.finish(k)
                cancel.detach(tok)

        t1 = threading.Thread(target=worker, args=(k1, results))
        t2 = threading.Thread(target=worker, args=(k2, results))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(results[k1])   # k1 未取消
        # k2 已 finish，查不到了
        self.assertFalse(cancel.cancel(k2))

    def test_no_context_is_safe(self):
        """未绑定上下文（如恢复通道）调用 check 不抛错。"""
        self.assertFalse(cancel.is_cancelled())
        cancel.check()


if __name__ == "__main__":
    unittest.main()
