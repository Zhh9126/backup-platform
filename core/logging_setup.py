# -*- coding: utf-8 -*-
"""统一日志基础设施（源码运行 / PyInstaller 可执行文件 / Docker 容器通用）。

设计目标：**任何一次失败都能在事后被定位**，不要求用户能复现。

关键能力
--------
1. **日志目录可写性兜底**
   配置的日志目录不可写时（只读挂载、EXE 放在只读目录、容器未挂载卷、
   普通用户无权限），自动逐级降级到可写位置，并把**最终位置**打印到
   控制台与日志首行。避免"日志根本没写下来，无从排查"。
2. **轮转**
   platform.log 按大小轮转（默认 20MB × 10 份）；error.log 只收 ERROR 及以上，
   长期运行（容器/服务）不会撑爆磁盘，同时错误永远完整保留。
3. **崩溃兜底**
   主线程未捕获异常、子线程未捕获异常、解释器段错误（faulthandler）、
   进程收到 SIGTERM/SIGINT，全部先落盘再退出/关闭。
4. **脱敏**
   命令、环境变量、异常堆栈中的口令 / 令牌 / 连接串凭据一律打码，
   日志可安全外发（工单、邮件、诊断包）。
5. **启动横幅**
   版本、运行形态（源码 / EXE / 容器）、关键目录、PID、主机名，
   让用户第一眼就知道"日志在哪、备份在哪"。

本模块只依赖 config，**不得 import core.db**（db.get_logger 反向依赖本模块，
避免循环导入）。
"""
import os
import re
import sys
import time
import signal
import logging
import logging.handlers
import platform as _platform
import tempfile
import threading
import traceback
from pathlib import Path

import config

# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
_MASK = "***"

# 出现这些词的 KEY= 值一律打码（大小写不敏感）
_SECRET_KEYWORDS = (
    "password", "passwd", "pwd", "secret", "token", "credential",
    "private_key", "access_key", "api_key", "apikey", "auth",
)

# 精确打码的环境变量名（引擎会注入这些传密码）
_SECRET_ENV_EXACT = {
    "MYSQL_PWD", "PGPASSWORD", "KINGBASE_PASSWORD", "DB_BACKUP_PASSWORD",
    "SQLCMDPASSWORD", "PLATFORM_DB_PASSWORD", "ORACLE_PWD", "DM_PASSWORD",
    "REDIS_PASSWORD", "MONGO_PASSWORD", "SECRET_KEY", "WEB_PASSWORD",
    "API_TOKEN", "SMTP_PASSWORD", "MAIL_PASSWORD",
}

_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^@\s/]+)@")
# export KEY='v' / KEY="v" / KEY=v（含 set KEY=v 的 Windows 形式）
_ENV_ASSIGN_RE = re.compile(
    r"(?i)\b((?:export\s+|set\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s;|&]+)")
# --password=v / --password v / -p v
_LONG_OPT_RE = re.compile(
    r"(?i)(--[a-z0-9\-_]*(?:password|passwd|pwd|secret|token|key)[a-z0-9\-_]*)"
    r"(\s*=\s*|\s+)(\"[^\"]*\"|'[^']*'|[^\s;|&]+)")
