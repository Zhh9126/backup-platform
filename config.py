# -*- coding: utf-8 -*-
"""
全局配置。

优先级：代码默认值 < 环境变量 < config.json（若存在）。
生产环境请通过环境变量或 config.json 覆盖 SECRET_KEY / WEB_PASSWORD 等敏感项，
不要直接把明文提交到版本库。
"""
import os
import sys
import json
from pathlib import Path

# 运行时根目录：
# - 普通源码运行：项目根目录（本文件所在目录）。
# - PyInstaller one-file 冻结：可执行文件所在目录（用于持久化 backups/instance/logs，
#   这些目录不能放在临时解压目录 _MEIPASS，否则每次启动都丢失）。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# ---------- 产品标识 ----------
# 产品正式名称与版本（对外 API 文档、健康检查、报告落款统一取这里，避免各处硬编码）。
PLATFORM_NAME = os.environ.get("AIDBM_NAME", "AIDBM")
PLATFORM_VERSION = os.environ.get("AIDBM_VERSION", "1.4.14")

# ---------- 路径 ----------
BACKUP_ROOT = os.environ.get("BACKUP_ROOT", str(BASE_DIR / "backups"))

# 全实例备份/恢复的临时工作目录（逐库 dump 落盘后再打包）。
# 留空 = 自动选择「产物所在分区下的 .bp_work」，再回退系统临时目录。
# 不再默认用 /tmp：/tmp 常是小分区或 tmpfs，大实例会把它写满，mysqldump
# 只会报 "Got errno 28 on write"（ENOSPC），用户看不出到底哪里满了。
FULL_INSTANCE_WORK_DIR = os.environ.get("BP_WORK_DIR", "")
INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", str(BASE_DIR / "instance")))
META_DB_PATH = os.environ.get("META_DB_PATH", str(INSTANCE_DIR / "meta.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))

# ---------- 元数据库后端（可插拔：sqlite / postgresql / mysql） ----------
# 默认仍为 SQLite（零依赖离线交付）；可在设置页切换为 PostgreSQL/MySQL，
# 切换结果持久化到 instance/meta_backend.json（密码 enc: 加密），重启后自动生效。
META_BACKEND_FILE = INSTANCE_DIR / "meta_backend.json"
META_BACKEND_KINDS = ("sqlite", "postgresql", "mysql")


def load_meta_backend() -> dict:
    """解析元数据库后端配置。优先级：环境变量 > meta_backend.json > 默认 sqlite。"""
    env_backend = os.environ.get("META_DB_BACKEND", "").strip().lower()
    if env_backend in META_BACKEND_KINDS:
        cfg = {"backend": env_backend}
        # META_DB_HOST -> host / META_DB_PORT -> port ...
        for k in ("META_DB_HOST", "META_DB_PORT", "META_DB_USER",
                  "META_DB_PASSWORD", "META_DB_NAME"):
            v = os.environ.get(k)
            if v:
                cfg[k[len("META_DB_"):].lower()] = v
        return cfg
    try:
        data = json.loads(META_BACKEND_FILE.read_text(encoding="utf-8"))
        if data.get("backend") in META_BACKEND_KINDS:
            return data
    except Exception:
        pass
    return {"backend": "sqlite"}


META_BACKEND = load_meta_backend()

# ---------- Web ----------
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
# 内置 Web 服务器是否多线程（默认开启）。
# 单线程模式下批量操作（数百并发的批量建任务/批量触发备份）会排队超时，
# 因此默认启用多线程；生产环境建议仍用 gunicorn -w 4 --threads 8 承载。
WEB_THREADED = os.environ.get("WEB_THREADED", "1") != "0"


def _load_or_create_secret_key() -> str:
    """安全整改：SECRET_KEY 不再使用公开默认值。

    优先级：环境变量 SECRET_KEY > instance/auth_secret.json（自动生成并持久化，
    保证重启后会话不失效）> 兜底随机值（每次启动不同，会话会失效）。
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    _key_file = INSTANCE_DIR / "auth_secret.json"
    try:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        if _key_file.exists():
            _data = json.loads(_key_file.read_text(encoding="utf-8"))
            if _data.get("secret_key"):
                return _data["secret_key"]
        import secrets as _secrets
        _key = _secrets.token_hex(32)
        _key_file.write_text(json.dumps({"secret_key": _key}), encoding="utf-8")
        return _key
    except Exception:
        import secrets as _secrets
        return _secrets.token_hex(32)


SECRET_KEY = _load_or_create_secret_key()
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
# 生产环境务必通过环境变量 WEB_PASSWORD 或 config.json 覆盖默认口令
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "admin123")
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "28800"))  # 秒

# ---------- 物理备份工具路径（只装平台侧，数据库服务器零安装） ----------
# 版本匹配原则：MySQL 5.5-5.7 → xtrabackup 2.4；MySQL 8.0+ → xtrabackup 8.0；
# MariaDB 10.x → mariabackup。备份时按远端服务器版本选用，并可将二进制
# 临时推送到远端 /tmp 执行（结束后清理），远端无需安装任何备份工具。
XTRABACKUP_8_PATH = os.environ.get("XTRABACKUP_8_PATH", "/usr/bin/xtrabackup")
XTRABACKUP_24_PATH = os.environ.get("XTRABACKUP_24_PATH",
                                    "/opt/xtrabackup24/usr/bin/xtrabackup")
MARIABACKUP_PATH = os.environ.get("MARIABACKUP_PATH",
                                  "/opt/mariabackup/usr/bin/mariabackup")

# ---------- 登录安全（暴力破解防护） ----------
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))        # 连续失败次数上限
LOGIN_LOCK_MINUTES = int(os.environ.get("LOGIN_LOCK_MINUTES", "15"))  # 达到上限后锁定分钟数

# ---------- 备份重试 / 大流量传输（10GB 级备份可完成性） ----------
BACKUP_RETRY_MAX = int(os.environ.get("BACKUP_RETRY_MAX", "3"))          # 失败重试次数
BACKUP_RETRY_DELAY = int(os.environ.get("BACKUP_RETRY_DELAY", "5"))      # 兼容旧名
# 重试间隔（秒）：大库备份失败后立刻重试意义不大，默认 60s 让远端/链路恢复
BACKUP_RETRY_INTERVAL = int(os.environ.get("BACKUP_RETRY_INTERVAL", "60"))
# 单次执行总时长上限（秒）：0 = 不限（推荐，大库备份不应被写死的时间砍掉）
BACKUP_CMD_TIMEOUT = int(os.environ.get("BACKUP_CMD_TIMEOUT", "0"))
# 空闲上限（秒）：连续这么久没有任何数据流出才判失败（区分"大表读得慢"与"真卡死"）
BACKUP_IDLE_TIMEOUT = int(os.environ.get("BACKUP_IDLE_TIMEOUT", "1800"))
# 断点续传（远端落盘 + 平台按 offset 增量拉取，中断后不重跑 dump）
BACKUP_RESUME_ENABLED = os.environ.get("BACKUP_RESUME_ENABLED", "true").lower() == "true"
BACKUP_RESUME_TTL = int(os.environ.get("BACKUP_RESUME_TTL", "43200"))    # 断点保留 12h
BACKUP_REMOTE_STAGE = os.environ.get("BACKUP_REMOTE_STAGE", "/tmp/bk_stage")  # 远端暂存目录
# 固定产物（物理备份 tar / expdp .dmp / .bak）没有 rc 标记文件，用「远端文件连续
# 多久不再增长」判定写盘结束；仅在远端备份命令尚未返回时才有实际等待。
BACKUP_STABLE_SECS = int(os.environ.get("BACKUP_STABLE_SECS", "20"))
# 拉回中断后是否在远端保留产物与完成标记：重试时可直接复用（跳过昂贵的备份执行），
# 成功拉回后仍会清理，数据库服务器上不留任何产物。
BACKUP_REMOTE_KEEP_DONE = os.environ.get("BACKUP_REMOTE_KEEP_DONE", "true").lower() == "true"

# ---------- 演示/兜底模式 ----------
# 自 2026-08-14 起不再支持仿真/兜底占位备份；该配置保留为兼容但强制按 off 处理。
DEMO_MODE = "off"

# 克隆服务审批模式：auto = 申请即异步拉起（免审批直通，业界 VDB 标准打法）；
# itsm = 保留 ITSM 审批流（可插拔钉钉 / ServiceNow）
CLONE_AUTO_APPROVE = os.environ.get("CLONE_AUTO_APPROVE", "true").lower() != "false"

# ---------- 调度 ----------
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"

# ---------- 保留策略默认值 ----------
DEFAULT_RETENTION_DAYS = int(os.environ.get("DEFAULT_RETENTION_DAYS", "30"))
DEFAULT_RETENTION_COUNT = int(os.environ.get("DEFAULT_RETENTION_COUNT", "50"))
COMPRESS_BY_DEFAULT = os.environ.get("COMPRESS_BY_DEFAULT", "true").lower() == "true"

# ---------- 支持的数据库类型 ----------
SUPPORTED_DB_TYPES = [
    "mysql", "postgresql", "oracle", "kingbase", "dameng",
    "redis", "mongodb", "neo4j", "vm", "object_storage",
]

# 各类型默认端口（供前端预填）
DEFAULT_PORTS = {
    "mysql": 3306, "mariadb": 3306, "postgresql": 5432, "oracle": 1521, "kingbase": 54321,
    "dameng": 5236, "sqlserver": 1433, "redis": 6379, "mongodb": 27017,
    "neo4j": 7687, "object_storage": 9000,
}

# 各类型显示名
DB_DISPLAY_NAMES = {
    "mysql": "MySQL", "mariadb": "MariaDB", "postgresql": "PostgreSQL",
    "oracle": "Oracle", "kingbase": "KingBase",
    "dameng": "DM 达梦", "sqlserver": "SQL Server",
    "redis": "Redis", "mongodb": "MongoDB", "file": "文件",
    "neo4j": "Neo4j", "vm": "虚拟机", "object_storage": "对象存储",
}

# 备份方式（backup_type）中文映射：full / incremental / differential
BACKUP_TYPE_DISPLAY_NAMES = {
    "full": "全量", "incremental": "增量", "differential": "差异",
    "mixed": "组合",
}

# 备份模式（backup_mode）中文映射：logical / physical
BACKUP_MODE_DISPLAY_NAMES = {
    "logical": "逻辑备份",
    "physical": "物理备份",
    "custom": "自定义脚本",
}

# 备份状态（status）中文映射：仪表盘/记录页统一展示
BACKUP_STATUS_DISPLAY_NAMES = {
    "success": "成功",
    "failed": "失败",
    "simulated": "仿真",
    "running": "运行中",
    "never": "未运行",
}

# ---------- 通知默认（任务级可覆盖） ----------
NOTIFY_DEFAULTS = {
    "enabled": os.environ.get("NOTIFY_ENABLED", "false").lower() == "true",
    "on_success": os.environ.get("NOTIFY_ON_SUCCESS", "false").lower() == "true",
    "on_failure": os.environ.get("NOTIFY_ON_FAILURE", "true").lower() == "true",
    "channels": [],  # [{"type":"webhook|dingtalk|wechat|feishu|email", ...}]
}

# ---------- 远程存储默认 ----------
REMOTE_DEFAULTS = {
    "type": "local",           # local | sftp
    "sftp_host": "", "sftp_port": 22,
    "sftp_user": "", "sftp_password": "", "sftp_key": "",
    "sftp_path": "/backups",
}

# ---------- 准 CDP 实时备份（Phase RT） ----------
# 总开关：关闭后 RtSupervisor 不抢锁、不起 worker，平台其余功能不受影响
RT_BACKUP_ENABLED = os.environ.get("RT_BACKUP_ENABLED", "true").lower() == "true"
# 数据库日志段仓库根目录（binlog / WAL 段落盘位置）
RT_LOG_ROOT = os.environ.get("RT_LOG_ROOT", str(Path(BACKUP_ROOT) / "rt_logs"))
# 文件准 CDP 增量归档根目录
RT_FILE_ROOT = os.environ.get("RT_FILE_ROOT", str(Path(BACKUP_ROOT) / "rt_files"))
# Supervisor 主循环 tick 间隔（秒）
RT_SUPERVISOR_TICK_SEC = int(os.environ.get("RT_SUPERVISOR_TICK_SEC", "10"))
# 单实例锁文件（多 worker 部署下保证只有一个进程跑守护）
RT_LOCK_FILE = str(INSTANCE_DIR / "rt_supervisor.lock")
# 锁心跳判定为陈旧的阈值（秒），超过则允许其他进程接管
RT_LOCK_STALE_SEC = int(os.environ.get("RT_LOCK_STALE_SEC", "60"))

# 文件近实时
RT_FILE_WATCHER = os.environ.get("RT_FILE_WATCHER", "auto")            # auto|polling|watchdog
RT_FILE_INTERVAL_SEC = int(os.environ.get("RT_FILE_INTERVAL_SEC", "180"))     # 强制 flush 上限
RT_FILE_DEBOUNCE_SEC = int(os.environ.get("RT_FILE_DEBOUNCE_SEC", "5"))       # 事件去抖
RT_FILE_RPO_TARGET_SEC = int(os.environ.get("RT_FILE_RPO_TARGET_SEC", "300"))  # 5 分钟
RT_FILE_RETENTION_DAYS = int(os.environ.get("RT_FILE_RETENTION_DAYS", "30"))

# 数据库日志流
RT_DB_MODE = os.environ.get("RT_DB_MODE", "auto")   # auto|stream|archive_poll|sample
RT_DB_SEAL_INTERVAL_SEC = int(os.environ.get("RT_DB_SEAL_INTERVAL_SEC", "300"))
RT_DB_RPO_TARGET_SEC = int(os.environ.get("RT_DB_RPO_TARGET_SEC", "30"))
RT_DB_LOG_RETENTION_DAYS = int(os.environ.get("RT_DB_LOG_RETENTION_DAYS", "7"))
RT_DB_STALL_TICKS = int(os.environ.get("RT_DB_STALL_TICKS", "6"))        # 停滞判定 tick 数
# 恢复性能：逻辑备份表级并行导入路数（>1 启用），物理备份 --parallel 线程数
RESTORE_PARALLEL = int(os.environ.get("RESTORE_PARALLEL", "4"))
# 实时保护 RPO 超限告警的最小间隔（秒，防止告警刷屏）
RT_RPO_ALERT_MIN_SEC = int(os.environ.get("RT_RPO_ALERT_MIN_SEC", "300"))
# 是否允许对源库执行 FLUSH BINARY LOGS 强制轮转（A7：默认开启）
RT_DB_FLUSH_LOGS = os.environ.get("RT_DB_FLUSH_LOGS", "true").lower() == "true"
# PG 是否创建物理复制槽（A6：默认开启，保证不丢 WAL，但源库有堆积风险）
RT_PG_CREATE_SLOT = os.environ.get("RT_PG_CREATE_SLOT", "true").lower() == "true"
# 真实日志捕获启动失败时，是否允许降级为仿真日志流。
# 默认 **关闭**：实时保护链路上绝不产出仿真数据（仿真恢复点不能作为 RPO/RTO 依据）。
# 仅当任务显式标记 demo_only、DEMO_MODE=on 或 rt_mode=sample 时才允许仿真；
# 需要演练/演示场景时设置 RT_ALLOW_SIMULATED_FALLBACK=true 显式开启。
RT_ALLOW_SIMULATED_FALLBACK = (
    os.environ.get("RT_ALLOW_SIMULATED_FALLBACK", "false").lower() == "true")

# 上云聚合（缓解对象存储写放大）
RT_UPLOAD_BATCH_MB = int(os.environ.get("RT_UPLOAD_BATCH_MB", "64"))
RT_UPLOAD_INTERVAL_MIN = int(os.environ.get("RT_UPLOAD_INTERVAL_MIN", "15"))

# ========== AI 智能助手（自然语言 Agent）执行策略 ==========
# auto（默认）：按风险分级授权——备份、快速巡检等低风险操作由用户自然语言指令直接执行，
#              执行后回传真实结果；全量巡检、恢复/删除类高风险操作仍需用户二次确认。
# always_confirm：所有执行类操作都必须人工确认（严格审批环境的保守模式）。
# auto_execute：不做任何确认拦截（仅供自动化测试，生产环境不建议）。
AI_AGENT_EXEC_MODE = os.environ.get("AI_AGENT_EXEC_MODE", "auto").strip().lower()

# 容错
RT_MAX_RESTART = int(os.environ.get("RT_MAX_RESTART", "5"))
RT_RESTART_BACKOFF_SEC = [5, 15, 60, 180, 600]
RT_DISK_QUOTA_GB = int(os.environ.get("RT_DISK_QUOTA_GB", "200"))       # 日志仓库配额，超限告警
# 同任务同类告警抑制窗口（分钟，A5）
RT_ALERT_SUPPRESS_MIN = int(os.environ.get("RT_ALERT_SUPPRESS_MIN", "15"))

# 若项目根存在 config.json，则用其覆盖上述顶层变量（零依赖、可选）
_CFG_FILE = BASE_DIR / "config.json"
_CONFIG_FILE_KEYS = set()
if _CFG_FILE.exists():
    try:
        _overrides = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
        for _k, _v in _overrides.items():
            if _k in globals():
                globals()[_k] = _v
                _CONFIG_FILE_KEYS.add(_k)
    except Exception:
        pass


# ==================== 本地备份存储位置（L1 落点） ====================
# 背景：未部署 MinIO/S3 时，本地目录是备份的唯一落点。该目录必须
# 1) 对用户可见（存储管理页明确展示实际路径）；
# 2) 可配置（界面配置 -> system_config.backup_root，重启后仍生效）。
# 优先级：界面配置 > 环境变量 BACKUP_ROOT / config.json > 程序目录下 backups（兜底）。
# 注意：默认兜底位于程序安装目录（打包路径）内，容器/升级场景易丢数据，
# 因此存储页会给出持久化风险提示。
BACKUP_ROOT_ENV = os.environ.get("BACKUP_ROOT") or ""
BACKUP_ROOT_DEFAULT = str(BASE_DIR / "backups")
BACKUP_ROOT_SETTING_KEY = "backup_root"
_BACKUP_ROOT_ORIGIN = ("env" if (BACKUP_ROOT_ENV or "BACKUP_ROOT" in _CONFIG_FILE_KEYS)
                       else "default")
_BACKUP_ROOT_ORIGIN_LABELS = {
    "ui": "界面配置",
    "env": "环境变量 BACKUP_ROOT / config.json",
    "default": "默认（程序安装目录，未配置）",
}
# 派生目录是否被显式指定（显式指定时不随根目录变化）
_DERIVED_ENV_OVERRIDE = {
    "RT_LOG_ROOT": bool(os.environ.get("RT_LOG_ROOT")) or "RT_LOG_ROOT" in _CONFIG_FILE_KEYS,
    "RT_FILE_ROOT": bool(os.environ.get("RT_FILE_ROOT")) or "RT_FILE_ROOT" in _CONFIG_FILE_KEYS,
}
# /proc/mounts 中不作为"持久化卷"判定的伪文件系统
_MOUNT_PSEUDO_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay",
    "squashfs", "mqueue", "securityfs", "debugfs", "tracefs", "pstore", "configfs",
    "fusectl", "bpf", "autofs", "binfmt_misc", "nsfs", "ramfs", "hugetlbfs",
    "rpc_pipefs", "fuse.portal", "selinuxfs", "efivarfs", "none", "mtd_inodefs",
    "nfsd", "fuse.gvfsd-fuse",
}


def get_backup_root() -> str:
    """当前生效的本地备份根目录（运行时可能被界面配置修改，勿在导入期缓存）。"""
    return str(BACKUP_ROOT)


def backup_root_source() -> str:
    """当前生效来源：ui | env | default。"""
    return _BACKUP_ROOT_ORIGIN


def backup_root_source_label() -> str:
    return _BACKUP_ROOT_ORIGIN_LABELS.get(_BACKUP_ROOT_ORIGIN, _BACKUP_ROOT_ORIGIN)


def _refresh_derived_paths() -> None:
    """根目录变化后重算派生目录（仅重算未被显式指定的）。"""
    global RT_LOG_ROOT, RT_FILE_ROOT
    if not _DERIVED_ENV_OVERRIDE["RT_LOG_ROOT"]:
        RT_LOG_ROOT = str(Path(BACKUP_ROOT) / "rt_logs")
    if not _DERIVED_ENV_OVERRIDE["RT_FILE_ROOT"]:
        RT_FILE_ROOT = str(Path(BACKUP_ROOT) / "rt_files")


def set_backup_root(path: str, origin: str = "ui") -> str:
    """运行时切换本地备份根目录（调用方负责创建目录与持久化）。"""
    global BACKUP_ROOT, _BACKUP_ROOT_ORIGIN
    p = os.path.abspath(os.path.expanduser(str(path).strip()))
    BACKUP_ROOT = p
    _BACKUP_ROOT_ORIGIN = origin
    _refresh_derived_paths()
    return p


def load_backup_root_from_db() -> str:
    """启动时应用界面持久化的本地备份目录（system_config.backup_root）。

    元数据库不可用或未配置时保持现有值（环境变量/默认），异常静默降级，
    不影响平台启动。
    """
    try:
        import core.db as _db
        row = _db.query_one(f"SELECT value FROM system_config WHERE {_db.qcol('key')}=?",
                            (BACKUP_ROOT_SETTING_KEY,))
        p = (row["value"] if row and row["value"] else "").strip()
        if p:
            return set_backup_root(p, origin="ui")
    except Exception:
        pass
    return str(BACKUP_ROOT)


def _is_under(child: str, parent: str) -> bool:
    """child 是否就是 parent 或在 parent 之下。

    通过 inode 逐级向上比对，兼容 bind mount / 符号链接导致的路径不一致
    （例如程序目录被 bind mount 后 realpath 与实际路径写法不同）。
    """
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    if child == parent or child.startswith(parent.rstrip(os.sep) + os.sep):
        return True
    try:
        pst = os.stat(parent)
    except OSError:
        return False
    cur = child
    while True:
        try:
            st = os.stat(cur)
            if st.st_dev == pst.st_dev and st.st_ino == pst.st_ino:
                return True
        except OSError:
            return False
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return False
        cur = nxt


def _is_mounted(path: str) -> bool:
    """path 是否位于 /proc/mounts 中的独立挂载点之下（容器挂载卷/独立分区）。"""
    root = os.path.realpath(path)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fstype = parts[1], parts[2]
                if fstype in _MOUNT_PSEUDO_FS:
                    continue
                mp = mp.rstrip("/") or "/"
                if mp == "/":
                    continue
                if root == mp or root.startswith(mp + "/"):
                    return True
    except Exception:
        pass
    return False


def backup_root_persistence() -> dict:
    """判断当前备份目录是否真正持久化（非程序目录 / 独立挂载点）。

    返回 {"persistent": True|False|None, "level": "ok|warn|info", "message": str}。
    启发式判定：① 程序安装目录内的非 data/ 路径 → 告警（升级/重建容器会丢）；
    ② 位于独立挂载点 → 正常；③ 容器内未挂载 → 告警；④ 其余 → 提示确认。
    """
    root = str(BACKUP_ROOT)
    under_program = _is_under(root, str(BASE_DIR))
    # 约定：程序目录下的 data/ 子目录为持久化数据目录（安装器会创建，不随升级清理）
    under_program_data = _is_under(root, os.path.join(str(BASE_DIR), "data"))
    if under_program and not under_program_data:
        return {"persistent": False, "level": "warn",
                "message": ("当前备份目录位于程序安装目录内（默认打包路径）："
                            "重新部署或重建容器会丢失备份，请修改为独立数据目录（如 /data/backups）。")}
    if _is_mounted(root):
        return {"persistent": True, "level": "ok",
                "message": "该目录位于独立挂载点，升级或重建容器后备份数据仍保留。"}
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return {"persistent": False, "level": "warn",
                "message": ("容器未把该目录挂载为持久化卷：重建容器会丢失备份。"
                            "请以 -v 宿主机目录:<该目录> 启动容器，或改用已挂载路径（如 /data/backups）。")}
    return {"persistent": None, "level": "info",
            "message": "无法确认该目录是否为独立挂载点，请确保它不会随程序升级被清理。"}


def backup_root_info() -> dict:
    """本地备份目录完整信息（供接口/日志使用）。"""
    return {
        "path": str(BACKUP_ROOT),
        "real_path": os.path.realpath(str(BACKUP_ROOT)),
        "source": backup_root_source(),
        "source_label": backup_root_source_label(),
        "env_path": BACKUP_ROOT_ENV,
        "default_path": BACKUP_ROOT_DEFAULT,
        "layout": "<该路径>/<数据库类型>/<任务ID_任务名>/",
        "persistence": backup_root_persistence(),
    }
