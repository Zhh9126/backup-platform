# -*- coding: utf-8 -*-
"""任务级操作日志（Operation Log）—— 一次备份/恢复/校验 = 一份完整日志文件。

为什么需要它
------------
系统日志（system_logs 表 + platform.log）是全平台混流的，当某次备份失败时，
用户真正需要的是**这一次操作**的完整现场：

  · 前置检查做了什么、结论是什么
  · 实际执行的命令（脱敏后）与注入的关键环境变量
  · 远端/本机命令的退出码、stdout 全文、stderr 全文
  · 每个阶段的耗时，卡在哪一步
  · 异常堆栈、最终结论

本模块把上述内容按操作维度落到独立文件：
    <LOG_DIR>/operations/<YYYYMMDD>/<kind>_t<task_id>_<时间戳>_r<record_id>.log

设计要点
--------
- **绝不影响主流程**：任何写日志失败都被吞掉并降级到 logging，日志系统自身
  不能成为新的故障点。
- **零安装可排查**：日志是纯文本，可执行文件 / 容器 / 离线环境都能直接看，
  也可一键导出诊断包发给他人。
- **默认脱敏**：所有内容经 logging_setup.mask_secrets，口令不会落盘。
- **输出截断保护**：单次命令输出超过上限（默认 2MB）时截断并标注原始大小，
  防止一条超大 dump 日志把磁盘写满。
- **上下文自动传播**：通过 contextvar 提供 `current()`，引擎内部执行命令时
  无需层层传参即可把细节写进当前操作的日志。
"""
import os
import re
import time
import gzip
import shutil
import tempfile
import contextlib
import contextvars
import datetime as _dt
import traceback
from pathlib import Path

from core.logging_setup import get_logger, mask_secrets, mask_command, mask_env

# 单次命令输出写入日志的上限（字节）；超出部分截断并注明
MAX_CAPTURE_BYTES = int(os.environ.get("OPLOG_MAX_CAPTURE_BYTES", 2 * 1024 * 1024))
# 操作日志保留天数（超期由 purge_old 清理）
OPLOG_RETENTION_DAYS = int(os.environ.get("OPLOG_RETENTION_DAYS", "30"))

_current: contextvars.ContextVar = contextvars.ContextVar("aidbm_oplog", default=None)


