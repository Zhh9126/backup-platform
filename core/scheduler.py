# -*- coding: utf-8 -*-
"""
调度与执行核心。

- run_task_now / run_restore_now：单次执行入口（供 API"立即备份/恢复"与调度触发共用）
- start_scheduler / reload_scheduler / stop_scheduler：基于 APScheduler 的后台调度

文件类型任务(db_type=file)的立即执行会放入后台线程，API 立即返回 202，
前端通过轮询 /api/tasks/<id> 获取最新状态。
"""
import time
import threading
from datetime import datetime
from typing import Optional

import config
import core.db as db
import core.sync
from core import models, storage, notifier

_logger = db.get_logger("scheduler")
_scheduler = None


# ------------------------- Phase 1：并发 / 流量控制 / 避峰 -------------------------

class _ConcurrencyController:
    """全局并发控制器：限制同时执行的备份数量。

    上限来自 system_config.max_concurrent_backups（缺省 2，保守不破坏旧串行行为）。
    配置变更时下次获取自动重建信号量。
    """

    def __init__(self):
        self._limit = None
        self._sem = None

    def _ensure(self):
        try:
            limit = int(db.get_system_config("max_concurrent_backups") or 2)
        except Exception:
            limit = 2
        limit = max(1, limit)
        if self._sem is None or self._limit != limit:
            self._limit = limit
            self._sem = threading.Semaphore(limit)

    def acquire(self):
        self._ensure()
        self._sem.acquire()

    def release(self):
        try:
            self._sem.release()
        except Exception:
            pass


class _BandwidthGovernor:
    """全局带宽令牌桶：传输前按令牌节流。

    cap_mbps=0 表示不限速（缺省）。令牌按真实时间补充，传输前按字节消耗，
    不足时 sleep 模拟节流；并记录简单 metrics 供可观测。
    """

    def __init__(self):
        self._cap = None
        self._rate = 0.0
        self._lock = threading.Lock()
        self._bucket = 0.0
        self._last = 0.0
        self.metrics = {"throttled_bytes": 0, "sleep_sec": 0.0}

    def _ensure(self):
        try:
            cap = float(db.get_system_config("bandwidth_cap_mbps") or 0.0)
        except Exception:
            cap = 0.0
        if self._cap != cap:
            self._cap = cap
            self._rate = cap * 1024 * 1024 if cap > 0 else 0.0
            self._bucket = self._rate  # 允许突发 1 秒
            self._last = time.time()

    def throttle(self, bytes_count: int):
        self._ensure()
        if not self._rate or bytes_count <= 0:
            return
        with self._lock:
            now = time.time()
            delta = now - self._last
            self._last = now
            self._bucket = min(self._rate, self._bucket + delta * self._rate)
            if self._bucket >= bytes_count:
                self._bucket -= bytes_count
                return
            deficit = bytes_count - self._bucket
            self._bucket = 0.0
        sleep_sec = min(deficit / self._rate, 5.0)  # 上限 5s，避免极端长眠
        if sleep_sec > 0:
            time.sleep(sleep_sec)
            self.metrics["sleep_sec"] += sleep_sec
            self.metrics["throttled_bytes"] += bytes_count


def _parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_peak_window(now=None) -> bool:
    """判断当前是否处于避峰窗口（system_config.peak_hours，形如 09:00-18:00）。"""
    raw = db.get_system_config("peak_hours") or ""
    if not raw or "-" not in raw:
        return False
    try:
        start_s, end_s = [p.strip() for p in raw.split("-")]
        start = _parse_hhmm(start_s)
        end = _parse_hhmm(end_s)
    except Exception:
        return False
    cur = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end  # 跨午夜窗口


def _peak_govern(task: dict, size_bytes: int) -> None:
    """避峰窗口：峰期内对非紧急（一般级）任务降速；核心/重要任务照常。"""
    if not _in_peak_window():
        return
    level = (task.get("protection_level") or "general")
    if level in ("core", "important"):
        return  # 紧急任务峰期照常
    _logger.info("[peak] 命中避峰窗口，一般任务 %s 降速传输", task.get("name"))
    _BANDWIDTH.throttle(size_bytes)


_CONCURRENCY = _ConcurrencyController()
_BANDWIDTH = _BandwidthGovernor()


def _duration_sec(start_iso: str, end_iso: str) -> float:
    try:
        s = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%S%z")
        e = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%S%z")
        return round((e - s).total_seconds(), 2)
    except Exception:
        return 0.0


def _t0_monotonic() -> float:
    """备份起始单调时间戳（毫秒精度，用于亚秒级备份时长）。"""
    import time as _t
    return _t.monotonic()


