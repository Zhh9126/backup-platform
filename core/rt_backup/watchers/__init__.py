# -*- coding: utf-8 -*-
"""
文件变更捕获器工厂与能力探测。

选择策略（设计文档 §1.3.2）::

    rt_mode == 'polling'                        → PollingWatcher
    rt_mode == 'watchdog'                       → WatchdogWatcher（不可用则降级并记原因）
    source_type == 'remote'                     → PollingWatcher（远程无事件源）
    watchdog 不可导入                            → PollingWatcher + degrade_reason
    Linux 且监控目录数 > max_user_watches*0.8    → PollingWatcher + degrade_reason
    否则（auto）                                 → WatchdogWatcher

**任何降级都不报错**，只把原因写进 ``watcher.degrade_reason``，
最终落到 ``rt_capture_state.degrade_reason`` 并在 UI 上以黄色提示展示。
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Optional

import config
import core.db as db

from ..types import ChangeBatch, RtConfig
from .base import FileChangeWatcher, RT_SNAPSHOT_NAMESPACE
from .polling import PollingWatcher
from .watchdog_watcher import (
    WatchdogWatcher,
    _count_dirs,
    _import_watchdog,
    _inotify_max_watches,
)

__all__ = [
    "FileChangeWatcher", "PollingWatcher", "WatchdogWatcher",
    "RT_SNAPSHOT_NAMESPACE", "WATCHER_REGISTRY",
    "create_watcher", "probe_capabilities",
]

# 实现注册表（供 UI 下拉与配置校验使用）
WATCHER_REGISTRY = {
    PollingWatcher.impl_key: PollingWatcher,
    WatchdogWatcher.impl_key: WatchdogWatcher,
}


def create_watcher(task: dict, rt_config: RtConfig,
                   on_batch: Callable[[ChangeBatch], None],
                   logger=None) -> FileChangeWatcher:
    """按环境与配置创建最合适的文件变更捕获器。

    Args:
        task: backup_tasks 行（需含 extra_options 以解析源配置）。
        rt_config: 任务级实时配置。
        on_batch: 变更批次回调。
        logger: 日志器，缺省 ``rt.watcher``。

    Returns:
        已构造但**未启动**的 FileChangeWatcher 实例。调用方负责 ``start()``。
        永不抛异常——任何不满足条件的情形都降级为 :class:`PollingWatcher`。
    """
    logger = logger or db.get_logger("rt.watcher")
    mode = (getattr(rt_config, "mode", "") or "auto").strip().lower()

    def _make(cls, reason: str = "") -> FileChangeWatcher:
        watcher = cls(task, rt_config, on_batch, logger=logger)
        if reason:
            watcher.degrade_reason = reason
        return watcher

    # 先用一个轻量实例解析源配置（构造 FileBackupEngine 不产生任何 IO 副作用）
    probe = PollingWatcher(task, rt_config, on_batch, logger=logger)
    source_cfg = probe.source_cfg

    # 1) 显式指定轮询
    if mode == PollingWatcher.impl_key:
        return probe

    # 2) 显式指定事件驱动
    if mode == WatchdogWatcher.impl_key:
        ok, reason = WatchdogWatcher.is_available(source_cfg)
        if ok:
            return _make(WatchdogWatcher)
        logger.warning("[rt.watcher] task=%s 指定 watchdog 但不可用，降级轮询: %s",
                       rt_config.task_id, reason)
        probe.degrade_reason = reason
        return probe

    # 3) auto：远程源直接轮询
    if (source_cfg.get("type") or "local") != "local":
        probe.degrade_reason = ""  # 远程走轮询是设计内的正常路径，不算降级
        return probe

    # 4) auto：本地源尝试升级到事件驱动
    ok, reason = WatchdogWatcher.is_available(source_cfg)
    if ok:
        return _make(WatchdogWatcher)
    probe.degrade_reason = reason
    if reason:
        logger.info("[rt.watcher] task=%s 使用轮询: %s", rt_config.task_id, reason)
    return probe


def probe_capabilities() -> dict:
    """环境自检：watchdog 是否可用、inotify 余量、各实现可用性。

    供「实时保护」页面的环境自检面板与 ``/api/rt_backup/capabilities`` 使用。
    本函数不接受任务参数，只探测进程级能力，绝不抛异常。
    """
    observer_cls, _handler_cls, reason = _import_watchdog()
    watchdog_ok = observer_cls is not None

    version = ""
    if watchdog_ok:
        try:
            import watchdog  # type: ignore
            version = getattr(watchdog, "__version__", "") or ""
        except Exception:
            version = ""

    result = {
        "platform": sys.platform,
        "default_mode": getattr(config, "RT_FILE_WATCHER", "auto"),
        "implementations": [
            {
                "key": PollingWatcher.impl_key,
                "name": PollingWatcher.display_name,
                "available": True,
                "reason": "",
                "supports_remote": True,
                "packages": list(PollingWatcher.required_packages),
            },
            {
                "key": WatchdogWatcher.impl_key,
                "name": WatchdogWatcher.display_name,
                "available": watchdog_ok,
                "reason": "" if watchdog_ok else (reason or "watchdog 未安装"),
                "supports_remote": False,
                "packages": list(WatchdogWatcher.required_packages),
            },
        ],
        "watchdog": {
            "installed": watchdog_ok,
            "version": version,
            "reason": "" if watchdog_ok else (reason or "watchdog 未安装"),
            "hint": "" if watchdog_ok else "pip install watchdog>=4.0 可获得秒级 RPO",
        },
    }

    if sys.platform.startswith("linux"):
        max_watches = _inotify_max_watches()
        result["inotify"] = {
            "max_user_watches": max_watches,
            "safe_dir_budget": int(max_watches * 0.8) if max_watches else 0,
            "hint": ("sysctl fs.inotify.max_user_watches=524288 可提升可监控目录数"
                     if max_watches and max_watches < 100000 else ""),
        }
    else:
        result["inotify"] = {"max_user_watches": 0, "safe_dir_budget": 0,
                             "hint": "非 Linux 平台，事件驱动由 ReadDirectoryChangesW 提供"}

    return result


def estimate_watch_cost(source_path: str) -> dict:
    """估算某源目录使用事件驱动所需的 watch 数量（Linux 诊断用）。

    Args:
        source_path: 源根目录。

    Returns:
        ``{'dirs': int, 'max_user_watches': int, 'fits': bool}``。
    """
    dirs = _count_dirs(source_path) if source_path else 0
    max_watches = _inotify_max_watches()
    fits = True
    if sys.platform.startswith("linux") and max_watches > 0:
        fits = dirs <= max_watches * 0.8
    return {
        "dirs": dirs,
        "max_user_watches": max_watches,
        "fits": fits,
        "path": (source_path or "").replace("\\", "/"),
        "exists": bool(source_path) and os.path.isdir(source_path),
    }