class OperationLog:
    """单次操作的详细日志写入器（线程内可见，跨线程需显式传递）。"""

    def __init__(self, kind: str = "op", task: dict = None,
                 task_id: int = None, task_name: str = "",
                 record_id: int = None, operator: str = "",
                 log_dir=None, extra: dict = None):
        self.kind = (kind or "op").strip() or "op"
        task = task or {}
        self.task_id = task_id if task_id is not None else task.get("id")
        self.task_name = task_name or task.get("name") or ""
        self.record_id = record_id
        self.operator = operator or ""
        self.extra = dict(extra or {})
        self.started = time.time()
        self._closed = False
        self._status = "running"
        self._t0 = time.time()

        base = Path(log_dir) if log_dir else _default_log_dir()
        day = _dt.datetime.now().strftime("%Y%m%d")
        self.dir = base / "operations" / day
        self.path = self.dir / self._filename()
        self._fh = None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        except Exception as e:
            # 写不了文件也要能跑：降级到 system logger
            self._fh = None
            get_logger("aidbm.oplog").warning(
                "操作日志文件创建失败（降级为普通日志）: %s -> %s", self.path, e)

    # ---------------- 文件与标识 ----------------
    def _filename(self) -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        t = f"t{self.task_id}" if self.task_id is not None else "t-"
        r = f"_r{self.record_id}" if self.record_id is not None else ""
        return f"{self.kind}_{t}_{ts}{r}.log"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def status(self) -> str:
        return self._status

    # ---------------- 基础写入 ----------------
    def _write(self, level: str, text: str) -> None:
        line = f"{_ts()} [{level}] {text}"
        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
                return
            except Exception:
                self._fh = None
        try:
            get_logger("aidbm.oplog").log(
                {"ERROR": 40, "WARN": 30, "INFO": 20}.get(level, 20), "%s", line)
        except Exception:
            pass

    def _truncate(self, text: str) -> str:
        """超长输出截断（保留头尾，中间标注省略量）。"""
        if not text:
            return ""
        raw = text
        if len(raw.encode("utf-8", "ignore")) <= MAX_CAPTURE_BYTES:
            return raw
        head_n = MAX_CAPTURE_BYTES // 2
        tail_n = MAX_CAPTURE_BYTES // 4
        head = raw[:head_n]
        tail = raw[-tail_n:]
        omitted = len(raw) - len(head) - len(tail)
        return (head + f"\n\n... [输出过大，省略中间 {omitted} 字符"
                       f"（上限 {MAX_CAPTURE_BYTES // 1024}KB，"
                       f"可用 OPLOG_MAX_CAPTURE_BYTES 调整）] ...\n\n" + tail)

    # ---------------- 对外写入 API ----------------
    def info(self, msg, *args):
        self._write("INFO", _fmt(msg, args))

    def warning(self, msg, *args):
        self._write("WARN", _fmt(msg, args))

    def error(self, msg, *args):
        self._write("ERROR", _fmt(msg, args))

    def section(self, title: str) -> None:
        self._write("INFO", "")
        self._write("INFO", "=" * 70)
        self._write("INFO", f"== {title}")
        self._write("INFO", "=" * 70)

    def context(self, data: dict, title: str = "操作上下文") -> None:
        """记录键值上下文（自动脱敏）。"""
        self.section(title)
        for k, v in (data or {}).items():
            self._write("INFO", f"  {k}: {mask_secrets(v)}")

    def command(self, cmd, env: dict = None, note: str = "", timeout=None) -> None:
        """记录将要执行的命令（脱敏）+ 关键环境变量。"""
        line = f"执行命令: {mask_command(cmd)}"
        if note:
            line += f"    [{note}]"
        if timeout:
            line += f"    [超时 {timeout}s]"
        self._write("INFO", line)
        if env:
            shown = mask_env(env)
            for k in sorted(shown):
                self._write("INFO", f"    env {k}={shown[k]}")

    def output(self, rc, stdout: str = "", stderr: str = "", label: str = "") -> None:
        """记录命令执行结果：退出码 + stdout 全文 + stderr 全文。"""
        tag = f"{label} " if label else ""
        self._write("INFO", f"{tag}退出码: {rc}")
        if stdout:
            self._write("INFO", f"{tag}stdout ({len(stdout)} 字符):")
            for ln in self._truncate(stdout).splitlines():
                self._write("INFO", f"  | {mask_secrets(ln)}")
        if stderr:
            self._write("WARN", f"{tag}stderr ({len(stderr)} 字符):")
            for ln in self._truncate(stderr).splitlines():
                self._write("WARN", f"  ! {mask_secrets(ln)}")
        if not stdout and not stderr:
            self._write("INFO", f"{tag}（无输出）")

    def exception(self, exc: BaseException, note: str = "") -> None:
        """记录异常与完整堆栈。"""
        head = f"{note} " if note else ""
        self._write("ERROR", f"{head}异常: {exc.__class__.__name__}: {mask_secrets(exc)}")
        try:
            tb = "".join(traceback.format_exception(
                exc.__class__, exc, exc.__traceback__))
            for ln in tb.rstrip().splitlines():
                self._write("ERROR", f"  # {mask_secrets(ln)}")
        except Exception:
            pass

    def fail(self, reason: str, exc: BaseException = None) -> None:
        """记录失败结论并关闭日志。"""
        self.section("失败结论")
        self._write("ERROR", mask_secrets(reason))
        if exc is not None:
            self.exception(exc, note="[失败原因]")
        self.close(status="failed", message=reason)

    @contextlib.contextmanager
    def step(self, name: str):
        """阶段计时：进入/退出各记一行，退出时输出耗时。"""
        t0 = time.time()
        self._write("INFO", f">>> 阶段开始: {name}")
        ok = True
        try:
            yield self
        except Exception as e:
            ok = False
            self.exception(e, note=f"[阶段 {name} 异常]")
            raise
        finally:
            cost = time.time() - t0
            self._write("INFO", f"<<< 阶段结束: {name} "
                                f"({'成功' if ok else '失败'}, 耗时 {cost:.3f}s)")

    def close(self, status: str = "success", message: str = "") -> str:
        """收尾：写总结与日志文件路径，返回日志文件路径。"""
        if self._closed:
            return str(self.path)
        self._closed = True
        self._status = status
        cost = time.time() - self.started
        self._write("INFO", "")
        self._write("INFO", f"结论: {status.upper()}"
                            + (f" | {mask_secrets(message)}" if message else ""))
        self._write("INFO", f"总耗时: {cost:.3f}s")
        self._write("INFO", f"日志文件: {self.path}")
        try:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        return str(self.path)

    # ---------------- 读取 ----------------
    def read(self, tail: int = 0) -> str:
        """读取日志内容；tail>0 时只取末尾 N 行。"""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        if tail and tail > 0:
            return "\n".join(text.splitlines()[-tail:])
        return text


# --------------------------------------------------------------------------- #
# 上下文传播
# --------------------------------------------------------------------------- #
def current() -> "OperationLog | None":
    return _current.get()