def _elapsed_sec(t0: float) -> float:
    """自 t0 起已经过的秒数（毫秒精度），用于亚秒级备份时长补偿。"""
    import time as _t
    return round(_t.monotonic() - t0, 3)


# 校验和补算的文件大小上限（MB）：超过则跳过，避免大文件全量哈希拖慢备份流程
CHECKSUM_MAX_FILE_MB = 2048


def _compute_checksum(path: str, max_file_mb: int = CHECKSUM_MAX_FILE_MB) -> str:
    """安全计算备份产物的 sha256；任何异常都返回空串，绝不影响备份主流程。

    Args:
        path: 备份产物路径（文件；目录型产物直接跳过）。
        max_file_mb: 文件大小上限（MB），超限跳过以免拖慢调度。

    Returns:
        小写十六进制 sha256 摘要；无法计算时返回空字符串。
    """
    import os
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            # 目录型产物（如 mongodump 输出目录）无法直接哈希
            return ""
        size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
        if max_file_mb > 0 and size_mb > max_file_mb:
            _logger.info("[checksum] 文件超过 %s MB，跳过 sha256 计算: %s",
                         max_file_mb, path)
            return ""
        return db.sha256_file(path)
    except Exception as e:
        _logger.warning("[checksum] 计算失败（不影响备份）: %s -> %s", path, e)
        return ""


def _previous_checksum(task_id: int, exclude_record_id: int = None) -> str:
    """取同任务上一条成功备份记录的 checksum，用于"源未变更"判定。

    Args:
        task_id: 备份任务 ID。
        exclude_record_id: 需要排除的记录 ID（通常是当前这次）。

    Returns:
        上一条记录的 checksum；不存在或异常时返回空字符串。
    """
    try:
        rows = db.query(
            "SELECT id, checksum FROM backup_records "
            "WHERE task_id=? AND status='success' AND checksum IS NOT NULL "
            "AND checksum!='' ORDER BY id DESC LIMIT 5",
            (task_id,))
    except Exception as e:
        _logger.debug("[checksum] 查询历史 checksum 失败: %s", e)
        return ""
    for row in rows or []:
        rid = row["id"] if not isinstance(row, dict) else row.get("id")
        if exclude_record_id is not None and rid == exclude_record_id:
            continue
        val = row["checksum"] if not isinstance(row, dict) else row.get("checksum")
        return (val or "").strip()
    return ""


def run_task_now(task_id: int, backup_type: str = None,
                 operator: str = None) -> Optional[dict]:
    """立即执行一次备份（API 手动触发或调度触发）。返回生成的备份记录。

    文件备份(db_type=file)在后台线程异步执行，API 立即返回 {"accepted": True}；
    其他类型同步执行并返回完整 record。
    """
    task = models.get_task(task_id, include_secret=True)
    if not task:
        _logger.warning("任务不存在: %s", task_id)
        return None

    # 文件/目录备份可能耗时很长(SSH传输大目录)，放入后台线程
    if task.get("db_type") == "file":
        t = threading.Thread(target=_bg_execute_backup,
                             args=(task, backup_type, operator),
                             daemon=True,
                             name=f"file-backup-{task_id}")
        t.start()
        _logger.info("[async] 文件任务 #%s 已提交后台线程执行", task_id)
        return {"accepted": True, "task_id": task_id, "status": "running"}

    return _execute_backup(task, backup_type, operator)


def _bg_execute_backup(task: dict, backup_type: str = None,
                       operator: str = None) -> None:
    """后台线程执行文件备份（捕获所有异常，防止线程崩溃）。"""
    try:
        _execute_backup(task, backup_type, operator)
    except Exception:
        _logger.exception("[async] 后台文件任务 #%s 执行异常", task.get("id"))


def _execute_backup(task: dict, backup_type: str = None,
                    operator: str = None) -> dict:
    from core.engines.base import BackupType, BackupResult
    bt = BackupType(backup_type or task.get("backup_type") or "full")
    # 全局并发控制：限制同时执行的备份数量（缺省保守，不破坏旧串行行为）
    _CONCURRENCY.acquire()
    try:
        return _execute_backup_core(task, bt, operator)
    finally:
        _CONCURRENCY.release()


