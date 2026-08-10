# -*- coding: utf-8 -*-
"""
准 CDP 实时备份的数据类型定义。

全部为纯 dataclass，无外部依赖、无 IO，便于单测与跨模块传递：
- RtConfig      任务级实时配置（从 backup_tasks 行解析，缺省回落 config.RT_*）
- ChangeBatch   文件变更批次（Watcher → FileRtCapture）
- RecoveryPoint 一个 PIT 恢复点（recovery_journal 一行的内存投影）
- RtHealth      任务实时健康状态（Supervisor → API → UI）
- RestorePlan   PITR 恢复计划（PITRRestore.build_plan 产物）
"""
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import config

# 文件型任务的 db_type（与 core/engines/file.py:FileBackupEngine.db_type 一致）
FILE_DB_TYPE = "file"

# 捕获类别
KIND_FILE = "file"
KIND_DB_LOG = "db-log"

# 恢复点类别（recovery_journal.rp_kind）
RP_BASE_FULL = "base-full"
RP_FILE_INC = "file-inc"
RP_DB_FULL = "db-full"
RP_DB_LOG = "db-log"

# rt_tasks.rt_mode 取值（T02/T03 注册的 worker 类型）
RT_MODE_FILE_WATCH = "file_watch"      # 文件近实时捕获（watchdog 优先 / polling 兜底）
RT_MODE_DB_CDC = "db_cdc"              # 数据库日志流持续捕获
RT_MODE_MIXED = "mixed"                # 预留：同任务同时含文件与库
RT_MODE_FILE_POLLING = "file_polling"  # 历史值，等价于 file_watch 的纯轮询实现

# 历史 rt_mode 值 → 规范值映射（建表默认值仍是 file_polling，读取时统一归一）
RT_MODE_ALIASES = {
    RT_MODE_FILE_POLLING: RT_MODE_FILE_WATCH,
    "polling": RT_MODE_FILE_WATCH,
    "watchdog": RT_MODE_FILE_WATCH,
    "file": RT_MODE_FILE_WATCH,
    "db": RT_MODE_DB_CDC,
    "stream": RT_MODE_DB_CDC,
}


def normalize_rt_mode(mode: str, capture_kind: str = KIND_FILE) -> str:
    """把任意历史/别名 rt_mode 归一到 ``file_watch`` / ``db_cdc`` / ``mixed``。"""
    value = (mode or "").strip().lower()
    if value in (RT_MODE_FILE_WATCH, RT_MODE_DB_CDC, RT_MODE_MIXED):
        return value
    if value in RT_MODE_ALIASES:
        return RT_MODE_ALIASES[value]
    return RT_MODE_DB_CDC if capture_kind == KIND_DB_LOG else RT_MODE_FILE_WATCH


# 支持日志流捕获的数据库引擎
# T06：信创三库（Oracle / Kingbase / Dameng）自研适配器轨道上线后一并纳入。
#   - mysql/mariadb  : mysqlbinlog 持续拉流（binlog 位点）
#   - postgresql     : pg_receivewal 流复制（LSN 位点）
#   - oracle         : DBMS_LOGMNR 轮询拉取（SCN 位点）
#   - kingbase       : 兼容 PG 流复制协议（LSN 位点）
#   - dameng         : DM_LOGMNR 轮询拉取（dm_lsn 位点）
STREAMABLE_ENGINES = (
    "mysql", "mariadb", "postgresql", "oracle", "kingbase", "dameng",
)
# 语义别名（设计文档 §3.2 用名），与 STREAMABLE_ENGINES 等价
DB_LOG_ENGINES = STREAMABLE_ENGINES

