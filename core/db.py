# -*- coding: utf-8 -*-
"""
SQLite 元数据库封装：连接、建表、加密与工具函数。

备份平台自身的元数据（任务、记录、日志）存放在 SQLite，零外部依赖、开箱即用。
真实数据库备份文件存放在 config.BACKUP_ROOT 下的本地或远程存储中。
"""
import sqlite3
import os
import json
import base64
import hashlib
import logging
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

import config

INSTANCE_DIR = config.INSTANCE_DIR
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = config.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 多线程（Flask + APScheduler）访问 SQLite，需要关闭单线程检查并加写锁
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    biz_system      TEXT,
    db_type         TEXT NOT NULL,
    host            TEXT,
    port            INTEGER,
    username        TEXT,
    password        TEXT,
    db_name         TEXT,
    auth_mode       TEXT DEFAULT 'password',
    backup_type     TEXT DEFAULT 'full',
    backup_mode     TEXT DEFAULT 'logical',
    schedule_type   TEXT DEFAULT 'none',
    cron_expr       TEXT,
    interval_minutes INTEGER,
    mixed_backup    INTEGER DEFAULT 0,          -- 0=单类型 1=混合（全量+增量）
    full_schedule_type   TEXT DEFAULT 'none', -- full/incremental 子调度类型
    full_schedule_expr   TEXT,                -- full 子调度表达式
    full_schedule_days   TEXT,                -- full 运行星期，逗号分隔 0(一)~6(日)，空=不限
    incremental_schedule_type TEXT DEFAULT 'none',
    incremental_schedule_expr   TEXT,
    incremental_schedule_days   TEXT,         -- incremental 运行星期，逗号分隔 0(一)~6(日)，空=不限
    bandwidth_limit  INTEGER DEFAULT 0,       -- 限速 KB/s，0=不限
    compress_level   INTEGER DEFAULT 0,       -- 压缩级别，0=跟随默认(_ZSTD_LEVEL)
    enabled         INTEGER DEFAULT 1,
    retention_days  INTEGER DEFAULT 30,
    retention_count INTEGER DEFAULT 50,
    storage_backend TEXT DEFAULT 'local',
    remote_host     TEXT,
    remote_port     INTEGER,
    remote_user     TEXT,
    remote_password TEXT,
    remote_path     TEXT,
    remote_key      TEXT,
    compress        INTEGER DEFAULT 1,
    extra_options   TEXT,
    demo_only       INTEGER DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    last_run_at     TEXT,
    last_status     TEXT
);

CREATE TABLE IF NOT EXISTS protection_policies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    level             TEXT NOT NULL DEFAULT 'general',   -- core | important | general
    rpo_target_min    INTEGER DEFAULT 0,                 -- RPO 目标（分钟），0 表示近实时
    rto_target_min    INTEGER DEFAULT 0,                 -- RTO 目标（分钟）
    backup_strategy   TEXT,                              -- JSON: 备份类型/模式/频率/并行度
    link_strategy     TEXT,                              -- JSON: 复制/容灾链路选择
    retention         TEXT,                              -- JSON: 保留/生命周期
    enabled           INTEGER DEFAULT 1,
    created_at        TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS restore_verify_policies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL,
    name                TEXT,
    recovery_pool       TEXT DEFAULT '',                  -- 恢复池/恢复目录标识
    schedule_type       TEXT DEFAULT 'manual',            -- none | cron | interval | manual
    cron_expr           TEXT,
    interval_minutes    INTEGER,
    clone_retention_min INTEGER DEFAULT 30,               -- 克隆保留（分钟）
    enabled             INTEGER DEFAULT 1,
    last_run_at         TEXT,
    last_status         TEXT,
    last_report_id      INTEGER,
    created_at          TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS restore_test_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id    INTEGER,
    task_id      INTEGER,
    record_id    INTEGER,                                 -- 基于哪次备份记录
    db_type      TEXT,
    status       TEXT,                                    -- running | success | failed
    duration_sec REAL,
    message      TEXT,
    cleaned      INTEGER DEFAULT 0,                       -- 测试克隆/临时文件是否已清理
    created_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS backup_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER,
    db_type       TEXT,
    backup_type   TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    duration_sec  REAL,
    status        TEXT,
    size_bytes    INTEGER DEFAULT 0,
    backup_path   TEXT,
    checksum      TEXT,
    is_simulated  INTEGER DEFAULT 0,
    message       TEXT,
    binlog_file   TEXT,
    binlog_pos    INTEGER,
    wal_lsn       TEXT,
    verified      INTEGER DEFAULT 0,
    verify_msg    TEXT,
    storage_tier  TEXT DEFAULT 'local'               -- local | minio | s3 | multi
);

CREATE TABLE IF NOT EXISTS restore_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    record_id    INTEGER,
    target_host  TEXT,
    target_db    TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,
    message      TEXT,
    operator     TEXT,
    detail_log   TEXT                            -- 恢复过程详细日志（stdout/stderr/各阶段说明）
);

CREATE TABLE IF NOT EXISTS system_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    level     TEXT,
    source    TEXT,
    message   TEXT
);