def _execute_backup_core(task: dict, bt, operator: str = None) -> dict:
    from core.engines.base import BackupResult
    # 解析保护策略：取该任务的并行度/备份策略（供调度与并发控制参考）
    try:
        from core.policy import policy_service
        pol = policy_service.resolve(task)
        parallel = int((pol.get("backup_strategy") or {}).get("parallel", 1) or 1)
        _logger.info("任务 %s 生效保护策略(level=%s, parallel=%s)",
                     task.get("id"), pol.get("level"), parallel)
    except Exception as e:
        _logger.debug("保护策略解析失败（不影响备份）: %s", e)
    started = db.now_iso()
    t0 = _t0_monotonic()
    # 启动时立即把任务状态置为 running，让 UI 不再停留在上一次的"失败"
    models.set_task_status(task["id"], started, "running")
    rec_id = models.create_record({
        "task_id": task["id"], "db_type": task["db_type"],
        "backup_type": bt.value, "started_at": started, "status": "running",
    })
    _logger.info("开始备份 task=%s(%s) type=%s", task["id"], task["name"], bt.value)

    result = BackupResult(success=False, message="未执行")
    try:
        from core.engines import get_engine
        engine = get_engine(task["db_type"], task, config.BACKUP_ROOT, _logger)
        # 备份前置检查：物理备份必须有真实客户端；逻辑备份允许仿真兜底
        pre_ok, pre_detail = engine.preflight()
        if not pre_ok:
            result = BackupResult(success=False, message=pre_detail)
            _logger.warning("备份前置检查失败 task=%s: %s", task["id"], pre_detail)
        else:
            if pre_detail and pre_detail != "ok":
                _logger.info("备份前置提示 task=%s: %s", task["id"], pre_detail)
            result = engine.backup(bt)
    except Exception as e:
        result = BackupResult(success=False, message=f"执行异常: {e}")
        _logger.exception("备份异常 task=%s", task["id"])

    finished = db.now_iso()
    status = result.status if hasattr(result, "status") else (
        "success" if result.success else "failed")
    size = int(getattr(result, "size_bytes", 0) or 0)
    path = getattr(result, "backup_path", None)
    checksum = getattr(result, "checksum", "")
    is_sim = 1 if getattr(result, "simulated", False) else 0
    msg = getattr(result, "message", "")

    # 压缩率统计：原始数据量 / 压缩后大小
    original = int(getattr(result, "original_size_bytes", 0) or 0)
    algo = getattr(result, "compress_algo", "") or ""
    ratio = float(getattr(result, "compress_ratio", 0.0) or 0.0)
    if original and size and ratio <= 0:
        ratio = round(size / original, 6)
    if original and size and not algo:
        algo = "gzip"

    # 校验和兜底：部分引擎（mongodb / 物理备份 / 目录型产物）不落 checksum，
    # 而 AI 告警的 L1 完整性校验依赖它作为基准，此处统一补算一次 sha256。
    if not checksum and not is_sim and path:
        checksum = _compute_checksum(path)
        if checksum:
            try:
                result.checksum = checksum
            except (AttributeError, TypeError):
                pass  # 只读结果对象，不影响落库

    db.execute(
        "UPDATE backup_records SET finished_at=?, duration_sec=?, status=?, "
        "size_bytes=?, backup_path=?, checksum=?, is_simulated=?, message=?, "
        "original_size_bytes=?, compress_algo=?, compress_ratio=? WHERE id=?",
        (finished, _elapsed_sec(t0), status, size, path,
         checksum, is_sim, msg, original, algo, ratio, rec_id))

    # CDC 基线捕获（MySQL binlog / PG WAL）
    if result.success and not is_sim:
        try:
            from core import restore_extras
            pwd = db.decrypt_secret(task.get("password") or "")
            if task["db_type"] == "mysql":
                cdc = restore_extras.capture_mysql_cdc(task, pwd)
                if cdc:
                    models.update_record_cdc(rec_id, binlog_file=cdc.get("file"),
                                              binlog_pos=cdc.get("pos"))
            elif task["db_type"] == "postgresql":
                cdc = restore_extras.capture_pg_cdc(task, pwd)
                if cdc:
                    models.update_record_cdc(rec_id, wal_lsn=cdc.get("lsn"))
        except Exception as e:
            _logger.warning("[cdc] capture failed: %s", e)

    # 自动校验（verify）：成功备份后做文件级完整性/可用性探测
    if result.success and not is_sim and path:
        try:
            verify_ok, verify_msg = _verify_backup(task, path, checksum=checksum,
                                                   record_id=rec_id)
            models.mark_record_verified(rec_id, verify_ok, verify_msg)
        except Exception as e:
            models.mark_record_verified(rec_id, False, f"verify exception: {e}")

    models.set_task_status(task["id"], finished, status)

    if result.success and path:
        # 流量控制 + 避峰：传输前按令牌节流（bandwidth_cap_mbps=0 表示不限速）
        _peak_govern(task, int(getattr(result, "size_bytes", 0) or 0))
        _BANDWIDTH.throttle(int(getattr(result, "size_bytes", 0) or 0))
        sm = storage.StorageManager(task, _logger)
        sm.upload_to_remote(path)
        sm.apply_retention()

    # 三级存储复制：备份成功后自动级联到 L2(MinIO) / L3(S3)
    if result.success and path:
        try:
            from core import tier_replication
            # 异步执行，不阻塞通知和返回
            tier_replication.replicate_async(path, task, rec_id, _logger)
        except Exception as e:
            _logger.warning("[tier] 三级复制启动失败（不影响备份结果）: %s", e)

    title = f"[{'成功' if result.success else '失败'}] 备份任务 {task['name']}"
    text = (f"数据库类型: {task['db_type']}\n目标: {task.get('host')}:"
            f"{task.get('port')}/{task.get('db_name')}\n类型: {bt.value}\n"
            f"状态: {status}\n耗时: {_elapsed_sec(t0)}s\n"
            f"大小: {db.human_size(size)}\n备注: {msg}")
    # 渲染 HTML 邮件（卡片样式）
    try:
        from core.email_template import render_backup_result
        html = render_backup_result({
            "name": task.get("name"),
            "db_type": task.get("db_type"),
            "host": task.get("host"),
            "port": task.get("port"),
            "db_name": task.get("db_name"),
            "backup_type": bt.value,
        }, {
            "status": status,
            "size_bytes": size,
            "duration_sec": _duration_sec(started, finished),
            "message": msg,
            "backup_path": path,
        }, trigger_label=("手动触发" if operator else "调度执行"))
    except Exception as e:
        _logger.warning("render_backup_result 失败，回退为纯文本: %s", e)
        html = None
    notifier.Notifier(task, _logger).notify(
        "success" if result.success else "failure", title, text=text, html=html)
    db.add_log("INFO" if result.success else "ERROR", "scheduler",
               f"task={task['id']} {task['name']} -> {status} ({db.human_size(size)})")
    return models.get_record(rec_id)


