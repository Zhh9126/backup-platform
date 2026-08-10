# -*- coding: utf-8 -*-
"""
T03 数据库 CDC 捕获 worker（DbRtCapture）。

把 :mod:`core.cdc` 的守护进程粘合到「日志段封存 → Recovery Journal」链路：

    CDCDaemon(mysqlbinlog --stop-never | pg_receivewal | simulated)
            │  tick() → seal_ready_segments()
            ▼
    LogRepository.sealed/<day>/<segment>
            │
            ▼
    RecoveryJournal.append(rp_kind='db-log', parent_rp_id=上一段)

链头（``db-full``）解析优先级：
1. journal 中已有的 ``db-full`` 恢复点 → 直接复用；
2. 该任务最近一次成功的**全量** ``backup_records`` 且归档仍在磁盘 → 登记为链头；
3. 都没有 → 生成一个基准占位归档（``is_simulated=1``），保证恢复链有头、
   时间轴不出现「孤儿日志段」，同时明确告知用户需补一次真实全量。

与 :class:`core.rt_backup.file_rt.FileRtCapture` 实现同一套 worker 协议，
由 RtSupervisor 统一 start / tick / stop。
"""
from __future__ import annotations

import json
import os
import tarfile
import threading
import time
from typing import List, Optional

import config
import core.db as db
import core.models as models
from core.cdc import create_daemon

from .journal import RecoveryJournal
from .repo import LogRepository
from .types import (
    HEALTH_UNKNOWN,
    KIND_DB_LOG,
    POSITION_KIND_LABELS,
    RP_DB_FULL,
    RP_DB_LOG,
    RT_MODE_DB_CDC,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    RecoveryPoint,
    RtConfig,
    RtHealth,
    norm_path,
)

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


