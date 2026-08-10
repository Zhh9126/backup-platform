# -*- coding: utf-8 -*-
"""准 CDP 实时备份子包门面。

对外只暴露少量稳定入口，内部模块（supervisor / file_rt / pitr / health）
一律**惰性导入**，保证：
  1. 任何一个子模块的可选依赖缺失都不会拖垮 ``import core.rt_backup``；
  2. 不与 ``core.scheduler`` / ``core.models`` 形成模块级循环导入。

典型用法::

    from core import rt_backup
    rt_backup.start()                     # 由 scheduler 在进程启动时调用
    rt_backup.status()                    # 看板汇总
    rt_backup.trigger_now(task_id=7)      # 手动立即捕获
    rt_backup.stop()                      # 进程退出前清理子进程/线程
"""
from __future__ import annotations

from typing import Optional

from .types import (  # noqa: F401  —— 供外部直接 from core.rt_backup import RtConfig
    ChangeBatch,
    RecoveryPoint,
    RestorePlan,
    RtConfig,
    RtHealth,
    DB_LOG_ENGINES,
    HEALTH_GREEN,
    HEALTH_RED,
    HEALTH_UNKNOWN,
    HEALTH_YELLOW,
    KIND_DB_LOG,
    KIND_FILE,
    RP_BASE_FULL,
    RP_DB_FULL,
    RP_DB_LOG,
    RP_FILE_INC,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
)
from .journal import RecoveryJournal  # noqa: F401
from .repo import LogRepository  # noqa: F401

__all__ = [
    "ChangeBatch", "RecoveryPoint", "RestorePlan", "RtConfig", "RtHealth",
    "RecoveryJournal", "LogRepository",
    "DB_LOG_ENGINES", "KIND_DB_LOG", "KIND_FILE",
    "RP_BASE_FULL", "RP_DB_FULL", "RP_DB_LOG", "RP_FILE_INC",
    "HEALTH_GREEN", "HEALTH_YELLOW", "HEALTH_RED", "HEALTH_UNKNOWN",
    "STATUS_STOPPED", "STATUS_STARTING", "STATUS_RUNNING",
    "STATUS_DEGRADED", "STATUS_FAILED",
    "get_supervisor", "start", "stop", "status", "status_of",
    "trigger_now", "reconcile", "restart_worker",
    "get_journal", "get_pitr", "get_health_monitor", "probe_capabilities",
]


def get_supervisor():
    """返回进程内 RtSupervisor 单例（惰性导入，避免启动期循环依赖）。"""
    from .supervisor import get_supervisor as _get
    return _get()


def start(scheduler=None) -> bool:
    """启动实时备份守护。

    Args:
        scheduler: 可选的外部 APScheduler 实例（由 ``core.scheduler`` 在进程启动时
            传入），复用同一调度器驱动 Supervisor 主循环；为 None 时 Supervisor 自建
            BackgroundScheduler（多 worker 部署时仅抢到锁的进程会自建）。

    Returns:
        抢锁失败 / 总开关关闭时返回 False（不抛异常）。
    """
    return get_supervisor().start(scheduler)


def stop(timeout: float = 15.0) -> None:
    """停止守护并释放单实例锁。幂等。"""
    get_supervisor().stop(timeout=timeout)


def status() -> dict:
    """守护总体状态 + 各 worker 健康。"""
    return get_supervisor().status()


def status_of(task_id: int) -> "RtHealth":
    """单任务健康快照。"""
    return get_supervisor().status_of(int(task_id))


def reconcile() -> dict:
    """立即对账（任务配置保存后调用）。"""
    return get_supervisor().reconcile()


def trigger_now(task_id: int, reason: str = "manual") -> dict:
    """手动触发一次立即捕获。"""
    return get_supervisor().trigger_now(int(task_id), reason=reason)


def restart_worker(task_id: int) -> dict:
    """人工复位处于 failed 状态的 worker。"""
    return get_supervisor().restart_worker(int(task_id))


def get_journal(logger=None) -> "RecoveryJournal":
    """构造一个 RecoveryJournal 实例（无状态，可随用随建）。"""
    return RecoveryJournal(logger=logger)


def get_pitr(logger=None):
    """构造 PITRRestore 实例（惰性导入）。"""
    from .pitr import PITRRestore
    return PITRRestore(logger=logger)


def get_health_monitor(logger=None):
    """构造 RtHealthMonitor 实例（惰性导入）。"""
    from .health import RtHealthMonitor
    return RtHealthMonitor(logger=logger)


def probe_capabilities() -> dict:
    """环境自检：watchdog / mysqlbinlog / pg_receivewal / inotify 上限。"""
    from .watchers import probe_capabilities as _probe_watchers
    caps = {"watchers": _probe_watchers()}
    try:
        from core.cdc import probe_clients
        caps["cdc"] = probe_clients()
    except Exception as exc:  # pragma: no cover —— 自检不允许影响页面
        caps["cdc"] = {"error": str(exc)}
    return caps


def get_repo(task_id: int, capture_kind: str = KIND_FILE,
             logger=None) -> "LogRepository":
    """构造某任务的日志仓库句柄。"""
    return LogRepository(int(task_id), capture_kind=capture_kind, logger=logger)


def latest_point(task_id: int, kind: Optional[str] = None):
    """便捷入口：取某任务最近一个恢复点。"""
    return RecoveryJournal().latest(int(task_id), kind=kind)
