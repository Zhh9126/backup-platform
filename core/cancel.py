# -*- coding: utf-8 -*-
"""运行中备份任务的协作式取消注册表。

用户「停止」运行中的备份：调度器在创建运行记录时注册取消事件（键 =
t<task_id>:r<record_id>，并绑定到当前执行上下文 ContextVar）；传输类长循环
（SFTP 增量拉取/推送、staged 分块拉取、SSH 数据管道）在**块边界**检查
is_cancelled()，命中即抛 CancelledError——调度器捕获后把记录标记为
cancelled。

设计约定：
- 协作式而非强杀：远端 dump 工具（mysqldump/pg_basebackup 等）无法被线程
  级终止，取消在下一次传输块边界生效；对非分块的短命令，等当前命令结束。
- 断点续传联动：取消不清传输断点（.part/stage 残件保留）——用户重新发起
  备份时从断点继续，已传字节不浪费。
"""
import threading
from contextvars import ContextVar

_lock = threading.Lock()
_events = {}
_current = ContextVar("aidbm_cancel_key", default=None)


class CancelledError(RuntimeError):
    """任务被用户手动停止。"""


def register(key: str) -> threading.Event:
    """注册取消事件（任务开始时调用）。"""
    ev = threading.Event()
    with _lock:
        _events[key] = ev
    return ev


def attach(key: str):
    """把当前执行上下文绑定到取消键（ContextVar，随线程/协程隔离）。"""
    return _current.set(key)


def detach(token):
    try:
        _current.reset(token)
    except Exception:
        pass


def finish(key: str) -> None:
    """任务结束（任何结局）后清理事件。"""
    with _lock:
        _events.pop(key, None)


def cancel(key: str) -> bool:
    """发出停止指令；返回是否命中了正在运行的任务。"""
    with _lock:
        ev = _events.get(key)
    if ev is None:
        return False
    ev.set()
    return True


def is_cancelled() -> bool:
    """当前执行上下文是否被要求停止（长循环的块边界调用）。"""
    key = _current.get()
    if not key:
        return False
    with _lock:
        ev = _events.get(key)
    return bool(ev and ev.is_set())


def check() -> None:
    """块边界便捷调用：已取消则抛 CancelledError。"""
    if is_cancelled():
        raise CancelledError("任务已被用户手动停止")
