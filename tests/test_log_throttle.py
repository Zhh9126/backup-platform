# -*- coding: utf-8 -*-
"""重复日志限频（2026-11-26 error.log 治理回归）。

背景：error.log 20488 行里 rt.cdc 占 7077 行（35%），同一达梦实时任务
一小时刷 736 行，把真错误淹没。
"""
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_DB_PATH", "/tmp/aidbm_test_throttle/meta.db")
os.environ.setdefault("LOG_DIR", "/tmp/aidbm_test_throttle/logs")

from core.log_throttle import DuplicateSuppressFilter, _DupState  # noqa: E402


def _rec(name="aidbm.rt.cdc", msg="task=670 启动失败", level=logging.ERROR):
    return logging.LogRecord(name, level, __file__, 1, msg, (), None)


class TestDupSuppression(unittest.TestCase):
    def test_first_occurrence_passes(self):
        f = DuplicateSuppressFilter(_DupState(base_window=60))
        self.assertTrue(f.filter(_rec()))

    def test_repeat_within_window_suppressed(self):
        f = DuplicateSuppressFilter(_DupState(base_window=60))
        self.assertTrue(f.filter(_rec()))
        for _ in range(20):
            self.assertFalse(f.filter(_rec()))

    def test_different_messages_not_suppressed(self):
        f = DuplicateSuppressFilter(_DupState(base_window=60))
        self.assertTrue(f.filter(_rec(msg="A")))
        self.assertTrue(f.filter(_rec(msg="B")))

    def test_different_logger_not_suppressed(self):
        f = DuplicateSuppressFilter(_DupState(base_window=60))
        self.assertTrue(f.filter(_rec(name="aidbm.a")))
        self.assertTrue(f.filter(_rec(name="aidbm.b")))

    def test_info_level_never_suppressed(self):
        f = DuplicateSuppressFilter(_DupState(base_window=60))
        for _ in range(10):
            self.assertTrue(f.filter(_rec(level=logging.INFO)))

    def test_window_expiry_passes_with_summary_count(self):
        state = _DupState(base_window=0.05, max_window=0.1)
        f = DuplicateSuppressFilter(state)
        self.assertTrue(f.filter(_rec()))
        for _ in range(5):
            self.assertFalse(f.filter(_rec()))
        # 窗口随重复次数增长（5 次重复 → 0.05*2^5 后被 max_window 截到 0.1s）
        time.sleep(0.2)
        rec = _rec()
        self.assertTrue(f.filter(rec))
        self.assertIn("同类错误此前已重复 5 次", rec.msg)

    def test_window_grows_exponentially(self):
        state = _DupState(base_window=0.05, max_window=10)
        f = DuplicateSuppressFilter(state)
        f.filter(_rec())
        f.filter(_rec())
        key = list(state._data.keys())[0]
        self.assertGreater(state._data[key]["window"], 0.05)

    def test_shared_state_decides_once_per_record(self):
        state = _DupState(base_window=60)
        f1 = DuplicateSuppressFilter(state)
        f2 = DuplicateSuppressFilter(state)
        rec = _rec()
        self.assertTrue(f1.filter(rec))       # 首个 handler 判定：放行
        self.assertTrue(f2.filter(rec))       # 第二个 handler 复用同一判定
        rec2 = _rec()
        self.assertFalse(f1.filter(rec2))     # 重复 → 抑制
        self.assertFalse(f2.filter(rec2))     # 两个 handler 同步抑制（同一判定）

    def test_stats_reports_suppressed(self):
        state = _DupState(base_window=60)
        f = DuplicateSuppressFilter(state)
        for _ in range(5):
            f.filter(_rec())
        st = state.stats()
        self.assertEqual(st["suppressed"], 4)
        self.assertEqual(st["passed"], 1)


class TestKeyBound(unittest.TestCase):
    def test_state_key_bounded(self):
        state = _DupState(base_window=60, max_keys=20)
        f = DuplicateSuppressFilter(state)
        for i in range(100):
            f.filter(_rec(msg="m%d" % i))
        self.assertLessEqual(len(state._data), 20)


if __name__ == "__main__":
    unittest.main()