# 位点种类（recovery_journal.position_kind / 守护 describe() 用）
# 复用 wal_lsn / wal_end_lsn 两列承载全部位点值（CH-T06-2，零 Schema 迁移），
# 由 position_kind 区分语义，UI 据此给出 "SCN: xxx" / "LSN: x/y" 前缀。
POSITION_KIND_LSN = "lsn"          # PostgreSQL / Kingbase
POSITION_KIND_SCN = "scn"          # Oracle
POSITION_KIND_DM_LSN = "dm_lsn"    # Dameng
POSITION_KIND_BINLOG = "binlog"    # MySQL / MariaDB
POSITION_KINDS = (
    POSITION_KIND_LSN, POSITION_KIND_SCN,
    POSITION_KIND_DM_LSN, POSITION_KIND_BINLOG,
)
# 位点种类 → UI 展示前缀
POSITION_KIND_LABELS = {
    POSITION_KIND_LSN: "LSN",
    POSITION_KIND_SCN: "SCN",
    POSITION_KIND_DM_LSN: "LSN",
    POSITION_KIND_BINLOG: "BINLOG",
}
# 引擎 → 默认位点种类（守护未上报 position_kind 时的兜底）
ENGINE_POSITION_KIND = {
    "mysql": POSITION_KIND_BINLOG,
    "mariadb": POSITION_KIND_BINLOG,
    "postgresql": POSITION_KIND_LSN,
    "kingbase": POSITION_KIND_LSN,
    "oracle": POSITION_KIND_SCN,
    "dameng": POSITION_KIND_DM_LSN,
}

# 守护状态（rt_capture_state.daemon_status）
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

# 健康灯（rt_capture_state.health）
HEALTH_GREEN = "green"
HEALTH_YELLOW = "yellow"
HEALTH_RED = "red"
HEALTH_UNKNOWN = "unknown"


def norm_path(path: str) -> str:
    """路径归一化：统一正斜杠，便于日志输出与跨平台字典序比较（共享知识 #1）。"""
    return (path or "").replace("\\", "/")