CREATE TABLE IF NOT EXISTS ssh_hosts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    host_key    TEXT NOT NULL UNIQUE,
    hostname    TEXT,
    port        INTEGER DEFAULT 22,
    username    TEXT,
    password    TEXT,
    auth_type   TEXT DEFAULT 'password',
    private_key TEXT,
    os_type     TEXT DEFAULT 'linux',
    remark      TEXT,
    last_status TEXT,
    last_check_at TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS plugin_host_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id       INTEGER,                  -- ssh_hosts.id；本机=0
    host_key      TEXT NOT NULL,            -- "user@hostname:port" 或 "local"
    plugin_id     TEXT NOT NULL,
    status        TEXT DEFAULT 'uninstalled', -- uninstalled|installing|installed|failed|manual|success_with_warn
    version       TEXT,                     -- 远端探测到的工具版本
    method        TEXT,                     -- package_manager|fallback_download|manual_only
    extract_dir   TEXT,                     -- 远端解压目录 /opt/backup_plugins/<pid>
    found_paths   TEXT,                     -- JSON {"xtrabackup": "/opt/.../bin/xtrabackup"}
    message       TEXT,
    installed_at  TEXT,
    updated_at    TEXT,
    UNIQUE(host_key, plugin_id)
);

CREATE TABLE IF NOT EXISTS sync_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    source_type        TEXT DEFAULT 'managed',   -- managed(引用现有备份任务) | manual
    source_task_id     INTEGER,
    src_db_type        TEXT,
    src_host           TEXT,
    src_port           INTEGER,
    src_username       TEXT,
    src_password       TEXT,
    src_db_name        TEXT,
    src_schema         TEXT,                     -- 源 schema/database
    tgt_db_type        TEXT,
    tgt_host           TEXT,
    tgt_port           INTEGER,
    tgt_username       TEXT,
    tgt_password       TEXT,
    tgt_db_name        TEXT,
    tgt_schema         TEXT,                     -- 目标 schema/database
    source_table       TEXT,                     -- 源表名
    target_table       TEXT,                     -- 目标表名
    source_tables_list TEXT,                     -- JSON，多表时选中的表列表
    sync_mode          TEXT DEFAULT 'full',      -- full | incremental | realtime
    save_mode          TEXT DEFAULT 'append',    -- append | overwrite | upsert | create_if_not_exists
    column_mapping     TEXT,                     -- JSON 字段映射 [{source,target,type,expr}]
    field_ide          TEXT DEFAULT 'origin',    -- origin | upper | lower | camel | underscore
    incremental_column TEXT,                     -- 增量列
    incremental_value  TEXT,                     -- 增量起始值
    batch_size         INTEGER DEFAULT 1000,
    source_where       TEXT,                     -- where 过滤条件
    error_threshold    INTEGER DEFAULT 0,        -- 0=不容错，N=允许 N 条错误
    realtime_enabled   INTEGER DEFAULT 0,        -- 实时同步开关（预留 Flink CDC）
    flink_config       TEXT,                     -- JSON Flink CDC 配置
    schedule_type      TEXT DEFAULT 'none',
    cron_expr          TEXT,
    interval_minutes   INTEGER,
    enabled            INTEGER DEFAULT 1,
    status             TEXT DEFAULT 'never',
    last_run_at        TEXT,
    last_status        TEXT,
    message            TEXT,
    created_at         TEXT,
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS sync_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_task_id  INTEGER,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,
    rows_synced   INTEGER DEFAULT 0,
    message       TEXT
);

CREATE TABLE IF NOT EXISTS inspection_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    task_name    TEXT,
    db_type      TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,        -- pass | warn | fail
    detail       TEXT,
    triggered_by TEXT
);