def run_restore_now(record_id: int, target_host: str = None,
                    target_host_id: int = None, target_db: str = None,
                    operator: str = None,
                    target_host_user: str = None,
                    target_host_password: str = None) -> Optional[dict]:
    rec = models.get_record(record_id)
    if not rec:
        return None
    task = models.get_task(rec["task_id"], include_secret=True)
    from core.engines.base import BackupResult
    started = db.now_iso()
    # 解析目标主机：优先 target_host_id（纳管主机），其次直接输入
    target_host_info = None
    target_host_label = target_host or ""
    if target_host_id:
        from core import ssh_hosts as ssh_mod
        target_host_info = ssh_mod.get_host(target_host_id, include_secret=True)
        if not target_host_info:
            return None
        target_host_label = f"{target_host_info.get('hostname')}:{target_host_info.get('port',22)} (跨主机)"
    elif target_host and (target_host_user or target_host_password):
        # 直接输入模式：从 target_host 字符串 "user@host:port" 解析，密码独立传入
        import re
        m = re.match(r'^(?:(\S+)@)?([^:]+)(?::(\d+))?$', target_host)
        if m:
            target_host_info = {
                "hostname": m.group(2),
                "port": int(m.group(3)) if m.group(3) else 22,
                "username": target_host_user or m.group(1) or "root",
                "password": target_host_password or "",
            }
            target_host_label = f"{target_host_info['hostname']}:{target_host_info['port']} (直接输入)"
        else:
            target_host_label = target_host
    rid = models.create_restore({
        "task_id": rec["task_id"], "record_id": record_id,
        "target_host": target_host_label, "target_db": target_db,
        "started_at": started, "status": "running", "operator": operator,
    })
    result = BackupResult(success=False, message="未执行")
    try:
        from core.engines import get_engine
        engine = get_engine(task["db_type"], task, config.BACKUP_ROOT, _logger)
        result = engine.restore(rec["backup_path"], target_host=target_host,
                                target_host_info=target_host_info,
                                target_db=target_db)
    except Exception as e:
        result = BackupResult(success=False, message=f"恢复异常: {e}")
        _logger.exception("恢复异常 record=%s", record_id)
    finished = db.now_iso()
    status = result.status if hasattr(result, "status") else (
        "success" if result.success else "failed")
    db.execute(
        "UPDATE restore_records SET finished_at=?, status=?, message=? WHERE id=?",
        (finished, status, getattr(result, "message", ""), rid))
    db.add_log("INFO" if result.success else "ERROR", "scheduler",
               f"restore record={record_id} -> {status}")
    return models.list_restores(limit=1)[0]