# JSON: "password": "xxx"
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:[a-z0-9_\-]*(?:password|passwd|pwd|secret|token|api_key)[a-z0-9_\-]*)"\s*:\s*)"[^"]*"')
# mysql 风格短选项明文密码：-pXXXX（前有空白，且不是 --xxx）
_SHORT_PWD_RE = re.compile(r"(^|[\s\"'])-p([^\s\-'\"][^\s'\"|;&]*)")


def mask_secrets(value) -> str:
    """把文本中的口令 / 令牌 / 连接串凭据替换为 ***。

    幂等：已打码的文本再次处理不会变化。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif not isinstance(value, str):
        value = str(value)
    s = value
    # ① URL 内嵌凭据：scheme://user:pass@host → scheme://user:***@host
    s = _URL_CRED_RE.sub(r"\1:%s@" % _MASK, s)
    # ② KEY=value（环境变量导出、配置文件片段）
    def _env_repl(m):
        prefix, key, _val = m.group(1), m.group(2), m.group(3)
        if key.upper() in _SECRET_ENV_EXACT or any(
                w in key.lower() for w in _SECRET_KEYWORDS):
            return f"{prefix}{key}={_MASK}"
        return m.group(0)
    s = _ENV_ASSIGN_RE.sub(_env_repl, s)
    # ③ 长选项 --password=xxx / --password xxx
    s = _LONG_OPT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", s)
    # ④ JSON 字段
    s = _JSON_SECRET_RE.sub(lambda m: f'{m.group(1)}"{_MASK}"', s)
    # ⑤ mysql 风格 -pXXXX（保守：仅打码，不影响 -P 端口大写）
    s = _SHORT_PWD_RE.sub(lambda m: f"{m.group(1)}-p{_MASK}", s)
    return s


def mask_env(env: dict) -> dict:
    """返回脱敏后的环境变量副本（用于日志展示）。"""
    if not env:
        return {}
    out = {}
    for k, v in env.items():
        ku = str(k).upper()
        if ku in _SECRET_ENV_EXACT or any(w in ku.lower() for w in _SECRET_KEYWORDS):
            out[k] = _MASK
        else:
            out[k] = v
    return out


def mask_command(cmd) -> str:
    """把命令（list 或 str）格式化为一行脱敏文本。"""
    try:
        if isinstance(cmd, (list, tuple)):
            text = " ".join(str(c) for c in cmd)
        else:
            text = str(cmd)
    except Exception:
        text = repr(cmd)
    return mask_secrets(text)


# --------------------------------------------------------------------------- #
# 日志目录解析（可写性兜底）
# --------------------------------------------------------------------------- #
def _is_writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".aidbm_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_log_dir() -> tuple:
    """解析可用日志目录，返回 (Path, 来源说明)。

    降级顺序：环境变量 LOG_DIR → config.LOG_DIR（程序目录） →
    用户目录 ~/.aidbm/logs → 系统临时目录（仅兜底，重启可能丢失）。
    """
    candidates = []
    env_dir = os.environ.get("LOG_DIR")
    if env_dir:
        candidates.append((Path(env_dir), "环境变量 LOG_DIR"))
    try:
        candidates.append((Path(config.LOG_DIR), "配置 LOG_DIR"))
    except Exception:
        pass
    try:
        candidates.append((Path(config.BASE_DIR) / "logs", "程序安装目录 logs"))
    except Exception:
        pass
    try:
        candidates.append((Path.home() / ".aidbm" / "logs", "用户目录 ~/.aidbm/logs"))
    except Exception:
        pass
    try:
        candidates.append((Path(tempfile.gettempdir()) / "aidbm-logs",
                           "系统临时目录（重启可能丢失，请配置持久化目录）"))
    except Exception:
        pass

    seen = set()
    for path, label in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if _is_writable(path):
            return path, label
    # 理论上不会走到这里（临时目录通常可写）
    fallback = Path(tempfile.gettempdir()) / "aidbm-logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, "系统临时目录（兜底）"


def runtime_form() -> str:
    """运行形态描述：源码 / 单文件可执行程序 / Docker 容器。"""
    parts = []
    if getattr(sys, "frozen", False):
        parts.append("可执行文件(PyInstaller)")
    else:
        parts.append("源码运行")
    try:
        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            parts.append("Docker 容器")
    except Exception:
        pass
    return " + ".join(parts)


# --------------------------------------------------------------------------- #
# 脱敏 Formatter / Handler
# --------------------------------------------------------------------------- #
class MaskingFormatter(logging.Formatter):
    """对最终输出行整体脱敏——无论 handler 写文件还是写 stdout。"""

    def format(self, record):
        try:
            return mask_secrets(super().format(record))
        except Exception:
            return super().format(record)


def _rotating(path: Path, level: int, max_bytes: int, backup_count: int,
              fmt: logging.Formatter) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(fmt)
    return h


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
_INIT_LOCK = threading.Lock()
_INITIALIZED = False
LOG_DIR = None          # type: Path | None
LOG_DIR_SOURCE = ""


def init_logging(app_name: str = "AIDBM", console: bool = True,
                 force: bool = False):
    """初始化全局日志（幂等）。返回实际使用的日志目录 Path。

    必须在创建 Flask app / 启动调度器之前调用一次。
    """
    global _INITIALIZED, LOG_DIR, LOG_DIR_SOURCE
    with _INIT_LOCK:
        if _INITIALIZED and not force:
            return LOG_DIR
        log_dir, source = resolve_log_dir()
        LOG_DIR, LOG_DIR_SOURCE = log_dir, source

        level_name = str(os.environ.get("LOG_LEVEL", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)
        max_bytes = int(getattr(config, "LOG_MAX_BYTES", 20 * 1024 * 1024) or 20 * 1024 * 1024)
        backup_count = int(getattr(config, "LOG_BACKUP_COUNT", 10) or 10)

        root = logging.getLogger()
        # 清理历史 handler（重复初始化 / 测试环境切换目录时避免重复输出）
        for h in list(root.handlers):
            try:
                root.removeHandler(h)
                h.close()
            except Exception:
                pass
        root.setLevel(level)

        fmt = MaskingFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")

        try:
            root.addHandler(_rotating(log_dir / "platform.log", level,
                                      max_bytes, backup_count, fmt))
        except Exception as e:
            print(f"[日志] 无法写入 {log_dir}/platform.log: {e}", file=sys.stderr)
        try:
            # 错误日志独立留存：排查时先看这一份即可
            root.addHandler(_rotating(log_dir / "error.log", logging.ERROR,
                                      max_bytes, backup_count, fmt))
        except Exception:
            pass
        if console:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setLevel(level)
            sh.setFormatter(fmt)
            root.addHandler(sh)

        _install_crash_handlers(log_dir)
        _INITIALIZED = True
        _print_banner(app_name, log_dir, source, max_bytes, backup_count)
        return log_dir


def get_logger(name: str = "aidbm") -> logging.Logger:
    """获取 logger（自动确保全局初始化）。"""
    init_logging()
    return logging.getLogger(name)


def log_locations() -> dict:
    """返回日志相关路径信息，供 API / 前端 / 诊断包展示。"""
    log_dir = LOG_DIR or resolve_log_dir()[0]
    return {
        "log_dir": str(log_dir),
        "log_dir_source": LOG_DIR_SOURCE or "未初始化",
        "platform_log": str(Path(log_dir) / "platform.log"),
        "error_log": str(Path(log_dir) / "error.log"),
        "crash_log": str(Path(log_dir) / "crash.log"),
        "operations_dir": str(Path(log_dir) / "operations"),
        "runtime_form": runtime_form(),
        "writable": _is_writable(Path(log_dir)),
    }


# --------------------------------------------------------------------------- #
# 崩溃兜底
# --------------------------------------------------------------------------- #
def _install_crash_handlers(log_dir: Path) -> None:
    """未捕获异常 / 子线程异常 / 段错误 / 终止信号 → 落盘。"""
    logger = logging.getLogger("aidbm.crash")

    # ① 段错误等硬崩溃：faulthandler 直接写文件（不经 logging，最可靠）
    try:
        import faulthandler
        crash_file = open(log_dir / "crash.log", "a", encoding="utf-8", buffering=1)
        faulthandler.enable(crash_file)
        globals()["_FAULT_HANDLER_FILE"] = crash_file  # 持有引用，防被 GC 关闭
    except Exception:
        pass

    # ② 主线程未捕获异常
    _prev_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        try:
            logger.critical("主线程未捕获异常：\n%s",
                            "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        _prev_hook(exc_type, exc, tb)
    sys.excepthook = _excepthook

    # ③ 子线程未捕获异常（后台调度/实时备份线程崩溃的关键线索）
    try:
        _prev_thread_hook = threading.excepthook

        def _thread_excepthook(args):
            try:
                logger.critical("子线程 %s 未捕获异常：\n%s",
                                getattr(args.thread, "name", "?"),
                                "".join(traceback.format_exception(
                                    args.exc_type, args.exc_value, args.exc_traceback)))
            except Exception:
                pass
            try:
                _prev_thread_hook(args)
            except Exception:
                pass
        threading.excepthook = _thread_excepthook
    except Exception:
        pass

    # ④ 终止信号：记录退出原因与调用栈，便于区分"被系统杀掉"与"自己崩了"
    def _sig_handler(signum, frame):
        try:
            logger.warning("收到信号 %s，进程即将退出。调用栈：\n%s", signum,
                           "".join(traceback.format_stack(frame)))
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
        except Exception:
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)   # 还原默认行为（保留正确退出码）

    for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        try:
            s = getattr(signal, sig, None)
            if s is not None:
                signal.signal(s, _sig_handler)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 启动横幅
# --------------------------------------------------------------------------- #
def _print_banner(app_name: str, log_dir: Path, source: str,
                  max_bytes: int, backup_count: int) -> None:
    """把"日志/数据在哪"明确打印到控制台——用户排查问题的第一入口。"""
    version = ""
    try:
        version = str(getattr(config, "APP_VERSION", "") or "")
    except Exception:
        pass
    if not version:
        try:
            version = str(json_load_version())
        except Exception:
            version = ""
    lines = [
        "=" * 74,
        f"{app_name} 启动"
        + (f"  |  版本 {version}" if version else "")
        + f"  |  {runtime_form()}",
        f"Python {sys.version.split()[0]}  |  主机 {_platform.node()}  |  PID {os.getpid()}",
        f"日志目录 : {log_dir}   （来源：{source}）",
        f"错误日志 : {log_dir / 'error.log'}",
        f"崩溃日志 : {log_dir / 'crash.log'}",
        f"日志轮转 : 单文件 {max_bytes // (1024 * 1024)}MB × {backup_count} 份",
    ]
    try:
        lines.append(f"备份目录 : {config.get_backup_root()}")
        lines.append(f"元数据库 : {config.META_DB_PATH}")
    except Exception:
        pass
    lines.append("=" * 74)
    text = "\n".join(lines)
    # 直接写 stdout（容器 docker logs / 可执行文件控制台一定能看到），
    # 同时进日志文件，保证事后可查。
    print(text, flush=True)
    try:
        logging.getLogger("aidbm").info("\n%s", text)
    except Exception:
        pass


def json_load_version() -> str:
    """从 config.json 读版本号（可选）。"""
    import json
    try:
        f = Path(config.BASE_DIR) / "config.json"
        if f.exists():
            return str(json.loads(f.read_text(encoding="utf-8")).get("APP_VERSION") or "")
    except Exception:
        pass
    return ""