def _int_or(value, fallback: int) -> int:
    """安全整型转换：None/空串/非法值一律回落 fallback。"""
    if value is None or value == "":
        return int(fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


@dataclass
class RtConfig:
    """任务级实时保护配置。

    所有阈值都遵循「任务列覆盖 > 全局 config.RT_* 默认」的优先级。
    """
    task_id: int = 0
    capture_kind: str = KIND_FILE          # file | db-log
    engine: str = FILE_DB_TYPE             # file | mysql | mariadb | postgresql
    enabled: bool = False
    mode: str = "auto"                     # 文件: auto|polling|watchdog；DB: auto|stream|archive_poll|sample
    interval_sec: int = 180                # 文件强制 flush 上限 / DB 封存间隔
    debounce_sec: int = 5                  # 事件去抖窗口
    consistency: str = "crash"             # crash | fs | app
    rpo_target_sec: int = 300
    log_retention_days: int = 7
    demo_only: bool = False

    @classmethod
    def from_task(cls, task: dict) -> "RtConfig":
        """从 backup_tasks 行构造实时配置。"""
        task = task or {}
        db_type = (task.get("db_type") or "").lower()
        is_file = db_type == FILE_DB_TYPE
        capture_kind = KIND_FILE if is_file else KIND_DB_LOG

        if is_file:
            default_interval = config.RT_FILE_INTERVAL_SEC
            default_rpo = config.RT_FILE_RPO_TARGET_SEC
            default_retention = config.RT_FILE_RETENTION_DAYS
            default_mode = config.RT_FILE_WATCHER
        else:
            default_interval = config.RT_DB_SEAL_INTERVAL_SEC
            default_rpo = config.RT_DB_RPO_TARGET_SEC
            default_retention = config.RT_DB_LOG_RETENTION_DAYS
            default_mode = config.RT_DB_MODE

        # rt_rpo_target_sec 为秒级精度覆盖；未设置时回落到 rpo_target_min（分钟）再回落全局
        rpo = task.get("rt_rpo_target_sec")
        if rpo in (None, ""):
            rpo_min = task.get("rpo_target_min")
            rpo = _int_or(rpo_min, 0) * 60 if rpo_min not in (None, "", 0) else default_rpo
        rpo = _int_or(rpo, default_rpo)
        if rpo <= 0:
            rpo = default_rpo

        return cls(
            task_id=_int_or(task.get("id"), 0),
            capture_kind=capture_kind,
            engine=db_type or FILE_DB_TYPE,
            enabled=bool(task.get("rt_enabled")),
            mode=(task.get("rt_mode") or default_mode or "auto"),
            interval_sec=max(10, _int_or(task.get("rt_interval_sec"), default_interval)),
            debounce_sec=max(1, _int_or(config.RT_FILE_DEBOUNCE_SEC, 5)),
            consistency=(task.get("rt_consistency") or "crash"),
            rpo_target_sec=rpo,
            log_retention_days=max(1, _int_or(task.get("rt_log_retention_days"),
                                              default_retention)),
            demo_only=bool(task.get("demo_only")),
        )

    @property
    def is_file(self) -> bool:
        return self.capture_kind == KIND_FILE

    @property
    def can_stream(self) -> bool:
        """该引擎是否支持真实日志流捕获（否则只能降级采样/仿真）。"""
        return self.engine in STREAMABLE_ENGINES

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangeBatch:
    """一次文件变更批次。changed/deleted 一律是相对源根目录的相对路径。"""
    detected_at: str = ""
    changed: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    total_files: int = 0
    trigger: str = "poll"                  # poll | event | interval | manual | base
    # 本次扫描得到的完整源文件列表 {rel: (size, mtime)}，供上层落快照，避免二次扫描
    snapshot: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.changed and not self.deleted

    def to_dict(self) -> dict:
        return {
            "detected_at": self.detected_at,
            "changed": len(self.changed),
            "deleted": len(self.deleted),
            "total_files": self.total_files,
            "trigger": self.trigger,
        }


@dataclass
class RecoveryPoint:
    """一个 PIT 恢复点（recovery_journal 一行）。"""
    id: int = 0
    task_id: int = 0
    record_id: Optional[int] = None
    set_id: Optional[int] = None
    parent_rp_id: Optional[int] = None
    rp_kind: str = RP_FILE_INC
    rp_type: str = "incremental"
    pit_at: str = ""
    pit_seq: int = 0
    consistency: str = "crash"
    binlog_file: str = ""
    binlog_pos: int = 0
    binlog_end_file: str = ""
    binlog_end_pos: int = 0
    wal_lsn: str = ""
    wal_end_lsn: str = ""
    file_set_key: str = ""
    changed_files: int = 0
    deleted_files: int = 0
    storage_tier: int = 1
    object_key: str = ""
    bundle_key: str = ""
    size_bytes: int = 0
    checksum: str = ""
    verified: int = 0
    verify_msg: str = ""
    is_simulated: int = 0
    message: str = ""
    expires_at: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "RecoveryPoint":
        """从 SQLite 行字典构造。未知列忽略，缺失列取 dataclass 默认值。"""
        row = dict(row or {})
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {}
        for key, value in row.items():
            if key not in known:
                continue
            if value is None:
                continue
            kwargs[key] = value
        return cls(**kwargs)

    @property
    def is_full(self) -> bool:
        """是否为恢复链链头（全量基准）。"""
        return self.rp_kind in (RP_BASE_FULL, RP_DB_FULL) or self.rp_type == "full"

    def position_label(self) -> str:
        """人类可读的位点标签，供 UI 详情面板直接展示。"""
        if self.binlog_file:
            return f"{self.binlog_file}:{self.binlog_pos}"
        if self.wal_lsn:
            return str(self.wal_lsn)
        if self.changed_files or self.deleted_files:
            return f"+{self.changed_files}/-{self.deleted_files}"
        return "-"

    def exists_on_disk(self) -> bool:
        """本地 Tier1 产物是否仍在磁盘上（非 Tier1 一律返回 True，不做远端探测）。"""
        if self.storage_tier != 1:
            return True
        return bool(self.object_key) and os.path.isfile(self.object_key)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["object_key"] = norm_path(self.object_key)
        data["is_full"] = self.is_full
        data["position_label"] = self.position_label()
        return data


@dataclass
class RtHealth:
    """任务实时健康状态。UI 健康灯与 RPO 大字都取自这里。"""
    task_id: int = 0
    task_name: str = ""
    capture_kind: str = KIND_FILE
    engine: str = FILE_DB_TYPE
    daemon_status: str = "stopped"         # stopped|starting|running|degraded|failed
    degrade_reason: str = ""
    watcher_impl: str = ""
    health: str = "unknown"                # green | yellow | red | unknown
    lag_sec: int = 0
    rpo_actual_sec: int = 0
    rpo_target_sec: int = 300
    last_rp_at: str = ""
    last_capture_at: str = ""
    position_label: str = "-"
    restart_count: int = 0
    consecutive_fail: int = 0
    rp_count_today: int = 0
    bytes_today: int = 0
    last_error: str = ""
    last_heartbeat_at: str = ""
    is_simulated: bool = False

    def is_breach(self) -> bool:
        """实际 RPO 是否已超出目标（黄灯及以上）。"""
        if self.rpo_target_sec <= 0:
            return False
        return self.rpo_actual_sec > self.rpo_target_sec

    def compute_health(self) -> str:
        """按 §6.1 阈值计算健康灯：绿 ≤target；黄 ≤2×target；红 >2×target 或守护异常。"""
        if self.daemon_status in ("failed",):
            return "red"
        if self.daemon_status in ("stopped",):
            return "unknown"
        if self.daemon_status == "degraded":
            return "yellow"
        target = self.rpo_target_sec
        if target <= 0:
            return "green"
        if not self.last_rp_at:
            # 刚起步、还没有任何恢复点：按运行时长宽容处理，交由 lag 判定
            return "yellow" if self.rpo_actual_sec > 2 * target else "green"
        if self.rpo_actual_sec <= target:
            return "green"
        if self.rpo_actual_sec <= 2 * target:
            return "yellow"
        return "red"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["is_breach"] = self.is_breach()
        return data


@dataclass
class RestorePlan:
    """PITR 恢复计划。complete=False 时 gap_reason 必须可读地说明缺口原因。"""
    task_id: int = 0
    kind: str = KIND_FILE                  # file | db-log
    engine: str = FILE_DB_TYPE
    target_ts: str = ""
    base_point: Optional[RecoveryPoint] = None
    chain: List[RecoveryPoint] = field(default_factory=list)
    archives: List[str] = field(default_factory=list)
    stop_binlog_file: str = ""
    stop_binlog_pos: int = 0
    stop_lsn: str = ""
    complete: bool = False
    gap_reason: str = ""
    total_bytes: int = 0

    def summary(self) -> str:
        """一句话摘要，供 UI 恢复二次确认展示。"""
        full_cnt = 1 if self.base_point else 0
        inc_cnt = max(0, len(self.chain) - full_cnt)
        if self.kind == KIND_DB_LOG:
            pos = (f"{self.stop_binlog_file}:{self.stop_binlog_pos}"
                   if self.stop_binlog_file else (self.stop_lsn or "-"))
            return (f"恢复至 {self.target_ts}（位点 {pos}）："
                    f"{full_cnt} 个全量 + {inc_cnt} 个日志段")
        return f"恢复至 {self.target_ts}：{full_cnt} 个基准全量 + {inc_cnt} 个增量"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "engine": self.engine,
            "target_ts": self.target_ts,
            "base_point": self.base_point.to_dict() if self.base_point else None,
            "chain": [p.to_dict() for p in self.chain],
            "archives": [norm_path(a) for a in self.archives],
            "summary": self.summary(),
            "stop_binlog_file": self.stop_binlog_file,
            "stop_binlog_pos": self.stop_binlog_pos,
            "stop_lsn": self.stop_lsn,
            "complete": self.complete,
            "gap_reason": self.gap_reason,
            "total_bytes": self.total_bytes,
            "chain_length": len(self.chain),
        }