# ------------------------- 调度器 -------------------------
def _make_trigger(task: dict):
    st = task.get("schedule_type")
    if st == "cron" and task.get("cron_expr"):
        from apscheduler.triggers.cron import CronTrigger
        return CronTrigger.from_crontab(task["cron_expr"])
    if st == "interval" and task.get("interval_minutes"):
        from apscheduler.triggers.interval import IntervalTrigger
        return IntervalTrigger(minutes=int(task["interval_minutes"]))
    return None


def _verify_backup(task: dict, backup_path: str, checksum: str = None,
                   record_id: int = None) -> tuple:
    """备份后自动校验：文件存在 + 可读 + 数据库客户端可识别 + 校验和落库。

    在原有"文件级可用性"校验基础上，补充 sha256 校验和的计算与比对：
    - checksum 为空时按需补算一次（供 AI 告警 L1 完整性校验作基准）；
    - 与同任务上一条成功记录的 checksum 相同时，在校验信息中标注
      "与上次一致（疑似源未变更）"，供运维识别"备份跑了但数据没变"的场景。

    Args:
        task: 备份任务字典，至少包含 id / db_type。
        backup_path: 备份产物路径。
        checksum: 引擎或调度已算出的 sha256；为空则本函数尝试补算。
        record_id: 当前备份记录 ID，用于在历史比对时排除自身。

    Returns:
        (ok, msg) 二元组；ok 为 bool，msg 为中文校验说明。
    """
    import os
    if not os.path.isfile(backup_path):
        return False, f"文件不存在: {backup_path}"
    size = os.path.getsize(backup_path)
    if size == 0:
        return False, "备份文件大小为 0"

    # ---- L1：校验和计算与落库（失败不阻断可用性校验） ----
    digest = (checksum or "").strip()
    if not digest:
        digest = _compute_checksum(backup_path)
    suffix = ""
    if digest:
        if record_id is not None:
            try:
                db.execute(
                    "UPDATE backup_records SET checksum=? "
                    "WHERE id=? AND (checksum IS NULL OR checksum='')",
                    (digest, record_id))
            except Exception as e:
                _logger.warning("[verify] checksum 落库失败（不影响校验）: %s", e)
        prev = _previous_checksum(task.get("id"), exclude_record_id=record_id)
        if prev and prev == digest:
            suffix = "；与上次一致（疑似源未变更）"
        else:
            suffix = f"；sha256={digest[:12]}"

    # ---- L2：类型特定的可用性探测 ----
    db_type = task.get("db_type")
    if db_type == "mysql":
        # 简单 grep 关键标记
        try:
            with open(backup_path, "rb") as f:
                head = f.read(8192)
            # gz 头 / 或 SQL 标记
            if head[:2] == b"\x1f\x8b":
                return True, f"通过（gzip 头, {size} bytes）{suffix}"
            if b"CREATE" in head.upper() or b"INSERT" in head.upper() or b"-- MySQL dump" in head:
                return True, f"通过（SQL dump, {size} bytes）{suffix}"
        except Exception:
            pass
        return True, f"通过（{size} bytes, 头检测失败但文件可读）{suffix}"
    if db_type == "postgresql":
        try:
            with open(backup_path, "rb") as f:
                head = f.read(8192)
            if head[:2] == b"\x1f\x8b":
                return True, f"通过（gzip 头, {size} bytes）{suffix}"
            if b"PostgreSQL" in head or b"pg_dump" in head or b"CREATE TABLE" in head.upper():
                return True, f"通过（pg_dump, {size} bytes）{suffix}"
        except Exception:
            pass
        return True, f"通过（{size} bytes）{suffix}"
    # 其他类型：仅检查文件可读
    return True, f"通过（{size} bytes）{suffix}"


def _job_wrapper(task_id: int):
    try:
        run_task_now(task_id)
    except Exception:
        _logger.exception("调度任务执行异常 task=%s", task_id)


def _inspection_job_wrapper():
    """调度触发的巡检：调用 inspection.run_inspection 并标记 triggered_by=schedule。"""
    try:
        from core import inspection as inspection_engine
        summary = inspection_engine.run_inspection(triggered_by="schedule")
        _logger.info("[inspection] 调度巡检完成: total=%s pass=%s warn=%s fail=%s",
                     summary.get("total"), summary.get("pass"),
                     summary.get("warn"), summary.get("fail"))
    except Exception:
        _logger.exception("调度巡检异常")


def _make_inspection_trigger(cron_expr: str):
    """根据 cron 表达式构造触发器；空或非法返回 None。"""
    from apscheduler.triggers.cron import CronTrigger
    try:
        return CronTrigger.from_crontab(cron_expr)
    except Exception:
        return None