CREATE TABLE IF NOT EXISTS system_config (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS storage_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'local',   -- local | minio | s3
    tier            INTEGER DEFAULT 1,                -- 1=本地 2=MinIO热数据 3=S3冷数据
    endpoint        TEXT,                             -- MinIO/S3 的 endpoint URL（本地存储时为路径）
    access_key      TEXT,
    secret_key      TEXT,                             -- 加密存储
    bucket          TEXT,
    region          TEXT,
    prefix          TEXT DEFAULT '',
    enabled         INTEGER DEFAULT 1,
    is_default      INTEGER DEFAULT 0,               -- 同 tier 内的默认目标
    last_error      TEXT,
    last_test_at    TEXT,
    extra_options   TEXT,                             -- JSON: skip_tls, path_style 等
    remark          TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS deployments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    db_type           TEXT NOT NULL,
    host_id           INTEGER,                       -- 纳管主机模式
    direct_host       TEXT,                          -- 直接输入：IP
    direct_port       INTEGER DEFAULT 22,            -- 直接输入：SSH 端口
    direct_user       TEXT DEFAULT 'root',            -- 直接输入：SSH 用户
    direct_password   TEXT,                          -- 直接输入：SSH 密码
    package_path      TEXT,
    base_dir          TEXT,
    data_dir          TEXT,
    port              INTEGER,
    password          TEXT,                          -- 数据库管理员密码
    config_json       TEXT,
    status            TEXT DEFAULT 'pending',
    progress_pct      INTEGER DEFAULT 0,
    log_output        TEXT,
    created_at        TEXT,
    started_at        TEXT,
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS vdb_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    source_record_id INTEGER,
    task_id         INTEGER,
    db_type         TEXT,
    port            INTEGER,
    host            TEXT,
    database_name   TEXT,
    username        TEXT,
    password        TEXT,
    status          TEXT DEFAULT 'creating',
    created_at      TEXT,
    expires_at      TEXT,
    last_used_at    TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS drills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    task_id         INTEGER,
    drill_type      TEXT DEFAULT 'full_recovery',
    scenario        TEXT,
    scheduled_at    TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT DEFAULT 'pending',
    rto_actual_sec  REAL,
    rpo_actual_sec  REAL,
    score           INTEGER,
    issues_found    TEXT,
    notes           TEXT,
    report          TEXT,
    triggered_by    TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS backup_sets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER,                         -- 关联备份任务
    record_id       INTEGER,                         -- 关联备份记录
    set_type        TEXT DEFAULT 'full',             -- full | incremental | synthetic_full
    storage_tier    INTEGER DEFAULT 1,               -- 1=L1本地 2=L2热数据 3=L3冷数据
    object_key      TEXT,                            -- 对象键名 / 本地路径
    parent_set_id   INTEGER,                         -- 增量链 / 合成全量链头
    verified        INTEGER DEFAULT 0,
    size_bytes      INTEGER DEFAULT 0,
    dedup_saved_bytes INTEGER DEFAULT 0,             -- 对象级去重累计节省量
    checksum        TEXT,                            -- 去重哈希(sha256)
    created_at      TEXT
);