class DbRtCapture:
    """数据库型任务的日志流捕获 worker。

    Attributes:
        task_id: backup_tasks.id。
        capture_kind: 恒为 ``db-log``。
        rt_mode: 注册到 ``rt_tasks.rt_mode`` 的类型，恒为 ``db_cdc``。
    """

    capture_kind: str = KIND_DB_LOG
    rt_mode: str = RT_MODE_DB_CDC

    def __init__(self, task: dict, rt_config: RtConfig = None, logger=None) -> None:
        self.task: dict = dict(task or {})
        self.task_id: int = int(self.task.get("id") or 0)
        self.task_name: str = self.task.get("name") or f"task_{self.task_id}"
        self.rt: RtConfig = rt_config or RtConfig.from_task(self.task)
        self.logger = logger or db.get_logger("rt.cdc")

        self.repo: LogRepository = LogRepository(self.task_id, KIND_DB_LOG,
                                                 logger=self.logger)
        self.journal: RecoveryJournal = RecoveryJournal(logger=self.logger)
        self.daemon = create_daemon(self.task, self.rt, self.repo,
                                    logger=self.logger)

        self._state_lock: threading.RLock = threading.RLock()
        self._started: bool = False
        self._tick_count: int = 0

        # 运行统计
        self.last_error: str = ""
        self.degrade_reason: str = self.daemon.degrade_reason or ""
        self.daemon_status: str = STATUS_STOPPED
        self.last_capture_at: str = ""
        self.last_rp_at: str = ""
        self.last_rp_id: Optional[int] = None
        self.rp_count: int = 0
        self.bytes_captured: int = 0
        self.consecutive_fail: int = 0
        self.restart_count: int = 0
        self.started_at: str = ""
        self.stall_ticks: int = 0

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    def config_fingerprint(self) -> str:
        """配置指纹：变化时 Supervisor 会重建 worker。"""
        return "|".join([
            str(self.task_id), self.rt.engine, self.rt.mode,
            str(self.rt.interval_sec), str(self.rt.rpo_target_sec),
            str(self.rt.log_retention_days), self.rt.consistency,
            str(self.task.get("host") or ""), str(self.task.get("port") or ""),
        ])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """启动 CDC 守护。已启动时幂等返回 True。"""
        with self._state_lock:
            if self._started and self.daemon.is_alive():
                return True
            self.started_at = db.now_iso()
            self.daemon_status = STATUS_STARTING
            self._sync_rt_task_row()
            self._persist_state()

        # 链头：先确保存在 db-full 恢复点
        try:
            self.ensure_base()
        except Exception as exc:
            self.last_error = f"建立基准全量失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)

        started = False
        try:
            started = bool(self.daemon.start())
        except Exception as exc:
            self.last_error = f"启动日志流失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)

        if not started:
            # 真实实现启动失败 → 就地降级到仿真，保证链路不中断（R6）
            self.last_error = self.last_error or self.daemon.last_error
            fallback_reason = (f"{self.daemon.display_name} 启动失败"
                               f"（{self.last_error or '原因未知'}），已降级仿真日志流")
            self.logger.warning("[rt.cdc] task=%s %s", self.task_id, fallback_reason)
            from core.cdc.simulated import SimulatedCDCDaemon
            self.daemon = SimulatedCDCDaemon(self.task, self.rt, self.repo,
                                             logger=self.logger)
            self.daemon.degrade_reason = fallback_reason
            self.degrade_reason = fallback_reason
            try:
                started = bool(self.daemon.start())
            except Exception as exc:
                self.last_error = f"仿真日志流启动失败: {exc}"
                self.daemon_status = STATUS_FAILED
                self._persist_state()
                return False

        with self._state_lock:
            self._started = True
            self.degrade_reason = self.degrade_reason or self.daemon.degrade_reason
            self.daemon_status = (STATUS_DEGRADED if self.degrade_reason
                                  else STATUS_RUNNING)
            self._persist_state()
        db.add_log("info", "rt.cdc",
                   f"任务 {self.task_name} 数据库日志流捕获已启动"
                   f"（{self.daemon.display_name}）")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """停止守护并把残留完整段收尾入账。幂等。"""
        try:
            self.daemon.stop(timeout=timeout)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 停止守护异常: %s",
                                self.task_id, exc)
        # 停机时把最后封存的段补登记，避免产物在磁盘却不在 journal
        try:
            self._register_segments(self._collect_orphan_segments())
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s 收尾登记异常: %s", self.task_id, exc)

        with self._state_lock:
            self._started = False
            self.daemon_status = STATUS_STOPPED
            self._persist_state()
            try:
                models.update_rt_task(self.task_id, {"is_running": 0})
            except Exception:
                pass

    def is_alive(self) -> bool:
        """守护是否存活。"""
        return bool(self._started and self.daemon.is_alive())

    # ------------------------------------------------------------------
    # Supervisor 周期调用
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """一次周期驱动：守护 tick → 段入账 → 健康/配额/清理。"""
        self._tick_count += 1

        usage = {}
        try:
            usage = self.repo.disk_usage()
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 用量统计失败: %s", self.task_id, exc)

        segments: List[dict] = []
        if usage.get("over_hard"):
            self.degrade_reason = (
                f"日志仓库已达配额上限（{usage.get('human')}/"
                f"{usage.get('quota_gb')}GB），已暂停封存")
            self.daemon_status = STATUS_DEGRADED
        elif self._started:
            try:
                result = self.daemon.tick()
                segments = result.get("segments") or []
                if result.get("error"):
                    self.last_error = result["error"]
                if not result.get("alive"):
                    self.daemon_status = STATUS_FAILED
            except Exception as exc:
                self.consecutive_fail += 1
                self.last_error = f"日志流 tick 异常: {exc}"
                self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)

        points = self._register_segments(segments)

        # 停滞判定：连续 N 个 tick 没有新段且守护存活 → 降级提示（R5）
        if self._started and self.daemon.is_alive():
            if points:
                self.stall_ticks = 0
                if not self.daemon.degrade_reason:
                    self.degrade_reason = ""
                    self.daemon_status = STATUS_RUNNING
            else:
                self.stall_ticks += 1
                if self.stall_ticks >= max(2, int(config.RT_DB_STALL_TICKS)):
                    self.degrade_reason = (
                        f"日志流已连续 {self.stall_ticks} 个周期无新段，"
                        f"可能源库无写入或日志未轮转")
                    self.daemon_status = STATUS_DEGRADED

        if usage.get("over_soft") and not usage.get("over_hard"):
            self.degrade_reason = (
                f"日志仓库用量 {usage.get('used_percent')}%，接近配额上限")

        pruned = 0
        if self._tick_count % _PRUNE_EVERY_TICKS == 0:
            pruned = self.prune()

        health = self.health()
        self._persist_state(health=health)
        try:
            models.update_rt_task(self.task_id, {
                "is_running": 1 if self.is_alive() else 0,
                "last_tick_at": db.now_iso(),
                "health_status": health.health,
                "rpo_current_seconds": health.rpo_actual_sec,
            })
        except Exception:
            pass

        return {
            "task_id": self.task_id,
            "alive": self.is_alive(),
            "health": health.health,
            "rpo_actual_sec": health.rpo_actual_sec,
            "segments": len(segments),
            "points": [p.id for p in points],
            "usage": usage,
            "pruned": pruned,
        }

    def trigger_now(self, reason: str = "manual") -> dict:
        """手动触发一次立即封存 + 入账。"""
        if not self._started:
            return {"ok": False, "task_id": self.task_id,
                    "message": "日志流未启动，无法立即捕获"}
        try:
            if getattr(self.daemon, "is_simulated", False):
                # 仿真流：直接产一段，保证手动触发一定有可见结果
                self.daemon.tick()
                segments = self.daemon.seal_ready_segments(force=True)
            else:
                segments = self.daemon.seal_ready_segments(force=False)
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "task_id": self.task_id, "message": str(exc)}
        points = self._register_segments(segments)
        return {
            "ok": True, "task_id": self.task_id, "mode": "sync",
            "recovery_points": [p.to_dict() for p in points],
            "message": (f"已封存 {len(points)} 个日志段" if points
                        else "当前无可封存的完整日志段"),
        }

    def prune(self) -> int:
        """按保留天数清理过期恢复点与磁盘产物。"""
        try:
            return self.journal.prune(self.task_id, self.rt.log_retention_days,
                                      repo=self.repo)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s prune 失败: %s", self.task_id, exc)
            return 0

    # ------------------------------------------------------------------
    # 链头：db-full
    # ------------------------------------------------------------------
    def ensure_base(self, force: bool = False) -> Optional[RecoveryPoint]:
        """确保 journal 中存在 ``db-full`` 链头。

        Args:
            force: 忽略既有链头强制重建。

        Returns:
            链头 RecoveryPoint；失败返回 None。
        """
        if not force:
            existing = self.journal.latest(self.task_id, kind=RP_DB_FULL)
            if existing and existing.exists_on_disk():
                with self._state_lock:
                    self.last_rp_id = self.last_rp_id or existing.id
                    self.last_rp_at = self.last_rp_at or existing.pit_at
                return existing

        archive, simulated, message = self._locate_full_archive()
        if not archive:
            self.last_error = message
            return None

        point = self.journal.append(self.task_id, {
            "rp_kind": RP_DB_FULL,
            "rp_type": "full",
            "pit_at": db.now_iso(),
            "consistency": self.rt.consistency,
            "storage_tier": 1,
            "object_key": archive,
            "size_bytes": os.path.getsize(archive) if os.path.isfile(archive) else 0,
            "is_simulated": 1 if simulated else 0,
            "message": message,
            "retention_days": max(self.rt.log_retention_days * 2, 30),
            **self._daemon_position_fields(self.daemon.source_position()
                                           or self.daemon.current_position()),
        })
        with self._state_lock:
            self.last_rp_id = point.id
            self.last_rp_at = point.pit_at
        self.logger.info("[rt.cdc] task=%s 链头 db-full #%s %s",
                         self.task_id, point.id, norm_path(archive))
        return point

    def _locate_full_archive(self) -> tuple:
        """定位可作为链头的全量归档。

        Returns:
            ``(归档路径, 是否仿真, 说明)``；找不到且无法生成时路径为空串。
        """
        # ① 最近一次成功的全量备份记录
        row = db.query_one(
            "SELECT * FROM backup_records WHERE task_id=? AND backup_type='full' "
            "AND status IN ('success','simulated') AND backup_path IS NOT NULL "
            "AND backup_path <> '' ORDER BY started_at DESC, id DESC LIMIT 1",
            (self.task_id,))
        if row and row.get("backup_path") and os.path.isfile(row["backup_path"]):
            return (row["backup_path"],
                    bool(row.get("status") == "simulated"),
                    f"复用最近一次全量备份记录 #{row.get('id')} 作为恢复链头")

        # ② 真实环境下尝试立刻做一次全量
        if config.DEMO_MODE != "on" and not self.task.get("demo_only"):
            archive = self._run_real_full()
            if archive:
                return archive, False, "已自动执行一次全量备份作为恢复链头"

        # ③ 兜底：生成基准占位归档，保证恢复链有头
        archive = self._write_placeholder_full()
        return (archive, True,
                "仿真基准全量（尚无真实全量备份，恢复前请先补一次全量）")

    def _run_real_full(self) -> str:
        """调用引擎执行一次真实全量备份。失败返回空串。"""
        try:
            from core.engines import get_engine, BackupType
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 加载引擎失败: %s", self.task_id, exc)
            return ""
        engine = get_engine(self.rt.engine, self.task, config.BACKUP_ROOT,
                            self.logger)
        if engine is None:
            return ""
        try:
            result = engine.backup(BackupType.FULL)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 自动全量失败: %s", self.task_id, exc)
            return ""
        path = getattr(result, "backup_path", "") or ""
        return path if path and os.path.isfile(path) else ""

    def _write_placeholder_full(self) -> str:
        """生成一个基准占位归档（含说明清单），落在 ``base/`` 下。"""
        name = f"{time.strftime('%Y%m%d_%H%M%S')}__{self.task_name}__dbfull.tar.gz"
        path = os.path.join(self.repo.base_dir(), name)
        manifest = {
            "_simulated": True,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "engine": self.rt.engine,
            "created_at": db.now_iso(),
            "note": ("仿真基准全量：仅用于维持恢复链完整性，"
                     "真实恢复前必须补一次全量备份"),
        }
        body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

        def _write(tmp_path: str) -> None:
            import io
            with tarfile.open(tmp_path, "w:gz") as tar:
                info = tarfile.TarInfo(name="_rt_base_manifest.json")
                info.size = len(body)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(body))

        self.repo.atomic_write(_write, path)
        return path if os.path.isfile(path) else ""

    # ------------------------------------------------------------------
    # 日志段入账
    # ------------------------------------------------------------------
    def _register_segments(self, segments: List[dict]) -> List[RecoveryPoint]:
        """把封存好的日志段登记为 ``db-log`` 恢复点。"""
        points: List[RecoveryPoint] = []
        for seg in segments or []:
            path = seg.get("path") or ""
            if not path or not os.path.isfile(path):
                continue
            size = int(seg.get("size") or 0)
            if size <= 0:
                continue  # 空段绝不入 journal（R9）
            parent_id = self._resolve_parent_id()
            payload = {
                "rp_kind": RP_DB_LOG,
                "rp_type": "log-segment",
                "pit_at": seg.get("sealed_at") or db.now_iso(),
                "parent_rp_id": parent_id,
                "consistency": self.rt.consistency,
                "storage_tier": 1,
                "object_key": path,
                "size_bytes": size,
                "checksum": seg.get("checksum") or "",
                "is_simulated": 1 if self.daemon.is_simulated else 0,
                "message": f"日志段 {seg.get('name')}",
                "retention_days": self.rt.log_retention_days,
            }
            payload.update(self._daemon_position_fields(seg.get("position") or {}))
            try:
                point = self.journal.append(self.task_id, payload)
            except Exception as exc:
                self.consecutive_fail += 1
                self.last_error = f"写入日志段恢复点失败: {exc}"
                self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)
                continue
            with self._state_lock:
                self.last_rp_id = point.id
                self.last_rp_at = point.pit_at
                self.last_capture_at = point.pit_at
                self.rp_count += 1
                self.bytes_captured += size
                self.consecutive_fail = 0
                self.last_error = ""
            points.append(point)
        return points

    def _collect_orphan_segments(self) -> List[dict]:
        """扫描 sealed/ 中尚未登记进 journal 的段（停机收尾用）。"""
        registered = set()
        try:
            for row in db.query(
                    "SELECT object_key FROM recovery_journal WHERE task_id=?",
                    (self.task_id,)):
                if row.get("object_key"):
                    registered.add(os.path.abspath(row["object_key"]))
        except Exception:
            return []

        orphans: List[dict] = []
        sealed_root = os.path.join(self.repo.base, "sealed")
        if not os.path.isdir(sealed_root):
            return []
        for dirpath, _dirs, names in os.walk(sealed_root):
            for name in names:
                full = os.path.join(dirpath, name)
                if os.path.abspath(full) in registered:
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size <= 0:
                    continue
                orphans.append({
                    "path": full, "name": name, "size": size,
                    "checksum": db.sha256_file(full),
                    "sealed_at": db.now_iso(),
                    "position": self.daemon.current_position(),
                })
        return orphans

    def _daemon_position_fields(self, position: dict) -> dict:
        """把守护位点字典映射到 recovery_journal 的位点列。

        CH-T06-2：Oracle SCN / 达梦 LSN **复用** ``wal_lsn`` / ``wal_end_lsn``
        两列承载（零 Schema 迁移），守护侧已在 ``current_position()`` 中同步好，
        因此这里不需要为信创库新增分支——只在守护未填 wal_* 时，
        从 ``scn`` / ``dm_lsn`` 语义别名兜底一次。
        """
        position = position or {}
        fields = {}
        for key in ("binlog_file", "binlog_end_file", "wal_lsn", "wal_end_lsn"):
            if position.get(key):
                fields[key] = str(position[key])
        # 语义别名兜底（守护只填了 scn / dm_lsn 的情形）
        alias = position.get("scn") or position.get("dm_lsn")
        if alias and not fields.get("wal_end_lsn"):
            fields["wal_end_lsn"] = str(alias)
        if alias and not fields.get("wal_lsn"):
            fields["wal_lsn"] = str(alias)
        for key in ("binlog_pos", "binlog_end_pos"):
            if position.get(key) is not None:
                try:
                    fields[key] = int(position[key])
                except (TypeError, ValueError):
                    continue
        return fields

    def _position_label(self, position: dict) -> str:
        """位点展示文案（UI「位点 / 变更」列）。

        T06 / CH-T06-2：三种位点共用 ``wal_lsn`` / ``wal_end_lsn`` 两列，
        仅靠数值无法区分 Oracle SCN（纯整数）与达梦 LSN（也是纯整数）。
        因此当守护显式上报 ``position_kind`` 时，加上 ``SCN: `` / ``LSN: ``
        前缀消歧；未上报 ``position_kind`` 的 MySQL / PostgreSQL 走
        T03 原有逻辑，展示文案**逐字不变**。

        Args:
            position: 守护 ``current_position()`` 返回的位点字典。

        Returns:
            展示用位点字符串；无位点时返回 ``"-"``。
        """
        position = position or {}
        if position.get("binlog_end_file"):
            return (f"{position['binlog_end_file']}:"
                    f"{position.get('binlog_end_pos', 0)}")
        if position.get("binlog_file"):
            return f"{position['binlog_file']}:{position.get('binlog_pos', 0)}"

        value = (position.get("wal_end_lsn") or position.get("wal_lsn")
                 or position.get("scn") or position.get("dm_lsn"))
        if not value:
            return "-"
        kind = str(position.get("position_kind") or "").strip().lower()
        prefix = POSITION_KIND_LABELS.get(kind, "") if kind else ""
        return f"{prefix}: {value}" if prefix else str(value)

    def _resolve_parent_id(self) -> Optional[int]:
        """当前日志段的父节点 id：优先内存缓存，回落 journal 查询。"""
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
        try:
            lag = int(self.daemon.lag_seconds())
        except Exception:
            lag = rpo_actual

        position = self.daemon.current_position()
        label = self._position_label(position)

        health = RtHealth(
            task_id=self.task_id,
            task_name=self.task_name,
            capture_kind=KIND_DB_LOG,
            engine=self.rt.engine,
            daemon_status=self.daemon_status,
            degrade_reason=self.degrade_reason or self.daemon.degrade_reason,
            watcher_impl=self.daemon.engine_key,
            lag_sec=lag,
            rpo_actual_sec=rpo_actual,
            rpo_target_sec=self.rt.rpo_target_sec,
            last_rp_at=last_rp_at,
            last_capture_at=self.last_capture_at,
            position_label=label,
            restart_count=self.restart_count,
            consecutive_fail=self.consecutive_fail,
            rp_count_today=self.rp_count,
            bytes_today=self.bytes_captured,
            last_error=self.last_error or self.daemon.last_error,
            last_heartbeat_at=db.now_iso(),
            is_simulated=bool(self.daemon.is_simulated),
        )
        health.health = health.compute_health()
        return health

    def _persist_state(self, health: RtHealth = None) -> None:
        """把运行态写入 ``rt_capture_state``（失败不阻断）。"""
        position = self.daemon.current_position()
        payload = {
            "capture_kind": KIND_DB_LOG,
            "engine": self.rt.engine,
            "daemon_status": self.daemon_status,
            "degrade_reason": self.degrade_reason or self.daemon.degrade_reason,
            "pid": (self.daemon.proc.pid if getattr(self.daemon, "proc", None)
                    else os.getpid()),
            "watcher_impl": self.daemon.engine_key,
            "last_heartbeat_at": db.now_iso(),
            "last_capture_at": self.last_capture_at,
            "last_rp_at": self.last_rp_at,
            "last_binlog_file": str(position.get("binlog_end_file")
                                    or position.get("binlog_file") or ""),
            "last_binlog_pos": int(position.get("binlog_end_pos")
                                   or position.get("binlog_pos") or 0),
            "last_wal_lsn": str(position.get("wal_end_lsn")
                                or position.get("wal_lsn") or ""),
            "source_pos_at": db.now_iso(),
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
            self.logger.debug("[rt.cdc] task=%s 写运行态失败: %s", self.task_id, exc)

    def _sync_rt_task_row(self) -> None:
        """确保 ``rt_tasks`` 中存在本任务行，且 ``rt_mode`` 注册为 ``db_cdc``。"""
        payload = {
            "rt_mode": self.rt_mode,
            "capture_interval": self.rt.interval_sec,
            "db_log_retention_days": self.rt.log_retention_days,
            "file_inc_retention_days": config.RT_FILE_RETENTION_DAYS,
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
            self.logger.warning("[rt.cdc] task=%s 同步 rt_tasks 失败: %s",
                                self.task_id, exc)