def attach(op: "OperationLog"):
    return _current.set(op)


def detach(token) -> None:
    try:
        _current.reset(token)
    except Exception:
        pass


@contextlib.contextmanager
def operation(kind: str, task: dict = None, task_id: int = None,
              task_name: str = "", record_id: int = None, operator: str = "",
              log_dir=None, extra: dict = None):
    """上下文管理器：在块内执行的命令细节都会写进本次操作日志。

    用法::

        with oplog.operation("backup", task=t, record_id=rid) as op:
            op.context({"数据库": "MySQL"})
            ...
    """
    op = OperationLog(kind=kind, task=task, task_id=task_id,
                      task_name=task_name, record_id=record_id,
                      operator=operator, log_dir=log_dir, extra=extra)
    token = attach(op)
    try:
        yield op
    finally:
        # 未显式 close（如异常路径）也要收尾，避免日志没有结论行
        if not op._closed:
            op.close(status="failed" if _current.get() is not None else "unknown",
                     message="操作未正常结束（可能被中断）")
        detach(token)


# --------------------------------------------------------------------------- #
# 目录/文件管理（供 API 与保留策略使用）
# --------------------------------------------------------------------------- #
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _default_log_dir() -> Path:
    """取当前生效的日志目录（与启动横幅、API 展示保持一致）。"""
    try:
        from core import logging_setup
        from core.logging_setup import LOG_DIR
        if LOG_DIR:
            return Path(LOG_DIR)
        return logging_setup.resolve_log_dir()[0]
    except Exception:
        import config
        return Path(getattr(config, "LOG_DIR", tempfile.gettempdir())) / "aidbm"


def operations_root() -> Path:
    root = _default_log_dir() / "operations"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return root


def list_operations(limit: int = 200, kind: str = "", task_id: int = None,
                    record_id: int = None, day: str = "", keyword: str = "") -> list:
    """列出操作日志文件（按时间倒序）。"""
    out = []
    root = operations_root()
    days = [day] if day else []
    if not days:
        try:
            days = sorted([p.name for p in root.iterdir() if p.is_dir()],
                          reverse=True)[:30]
        except Exception:
            days = []
    for d in days:
        day_dir = root / d
        if not day_dir.is_dir():
            continue
        try:
            files = sorted(day_dir.glob("*.log"), reverse=True)
        except Exception:
            continue
        for f in files:
            if kind and not f.name.startswith(kind + "_"):
                continue
            if task_id is not None and f"t{task_id}_" not in f.name:
                continue
            if record_id is not None and f"_r{record_id}." not in f.name:
                continue
            if keyword and keyword.lower() not in f.name.lower():
                continue
            try:
                st = f.stat()
            except Exception:
                continue
            out.append({
                "name": f.name,
                "day": d,
                "kind": f.name.split("_", 1)[0],
                "size_bytes": st.st_size,
                "mtime": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "path": str(f),
            })
            if len(out) >= limit:
                return out
    return out


def read_operation(name: str, day: str = "", tail: int = 0) -> str:
    """读取指定操作日志（名称严格校验，杜绝路径穿越）。"""
    if not name or not _SAFE_NAME.match(name):
        return ""
    if day and not _SAFE_NAME.match(day):
        return ""
    root = operations_root()
    candidates = []
    if day:
        candidates.append(root / day / name)
    else:
        try:
            for p in sorted(root.iterdir(), reverse=True):
                if p.is_dir():
                    candidates.append(p / name)
        except Exception:
            pass
    for c in candidates:
        try:
            real = c.resolve()
            if not str(real).startswith(str(root.resolve())):
                continue
            if not real.is_file():
                continue
            text = real.read_text(encoding="utf-8", errors="replace")
            if tail and tail > 0:
                return "\n".join(text.splitlines()[-tail:])
            return text
        except Exception:
            continue
    return ""


def purge_old(days: int = None) -> int:
    """清理超过保留期的操作日志，返回清理的目录数（按天）。"""
    keep = int(days if days is not None else OPLOG_RETENTION_DAYS)
    if keep <= 0:
        return 0
    root = operations_root()
    cutoff = _dt.date.today() - _dt.timedelta(days=keep)
    removed = 0
    try:
        for p in list(root.iterdir()):
            if not p.is_dir():
                continue
            try:
                d = _dt.datetime.strptime(p.name, "%Y%m%d").date()
            except Exception:
                continue
            if d < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    except Exception:
        pass
    return removed


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _fmt(msg, args) -> str:
    try:
        text = msg % args if args else str(msg)
    except Exception:
        text = f"{msg} {args}"
    return mask_secrets(text)
