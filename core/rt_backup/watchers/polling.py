# -*- coding: utf-8 -*-
"""
PollingWatcher —— 定时轮询式文件变更捕获（默认兜底实现）。

为什么它是默认：
- **全平台覆盖**：Windows / Linux 同一套代码，不依赖任何内核事件机制；
- **唯一支持远程源**：远程 SSH 目录没有事件源可订阅，只能靠 ``find`` 列表比对；
- **正确性不打折**：真值来自快照 diff，不存在事件丢失问题（风险 R3）。

代价是 RPO 下限等于轮询间隔（``rt_interval_sec``，默认 180s）。若需要更小的
RPO，安装 ``watchdog`` 后由 :class:`WatchdogWatcher` 自动接管事件加速，
本实现仍作为其强制 flush 兜底继续存在。
"""
from __future__ import annotations

from typing import Tuple

from .base import FileChangeWatcher


class PollingWatcher(FileChangeWatcher):
    """定时轮询捕获器。"""

    impl_key: str = "polling"
    display_name: str = "定时轮询"
    required_packages: list = []

    @classmethod
    def is_available(cls, source_cfg: dict) -> Tuple[bool, str]:
        """轮询在任何环境下都可用，只要配置了源路径。"""
        paths = (source_cfg or {}).get("paths") or []
        if not paths:
            return False, "未配置源路径(source_paths)"
        return True, ""

    def _run(self) -> None:
        """主循环：每 ``interval_sec`` 轮询一次，期间可被 request_flush 打断。"""
        interval = max(5, int(self.rt.interval_sec or 180))
        self.logger.info("[rt.watcher] task=%s 轮询间隔 %ss", self.task_id, interval)

        # 启动后立即执行一次，避免「刚开保护却要等一个完整周期」
        self._emit(trigger="interval")

        while not self._stop_event.is_set():
            if not self._wait(interval):
                break
            reason = self._consume_flush()
            if self._stop_event.is_set():
                break
            self._emit(trigger=reason or "interval")

    def _teardown(self) -> None:
        """轮询实现无外部句柄，无需释放。"""
        return None
