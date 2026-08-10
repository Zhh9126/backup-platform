# -*- coding: utf-8 -*-
"""
T03 实时备份守护总控（RtSupervisor）。

职责：
1. **单实例保证**：进程级 ``os.open(O_CREAT|O_EXCL)`` 文件锁，
   多 worker 部署（gunicorn -w N）时只有一个进程真正跑守护，其余静默退出；
2. **worker 生命周期**：按 ``backup_tasks.rt_enabled`` 对账，
   文件任务 → :class:`~core.rt_backup.file_rt.FileRtCapture`（``file_watch``），
   数据库任务 → :class:`~core.rt_backup.db_rt.DbRtCapture`（``db_cdc``）；
3. **周期驱动**：APScheduler ``IntervalTrigger``，job 必须
   ``max_instances=1`` + ``coalesce=True``（否则 tick 堆积会撕裂状态机）；
4. **自愈**：worker 掉线按 ``RT_RESTART_BACKOFF_SEC`` 退避重启，
   超过 ``RT_MAX_RESTART`` 后置 failed 并等待人工 ``restart_worker``。

APScheduler 不可用时自动回落到一个 daemon 线程，保证守护能力不依赖可选依赖。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

try:  # Python 3.8+ 标准库；低版本回落到鸭子类型
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

    def runtime_checkable(cls):  # type: ignore
        return cls

import config
import core.db as db
import core.models as models

from .types import (
    KIND_DB_LOG,
    KIND_FILE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    FILE_DB_TYPE,
    RtConfig,
    RtHealth,
)

_JOB_ID = "rt_supervisor_tick"


@runtime_checkable
class _RtWorker(Protocol):
    """实时捕获 worker 协议。

    ``FileRtCapture``（T02）与 ``DbRtCapture``（T03）都实现本协议，
    Supervisor 只面向协议编程，不感知文件/数据库的差异。
    """

    task_id: int
    capture_kind: str
    rt_mode: str

    def start(self) -> bool:
        """启动捕获。幂等，失败返回 False 而非抛异常。"""
        ...

    def stop(self, timeout: float = 10.0) -> None:
        """停止捕获并释放资源。幂等。"""
        ...

    def is_alive(self) -> bool:
        """捕获是否存活。"""
        ...

    def tick(self) -> dict:
        """一次周期驱动，返回本轮摘要。"""
        ...

    def trigger_now(self, reason: str = "manual") -> dict:
        """手动触发一次立即捕获。"""
        ...

    def health(self) -> RtHealth:
        """当前健康快照。"""
        ...

    def config_fingerprint(self) -> str:
        """配置指纹；变化时 Supervisor 重建 worker。"""
        ...


class RtSupervisor:
    """实时备份守护总控。进程内单例，通过 :func:`get_supervisor` 获取。"""

    def __init__(self, logger=None) -> None:
        self.logger = logger or db.get_logger("rt.supervisor")
        self.workers: Dict[int, object] = {}
        self.fingerprints: Dict[int, str] = {}
        self.restart_budget: Dict[int, int] = {}
        self.next_retry_at: Dict[int, float] = {}

        self._lock: threading.RLock = threading.RLock()
        self._lock_fd: Optional[int] = None
        self._running: bool = False
        self._owns_scheduler: bool = False
        self._scheduler = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._tick_count: int = 0
        self._started_at: str = ""
        self._last_tick_at: str = ""
        self._last_error: str = ""

    # ==================================================================
    # 单实例锁
    # ==================================================================
    def _acquire_lock(self) -> bool:
        """抢占单实例文件锁。

        Returns:
            True 表示本进程获得守护权；False 表示已有活跃守护进程。
        """
        path = config.RT_LOCK_FILE
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except OSError as exc:
            self._last_error = f"创建锁目录失败: {exc}"
            return False

        for attempt in (1, 2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 1 and self._clear_stale_lock(path):
                    continue
                self.logger.info("[rt.supervisor] 已有守护进程持有锁 %s，本进程不启动",
                                 path.replace("\\", "/"))
                return False
            except OSError as exc:
                self._last_error = f"获取单实例锁失败: {exc}"
                self.logger.warning("[rt.supervisor] %s", self._last_error)
                return False
            else:
                try:
                    os.write(fd, f"{os.getpid()}\n{db.now_iso()}\n".encode("utf-8"))
                except OSError:
                    pass
                self._lock_fd = fd
                return True
        return False

    def _clear_stale_lock(self, path: str) -> bool:
        """清理陈旧锁（心跳超过 ``RT_LOCK_STALE_SEC`` 未刷新）。

        Returns:
            True 表示确实清掉了一个陈旧锁，调用方可以重试抢占。
        """
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            return False
        if age < max(10, int(config.RT_LOCK_STALE_SEC)):
            return False
        try:
            os.unlink(path)
        except OSError:
            return False
        self.logger.warning("[rt.supervisor] 清理陈旧单实例锁（%.0fs 未刷新）", age)
        return True

    def _touch_lock(self) -> None:
        """刷新锁文件 mtime 作为心跳，供其它进程判定存活。"""
        if self._lock_fd is None:
            return
        try:
            os.utime(config.RT_LOCK_FILE, None)
        except OSError:
            pass

    def _release_lock(self) -> None:
        """释放单实例锁。幂等。"""
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        try:
            if os.path.exists(config.RT_LOCK_FILE):
                os.unlink(config.RT_LOCK_FILE)
        except OSError:
            pass

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self, scheduler=None) -> bool:
        """启动守护。

        Args:
            scheduler: 可选的外部 APScheduler 实例（``core.scheduler`` 传入）。
                为 None 时 Supervisor 自建 BackgroundScheduler；
                APScheduler 不可用时回落到 daemon 线程。

        Returns:
            True 表示本进程已成为守护；False 表示总开关关闭或未抢到锁。
        """
        with self._lock:
            if self._running:
                return True
            if not config.RT_BACKUP_ENABLED:
                self.logger.info("[rt.supervisor] 实时备份总开关关闭 "
                                 "(RT_BACKUP_ENABLED=false)")
                return False
            if not self._acquire_lock():
                return False

            self._stop_event.clear()
            self._running = True
            self._started_at = db.now_iso()
            self._tick_count = 0

        try:
            summary = self.reconcile()
        except Exception as exc:
            summary = {"error": str(exc)}
            self.logger.error("[rt.supervisor] 首次对账异常: %s", exc)

        self._start_driver(scheduler)
        self.logger.info("[rt.supervisor] 已启动，管理 %d 个实时任务",
                         len(self.workers))
        db.add_log("info", "rt.supervisor",
                   f"实时备份守护已启动，管理 {len(self.workers)} 个任务 "
                   f"（新增 {len(summary.get('started', []))}）")
        return True

    def _start_driver(self, scheduler=None) -> None:
        """注册周期驱动。优先 APScheduler，失败回落线程。"""
        tick_sec = max(2, int(config.RT_SUPERVISOR_TICK_SEC))
        if scheduler is not None:
            self._scheduler = scheduler
            self._owns_scheduler = False
        else:
            try:
                from apscheduler.schedulers.background import BackgroundScheduler
                self._scheduler = BackgroundScheduler()
                self._owns_scheduler = True
            except Exception as exc:
                self.logger.warning("[rt.supervisor] APScheduler 不可用（%s），"
                                    "回落线程驱动", exc)
                self._scheduler = None
                self._owns_scheduler = False

        if self._scheduler is not None:
            try:
                from apscheduler.triggers.interval import IntervalTrigger
                try:
                    self._scheduler.remove_job(_JOB_ID)
                except Exception:
                    pass
                # max_instances=1 + coalesce=True：tick 绝不并发、堆积只跑一次
                self._scheduler.add_job(
                    self._safe_tick, IntervalTrigger(seconds=tick_sec),
                    id=_JOB_ID, replace_existing=True,
                    max_instances=1, coalesce=True, misfire_grace_time=tick_sec * 3)
                if self._owns_scheduler and not self._scheduler.running:
                    self._scheduler.start()
                self.logger.info("[rt.supervisor] 已注册 tick job（每 %ds，"
                                 "max_instances=1, coalesce=True）", tick_sec)
                return
            except Exception as exc:
                self.logger.warning("[rt.supervisor] 注册 tick job 失败（%s），"
                                    "回落线程驱动", exc)
                self._scheduler = None
                self._owns_scheduler = False

        self._thread = threading.Thread(target=self._thread_loop,
                                        name="rt-supervisor", daemon=True)
        self._thread.start()

    def _thread_loop(self) -> None:
        """线程驱动回落路径。"""
        tick_sec = max(2, int(config.RT_SUPERVISOR_TICK_SEC))
        while not self._stop_event.is_set():
            self._safe_tick()
            self._stop_event.wait(timeout=tick_sec)

    def stop(self, timeout: float = 15.0) -> None:
        """停止守护、回收所有 worker 并释放锁。幂等。"""
        with self._lock:
            if not self._running and not self.workers:
                self._release_lock()
                return
            self._running = False
        self._stop_event.set()

        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(_JOB_ID)
            except Exception:
                pass
            if self._owns_scheduler:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:
                    pass
            self._scheduler = None
            self._owns_scheduler = False

        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(1.0, float(timeout) / 3))
        self._thread = None

        per_worker = max(1.0, float(timeout) / max(1, len(self.workers)))
        for task_id, worker in list(self.workers.items()):
            try:
                worker.stop(timeout=per_worker)
            except Exception as exc:
                self.logger.warning("[rt.supervisor] task=%s 停止异常: %s",
                                    task_id, exc)
        self.workers.clear()
        self.fingerprints.clear()
        self._release_lock()
        self.logger.info("[rt.supervisor] 已停止")

    def is_running(self) -> bool:
        """守护是否在本进程运行。"""
        return self._running

    # ==================================================================
    # 对账
    # ==================================================================
    def reconcile(self) -> dict:
        """按 ``backup_tasks.rt_enabled`` 对账 worker 集合。

        - 新增开启实时的任务 → 创建并启动 worker；
        - 关闭实时 / 被删除的任务 → 停止并移除 worker；
        - 配置指纹变化的任务 → 重建 worker（先 stop 后 start）。

        Returns:
            ``{'started': [...], 'stopped': [...], 'rebuilt': [...], 'total': n}``
        """
        started: List[int] = []
        stopped: List[int] = []
        rebuilt: List[int] = []

        try:
            tasks = models.list_rt_tasks(only_enabled=True)
        except Exception as exc:
            self.logger.error("[rt.supervisor] 读取实时任务失败: %s", exc)
            return {"started": [], "stopped": [], "rebuilt": [],
                    "total": len(self.workers), "error": str(exc)}

        wanted = {int(t["id"]): t for t in tasks if t.get("id")}

        with self._lock:
            # ① 下线不再需要的 worker
            for task_id in list(self.workers.keys()):
                if task_id in wanted:
                    continue
                self._stop_worker(task_id)
                stopped.append(task_id)

            # ② 上线 / 重建
            for task_id, task in wanted.items():
                rt_cfg = RtConfig.from_task(task)
                worker = self.workers.get(task_id)
                if worker is None:
                    if self._spawn_worker(task, rt_cfg):
                        started.append(task_id)
                    continue
                fingerprint = ""
                try:
                    fingerprint = worker.config_fingerprint()
                except Exception:
                    fingerprint = self.fingerprints.get(task_id, "")
                if fingerprint and fingerprint != self.fingerprints.get(task_id):
                    self.logger.info("[rt.supervisor] task=%s 配置变更，重建 worker",
                                     task_id)
                    self._stop_worker(task_id)
                    if self._spawn_worker(task, rt_cfg):
                        rebuilt.append(task_id)

        return {"started": started, "stopped": stopped, "rebuilt": rebuilt,
                "total": len(self.workers)}

    def _spawn_worker(self, task: dict, rt_cfg: RtConfig = None) -> bool:
        """创建并启动一个 worker。失败只记录，不抛异常。"""
        task_id = int(task.get("id") or 0)
        rt_cfg = rt_cfg or RtConfig.from_task(task)
        try:
            worker = self._build_worker(task, rt_cfg)
        except Exception as exc:
            self._last_error = f"task={task_id} 创建 worker 失败: {exc}"
            self.logger.error("[rt.supervisor] %s", self._last_error)
            return False

        try:
            ok = bool(worker.start())
        except Exception as exc:
            self._last_error = f"task={task_id} 启动 worker 失败: {exc}"
            self.logger.error("[rt.supervisor] %s", self._last_error)
            ok = False

        self.workers[task_id] = worker
        try:
            self.fingerprints[task_id] = worker.config_fingerprint()
        except Exception:
            self.fingerprints[task_id] = ""
        self.restart_budget.setdefault(task_id, 0)
        return ok

    @staticmethod
    def _build_worker(task: dict, rt_cfg: RtConfig):
        """按 db_type 选择 worker 实现。"""
        db_type = (task.get("db_type") or "").lower()
        if db_type == FILE_DB_TYPE:
            from .file_rt import FileRtCapture
            return FileRtCapture(task, rt_cfg)
        from .db_rt import DbRtCapture
        return DbRtCapture(task, rt_cfg)

    def _stop_worker(self, task_id: int) -> None:
        """停止并移除一个 worker。"""
        worker = self.workers.pop(int(task_id), None)
        self.fingerprints.pop(int(task_id), None)
        if worker is None:
            return
        try:
            worker.stop(timeout=10.0)
        except Exception as exc:
            self.logger.warning("[rt.supervisor] task=%s 停止异常: %s", task_id, exc)

    # ==================================================================
    # 周期驱动
    # ==================================================================
    def _safe_tick(self) -> dict:
        """tick 的异常隔离包装：任何异常都不允许打断调度。"""
        try:
            return self.tick()
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.exception("[rt.supervisor] tick 异常: %s", exc)
            return {"error": str(exc)}

    def tick(self) -> dict:
        """一次总控周期：对账（低频）→ 逐 worker tick → 自愈。"""
        self._tick_count += 1
        self._last_tick_at = db.now_iso()
        self._touch_lock()

        # 每 6 个 tick 做一次对账（默认 10s tick → 每分钟一次）
        reconciled = {}
        if self._tick_count % 6 == 1:
            reconciled = self.reconcile()

        results = []
        for task_id, worker in list(self.workers.items()):
            try:
                results.append(worker.tick())
            except Exception as exc:
                self.logger.error("[rt.supervisor] task=%s tick 异常: %s",
                                  task_id, exc)
                results.append({"task_id": task_id, "error": str(exc)})
            self._heal(task_id, worker)

        return {
            "tick": self._tick_count,
            "at": self._last_tick_at,
            "workers": len(self.workers),
            "reconciled": reconciled,
            "results": results,
        }

    def _heal(self, task_id: int, worker) -> None:
        """worker 掉线时按退避预算自愈重启。"""
        try:
            if worker.is_alive():
                self.restart_budget[task_id] = 0
                self.next_retry_at.pop(task_id, None)
                return
        except Exception:
            return

        now = time.time()
        if now < self.next_retry_at.get(task_id, 0.0):
            return

        used = self.restart_budget.get(task_id, 0)
        if used >= max(1, int(config.RT_MAX_RESTART)):
            if getattr(worker, "daemon_status", "") != STATUS_FAILED:
                worker.daemon_status = STATUS_FAILED
                worker.last_error = (
                    f"已连续重启 {used} 次仍不可用，已停止自动重试，"
                    f"请排查后手动复位")
                self.logger.error("[rt.supervisor] task=%s %s",
                                  task_id, worker.last_error)
                db.add_log("error", "rt.supervisor",
                           f"任务 {task_id} 实时捕获连续重启 {used} 次失败，已挂起")
            return

        backoff_list = list(config.RT_RESTART_BACKOFF_SEC) or [30]
        backoff = backoff_list[min(used, len(backoff_list) - 1)]
        self.restart_budget[task_id] = used + 1
        self.next_retry_at[task_id] = now + backoff
        try:
            worker.restart_count = getattr(worker, "restart_count", 0) + 1
        except Exception:
            pass
        self.logger.warning("[rt.supervisor] task=%s 捕获掉线，第 %d 次重启"
                            "（下次退避 %ds）", task_id, used + 1, backoff)
        try:
            worker.stop(timeout=5.0)
        except Exception:
            pass
        try:
            worker.start()
        except Exception as exc:
            self.logger.error("[rt.supervisor] task=%s 重启失败: %s", task_id, exc)

    # ==================================================================
    # 对外查询与操作
    # ==================================================================
    def status(self) -> dict:
        """守护总体状态 + 各 worker 健康。"""
        healths = []
        for task_id, worker in list(self.workers.items()):
            try:
                healths.append(worker.health().to_dict())
            except Exception as exc:
                healths.append({"task_id": task_id, "health": "unknown",
                                "last_error": str(exc)})
        breach = sum(1 for h in healths if h.get("is_breach"))
        return {
            "running": self._running,
            "enabled": bool(config.RT_BACKUP_ENABLED),
            "has_lock": self._lock_fd is not None,
            "lock_file": config.RT_LOCK_FILE.replace("\\", "/"),
            "started_at": self._started_at,
            "last_tick_at": self._last_tick_at,
            "tick_count": self._tick_count,
            "tick_interval_sec": int(config.RT_SUPERVISOR_TICK_SEC),
            "driver": ("apscheduler" if self._scheduler is not None
                       else ("thread" if self._thread else "none")),
            "worker_count": len(self.workers),
            "breach_count": breach,
            "workers": healths,
            "last_error": self._last_error,
        }

    def status_of(self, task_id: int) -> RtHealth:
        """单任务健康快照。worker 不存在时返回 stopped 占位。"""
        worker = self.workers.get(int(task_id))
        if worker is not None:
            try:
                return worker.health()
            except Exception as exc:
                self.logger.warning("[rt.supervisor] task=%s 取健康失败: %s",
                                    task_id, exc)
        return self._offline_health(int(task_id))

    @staticmethod
    def _offline_health(task_id: int) -> RtHealth:
        """从 ``rt_capture_state`` 还原一个离线健康快照。"""
        row = {}
        try:
            row = models.get_rt_state(task_id) or {}
        except Exception:
            row = {}
        task = {}
        try:
            task = models.get_task(task_id) or {}
        except Exception:
            task = {}
        health = RtHealth(
            task_id=task_id,
            task_name=task.get("name") or f"task_{task_id}",
            capture_kind=row.get("capture_kind")
            or (KIND_FILE if (task.get("db_type") == FILE_DB_TYPE) else KIND_DB_LOG),
            engine=row.get("engine") or (task.get("db_type") or ""),
            daemon_status=row.get("daemon_status") or STATUS_STOPPED,
            degrade_reason=row.get("degrade_reason") or "",
            watcher_impl=row.get("watcher_impl") or "",
            lag_sec=int(row.get("lag_sec") or 0),
            rpo_actual_sec=int(row.get("rpo_actual_sec") or 0),
            rpo_target_sec=RtConfig.from_task(task).rpo_target_sec if task else 300,
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

    def trigger_now(self, task_id: int, reason: str = "manual") -> dict:
        """手动触发一次立即捕获。

        worker 未在管时按需临时创建（不纳入管理），保证「守护未启动也能手动捕获」。
        """
        task_id = int(task_id)
        worker = self.workers.get(task_id)
        if worker is not None:
            try:
                return worker.trigger_now(reason=reason)
            except Exception as exc:
                return {"ok": False, "task_id": task_id, "message": str(exc)}

        try:
            task = models.get_task(task_id, include_secret=True)
        except TypeError:
            task = models.get_task(task_id)
        if not task:
            return {"ok": False, "task_id": task_id, "message": "任务不存在"}
        try:
            temp = self._build_worker(task, RtConfig.from_task(task))
            result = temp.trigger_now(reason=reason)
            result["ephemeral"] = True
            return result
        except Exception as exc:
            self.logger.error("[rt.supervisor] task=%s 临时捕获失败: %s", task_id, exc)
            return {"ok": False, "task_id": task_id, "message": str(exc)}

    def restart_worker(self, task_id: int) -> dict:
        """人工复位一个 worker（清空重启预算后重建）。"""
        task_id = int(task_id)
        self.restart_budget[task_id] = 0
        self.next_retry_at.pop(task_id, None)
        try:
            task = models.get_task(task_id, include_secret=True)
        except TypeError:
            task = models.get_task(task_id)
        if not task:
            return {"ok": False, "task_id": task_id, "message": "任务不存在"}
        with self._lock:
            self._stop_worker(task_id)
            ok = self._spawn_worker(task, RtConfig.from_task(task))
        return {"ok": ok, "task_id": task_id,
                "message": "已复位并重启实时捕获" if ok else "复位失败，请查看日志"}

    def worker_of(self, task_id: int):
        """取某任务当前在管的 worker（不存在返回 None）。"""
        return self.workers.get(int(task_id))


# ======================================================================
# 单例
# ======================================================================
_supervisor: Optional[RtSupervisor] = None
_singleton_lock = threading.Lock()


def get_supervisor() -> RtSupervisor:
    """返回进程内 RtSupervisor 单例（线程安全）。"""
    global _supervisor
    if _supervisor is None:
        with _singleton_lock:
            if _supervisor is None:
                _supervisor = RtSupervisor()
    return _supervisor


def reset_supervisor() -> None:
    """销毁单例（仅供测试使用，保证用例之间互不污染）。"""
    global _supervisor
    with _singleton_lock:
        if _supervisor is not None:
            try:
                _supervisor.stop(timeout=5.0)
            except Exception:
                pass
        _supervisor = None
