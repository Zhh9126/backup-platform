# -*- coding: utf-8 -*-
"""重复日志限频（Duplicate log throttling）。

背景（2026-11-26 实测）：平台 error.log 20488 行里，**rt.cdc 一个来源占
7077 行（35%）**——达梦实时任务因实例未启动而反复启动失败，每次失败打
**两条内容重复**的 ERROR；参数化任务（Oracle LogMiner / MySQL binlog /
达梦）同样每轮重试刷屏。最近一小时 task=670 一个任务就刷了 736 行，把
真正的错误彻底淹没。

本模块提供三件事：

1. **同一条错误只留首条**：同一 (logger, 消息模板, 级别) 在窗口内重复出现
   直接抑制；
2. **窗口指数增长**：重复越多，抑制窗口越长（默认 5min -> 10min -> ...
   上限 1h），从根上杜绝「每 30 秒一条」的刷屏；
3. **不丢信号**：窗口到期后再次出现时，补一条「同类错误此前已重复 N 次」
   的汇总，运维一眼知道规模。

只作用于 **ERROR/CRITICAL**；INFO/WARNING 不受影响（业务语义完整保留）。
"""

from __future__ import annotations

import logging
import threading
import time

# 判定为「同一条错误」的键：(logger 名, 消息模板, 级别)
_KEY_ATTR = "_aidbm_dup_decision"


class _DupState:
    """限频状态（线程安全，多 handler 共享同一份判定）。"""

    def __init__(self, base_window: float = 300.0, max_window: float = 3600.0,
                 max_keys: int = 2000):
        self.base_window = float(base_window)
        self.max_window = float(max_window)
        self.max_keys = int(max_keys)
        self._lock = threading.Lock()
        self._data = {}          # key -> {"last": ts, "count": n, "window": s}
        self.suppressed = 0
        self.passed = 0

    def _evict_if_needed(self):
        if len(self._data) <= self.max_keys:
            return
        # 淘汰最久未出现的一批，避免长期运行内存无界增长
        items = sorted(self._data.items(), key=lambda kv: kv[1]["last"])
        for key, _v in items[: len(self._data) // 2]:
            self._data.pop(key, None)

    def decide(self, record) -> bool:
        """返回 True 表示**抑制**（不输出）。"""
        key = (record.name, record.msg, record.levelno)
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._data[key] = {"last": now, "count": 0,
                                   "window": self.base_window}
                self._evict_if_needed()
                self.passed += 1
                return False
            if now - entry["last"] < entry["window"]:
                entry["count"] += 1
                entry["last"] = now
                # 重复越多，等待越久：热循环（每 10s 一条）会迅速收敛到
                # 最大窗口（默认 1h），从根上杜绝刷屏。
                entry["window"] = min(entry["window"] * 2, self.max_window)
                self.suppressed += 1
                return True
            # 窗口到期：放行，并把抑制期间的条数汇总进本条
            missed = entry["count"]
            entry["count"] = 0
            entry["last"] = now
            self.passed += 1
            if missed:
                try:
                    record.msg = "%s（同类错误此前已重复 %d 次）" % (record.msg, missed)
                except Exception:
                    pass
            self._evict_if_needed()
            return False

    def stats(self) -> dict:
        with self._lock:
            return {"passed": self.passed, "suppressed": self.suppressed,
                    "keys": len(self._data)}

    def reset(self):
        with self._lock:
            self._data.clear()
            self.suppressed = 0
            self.passed = 0


class DuplicateSuppressFilter(logging.Filter):
    """装在 handler 上的重复日志抑制器。

    多个 handler 共用同一个 :class:`_DupState`：**每条记录只判定一次**，
    判定结果挂在 record 上，后续 handler 直接复用，避免「同一错误在不同
    文件里被重复计数」。
    """

    def __init__(self, state: _DupState = None):
        super().__init__()
        self.state = state or _DupState()

    def filter(self, record) -> bool:
        if record.levelno < logging.ERROR:
            return True
        decision = getattr(record, _KEY_ATTR, None)
        if decision is None:
            decision = self.state.decide(record)
            try:
                setattr(record, _KEY_ATTR, decision)
            except Exception:
                pass
        return not decision


def build_filter(base_window: float = 300.0, max_window: float = 3600.0):
    """构造 (filter, state)——state 用于诊断接口查询统计。"""
    state = _DupState(base_window=base_window, max_window=max_window)
    return DuplicateSuppressFilter(state), state