def _register_inspection(sched):
    """根据 system_config 中的 inspection_schedule 注册巡检 job。

    inspection_schedule 形如：
        {"enabled": true, "cron": "0 9 * * *"}
    无配置 / enabled=false / cron 非法 → 跳过。
    """
    raw = db.get_system_config("inspection_schedule")
    if not raw:
        return
    try:
        import json
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return
    if not cfg.get("enabled"):
        return
    cron = (cfg.get("cron") or "").strip()
    if not cron:
        return
    trig = _make_inspection_trigger(cron)
    if not trig:
        _logger.warning("[inspection] cron 表达式非法: %r", cron)
        return
    # 移除旧的，再添加新的（reload 时保证幂等）
    try:
        sched.remove_job("inspection_global")
    except Exception:
        pass
    sched.add_job(_inspection_job_wrapper, trig, id="inspection_global",
                  replace_existing=True, misfire_grace_time=3600)
    _logger.info("[inspection] 巡检已注册 cron=%s", cron)


def _lifecycle_job_wrapper():
    """调度触发的生命周期流转：运行 LifecycleEngine.run_once()。"""
    try:
        from core import lifecycle as lifecycle_engine
        summary = lifecycle_engine.LifecycleEngine().run_once()
        _logger.info("[lifecycle] 调度流转完成: %s", summary)
    except Exception:
        _logger.exception("[lifecycle] 调度流转异常")


def _register_lifecycle(sched):
    """根据 system_config 中的 lifecycle_interval_hours 注册生命周期周期 job。

    缺省每天一次；间隔 <=0 时跳过。reload 时保证幂等（replace_existing）。
    """
    raw = db.get_system_config("lifecycle_interval_hours")
    try:
        hours = int(raw) if raw else 24
    except Exception:
        hours = 24
    if hours <= 0:
        return
    from apscheduler.triggers.interval import IntervalTrigger
    try:
        sched.remove_job("lifecycle_engine")
    except Exception:
        pass
    sched.add_job(_lifecycle_job_wrapper, IntervalTrigger(hours=hours),
                  id="lifecycle_engine", replace_existing=True,
                  misfire_grace_time=3600)
    _logger.info("[lifecycle] 已注册周期任务，间隔 %d 小时", hours)


def _clone_expire_job_wrapper():
    """调度触发的克隆到期自动销毁：运行 CloneService.expire_due_clones()。"""
    try:
        from core import clone_service
        clone_service.clone_service.expire_due_clones()
    except Exception:
        _logger.exception("[clone] 到期销毁调度异常")


def _register_clone_expire(sched):
    """根据 system_config 中的 clone_expire_interval_hours 注册克隆到期销毁周期 job。

    缺省每天一次；间隔 <=0 时跳过。reload 时保证幂等（replace_existing）。
    """
    raw = db.get_system_config("clone_expire_interval_hours")
    try:
        hours = int(raw) if raw else 24
    except Exception:
        hours = 24
    if hours <= 0:
        return
    from apscheduler.triggers.interval import IntervalTrigger
    try:
        sched.remove_job("clone_expire")
    except Exception:
        pass
    sched.add_job(_clone_expire_job_wrapper, IntervalTrigger(hours=hours),
                  id="clone_expire", replace_existing=True,
                  misfire_grace_time=3600)
    _logger.info("[clone] 已注册到期销毁周期任务，间隔 %d 小时", hours)


def _ai_alert_job_wrapper():
    """调度触发的 AI 预测分析：运行 AIPredictor.run_all_checks()。"""
    try:
        from core import ai_alert as ai_alert_engine
        summary = ai_alert_engine.AIPredictor().run_all_checks()
        _logger.info("[ai_alert] 周期分析完成: recorded=%s critical=%s",
                     summary.get("recorded"), summary.get("critical_fired"))
    except Exception:
        _logger.exception("[ai_alert] 周期分析异常")


def _register_ai_alert(sched):
    """根据 AIPredictor 配置的 ai_alert_interval_hours 注册 AI 分析周期 job。

    缺省每 6 小时一次；间隔 <=0 时跳过。reload 时保证幂等（replace_existing）。
    """
    hours = 6
    try:
        from core import ai_alert as ai_alert_engine
        hours = int(ai_alert_engine.AIPredictor().get_config()
                    .get("ai_alert_interval_hours") or 6)
    except Exception:
        hours = 6
    if hours <= 0:
        return
    from apscheduler.triggers.interval import IntervalTrigger
    try:
        sched.remove_job("ai_alert_engine")
    except Exception:
        pass
    sched.add_job(_ai_alert_job_wrapper, IntervalTrigger(hours=hours),
                  id="ai_alert_engine", replace_existing=True,
                  misfire_grace_time=3600)
    _logger.info("[ai_alert] 已注册周期分析任务，间隔 %d 小时", hours)


