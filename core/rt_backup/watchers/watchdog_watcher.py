# -*- coding: utf-8 -*-
"""
WatchdogWatcher —— 事件驱动的文件变更捕获（可选加速实现）。

底层由 ``watchdog`` 包适配到各平台原生机制：
- Linux   : inotify
- Windows : ReadDirectoryChangesW
- macOS   : FSEvents

**事件只做触发器**：收到任意事件仅置脏标记并记录时间戳，静默
``rt_debounce_sec``（默认 5s）后触发一次 :meth:`poll_once`，真值仍来自快照
diff。这样即便 inotify 队列溢出（``IN_Q_OVERFLOW``）或 RDCW 缓冲丢事件，
也不会漏备（风险 R3）。

同时内置两道兜底：
1. **强制 flush 上限**：距上次捕获超过 ``rt_interval_sec`` 必定触发一次，
   等价于内嵌了一个轮询器；
2. **观察者猝死降级**：Observer 线程异常退出时自动切换到
   :class:`PollingWatcher` 接管，并置 ``degrade_reason``，不中断保护。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional, Tuple

from .base import FileChangeWatcher
from .polling import PollingWatcher

# 单次目录计数上限（防止超大目录树在能力探测阶段卡住）
_DIR_COUNT_CAP = 100000
# inotify watch 使用率安全水位（超过则判定不宜使用事件驱动）
_INOTIFY_SAFE_RATIO = 0.8


def _import_watchdog():
    """惰性导入 watchdog。不可用时返回 (None, None, 原因)。"""
    try:
        from watchdog.observers import Observer  # type: ignore
        from watchdog.events import FileSystemEventHandler  # type: ignore
        return Observer, FileSystemEventHandler, ""
    except Exception as exc:  # ImportError 或其平台后端初始化失败
        return None, None, f"watchdog 不可用: {exc}"


def _count_dirs(root: str, cap: int = _DIR_COUNT_CAP) -> int:
    """统计目录树中的目录数量（含根），最多数到 cap 便提前返回。"""
    if not root or not os.path.isdir(root):
        return 0
    total = 1
    for _dirpath, dirnames, _files in os.walk(root):
        total += len(dirnames)
        if total >= cap:
            return cap
    return total


def _inotify_max_watches() -> int:
    """读取 Linux inotify 单用户 watch 上限。非 Linux 或读取失败返回 0。"""
    path = "/proc/sys/fs/inotify/max_user_watches"
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip())
    except Exception:
        return 0


class WatchdogWatcher(FileChangeWatcher):
    """事件驱动捕获器（需要 ``watchdog>=4.0``，仅支持本地源）。"""

    impl_key: str = "watchdog"
    display_name: str = "事件驱动(inotify/RDCW)"
    required_packages: list = ["watchdog>=4.0"]

    # ---------------- 能力探测 ----------------
    @classmethod
    def is_available(cls, source_cfg: dict) -> Tuple[bool, str]:
        """判定当前环境能否使用事件驱动。"""
        source_cfg = source_cfg or {}
        paths = source_cfg.get("paths") or []
        if not paths:
            return False, "未配置源路径(source_paths)"
        if (source_cfg.get("type") or "local") != "local":
            return False, "远程 SSH 源无事件源可订阅，使用轮询"

        observer_cls, _handler_cls, reason = _import_watchdog()
        if observer_cls is None:
            return False, reason or "watchdog 未安装"

        root = paths[0]
        if not os.path.isdir(root):
            return False, f"源目录不存在: {root}"

        if sys.platform.startswith("linux"):
            max_watches = _inotify_max_watches()
            if max_watches > 0:
                need = _count_dirs(root)
                if need > max_watches * _INOTIFY_SAFE_RATIO:
                    return False, (f"inotify watch 不足（需 ~{need}，"
                                   f"上限 {max_watches}），已降级为轮询")
        return True, ""

    # ---------------- 构造 ----------------
    def __init__(self, task: dict, rt_config, on_batch, logger=None) -> None:
        super().__init__(task, rt_config, on_batch, logger=logger)
        self._observer = None
        self._handler = None
        self._dirty: bool = False
        self._last_event_at: float = 0.0
        self._last_emit_at: float = 0.0
        self._event_lock: threading.Lock = threading.Lock()
        # 观察者猝死时接管的兜底轮询器（惰性创建）
        self._fallback: Optional[PollingWatcher] = None
        self._delegated: bool = False

    # ---------------- 事件回调 ----------------
    def _on_any_event(self, event) -> None:
        """watchdog 事件回调。必须极轻量——只置脏标记，绝不做 IO。"""
        try:
            if getattr(event, "is_directory", False):
                # 目录事件本身不代表内容变化，但其子文件事件会单独到达；
                # 仅在「目录被移动/删除」时也置脏，避免整目录消失无人察觉。
                if getattr(event, "event_type", "") not in ("moved", "deleted"):
                    return
            with self._event_lock:
                self._events_seen += 1
                self._dirty = True
                self._last_event_at = time.time()
        except Exception:
            # 事件回调里任何异常都不允许冒泡到 watchdog 线程
            pass

    def _build_handler(self, handler_cls):
        """构造事件处理器实例（把所有事件统一路由到 _on_any_event）。"""
        watcher = self

        class _Handler(handler_cls):  # type: ignore[misc, valid-type]
            """把 watchdog 的全部事件统一收敛为脏标记。"""

            def on_any_event(self, event) -> None:  # noqa: D401
                watcher._on_any_event(event)

        return _Handler()

    # ---------------- 生命周期 ----------------
    def _start_observer(self) -> bool:
        """启动 watchdog Observer。失败返回 False 并置 degrade_reason。"""
        observer_cls, handler_cls, reason = _import_watchdog()
        if observer_cls is None:
            self.degrade_reason = reason
            return False
        root = self.source_root()
        if not root or not os.path.isdir(root):
            self.degrade_reason = f"源目录不存在: {root}"
            return False
        try:
            self._handler = self._build_handler(handler_cls)
            self._observer = observer_cls()
            self._observer.schedule(self._handler, root, recursive=True)
            self._observer.daemon = True
            self._observer.start()
            return True
        except Exception as exc:
            self.degrade_reason = f"事件监听启动失败: {exc}"
            self.logger.warning("[rt.watcher] task=%s %s", self.task_id,
                                self.degrade_reason)
            self._observer = None
            return False

    def _activate_fallback(self, reason: str) -> None:
        """观察者不可用时切换到轮询兜底，保证保护不中断。"""
        if self._delegated:
            return
        self.degrade_reason = reason
        self._delegated = True
        self.logger.warning("[rt.watcher] task=%s 降级为轮询: %s",
                            self.task_id, reason)
        self._fallback = PollingWatcher(self.task, self.rt, self.on_batch,
                                        logger=self.logger)
        self._fallback.degrade_reason = reason
        self._fallback.start()

    def _run(self) -> None:
        """主体：启动观察者 + 去抖循环；观察者不可用时降级轮询。"""
        if not self._start_observer():
            self._activate_fallback(self.degrade_reason or "watchdog 不可用")
            # 兜底轮询器自带线程，本线程只需等待停止信号
            while not self._stop_event.is_set():
                if not self._wait(1.0):
                    break
                self._consume_flush()
            return

        self.logger.info("[rt.watcher] task=%s 事件监听已就绪（去抖 %ss / 强制 %ss）",
                         self.task_id, self.rt.debounce_sec, self.rt.interval_sec)
        # 启动即做一次基线捕获，抹平「开启保护前已积累的变更」
        self._emit(trigger="interval")
        self._last_emit_at = time.time()
        self._debounce_loop()

    def _debounce_loop(self) -> None:
        """去抖 + 强制 flush 上限。事件风暴下只在静默后触发一次。"""
        debounce = max(1, int(self.rt.debounce_sec or 5))
        force_interval = max(10, int(self.rt.interval_sec or 180))
        tick = min(1.0, float(debounce))

        while not self._stop_event.is_set():
            if not self._wait(tick):
                break

            manual = self._consume_flush()
            if self._stop_event.is_set():
                break

            # 观察者猝死 → 立即降级，不留静默漏备窗口
            observer = self._observer
            if observer is not None and not observer.is_alive():
                self._activate_fallback("事件监听线程已退出，自动降级为轮询")
                continue

            now = time.time()
            trigger = ""
            if manual:
                trigger = manual
            else:
                with self._event_lock:
                    dirty = self._dirty
                    quiet_for = now - self._last_event_at
                if dirty and quiet_for >= debounce:
                    trigger = "event"
                elif now - self._last_emit_at >= force_interval:
                    trigger = "interval"

            if not trigger:
                continue

            with self._event_lock:
                self._dirty = False
            self._emit(trigger=trigger)
            self._last_emit_at = time.time()

    def _teardown(self) -> None:
        """停止 Observer 与兜底轮询器。幂等。"""
        observer = self._observer
        self._observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5.0)
            except Exception as exc:
                self.logger.warning("[rt.watcher] task=%s 停止事件监听异常: %s",
                                    self.task_id, exc)
        fallback = self._fallback
        self._fallback = None
        if fallback is not None:
            fallback.stop(timeout=5.0)
        self._delegated = False

    # ---------------- 状态 ----------------
    def is_alive(self) -> bool:
        """事件线程或兜底轮询线程任一存活即视为存活。"""
        if self._delegated and self._fallback is not None:
            return self._fallback.is_alive()
        if not super().is_alive():
            return False
        observer = self._observer
        return observer is None or observer.is_alive()

    def stats(self) -> dict:
        """在基类统计上补充事件驱动特有指标。"""
        data = super().stats()
        if self._delegated and self._fallback is not None:
            fb = self._fallback.stats()
            data.update({
                "impl": f"{self.impl_key}->polling",
                "polls": fb.get("polls", 0),
                "last_poll_at": fb.get("last_poll_at", ""),
                "last_batch_size": fb.get("last_batch_size", 0),
            })
        data["dirty"] = self._dirty
        data["debounce_sec"] = self.rt.debounce_sec
        data["force_interval_sec"] = self.rt.interval_sec
        return data
