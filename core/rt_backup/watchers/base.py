# -*- coding: utf-8 -*-
"""
文件变更捕获抽象基类。

跨平台统一接口，上层（FileRtCapture）不感知 inotify / ReadDirectoryChangesW /
轮询之间的差异。

设计契约（设计文档 §3.3）：
  1. ``on_batch(ChangeBatch)`` 回调必须在独立线程中调用，不得阻塞事件源；
  2. **事件仅作触发器**，``changed``/``deleted`` 的最终真值一律由
     :meth:`poll_once` 内部的快照 diff 产生（复用
     ``FileBackupEngine._diff_against_snapshot``），从根本上规避 inotify
     ``IN_Q_OVERFLOW`` / RDCW 缓冲溢出导致的静默漏备（风险 R3）；
  3. :meth:`stop` 必须幂等，且在 timeout 内返回，不得留下僵尸线程/句柄。

所有实现共用本基类的 :meth:`poll_once` 与 :meth:`_emit`，子类只负责决定
「什么时候该 poll」。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import config
import core.db as db
from core.engines.file import FileBackupEngine

from ..types import ChangeBatch, RtConfig, norm_path

# 实时任务的快照命名空间（与普通增量任务的基准隔离，设计文档 R4）
RT_SNAPSHOT_NAMESPACE = "rt"


class FileChangeWatcher:
    """文件变更捕获抽象。

    Attributes:
        impl_key: 实现标识，落库到 ``rt_capture_state.watcher_impl``。
        display_name: 中文展示名，供 UI 与自检页面使用。
        required_packages: 该实现依赖的第三方包名列表（用于能力探测提示）。
    """

    impl_key: str = "base"
    display_name: str = "抽象捕获器"
    required_packages: List[str] = []

    # ---------------- 能力探测 ----------------
    @classmethod
    def is_available(cls, source_cfg: dict) -> Tuple[bool, str]:
        """判断该实现在当前环境 + 给定源配置下是否可用。

        Args:
            source_cfg: ``FileBackupEngine._parse_source()`` 的返回值。

        Returns:
            ``(可用, 原因)``。不可用时 ``create_watcher`` 会据此降级到轮询，
            并把原因写入 ``degrade_reason`` 供 UI 展示。
        """
        return True, ""

    # ---------------- 构造 ----------------
    def __init__(self, task: dict, rt_config: RtConfig,
                 on_batch: Callable[[ChangeBatch], None], logger=None) -> None:
        self.task: dict = dict(task or {})
        self.rt: RtConfig = rt_config or RtConfig()
        self.on_batch: Callable[[ChangeBatch], None] = on_batch
        self.logger = logger or db.get_logger("rt.watcher")

        self.task_id: int = int(self.task.get("id") or self.rt.task_id or 0)
        self.task_name: str = self.task.get("name") or f"task_{self.task_id}"
        self.degrade_reason: str = ""

        # 复用现有文件引擎做扫描 / diff / 打包，绝不另起一套实现
        self._engine: FileBackupEngine = FileBackupEngine(
            self.task, config.BACKUP_ROOT, self.logger)
        self._engine.snapshot_namespace = RT_SNAPSHOT_NAMESPACE
        self.source_cfg: dict = self._engine._parse_source()

        # 线程与生命周期
        self._stop_event: threading.Event = threading.Event()
        self._flush_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._poll_lock: threading.Lock = threading.Lock()
        self._started: bool = False

        # 统计
        self._events_seen: int = 0
        self._polls: int = 0
        self._last_poll_at: str = ""
        self._last_batch_size: int = 0
        self._last_error: str = ""
        self._flush_reason: str = "interval"

    # ---------------- 引擎访问 ----------------
    @property
    def engine(self) -> FileBackupEngine:
        """底层文件引擎（FileRtCapture 复用它做 capture_increment）。"""
        return self._engine

    def source_root(self) -> str:
        """源根目录（第一个源路径）。未配置时返回空串。"""
        paths = self.source_cfg.get("paths") or []
        return paths[0] if paths else ""

    # ---------------- 真值路径（所有实现共用） ----------------
    def poll_once(self, trigger: str = "poll") -> ChangeBatch:
        """同步执行一次完整差异计算，返回变更批次。

        无论触发来源是定时器还是文件系统事件，最终都走这条路径，
        保证「事件只做触发器、快照 diff 才是真值」。

        Args:
            trigger: 触发来源标记（poll | event | interval | manual | base）。

        Returns:
            ChangeBatch。远程源不可达时返回空批次并置 ``last_error``。
        """
        with self._poll_lock:
            self._polls += 1
            self._last_poll_at = db.now_iso()
            batch = ChangeBatch(detected_at=self._last_poll_at, trigger=trigger)

            source_files = self._list_source()
            if source_files is None:
                self._last_error = "源文件列表获取失败（远程不可达）"
                self.logger.warning("[rt.watcher] task=%s %s", self.task_id,
                                    self._last_error)
                return batch

            batch.total_files = len(source_files)
            batch.snapshot = source_files

            snapshot = self._engine._load_snapshot()
            if snapshot is None:
                # 尚无基准：交由 FileRtCapture 触发 ensure_base_full()
                batch.trigger = "base"
                self._last_batch_size = 0
                return batch

            changed, deleted = self._engine._diff_against_snapshot(
                source_files, snapshot)
            batch.changed = changed
            batch.deleted = deleted
            self._last_batch_size = len(changed) + len(deleted)
            self._last_error = ""

            if not batch.is_empty():
                self.logger.info(
                    "[rt.watcher] task=%s %s 触发: 总计=%d 变化=%d 删除=%d",
                    self.task_id, trigger, batch.total_files,
                    len(changed), len(deleted))
            return batch

    def _list_source(self) -> Optional[Dict[str, Tuple[int, int]]]:
        """列出源文件状态。本地返回字典，远程失败返回 None。"""
        try:
            return self._engine.list_source_files()
        except Exception as exc:
            self.logger.warning("[rt.watcher] task=%s 扫描源失败: %s",
                                self.task_id, exc)
            return None

    def _emit(self, trigger: str = "poll") -> None:
        """执行一次 poll_once 并把结果投递给上层。异常绝不外泄到线程外。"""
        try:
            batch = self.poll_once(trigger=trigger)
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("[rt.watcher] task=%s poll 异常: %s",
                              self.task_id, exc)
            return
        if self.on_batch is None:
            return
        try:
            self.on_batch(batch)
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("[rt.watcher] task=%s on_batch 回调异常: %s",
                              self.task_id, exc)

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        """启动捕获线程。重复调用幂等。"""
        if self._started and self.is_alive():
            return
        self._stop_event.clear()
        self._started = True
        self._thread = threading.Thread(
            target=self._run, name=f"rt-watch-{self.task_id}-{self.impl_key}",
            daemon=True)
        self._thread.start()
        self.logger.info("[rt.watcher] task=%s 启动 %s（源=%s）",
                         self.task_id, self.impl_key,
                         norm_path(self.source_root()))

    def stop(self, timeout: float = 10.0) -> None:
        """停止捕获线程并释放资源。幂等，且保证在 timeout 内返回。"""
        self._stop_event.set()
        self._flush_event.set()
        try:
            self._teardown()
        except Exception as exc:
            self.logger.warning("[rt.watcher] task=%s 释放资源异常: %s",
                                self.task_id, exc)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))
            if thread.is_alive():
                self.logger.warning("[rt.watcher] task=%s 线程未在 %.1fs 内退出",
                                    self.task_id, timeout)
        self._thread = None
        self._started = False
        self.logger.info("[rt.watcher] task=%s 已停止 %s", self.task_id,
                         self.impl_key)

    def is_alive(self) -> bool:
        """捕获线程是否存活。"""
        return bool(self._thread and self._thread.is_alive())

    def request_flush(self, reason: str = "manual") -> None:
        """请求立即触发一次 poll_once + on_batch（供手动捕获 / 强制 flush 使用）。

        本方法只置位，不在调用线程内做 IO，保证 API 侧调用即时返回。
        """
        self._flush_reason = reason or "manual"
        self._flush_event.set()

    def stats(self) -> dict:
        """运行统计，直接进 ``rt_capture_state`` 与 UI 详情面板。"""
        return {
            "impl": self.impl_key,
            "display_name": self.display_name,
            "events_seen": self._events_seen,
            "polls": self._polls,
            "last_poll_at": self._last_poll_at,
            "last_batch_size": self._last_batch_size,
            "degrade_reason": self.degrade_reason,
            "last_error": self._last_error,
            "alive": self.is_alive(),
            "source_root": norm_path(self.source_root()),
        }

    # ---------------- 子类扩展点 ----------------
    def _run(self) -> None:
        """捕获线程主体。子类必须实现「何时触发 poll」的策略。"""
        raise NotImplementedError

    def _teardown(self) -> None:
        """释放实现相关资源（观察者、句柄等）。默认无操作。"""
        return None

    # ---------------- 工具 ----------------
    def _wait(self, seconds: float) -> bool:
        """可被 stop/flush 打断的等待。

        Returns:
            True 表示应继续运行；False 表示收到停止信号。
        """
        deadline = time.time() + max(0.05, float(seconds))
        while not self._stop_event.is_set():
            remain = deadline - time.time()
            if remain <= 0:
                return True
            if self._flush_event.wait(timeout=min(remain, 1.0)):
                return True
        return False

    def _consume_flush(self) -> Optional[str]:
        """若存在待处理的强制 flush 请求，消费之并返回其 reason。"""
        if self._flush_event.is_set():
            self._flush_event.clear()
            reason = self._flush_reason or "manual"
            self._flush_reason = "interval"
            return reason
        return None
