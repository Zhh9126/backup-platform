# -*- coding: utf-8 -*-
"""实时保护健康监控与告警。

负责把 :class:`core.rt_backup.types.RtHealth` 汇总成看板可用的口径，并在
RPO 超标 / 守护失败 / 磁盘配额逼近时产生告警（带抑制窗口，避免刷屏）。

数据来源优先级：
  1. 进程内在管 worker 的实时 ``health()``（最准）；
  2. ``rt_capture_state`` 表的落库快照（守护未启动或跨进程时兜底）。

告警抑制：同一 ``(task_id, code)`` 在 ``config.RT_ALERT_SUPPRESS_MIN`` 分钟内
只发一次，进程内内存计时，重启后重新计时。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import config
import core.db as db
import core.models as models

from .types import (
    FILE_DB_TYPE,
    HEALTH_GREEN,
    HEALTH_RED,
    HEALTH_UNKNOWN,
    HEALTH_YELLOW,
    KIND_DB_LOG,
    KIND_FILE,
    RtConfig,
    RtHealth,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_STOPPED,
)

# 告警码
ALERT_RPO_BREACH = "rt_rpo_breach"
ALERT_DAEMON_FAILED = "rt_daemon_failed"
ALERT_DAEMON_DEGRADED = "rt_daemon_degraded"
ALERT_DISK_QUOTA = "rt_disk_quota"

# 告警级别映射
_ALERT_LEVEL = {
    ALERT_RPO_BREACH: "warning",
    ALERT_DAEMON_FAILED: "error",
    ALERT_DAEMON_DEGRADED: "warning",
    ALERT_DISK_QUOTA: "warning",
}


class RtHealthMonitor:
    """实时保护健康监控器。无状态查询 + 进程内告警抑制表。"""

    def __init__(self, logger=None) -> None:
        self.logger = logger or db.get_logger("rt.health")
        self._suppress: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ==================================================================
    # 健康快照
    # ==================================================================
    def of(self, task_id: int) -> RtHealth:
        """单任务健康。优先取在管 worker，回落状态表。"""
        task_id = int(task_id)
        try:
            from .supervisor import get_supervisor
            return get_supervisor().status_of(task_id)
        except Exception as exc:  # pragma: no cover —— 守护不可用时降级
            self.logger.debug("[rt.health] supervisor 不可用，回落状态表: %s", exc)
            return self._from_state(task_id)

    def snapshot(self) -> List[dict]:
        """所有开启实时保护的任务的健康列表。"""
        try:
            tasks = models.list_rt_tasks(only_enabled=False)
        except Exception as exc:
            self.logger.warning("[rt.health] 读取实时任务失败: %s", exc)
            tasks = []

        out: List[dict] = []
        for task in tasks:
            task_id = int(task.get("id") or 0)
            if task_id <= 0:
                continue
            try:
                health = self.of(task_id)
            except Exception as exc:
                self.logger.warning("[rt.health] task=%s 取健康失败: %s",
                                    task_id, exc)
                continue
            if not health.task_name:
                health.task_name = task.get("name") or f"task_{task_id}"
            data = health.to_dict()
            data["enabled"] = bool(task.get("enabled", 1))
            data["rt_enabled"] = bool(task.get("rt_enabled"))
            out.append(data)
        return out

    def summary(self) -> dict:
        """看板汇总：健康灯分布 + RPO 达标率。"""
        items = self.snapshot()
        counter = {HEALTH_GREEN: 0, HEALTH_YELLOW: 0,
                   HEALTH_RED: 0, HEALTH_UNKNOWN: 0}
        breach = 0
        rp_today = 0
        bytes_today = 0
        for item in items:
            key = item.get("health") or HEALTH_UNKNOWN
            counter[key] = counter.get(key, 0) + 1
            if item.get("is_breach"):
                breach += 1
            rp_today += int(item.get("rp_count_today") or 0)
            bytes_today += int(item.get("bytes_today") or 0)

        total = len(items)
        measured = total - counter.get(HEALTH_UNKNOWN, 0)
        compliance = round(100.0 * (measured - breach) / measured, 1) if measured else 0.0
        return {
            "total": total,
            "green": counter.get(HEALTH_GREEN, 0),
            "yellow": counter.get(HEALTH_YELLOW, 0),
            "red": counter.get(HEALTH_RED, 0),
            "unknown": counter.get(HEALTH_UNKNOWN, 0),
            "breach": breach,
            "rpo_compliance_pct": compliance,
            "rp_count_today": rp_today,
            "bytes_today": bytes_today,
            "bytes_today_human": db.human_size(bytes_today),
        }

    # ==================================================================
    # 告警
    # ==================================================================
    def check_alerts(self, emit: bool = True) -> List[dict]:
        """扫描所有实时任务，产出需要告警的条目。

        Args:
            emit: True 时把未被抑制的告警写入系统日志（``db.add_log``）。

        Returns:
            告警列表 ``[{task_id, task_name, code, level, message, suppressed}]``
        """
        alerts: List[dict] = []
        for item in self.snapshot():
            if not item.get("rt_enabled"):
                continue
            for alert in self._alerts_for(item):
                alert["suppressed"] = not self._allow(alert["task_id"],
                                                      alert["code"])
                if emit and not alert["suppressed"]:
                    db.add_log(alert["level"], "rt.health", alert["message"])
                alerts.append(alert)
        return alerts

    @staticmethod
    def _alerts_for(item: dict) -> List[dict]:
        """把一条健康快照翻译成 0..N 条告警。"""
        out: List[dict] = []
        task_id = int(item.get("task_id") or 0)
        name = item.get("task_name") or f"task_{task_id}"
        status = item.get("daemon_status") or STATUS_STOPPED

        if status == STATUS_FAILED:
            out.append({
                "task_id": task_id, "task_name": name,
                "code": ALERT_DAEMON_FAILED,
                "level": _ALERT_LEVEL[ALERT_DAEMON_FAILED],
                "message": (f"实时保护守护已失败：{name}"
                            f"（连续失败 {item.get('consecutive_fail', 0)} 次，"
                            f"{item.get('last_error') or '无详细错误'}）"),
            })
        elif status == STATUS_DEGRADED:
            out.append({
                "task_id": task_id, "task_name": name,
                "code": ALERT_DAEMON_DEGRADED,
                "level": _ALERT_LEVEL[ALERT_DAEMON_DEGRADED],
                "message": (f"实时保护已降级运行：{name}"
                            f"（{item.get('degrade_reason') or '原因未知'}）"),
            })

        if item.get("is_breach"):
            out.append({
                "task_id": task_id, "task_name": name,
                "code": ALERT_RPO_BREACH,
                "level": _ALERT_LEVEL[ALERT_RPO_BREACH],
                "message": (f"RPO 超标：{name} 实际 "
                            f"{item.get('rpo_actual_sec', 0)}s > 目标 "
                            f"{item.get('rpo_target_sec', 0)}s"),
            })
        return out

    def _allow(self, task_id: int, code: str) -> bool:
        """抑制窗口判定：允许发送返回 True，并记录本次发送时间。"""
        window = max(0, int(getattr(config, "RT_ALERT_SUPPRESS_MIN", 10))) * 60
        key = f"{task_id}:{code}"
        now = time.time()
        with self._lock:
            last = self._suppress.get(key, 0.0)
            if window > 0 and (now - last) < window:
                return False
            self._suppress[key] = now
            return True

    def reset_suppression(self, task_id: Optional[int] = None) -> None:
        """清空抑制表（测试与人工复位用）。"""
        with self._lock:
            if task_id is None:
                self._suppress.clear()
                return
            prefix = f"{int(task_id)}:"
            for key in [k for k in self._suppress if k.startswith(prefix)]:
                self._suppress.pop(key, None)

    # ==================================================================
    # 内部
    # ==================================================================
    @staticmethod
    def _from_state(task_id: int) -> RtHealth:
        """纯粹从 ``rt_capture_state`` 还原健康（守护未启动时的兜底）。"""
        row = {}
        task = {}
        try:
            row = models.get_rt_state(task_id) or {}
        except Exception:
            row = {}
        try:
            task = models.get_task(task_id) or {}
        except Exception:
            task = {}

        health = RtHealth(
            task_id=int(task_id),
            task_name=task.get("name") or f"task_{task_id}",
            capture_kind=row.get("capture_kind")
            or (KIND_FILE if (task.get("db_type") == FILE_DB_TYPE) else KIND_DB_LOG),
            engine=row.get("engine") or (task.get("db_type") or ""),
            daemon_status=row.get("daemon_status") or STATUS_STOPPED,
            degrade_reason=row.get("degrade_reason") or "",
            watcher_impl=row.get("watcher_impl") or "",
            lag_sec=int(row.get("lag_sec") or 0),
            rpo_actual_sec=int(row.get("rpo_actual_sec") or 0),
            rpo_target_sec=(RtConfig.from_task(task).rpo_target_sec
                            if task else 300),
            last_rp_at=row.get("last_rp_at") or "",
            last_capture_at=row.get("last_capture_at") or "",
            restart_count=int(row.get("restart_count") or 0),
            consecutive_fail=int(row.get("consecutive_fail") or 0),
            rp_count_today=int(row.get("rp_count_today") or 0),
            bytes_today=int(row.get("bytes_today") or 0),
            last_error=row.get("last_error") or "",
            last_heartbeat_at=row.get("last_heartbeat_at") or "",
        )
        health.health = health.compute_health()
        return health


# ======================================================================
# 模块级便捷入口
# ======================================================================
_monitor: Optional[RtHealthMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> RtHealthMonitor:
    """进程内共享的监控器单例（抑制表需要跨调用保持）。"""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = RtHealthMonitor()
    return _monitor


def summary() -> dict:
    """便捷入口：看板汇总。"""
    return get_monitor().summary()


def snapshot() -> List[dict]:
    """便捷入口：全部任务健康列表。"""
    return get_monitor().snapshot()


def check_alerts(emit: bool = True) -> List[dict]:
    """便捷入口：告警扫描。"""
    return get_monitor().check_alerts(emit=emit)
