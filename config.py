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

# ---------- 路径 ----------
BACKUP_ROOT = os.environ.get("BACKUP_ROOT", str(BASE_DIR / "backups"))
INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", str(BASE_DIR / "instance")))
META_DB_PATH = os.environ.get("META_DB_PATH", str(INSTANCE_DIR / "meta.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))

# ---------- Web ----------
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))


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

# ---------- 备份重试 ----------
BACKUP_RETRY_MAX = int(os.environ.get("BACKUP_RETRY_MAX", "3"))
BACKUP_RETRY_DELAY = int(os.environ.get("BACKUP_RETRY_DELAY", "5"))

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
    "redis", "mongodb",
]

# 各类型默认端口（供前端预填）
DEFAULT_PORTS = {
    "mysql": 3306, "mariadb": 3306, "postgresql": 5432, "oracle": 1521, "kingbase": 54321,
    "dameng": 5236, "sqlserver": 1433, "redis": 6379, "mongodb": 27017,
}

# 各类型显示名
DB_DISPLAY_NAMES = {
    "mysql": "MySQL", "mariadb": "MariaDB", "postgresql": "PostgreSQL",
    "oracle": "Oracle", "kingbase": "KingBase",
    "dameng": "DM 达梦", "sqlserver": "SQL Server",
    "redis": "Redis", "mongodb": "MongoDB", "file": "文件",
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

# 上云聚合（缓解对象存储写放大）
RT_UPLOAD_BATCH_MB = int(os.environ.get("RT_UPLOAD_BATCH_MB", "64"))
RT_UPLOAD_INTERVAL_MIN = int(os.environ.get("RT_UPLOAD_INTERVAL_MIN", "15"))

# 容错
RT_MAX_RESTART = int(os.environ.get("RT_MAX_RESTART", "5"))
RT_RESTART_BACKOFF_SEC = [5, 15, 60, 180, 600]
RT_DISK_QUOTA_GB = int(os.environ.get("RT_DISK_QUOTA_GB", "200"))       # 日志仓库配额，超限告警
# 同任务同类告警抑制窗口（分钟，A5）
RT_ALERT_SUPPRESS_MIN = int(os.environ.get("RT_ALERT_SUPPRESS_MIN", "15"))

# 若项目根存在 config.json，则用其覆盖上述顶层变量（零依赖、可选）
_CFG_FILE = BASE_DIR / "config.json"
if _CFG_FILE.exists():
    try:
        _overrides = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
        for _k, _v in _overrides.items():
            if _k in globals():
                globals()[_k] = _v
    except Exception:
        pass