-- ① PIT 恢复点日志（Recovery Journal）—— 准 CDP 的核心索引
CREATE TABLE IF NOT EXISTS recovery_journal (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL,                  -- → backup_tasks.id
    record_id      INTEGER,                           -- → backup_records.id（可空，日志段默认不建 record）
    set_id         INTEGER,                           -- → backup_sets.id（上云后回填）
    parent_rp_id   INTEGER,                           -- 增量链父节点；NULL = 链头（base-full）
    rp_kind        TEXT NOT NULL DEFAULT 'file-inc',  -- base-full | file-inc | db-log | db-full
    rp_type        TEXT DEFAULT 'incremental',        -- full | incremental | log-segment
    pit_at         TEXT NOT NULL,                     -- 恢复点时刻 ISO8601（db.now_iso()）
    pit_seq        INTEGER DEFAULT 0,                 -- 同秒内序号，与 pit_at 共同唯一定位
    consistency    TEXT DEFAULT 'crash',              -- crash | fs | app
    binlog_file    TEXT,                              -- DB：起始 binlog 文件名
    binlog_pos     INTEGER,                           -- DB：起始位点
    binlog_end_file TEXT,                             -- DB：结束 binlog 文件名（段末）
    binlog_end_pos INTEGER,
    wal_lsn        TEXT,                              -- PG：起始 LSN
    wal_end_lsn    TEXT,                              -- PG：结束 LSN
    file_set_key   TEXT,                              -- File：源配置指纹
    changed_files  INTEGER DEFAULT 0,
    deleted_files  INTEGER DEFAULT 0,
    storage_tier   INTEGER DEFAULT 1,                 -- 1=本地 2=MinIO 3=S3
    object_key     TEXT NOT NULL,                     -- 本地绝对路径 或 对象键
    bundle_key     TEXT,                              -- 聚合上云后所属 bundle 的 object_key
    size_bytes     INTEGER DEFAULT 0,
    checksum       TEXT,                              -- sha256（db.sha256_file）
    verified       INTEGER DEFAULT 0,
    verify_msg     TEXT,
    is_simulated   INTEGER DEFAULT 0,
    message        TEXT,
    expires_at     TEXT,                              -- 保留策略到期时间
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_rj_task_time ON recovery_journal(task_id, pit_at DESC);
CREATE INDEX IF NOT EXISTS idx_rj_kind      ON recovery_journal(task_id, rp_kind, pit_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rj_obj ON recovery_journal(task_id, object_key);

CREATE TABLE IF NOT EXISTS rt_capture_state (
    task_id            INTEGER PRIMARY KEY,           -- → backup_tasks.id
    capture_kind       TEXT,                          -- db-log | file
    engine             TEXT,                          -- mysql | mariadb | postgresql | file
    daemon_status      TEXT DEFAULT 'stopped',        -- stopped|starting|running|degraded|failed
    degrade_reason     TEXT,
    pid                INTEGER,                       -- DB 守护子进程 pid（file 为 NULL）
    watcher_impl       TEXT,                          -- polling | watchdog（file 专用）
    last_heartbeat_at  TEXT,
    last_capture_at    TEXT,                          -- 最近一次成功捕获
    last_rp_at         TEXT,                          -- 最近一个 journal 恢复点时刻
    last_binlog_file   TEXT,
    last_binlog_pos    INTEGER,
    last_wal_lsn       TEXT,
    source_pos_at      TEXT,                          -- 最近一次探测到的源端位点时刻
    lag_sec            INTEGER DEFAULT 0,             -- 捕获延迟
    rpo_actual_sec     INTEGER DEFAULT 0,             -- now - last_rp_at
    health             TEXT DEFAULT 'unknown',        -- green | yellow | red | unknown
    consecutive_fail   INTEGER DEFAULT 0,
    restart_count      INTEGER DEFAULT 0,
    bytes_today        INTEGER DEFAULT 0,
    rp_count_today     INTEGER DEFAULT 0,
    last_error         TEXT,
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS migration_plans (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                  INTEGER,                 -- 目标备份任务（迁移源端生产库）
    stage                    TEXT DEFAULT 'pre',       -- pre | mid | post
    golden_backup_record_id  INTEGER,                 -- 黄金回退点（pre 阶段的全量备份记录）
    verified                 INTEGER DEFAULT 0,       -- 黄金点恢复校验是否通过
    old_retention_days       INTEGER,                 -- 旧系统备份保留天数（post 阶段设置）
    note                     TEXT,
    status                   TEXT DEFAULT 'created',  -- created | pre | mid | post | done
    created_at               TEXT,
    updated_at               TEXT
);

CREATE TABLE IF NOT EXISTS clone_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_record_id INTEGER,                         -- 克隆源备份记录
    target_env     TEXT NOT NULL,                    -- 目标环境（test / dev / staging ...）
    status         TEXT DEFAULT 'pending',            -- pending|approved|rejected|creating|ready|expired|deleted
    itsm_ticket_id INTEGER,                          -- 关联的 ITSM 工单
    requested_by   TEXT,
    approved_by    TEXT,
    expires_at     TEXT,                             -- 自动销毁到期时间
    vdb_instance_id INTEGER,                         -- 拉起的 VDB 实例 id
    note           TEXT,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS hetero_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    src_db_type    TEXT NOT NULL,                    -- 源库类型（如 oracle）
    dst_db_type    TEXT NOT NULL,                    -- 目标分布式库类型（kingbase/dameng/mysql）
    src_record_id  INTEGER,                          -- 源备份记录
    status         TEXT DEFAULT 'pending',           -- pending | running | done | failed
    result_path    TEXT,                             -- 转换产物路径
    note           TEXT,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS itsm_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    system      TEXT NOT NULL DEFAULT 'internal',    -- internal | dingtalk | servicenow
    ticket_no   TEXT,                                -- 外部系统工单号
    ref_type    TEXT,                                -- clone | migration | drill
    ref_id      INTEGER,                             -- 关联对象 id
    status      TEXT DEFAULT 'open',                 -- open | approved | rejected | closed
    payload     TEXT,                                -- 审批/工单详情（JSON）
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS disaster_links (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    primary_site            TEXT,                              -- 主站点（标识/地址）
    dr_site                 TEXT,                              -- 备站点（标识/地址）
    status                  TEXT DEFAULT 'standby',           -- active | standby | filling | broken
    route_policy            TEXT,                              -- JSON: 多专线配置 [{provider, endpoint, priority, enabled}]
    last_consistency_check  TEXT,                             -- 最近一次一致性校验时间
    consistency_result      TEXT,                             -- pass | warn | fail | NULL
    note                    TEXT,
    created_at              TEXT,
    updated_at              TEXT,
    enabled                 INTEGER DEFAULT 1,
    source_kind             TEXT DEFAULT 'manual',            -- sync_task | rt_task | manual
    source_id               INTEGER                            -- 源任务 id（manual 时为空）
);

CREATE TABLE IF NOT EXISTS alert_predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    metric        TEXT NOT NULL,                        -- backup_fail | storage_full | link_degraded | drill_overdue | rpo_breach
    risk_score    REAL DEFAULT 0,                       -- 0-100
    risk_level    TEXT DEFAULT 'low',                   -- low | medium | high | critical
    predicted_at  TEXT,
    actual_at     TEXT,                                 -- 是否真的发生了
    resolved_at   TEXT,                                 -- 处置完成时间
    details       TEXT,                                 -- JSON: 额外上下文（机器可读原始指标）
    predicted_content TEXT,                             -- 人类可读的预测结论（"预测未来 30 天内 …"）
    basis         TEXT,                                 -- JSON: 人类可读依据因子列表 ["近30天失败率 40%（≥阈值20%）", ...]
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS anonymized_exports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_record_id  INTEGER NOT NULL,                -- 来源备份记录 id
    columns           TEXT,                            -- JSON: 导出的列名列表
    mask_rules        TEXT,                            -- JSON: 各列脱敏规则 {col: rule}
    file_path         TEXT NOT NULL,                   -- 生成的脱敏文件（CSV）路径
    row_count         INTEGER DEFAULT 0,               -- 导出（脱敏后）行数
    note              TEXT,
    created_at        TEXT
);

-- ========== 准 CDP 实时备份（Phase RT）==========

-- ③ 实时备份任务扩展（与 backup_tasks 1:1 关联）
CREATE TABLE IF NOT EXISTS rt_tasks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 INTEGER NOT NULL UNIQUE,          -- → backup_tasks.id
    rt_mode                 TEXT NOT NULL DEFAULT 'file_polling', -- file_polling | db_cdc | mixed
    capture_interval        INTEGER DEFAULT 180,             -- 秒，文件捕获间隔
    db_log_retention_days   INTEGER DEFAULT 7,
    file_inc_retention_days INTEGER DEFAULT 30,
    db_flush_interval       INTEGER DEFAULT 300,             -- 秒，FLUSH BINARY LOGS 间隔
    is_running              INTEGER DEFAULT 0,               -- 守护进程是否在运行
    last_tick_at            TEXT,
    health_status           TEXT DEFAULT 'unknown',          -- healthy | degraded | stopped | unknown
    rpo_current_seconds     INTEGER DEFAULT -1,              -- -1 表示未测量
    disk_quota_gb           INTEGER DEFAULT 200,
    created_at              TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ④ 日志仓库目录管理（每任务一行）
CREATE TABLE IF NOT EXISTS log_repository (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,
    repo_root       TEXT NOT NULL,                          -- 日志仓库根路径
    db_log_dir      TEXT,                                  -- DB 日志子目录
    file_inc_dir    TEXT,                                  -- 文件增量子目录
    current_size_bytes INTEGER DEFAULT 0,
    quota_bytes     INTEGER DEFAULT 214748364800,           -- 200GB
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ========== AI 智能助手会话与消息 ==========
CREATE TABLE IF NOT EXISTS ai_sessions (
    id              TEXT PRIMARY KEY,                       -- UUID
    title           TEXT DEFAULT '新对话',                  -- 首条消息摘要生成
    created_at      TEXT NOT NULL,                          -- ISO 8601
    updated_at      TEXT NOT NULL,
    message_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES ai_sessions(id),
    role            TEXT NOT NULL,                          -- user / assistant / tool / system
    content         TEXT,                                   -- 消息文本
    tool_calls      TEXT,                                   -- JSON: [{name, args}]
    tool_name       TEXT,                                   -- 单次工具名（便于查询）
    tool_result     TEXT,                                   -- JSON: 工具返回结果
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_messages_session
    ON ai_messages(session_id, created_at);
"""

# ------------------------- 连接与执行 -------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.META_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema() -> None:
    with _write_lock:
        conn = get_conn()
        try:
            conn.executescript(SCHEMA)
            # 迁移：确保 backup_mode 列存在（SQLite 不支持 IF NOT EXISTS 的 ALTER TABLE）
            try:
                conn.execute("ALTER TABLE backup_tasks ADD COLUMN backup_mode TEXT DEFAULT 'logical'")
            except Exception:
                pass  # 列已存在，忽略
            # 迁移：ssh_hosts 状态列
            for col, typedef in [("last_status", "TEXT"), ("last_check_at", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE ssh_hosts ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            # 迁移：恢复记录详细日志列
            try:
                conn.execute("ALTER TABLE restore_records ADD COLUMN detail_log TEXT")
            except Exception:
                pass
            # 迁移：backup_records CDC/校验列
            for col, typedef in [("binlog_file", "TEXT"), ("binlog_pos", "INTEGER"),
                                 ("wal_lsn", "TEXT"), ("verified", "INTEGER DEFAULT 0"),
                                 ("verify_msg", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE backup_records ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            # 迁移：storage_tier 列
            try:
                conn.execute("ALTER TABLE backup_records ADD COLUMN storage_tier TEXT DEFAULT 'local'")
            except Exception:
                pass
            # 迁移：压缩率统计列（Phase 压缩增强）
            for col, typedef in [
                ("original_size_bytes", "INTEGER DEFAULT 0"),
                ("compress_algo", "TEXT DEFAULT ''"),
                ("compress_ratio", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_records ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            # 迁移：保护策略关联列（Phase 0）
            for col, typedef in [
                ("policy_id", "INTEGER"),
                ("protection_level", "TEXT"),
                ("adapter_tier", "TEXT"),
                ("rpo_target_min", "INTEGER"),
                ("rto_target_min", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略
            # 迁移：组合调度按天选择 / 限速 / 压缩级别（Phase 2）
            for col, typedef in [
                ("full_schedule_days", "TEXT"),
                ("incremental_schedule_days", "TEXT"),
                ("bandwidth_limit", "INTEGER DEFAULT 0"),
                ("compress_level", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略
            # 迁移：备份集表（Phase 1 —— 合成全量 / 去重 / 生命周期）
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS backup_sets ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, "
                    "record_id INTEGER, set_type TEXT DEFAULT 'full', "
                    "storage_tier INTEGER DEFAULT 1, object_key TEXT, "
                    "parent_set_id INTEGER, verified INTEGER DEFAULT 0, "
                    "size_bytes INTEGER DEFAULT 0, dedup_saved_bytes INTEGER DEFAULT 0, "
                    "checksum TEXT, created_at TEXT, "
                    "chain_id TEXT, chain_status TEXT DEFAULT 'active')")
            except Exception:
                pass
            for col, typedef in [
                ("set_type", "TEXT DEFAULT 'full'"),
                ("storage_tier", "INTEGER DEFAULT 1"),
                ("object_key", "TEXT"),
                ("parent_set_id", "INTEGER"),
                ("verified", "INTEGER DEFAULT 0"),
                ("size_bytes", "INTEGER DEFAULT 0"),
                ("dedup_saved_bytes", "INTEGER DEFAULT 0"),
                ("checksum", "TEXT"),
                ("chain_id", "TEXT"),
                ("chain_status", "TEXT DEFAULT 'active'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_sets ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：全局去重索引（参照鼎甲迪备 §2.4 全局重删）
            # 以内容哈希(block_hash)为键，跨任务、跨备份集复用同一物理块，
            # 命中即记账 dedup_saved_bytes，降低存储与网络占用。
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS dedup_index ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "block_hash TEXT NOT NULL, "          # 内容寻址哈希(sha256)
                    "size_bytes INTEGER DEFAULT 0, "      # 该块原始大小
                    "ref_count INTEGER DEFAULT 1, "       # 被引用次数(跨任务/集)
                    "first_task_id INTEGER, "             # 首次写入的任务
                    "first_set_id INTEGER, "              # 首次写入的备份集
                    "object_key TEXT, "                   # 实际物理块存储路径
                    "created_at TEXT)")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_dedup_block_hash ON dedup_index(block_hash)")
            except Exception:
                pass

            # 迁移：准 CDP 实时备份（Phase RT）—— backup_tasks 追加 6 列
            for col, typedef in [
                ("rt_enabled", "INTEGER DEFAULT 0"),
                ("rt_mode", "TEXT DEFAULT 'auto'"),
                ("rt_interval_sec", "INTEGER DEFAULT 180"),
                ("rt_consistency", "TEXT DEFAULT 'crash'"),
                ("rt_log_retention_days", "INTEGER DEFAULT 7"),
                ("rt_rpo_target_sec", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：Phase RT 准 CDP 实时备份 —— backup_tasks 追加 6 列
            for col, typedef in [
                ("rt_enabled", "INTEGER DEFAULT 0"),
                ("rt_mode", "TEXT DEFAULT 'auto'"),
                ("rt_interval_sec", "INTEGER DEFAULT 180"),
                ("rt_consistency", "TEXT DEFAULT 'crash'"),
                ("rt_log_retention_days", "INTEGER DEFAULT 7"),
                ("rt_rpo_target_sec", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：业务系统字段化（record_display_v2）—— backup_tasks 追加 1 列
            # 允许 NULL、无 DEFAULT、无 UNIQUE、无索引（设计 §3.1 D3）：
            # 存量行为 NULL，由 models.compute_biz_label() 的 R2 规则回退到任务名展示。
            for col, typedef in [
                ("biz_system", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：混合备份（全量+增量组合调度）—— backup_tasks 追加 5 列
            for col, typedef in [
                ("mixed_backup", "INTEGER DEFAULT 0"),
                ("full_schedule_type", "TEXT DEFAULT 'none'"),
                ("full_schedule_expr", "TEXT"),
                ("incremental_schedule_type", "TEXT DEFAULT 'none'"),
                ("incremental_schedule_expr", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE backup_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：recovery_journal 后续新增列的预留位（保持与 SCHEMA 同步）
            for col, typedef in [
                ("bundle_key", "TEXT"),
                ("binlog_end_file", "TEXT"),
                ("binlog_end_pos", "INTEGER"),
                ("wal_end_lsn", "TEXT"),
                ("file_set_key", "TEXT"),
                ("expires_at", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE recovery_journal ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在或表刚建好，忽略

            # 迁移：AI 预测透明化 —— alert_predictions 追加人类可读预测内容与依据
            for col, typedef in [
                ("predicted_content", "TEXT"),
                ("basis", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE alert_predictions ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略

            # 迁移：Phase 4 默认配置（drill_schedule 季度演练排程）
            # 默认关闭，由用户在「容灾演练」页配置开启；next_run 指向下个季度初。
            try:
                _default_drill_schedule = json.dumps({
                    "enabled": False,
                    "frequency": "quarterly",          # quarterly | monthly | weekly
                    "next_run": _default_next_quarter(),
                    "target_task_ids": [],
                    "auto_score": True,
                }, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO system_config(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    ("drill_schedule", _default_drill_schedule))
            except Exception:
                pass  # 配置已存在或写入失败，忽略

            # 迁移：rt_tasks 表新增列（ALTER 兜底）
            for col, typedef in [
                ("rt_mode", "TEXT NOT NULL DEFAULT 'file_polling'"),
                ("capture_interval", "INTEGER DEFAULT 180"),
                ("db_log_retention_days", "INTEGER DEFAULT 7"),
                ("file_inc_retention_days", "INTEGER DEFAULT 30"),
                ("db_flush_interval", "INTEGER DEFAULT 300"),
                ("is_running", "INTEGER DEFAULT 0"),
                ("last_tick_at", "TEXT"),
                ("health_status", "TEXT DEFAULT 'unknown'"),
                ("rpo_current_seconds", "INTEGER DEFAULT -1"),
                ("disk_quota_gb", "INTEGER DEFAULT 200"),
                ("updated_at", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE rt_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在或表刚建好，忽略

            # 迁移：容灾链路数据源（UX-20260801 模块 D）—— disaster_links 追加 2 列
            # source_kind: sync_task | rt_task | manual；source_id: 源任务主键
            for col, typedef in [
                ("source_kind", "TEXT DEFAULT 'manual'"),
                ("source_id", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE disaster_links ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在，忽略
            # 存量行回填：老链路一律视为手工模式，避免前端读到 NULL 分支报错
            try:
                conn.execute(
                    "UPDATE disaster_links SET source_kind='manual' "
                    "WHERE source_kind IS NULL OR source_kind=''")
            except Exception:
                pass  # 表刚建好或无存量数据，忽略

            # 迁移：log_repository 表新增列（ALTER 兜底）
            for col, typedef in [
                ("db_log_dir", "TEXT"),
                ("file_inc_dir", "TEXT"),
                ("current_size_bytes", "INTEGER DEFAULT 0"),
                ("quota_bytes", "INTEGER DEFAULT 214748364800"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE log_repository ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在或表刚建好，忽略

            # 迁移：恢复校验策略与测试报告表
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS restore_verify_policies (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id             INTEGER NOT NULL,
                    name                TEXT,
                    recovery_pool       TEXT DEFAULT '',
                    schedule_type       TEXT DEFAULT 'manual',
                    cron_expr           TEXT,
                    interval_minutes    INTEGER,
                    clone_retention_min INTEGER DEFAULT 30,
                    enabled             INTEGER DEFAULT 1,
                    last_run_at         TEXT,
                    last_status         TEXT,
                    last_report_id      INTEGER,
                    created_at          TEXT,
                    updated_at          TEXT
                );
                CREATE TABLE IF NOT EXISTS restore_test_reports (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy_id    INTEGER,
                    task_id      INTEGER,
                    record_id    INTEGER,
                    db_type      TEXT,
                    status       TEXT,
                    duration_sec REAL,
                    message      TEXT,
                    cleaned      INTEGER DEFAULT 0,
                    created_at   TEXT,
                    finished_at  TEXT
                );
            """)

            # 迁移：deployments 表新增直接输入主机字段
            for col, typedef in [
                ("direct_host", "TEXT"),
                ("direct_port", "INTEGER DEFAULT 22"),
                ("direct_user", "TEXT DEFAULT 'root'"),
                ("direct_password", "TEXT"),
                ("dependency_path", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE deployments ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在或表刚建好，忽略
            for col, typedef in [("name", "TEXT"), ("last_report_id", "INTEGER")]:
                try:
                    conn.execute(f"ALTER TABLE restore_verify_policies ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            for col, typedef in [("finished_at", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE restore_test_reports ADD COLUMN {col} {typedef}")
                except Exception:
                    pass

            # 默认配置：自动合成全量（CDM "系统内自动合成全量"）
            try:
                _seed_system_config(conn, "synthesize_config", {
                    "enabled": True, "min_incremental": 2, "cron": "0 3 * * 0"})
            except Exception:
                pass

            # 迁移：数据同步任务表扩展（Reader/Writer + 字段映射 + Flink CDC 预留）
            for col, typedef in [
                ("src_schema", "TEXT"),
                ("tgt_schema", "TEXT"),
                ("source_table", "TEXT"),
                ("target_table", "TEXT"),
                ("source_tables_list", "TEXT"),
                ("sync_mode", "TEXT DEFAULT 'full'"),
                ("save_mode", "TEXT DEFAULT 'append'"),
                ("column_mapping", "TEXT"),
                ("field_ide", "TEXT DEFAULT 'origin'"),
                ("incremental_column", "TEXT"),
                ("incremental_value", "TEXT"),
                ("batch_size", "INTEGER DEFAULT 1000"),
                ("source_where", "TEXT"),
                ("error_threshold", "INTEGER DEFAULT 0"),
                ("realtime_enabled", "INTEGER DEFAULT 0"),
                ("flink_config", "TEXT"),
                ("full_db_migrate", "INTEGER DEFAULT 0"),
                ("validate_before_run", "INTEGER DEFAULT 0"),
                ("verify_after_run", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE sync_tasks ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 列已存在或表刚建好，忽略

            # 迁移：插件主机状态表（插件服务端化）—— 幂等建表，供存量 DB 补齐
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS plugin_host_state ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "host_id INTEGER, "
                    "host_key TEXT NOT NULL, "
                    "plugin_id TEXT NOT NULL, "
                    "status TEXT DEFAULT 'uninstalled', "
                    "version TEXT, "
                    "method TEXT, "
                    "extract_dir TEXT, "
                    "found_paths TEXT, "
                    "message TEXT, "
                    "installed_at TEXT, "
                    "updated_at TEXT, "
                    "UNIQUE(host_key, plugin_id))")
            except Exception:
                pass  # 表已存在，忽略

            conn.commit()
        finally:
            conn.close()


def execute(sql: str, params: tuple = ()) -> int:
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# ------------------------- 插件主机状态（plugin_host_state） -------------------------
# 插件服务端化：以「主机 × 插件」二维维度持久化安装状态。
# 权威状态落此表；core/plugins/state/*.json 仅承载实时安装进度。
_PLUGIN_STATE_FIELDS = (
    "host_id", "status", "version", "method", "extract_dir",
    "found_paths", "message", "installed_at", "updated_at",
)


def upsert_plugin_host_state(host_key: str, plugin_id: str, fields: dict) -> int:
    """按 (host_key, plugin_id) 幂等 upsert 一条插件主机状态。

    fields 中可含 host_id/status/version/method/extract_dir/found_paths/
    message/installed_at/updated_at；host_key 与 plugin_id 由入参决定。
    返回受影响行的 rowid。
    """
    data = {k: fields.get(k) for k in _PLUGIN_STATE_FIELDS}
    data["host_key"] = host_key
    data["plugin_id"] = plugin_id
    data.setdefault("status", "uninstalled")
    data.setdefault("updated_at", now_iso())
    cols = list(data.keys())
    update_cols = [c for c in cols if c not in ("host_key", "plugin_id")]
    sql = (
        "INSERT INTO plugin_host_state ({cols}) VALUES ({ph}) "
        "ON CONFLICT(host_key, plugin_id) DO UPDATE SET {sets}"
    ).format(
        cols=",".join(cols),
        ph=",".join("?" * len(cols)),
        sets=",".join(f"{c}=excluded.{c}" for c in update_cols),
    )
    return execute(sql, tuple(data.values()))


def get_plugin_host_state(host_key: str, plugin_id: str) -> dict | None:
    """按 (host_key, plugin_id) 查询单条插件主机状态。"""
    return query_one(
        "SELECT * FROM plugin_host_state WHERE host_key=? AND plugin_id=?",
        (host_key, plugin_id),
    )


def list_plugin_host_state(host_key: str | None = None,
                           plugin_id: str | None = None) -> list[dict]:
    """查询插件主机状态，可按 host_key / plugin_id 过滤。"""
    sql = "SELECT * FROM plugin_host_state WHERE 1=1"
    params: list = []
    if host_key:
        sql += " AND host_key=?"
        params.append(host_key)
    if plugin_id:
        sql += " AND plugin_id=?"
        params.append(plugin_id)
    sql += " ORDER BY updated_at DESC"
    return query(sql, tuple(params))


def delete_plugin_host_state(host_key: str, plugin_id: str) -> None:
    """删除指定 (host_key, plugin_id) 的插件主机状态。"""
    execute(
        "DELETE FROM plugin_host_state WHERE host_key=? AND plugin_id=?",
        (host_key, plugin_id),
    )


# ------------------------- 加密（敏感字段） -------------------------
def _derive_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def encrypt_secret(plain: str) -> str:
    """对密码等敏感字段做轻量混淆存储（非高强度加密，生产请用密钥文件/环境变量）。"""
    if not plain:
        return ""
    k = _derive_key(config.SECRET_KEY)
    data = plain.encode("utf-8")
    out = bytes(b ^ k[i % len(k)] for i, b in enumerate(data))
    return "enc:" + base64.b64encode(out).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    if token.startswith("enc:"):
        token = token[4:]
        data = base64.b64decode(token)
        k = _derive_key(config.SECRET_KEY)
        return bytes(b ^ k[i % len(k)] for i, b in enumerate(data)).decode("utf-8", "ignore")
    return token  # 兼容明文


def mask_secret(token: str) -> str:
    if not token:
        return ""
    return "******" if token else ""


# ------------------------- 工具函数 -------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _default_next_quarter() -> str:
    """返回下一个季度第一天的 ISO 时间（用于 drill_schedule.next_run 默认值）。"""
    now = datetime.now()
    q = (now.month - 1) // 3
    next_q_month = (q + 1) * 3 + 1
    year = now.year + (next_q_month - 1) // 12
    month = (next_q_month - 1) % 12 + 1
    return datetime(year, month, 1).isoformat(timespec="seconds")


def human_size(n: int) -> str:
    if n is None:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} EB"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_logger(name: str = "backup") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(LOG_DIR / "platform.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def add_log(level: str, source: str, message: str) -> None:
    try:
        execute(
            "INSERT INTO system_logs(ts, level, source, message) VALUES (?,?,?,?)",
            (now_iso(), level, source, message),
        )
    except Exception:
        pass


# ------------------------- 系统配置（键值） -------------------------
def get_system_config(key: str, default=None):
    row = query_one("SELECT value FROM system_config WHERE key=?", (key,))
    return row["value"] if row else default


def set_system_config(key: str, value) -> None:
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO system_config(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            conn.commit()
        finally:
            conn.close()


def _seed_system_config(conn, key: str, value) -> None:
    """init_schema 阶段写入默认配置（仅当 key 不存在时），不依赖 _write_lock。"""
    import json
    cur = conn.execute("SELECT 1 FROM system_config WHERE key=?", (key,))
    if cur.fetchone():
        return
    conn.execute(
        "INSERT INTO system_config(key, value) VALUES(?, ?)",
        (key, json.dumps(value, ensure_ascii=False)))
