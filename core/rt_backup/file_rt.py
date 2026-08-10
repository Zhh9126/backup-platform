# -*- coding: utf-8 -*-
"""
T02 文件近实时捕获引擎（FileRtCapture）。

把 :mod:`core.rt_backup.watchers` 产出的 :class:`ChangeBatch` 粘合到
「增量归档 → 日志仓库 → Recovery Journal」这条落地链路上：

    FileChangeWatcher(watchdog | polling)
            │  on_batch(ChangeBatch)      ← 事件只做触发器
            ▼
    FileRtCapture._on_batch
            │  去重 / 合并（同一时刻只允许一次捕获在飞）
            ▼
    FileBackupEngine.capture_increment(out_dir=LogRepository.inc_dir())
            │  真值 = 快照 diff
            ▼
    RecoveryJournal.append(rp_kind='file-inc', parent_rp_id=上一个恢复点)

设计要点：
1. **绝不重复造轮子**：扫描/diff/打包/快照全部复用 ``FileBackupEngine``，
   仓库布局复用 ``LogRepository``，恢复点索引复用 ``RecoveryJournal``；
2. **事件合并**：捕获耗时期间到达的所有事件合并成一次后续捕获（``_pending``），
   避免变更风暴打爆磁盘；
3. **空包不入账**（R9）：``capture_increment`` 返回空路径时不写 journal；
4. **配额守护**（R8）：仓库用量达硬线时暂停捕获并降级，不抛异常；
5. 任何异常都被吞进 ``last_error`` 并反映到健康灯，绝不让守护线程崩掉。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import config
import core.db as db
import core.models as models

from .journal import RecoveryJournal
from .repo import LogRepository
from .types import (
    HEALTH_UNKNOWN,
    KIND_FILE,
    RP_BASE_FULL,
    RP_FILE_INC,
    RT_MODE_FILE_WATCH,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    ChangeBatch,
    RecoveryPoint,
    RtConfig,
    RtHealth,
    norm_path,
)
from .watchers import create_watcher

# 每多少个 tick 做一次过期清理（tick 间隔默认 10s → 约 30 分钟一次）
_PRUNE_EVERY_TICKS = 180


def _epoch(iso: str) -> float:
    """ISO8601 → epoch 秒；不可解析返回 0.0。"""
    if not iso:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


class FileRtCapture:
    """文件型任务的近实时捕获 worker。

    实现 :class:`core.rt_backup.supervisor._RtWorker` 协议，由 RtSupervisor
    统一 start / tick / stop。

    Attributes:
        task_id: backup_tasks.id。
        capture_kind: 恒为 ``file``。
        rt_mode: 注册到 ``rt_tasks.rt_mode`` 的类型，恒为 ``file_watch``。
    """

    capture_kind: str = KIND_FILE
    rt_mode: str = RT_MODE_FILE_WATCH

    def __init__(self, task: dict, rt_config: RtConfig = None, logger=None) -> None:
        self.task: dict = dict(task or {})
        self.task_id: int = int(self.task.get("id") or 0)
        self.task_name: str = self.task.get("name") or f"task_{self.task_id}"
        self.rt: RtConfig = rt_config or RtConfig.from_task(self.task)
        self.logger = logger or db.get_logger("rt.file")

        self.repo: LogRepository = LogRepository(self.task_id, KIND_FILE,
                                                 logger=self.logger)
        self.journal: RecoveryJournal = RecoveryJournal(logger=self.logger)
        self.watcher = create_watcher(self.task, self.rt, self._on_batch,
                                      logger=self.logger)

        # 并发控制：同一时刻只允许一次捕获在飞，其余合并为 _pending
        self._capture_lock: threading.Lock = threading.Lock()
        self._state_lock: threading.RLock = threading.RLock()
        self._pending: int = 0
        self._started: bool = False
        self._tick_count: int = 0

        # 运行统计
        self.last_error: str = ""
        self.degrade_reason: str = self.watcher.degrade_reason or ""
        self.daemon_status: str = STATUS_STOPPED
        self.last_capture_at: str = ""
        self.last_rp_at: str = ""
        self.last_rp_id: Optional[int] = None
        self.rp_count: int = 0
        self.bytes_captured: int = 0
        self.consecutive_fail: int = 0
        self.restart_count: int = 0
        self.started_at: str = ""

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    @property
    def engine(self):
        """底层 FileBackupEngine（与 watcher 共用同一实例，快照命名空间为 rt）。"""
        return self.watcher.engine

    def file_set_key(self) -> str:
        """源配置指纹，落到 ``recovery_journal.file_set_key``。"""
        try:
            key = self.engine._source_config_key()
            return "|".join([str(key[0]), str(key[1]), ",".join(key[2])])
        except Exception:
            return ""

    def config_fingerprint(self) -> str:
        """配置指纹：变化时 Supervisor 会重建 worker。"""
        return "|".join([
            str(self.task_id), self.rt.mode, str(self.rt.interval_sec),
            str(self.rt.rpo_target_sec), str(self.rt.log_retention_days),
            self.rt.consistency, self.file_set_key(),
        ])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """启动捕获。已启动时幂等返回 True。"""
        with self._state_lock:
            if self._started and self.watcher.is_alive():
                return True
            self.started_at = db.now_iso()
            self.daemon_status = STATUS_STARTING
            self._sync_rt_task_row()
            self._persist_state()

        # 基准全量：无快照/无全量归档时先做一次，失败不阻断（下轮 batch 会重试）
        try:
            self.ensure_base()
        except Exception as exc:
            self.last_error = f"建立基准全量失败: {exc}"
            self.logger.error("[rt.file] task=%s %s", self.task_id, self.last_error)

        try:
            self.watcher.start()
        except Exception as exc:
            self.last_error = f"启动捕获器失败: {exc}"
            self.daemon_status = STATUS_FAILED
            self.logger.error("[rt.file] task=%s %s", self.task_id, self.last_error)
            self._persist_state()
            return False

        with self._state_lock:
            self._started = True
            self.daemon_status = (STATUS_DEGRADED if self.degrade_reason
                                  else STATUS_RUNNING)
            self._persist_state()
        db.add_log("info", "rt.file",
                   f"任务 {self.task_name} 文件实时捕获已启动"
                   f"（{self.watcher.impl_key}）")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """停止捕获。幂等。"""
        try:
            self.watcher.stop(timeout=timeout)
        except Exception as exc:
            self.logger.warning("[rt.file] task=%s 停止捕获器异常: %s",
                                self.task_id, exc)
        with self._state_lock:
            self._started = False
            self.daemon_status = STATUS_STOPPED
            self._persist_state()
            try:
                models.update_rt_task(self.task_id, {"is_running": 0})
            except Exception:
                pass

    def is_alive(self) -> bool:
        """捕获线程是否存活。"""
        return bool(self._started and self.watcher.is_alive())

    # ------------------------------------------------------------------
    # Supervisor 周期调用
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """每个 supervisor tick 调用一次：心跳、健康、配额、过期清理。

        Returns:
            ``{'task_id','alive','health','rpo_actual_sec','usage','pruned'}``
        """
        self._tick_count += 1
        alive = self.is_alive()

        # 捕获线程意外退出 → 自愈重启（受 Supervisor 的重启预算约束）
        if self._started and not alive:
            self.last_error = self.last_error or "捕获线程意外退出"
            self.daemon_status = STATUS_FAILED

        usage = {}
        try:
            usage = self.repo.disk_usage()
            if usage.get("over_hard"):
                self.degrade_reason = (
                    f"实时仓库已达配额上限（{usage['human']}/"
                    f"{usage['quota_gb']}GB），已暂停捕获")
                self.daemon_status = STATUS_DEGRADED
            elif usage.get("over_soft"):
                self.degrade_reason = (
                    f"实时仓库用量 {usage['used_percent']}%，接近配额上限")
        except Exception as exc:
            self.logger.warning("[rt.file] task=%s 用量统计失败: %s",
                                self.task_id, exc)

        pruned = 0
        if self._tick_count % _PRUNE_EVERY_TICKS == 0:
            pruned = self.prune()

        health = self.health()
        self._persist_state(health=health)
        try:
            models.update_rt_task(self.task_id, {
                "is_running": 1 if alive else 0,
                "last_tick_at": db.now_iso(),
                "health_status": health.health,
                "rpo_current_seconds": health.rpo_actual_sec,
            })
        except Exception:
            pass

        return {
            "task_id": self.task_id,
            "alive": alive,
            "health": health.health,
            "rpo_actual_sec": health.rpo_actual_sec,
            "usage": usage,
            "pruned": pruned,
        }

    def trigger_now(self, reason: str = "manual") -> dict:
        """手动触发一次立即捕获。

        捕获器存活时走 ``request_flush``（非阻塞）；未存活时同步执行一次，
        保证「停机状态下点击手动捕获」也能拿到结果。
        """
        if self.is_alive():
            self.watcher.request_flush(reason)
            return {"ok": True, "task_id": self.task_id, "mode": "async",
                    "message": "已请求立即捕获，稍后可在时间轴查看新恢复点"}
        try:
            batch = self.watcher.poll_once(trigger=reason)
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "task_id": self.task_id, "message": str(exc)}
        point = self._handle_batch(batch)
        return {
            "ok": True, "task_id": self.task_id, "mode": "sync",
            "recovery_point": point.to_dict() if point else None,
            "message": ("已生成恢复点" if point else "无变化，未生成恢复点"),
        }

    def prune(self) -> int:
        """按保留天数清理过期恢复点与磁盘产物。"""
        try:
            return self.journal.prune(self.task_id, self.rt.log_retention_days,
                                      repo=self.repo)
        except Exception as exc:
            self.logger.warning("[rt.file] task=%s prune 失败: %s",
                                self.task_id, exc)
            return 0

    # ------------------------------------------------------------------
    # 基准全量
    # ------------------------------------------------------------------
    def ensure_base(self, force: bool = False) -> Optional[RecoveryPoint]:
        """确保存在基准全量，并把它登记为 ``base-full`` 恢复点。

        Args:
            force: 忽略既有基准强制重做（运维「重建基准」动作）。

        Returns:
            新登记的 RecoveryPoint；复用既有基准且已在 journal 中时返回该点；
            失败返回 None（原因写入 ``last_error``）。
        """
        engine = self.engine
        result = engine.ensure_base_full(out_dir=self.repo.base_dir(), force=force)
        if not result.success:
            self.last_error = result.message
            self.logger.warning("[rt.file] task=%s 基准全量失败: %s",
                                self.task_id, result.message)
            return None

        archive = result.backup_path or ""
        if not archive or not os.path.isfile(archive):
            self.last_error = "基准全量归档缺失"
            return None

        # 幂等：journal.append 内部对 (task_id, object_key) 做唯一约束更新
        point = self.journal.append(self.task_id, {
            "rp_kind": RP_BASE_FULL,
            "rp_type": "full",
            "pit_at": db.now_iso(),
            "consistency": self.rt.consistency,
            "file_set_key": self.file_set_key(),
            "storage_tier": 1,
            "object_key": archive,
            "size_bytes": result.size_bytes or 0,
            "checksum": result.checksum or "",
            "is_simulated": 0,
            "message": result.message,
            "retention_days": max(self.rt.log_retention_days * 2, 30),
        })
        with self._state_lock:
            self.last_rp_id = point.id
            self.last_rp_at = point.pit_at
            self.last_capture_at = point.pit_at
        return point

    # ------------------------------------------------------------------
    # 变更批次处理（Watcher 回调）
    # ------------------------------------------------------------------
    def _on_batch(self, batch: ChangeBatch) -> None:
        """Watcher 回调入口：做「合并去重」后交给 :meth:`_handle_batch`。

        捕获期间到达的批次不会排队执行，而是记一个 ``_pending`` 标记，
        在当前捕获结束后合并成一次 flush —— 这正是「事件去重/合并」的落点。
        """
        if not self._capture_lock.acquire(blocking=False):
            with self._state_lock:
                self._pending += 1
            self.logger.debug("[rt.file] task=%s 捕获进行中，合并本批次（pending=%d）",
                              self.task_id, self._pending)
            return
        try:
            self._handle_batch(batch)
        finally:
            self._capture_lock.release()

        # 合并期内确实有新事件 → 触发一次补捕
        with self._state_lock:
            pending = self._pending
            self._pending = 0
        if pending > 0 and self.is_alive():
            self.watcher.request_flush("merged")

    def _handle_batch(self, batch: ChangeBatch) -> Optional[RecoveryPoint]:
        """把一个变更批次落成恢复点。异常不外抛。"""
        self.last_capture_at = batch.detected_at or db.now_iso()

        # 尚无基准 → 先建基准，本轮不产增量
        if batch.trigger == "base":
            return self.ensure_base()

        if batch.is_empty():
            self.consecutive_fail = 0
            return None

        # 配额硬线：暂停捕获，避免把磁盘写爆（R8）
        try:
            usage = self.repo.disk_usage()
        except Exception:
            usage = {}
        if usage.get("over_hard"):
            self.degrade_reason = (
                f"实时仓库已达配额上限（{usage.get('human')}/"
                f"{usage.get('quota_gb')}GB），已暂停捕获")
            self.daemon_status = STATUS_DEGRADED
            self.logger.warning("[rt.file] task=%s %s", self.task_id,
                                self.degrade_reason)
            return None

        try:
            result = self.engine.capture_increment(
                out_dir=self.repo.inc_dir(),
                tag=time.strftime("%Y%m%d_%H%M%S"),
                changed=batch.changed,
                deleted=batch.deleted,
                source_files=batch.snapshot or None,
            )
        except Exception as exc:
            self.consecutive_fail += 1
            self.last_error = f"增量捕获异常: {exc}"
            self.daemon_status = STATUS_DEGRADED
            self.logger.error("[rt.file] task=%s %s", self.task_id, self.last_error)
            return None

        if not result.success:
            self.consecutive_fail += 1
            self.last_error = result.message
            self.logger.warning("[rt.file] task=%s 增量捕获失败: %s",
                                self.task_id, result.message)
            return None

        archive = result.backup_path or ""
        if not archive or not os.path.isfile(archive):
            # 无变化的成功返回（capture_increment 的 "无变化文件" 分支）
            self.consecutive_fail = 0
            return None

        parent_id = self._resolve_parent_id()
        try:
            point = self.journal.append(self.task_id, {
                "rp_kind": RP_FILE_INC,
                "rp_type": "incremental",
                "pit_at": batch.detected_at or db.now_iso(),
                "parent_rp_id": parent_id,
                "consistency": self.rt.consistency,
                "file_set_key": self.file_set_key(),
                "changed_files": len(batch.changed),
                "deleted_files": len(batch.deleted),
                "storage_tier": 1,
                "object_key": archive,
                "size_bytes": result.size_bytes or 0,
                "checksum": result.checksum or "",
                "is_simulated": 0,
                "message": f"[{batch.trigger}] {result.message}",
                "retention_days": self.rt.log_retention_days,
            })
        except Exception as exc:
            self.consecutive_fail += 1
            self.last_error = f"写入恢复点失败: {exc}"
            self.logger.error("[rt.file] task=%s %s", self.task_id, self.last_error)
            return None

        with self._state_lock:
            self.last_rp_id = point.id
            self.last_rp_at = point.pit_at
            self.rp_count += 1
            self.bytes_captured += int(point.size_bytes or 0)
            self.consecutive_fail = 0
            self.last_error = ""
            if not self.degrade_reason:
                self.daemon_status = STATUS_RUNNING
        self.logger.info("[rt.file] task=%s 恢复点 #%s 变化=%d 删除=%d %s",
                         self.task_id, point.id, point.changed_files,
                         point.deleted_files, norm_path(archive))
        return point

    def _resolve_parent_id(self) -> Optional[int]:
        """当前增量的父节点 id：优先内存缓存，回落 journal 查询。"""
        if self.last_rp_id:
            return self.last_rp_id
        latest = self.journal.latest(self.task_id)
        return latest.id if latest else None

    # ------------------------------------------------------------------
    # 健康与状态持久化
    # ------------------------------------------------------------------
    def health(self) -> RtHealth:
        """计算当前健康快照。"""
        last_rp_at = self.last_rp_at
        if not last_rp_at:
            latest = self.journal.latest(self.task_id)
            last_rp_at = latest.pit_at if latest else ""
            if latest:
                self.last_rp_at = last_rp_at
                self.last_rp_id = self.last_rp_id or latest.id

        anchor = _epoch(last_rp_at) or _epoch(self.started_at)
        rpo_actual = int(max(0.0, time.time() - anchor)) if anchor else 0

        stats = {}
        try:
            stats = self.watcher.stats()
        except Exception:
            stats = {}

        health = RtHealth(
            task_id=self.task_id,
            task_name=self.task_name,
            capture_kind=KIND_FILE,
            engine=self.rt.engine,
            daemon_status=self.daemon_status,
            degrade_reason=self.degrade_reason or stats.get("degrade_reason", ""),
            watcher_impl=stats.get("impl", self.watcher.impl_key),
            lag_sec=rpo_actual,
            rpo_actual_sec=rpo_actual,
            rpo_target_sec=self.rt.rpo_target_sec,
            last_rp_at=last_rp_at,
            last_capture_at=self.last_capture_at,
            position_label=f"+{stats.get('last_batch_size', 0)}",
            restart_count=self.restart_count,
            consecutive_fail=self.consecutive_fail,
            rp_count_today=self.rp_count,
            bytes_today=self.bytes_captured,
            last_error=self.last_error or stats.get("last_error", ""),
            last_heartbeat_at=db.now_iso(),
            is_simulated=False,
        )
        health.health = health.compute_health()
        return health

    def _persist_state(self, health: RtHealth = None) -> None:
        """把运行态写入 ``rt_capture_state``（高频 UPSERT，失败不阻断）。"""
        try:
            stats = self.watcher.stats()
        except Exception:
            stats = {}
        payload = {
            "capture_kind": KIND_FILE,
            "engine": self.rt.engine,
            "daemon_status": self.daemon_status,
            "degrade_reason": self.degrade_reason,
            "pid": os.getpid(),
            "watcher_impl": stats.get("impl", self.watcher.impl_key),
            "last_heartbeat_at": db.now_iso(),
            "last_capture_at": self.last_capture_at,
            "last_rp_at": self.last_rp_at,
            "consecutive_fail": self.consecutive_fail,
            "restart_count": self.restart_count,
            "bytes_today": self.bytes_captured,
            "rp_count_today": self.rp_count,
            "last_error": self.last_error,
        }
        if health is not None:
            payload["lag_sec"] = health.lag_sec
            payload["rpo_actual_sec"] = health.rpo_actual_sec
            payload["health"] = health.health
        else:
            payload["health"] = HEALTH_UNKNOWN
        try:
            models.upsert_rt_state(self.task_id, payload)
        except Exception as exc:
            self.logger.debug("[rt.file] task=%s 写运行态失败: %s", self.task_id, exc)

    def _sync_rt_task_row(self) -> None:
        """确保 ``rt_tasks`` 中存在本任务行，且 ``rt_mode`` 注册为 ``file_watch``。"""
        payload = {
            "rt_mode": self.rt_mode,
            "capture_interval": self.rt.interval_sec,
            "file_inc_retention_days": self.rt.log_retention_days,
            "db_log_retention_days": config.RT_DB_LOG_RETENTION_DAYS,
            "db_flush_interval": config.RT_DB_SEAL_INTERVAL_SEC,
            "is_running": 1,
            "health_status": HEALTH_UNKNOWN,
            "disk_quota_gb": config.RT_DISK_QUOTA_GB,
        }
        try:
            if models.get_rt_task(self.task_id):
                models.update_rt_task(self.task_id, payload)
            else:
                payload["task_id"] = self.task_id
                models.create_rt_task(payload)
        except Exception as exc:
            self.logger.warning("[rt.file] task=%s 同步 rt_tasks 失败: %s",
                                self.task_id, exc)