# ------------------------- Phase 4：季度演练排程 -------------------------
def _drill_schedule_job_wrapper():
    """调度触发的季度演练：运行 DrillEngine.run_scheduled_drill()。

    真正是否执行由 drill_schedule 的 enabled / next_run 控制（run_scheduled_drill 内部判定）。
    """
    try:
        from core import drill as drill_engine
        summary = drill_engine.run_scheduled_drill()
        _logger.info("[drill] 周期检查: ran=%s next_run=%s",
                     summary.get("count"), summary.get("next_run"))
    except Exception:
        _logger.exception("[drill] 周期检查异常")


def _register_drill_schedule(sched):
    """注册季度演练排程周期检查 job（每 24h 检查一次是否到期）。

    无论是否启用都注册，启用且到期后由 run_scheduled_drill 自动触发。
    reload 时保证幂等（replace_existing）。
    """
    from apscheduler.triggers.interval import IntervalTrigger
    try:
        sched.remove_job("drill_schedule")
    except Exception:
        pass
    sched.add_job(_drill_schedule_job_wrapper, IntervalTrigger(hours=24),
                  id="drill_schedule", replace_existing=True,
                  misfire_grace_time=3600)
    _logger.info("[drill] 已注册季度演练排程周期检查（每 24h）")


def _register(sched, task: dict, prefix: str = "task"):
    trig = _make_trigger(task)
    if not trig:
        return
    wrapper = _job_wrapper if prefix == "task" else _job_wrapper_sync
    sched.add_job(wrapper, trig, id=f"{prefix}_{task['id']}",
                  replace_existing=True, args=[task["id"]],
                  misfire_grace_time=3600)


def _job_wrapper_sync(sync_id: int):
    try:
        core.sync.run_sync(sync_id)
    except Exception:
        _logger.exception("调度同步异常 sync=%s", sync_id)


# ------------------------- RT 实时备份：调度生命周期集成 -------------------------
# T03-S3：调度器拉起 RtSupervisor，并注册 3 个强制单实例的周期任务。
# Supervisor 主循环在共享 APScheduler 上的 job id 固定为 rt_supervisor_tick，
# reload 时据此跳过，做到「重载不重启守护」。
_RT_SUPERVISOR_TICK_ID = "rt_supervisor_tick"


def _rt_health_job_wrapper():
    """周期健康扫描：产出 RPO 超标 / 守护失败 / 磁盘配额告警（带抑制窗口）。"""
    try:
        from core.rt_backup import health as rt_health_mod
        alerts = rt_health_mod.check_alerts(emit=True)
        if alerts:
            _logger.info("[rt.health] 周期扫描发现 %d 条告警", len(alerts))
    except Exception:
        _logger.exception("[rt.health] 周期扫描异常")


def _rt_prune_job_wrapper():
    """周期清理：按保留天数删除过期的 sealed / inc 段（base / bundles 不动）。

    遍历所有开启过实时保护的任务（含已停用的），分别清理文件仓库与 DB 日志仓库；
    prune 内部已跳过仍被 recovery_journal 引用的活跃段，绝不误删恢复链。
    """
    import time
    try:
        from core.rt_backup.repo import KIND_DB_LOG, KIND_FILE, LogRepository
        now = time.time()
        file_ret = max(1, int(config.RT_FILE_RETENTION_DAYS))
        db_ret = max(1, int(config.RT_DB_LOG_RETENTION_DAYS))
        try:
            tasks = models.list_rt_tasks(only_enabled=False)
        except Exception as exc:
            _logger.warning("[rt.prune] 读取实时任务失败: %s", exc)
            return
        removed = 0
        for task in tasks:
            tid = int(task.get("id") or 0)
            if tid <= 0:
                continue
            try:
                removed += LogRepository(tid, KIND_FILE).prune(now - file_ret * 86400)
                removed += LogRepository(tid, KIND_DB_LOG).prune(now - db_ret * 86400)
            except Exception as exc:
                _logger.warning("[rt.prune] task=%s 清理异常: %s", tid, exc)
        if removed:
            _logger.info("[rt.prune] 清理过期日志 %d 个文件", removed)
    except Exception:
        _logger.exception("[rt.prune] 周期清理异常")


def _rt_watchdog_job_wrapper():
    """看门狗：Supervisor 仍在运行时做安全网对账（主循环每 6 tick 也对账一次）。"""
    try:
        from core import rt_backup
        sup = rt_backup.get_supervisor()
        if not sup.is_running():
            return
        sup.reconcile()
    except Exception:
        _logger.exception("[rt.watchdog] 看门狗异常")


def _register_rt_periodic_jobs(sched) -> None:
    """注册 3 个强制单实例的 RT 周期任务（幂等，reload 时复用）。

    三个 job 均 ``max_instances=1 + coalesce=True``，杜绝并发 / tick 堆积撕裂状态机。
    """
    from apscheduler.triggers.interval import IntervalTrigger
    plan = [
        ("rt_health", _rt_health_job_wrapper, IntervalTrigger(minutes=1), 300),
        ("rt_prune", _rt_prune_job_wrapper, IntervalTrigger(hours=1), 3600),
        ("rt_watchdog", _rt_watchdog_job_wrapper, IntervalTrigger(minutes=5), 600),
    ]
    for job_id, fn, trig, grace in plan:
        try:
            sched.remove_job(job_id)
        except Exception:
            pass
        sched.add_job(fn, trig, id=job_id, replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=grace)
    _logger.info("[rt] 已注册周期任务 rt_health / rt_prune / rt_watchdog")


def _register_rt_backup(sched) -> None:
    """调度生命周期集成：由调度器拉起 RtSupervisor 并注册 3 个周期任务。

    - RT 总开关关闭时直接跳过（不抢锁、不注册任何 job）；
    - 把外部 APScheduler 实例传给 ``RtSupervisor.start()``，复用同一调度器驱动
      主循环，避免多 worker（gunicorn -w N）部署时每进程各起一个 BackgroundScheduler；
    - 三个周期任务均 ``max_instances=1 + coalesce=True``，绝不并发执行。
    """
    if not config.RT_BACKUP_ENABLED:
        _logger.info("[rt] 实时备份总开关关闭 (RT_BACKUP_ENABLED=false)，跳过调度集成")
        return
    try:
        from core import rt_backup
        ok = rt_backup.start(sched)
        if not ok:
            _logger.info("[rt] RtSupervisor 未在本进程启动（总开关关闭或未抢到锁）")
            return
        _register_rt_periodic_jobs(sched)
    except Exception:
        _logger.exception("[rt] 调度集成 RtSupervisor 异常")


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if not config.SCHEDULER_ENABLED:
        _logger.info("调度器已禁用 (SCHEDULER_ENABLED=false)")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    for task in models.list_tasks(enabled=True):
        _register(_scheduler, task)
    for st in models.list_sync_tasks(enabled=True):
        _register(_scheduler, st, prefix="sync")
    _register_inspection(_scheduler)
    _register_lifecycle(_scheduler)
    _register_clone_expire(_scheduler)
    _register_ai_alert(_scheduler)
    _register_drill_schedule(_scheduler)
    _register_rt_backup(_scheduler)
    _scheduler.start()
    _logger.info("调度器已启动，已注册 %d 个任务", len(_scheduler.get_jobs()))
    return _scheduler


def reload_scheduler():
    global _scheduler
    if _scheduler is None:
        return start_scheduler()
    for j in list(_scheduler.get_jobs()):
        # 不重启 Supervisor：保留其主循环 tick job，仅重载常规与 3 个 RT 周期任务
        if j.id == _RT_SUPERVISOR_TICK_ID:
            continue
        _scheduler.remove_job(j.id)
    for task in models.list_tasks(enabled=True):
        _register(_scheduler, task)
    for st in models.list_sync_tasks(enabled=True):
        _register(_scheduler, st, prefix="sync")
    _register_inspection(_scheduler)
    _register_lifecycle(_scheduler)
    _register_clone_expire(_scheduler)
    _register_ai_alert(_scheduler)
    _register_drill_schedule(_scheduler)
    # RT 周期任务幂等重注册；Supervisor 主循环 tick 已保留，不重启守护
    if config.RT_BACKUP_ENABLED:
        _register_rt_periodic_jobs(_scheduler)
    _logger.info("调度器已重载，当前 %d 个任务", len(_scheduler.get_jobs()))


def stop_scheduler():
    global _scheduler
    if _scheduler:
        try:
            from core import rt_backup
            rt_backup.stop()
        except Exception:
            _logger.warning("[rt] 停止 RtSupervisor 异常（忽略）")
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_status() -> dict:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for j in _scheduler.get_jobs():
        nxt = j.next_run_time.isoformat() if j.next_run_time else None
        jobs.append({"id": j.id, "next_run": nxt})
    return {"running": _scheduler.running, "jobs": jobs}
