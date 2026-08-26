# -*- coding: utf-8 -*-
"""
实时同步（Binlog CDC）运行器管理。

每个 realtime 同步任务对应一个后台线程 + stop_event。
线程内调用 core.sync.engine.run_sync_task（其 realtime 分支进入 Binlog 监听循环）。
"""
import logging
import threading
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_RUNNERS: Dict[int, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def is_running(task_id: int) -> bool:
    with _LOCK:
        r = _RUNNERS.get(task_id)
        return bool(r and r["thread"] is not None and r["thread"].is_alive())


def get_stop_event(task_id: int) -> threading.Event:
    with _LOCK:
        r = _RUNNERS.get(task_id)
        if r:
            return r["stop_event"]
        ev = threading.Event()
        _RUNNERS[task_id] = {"thread": None, "stop_event": ev, "result": None}
        return ev


def start_runner(task_id: int, fn: Callable[..., Any], *args, **kwargs) -> bool:
    """启动后台实时同步线程。若已有运行中线程返回 False。"""
    with _LOCK:
        if task_id in _RUNNERS:
            r = _RUNNERS[task_id]
            if r["thread"] is not None and r["thread"].is_alive():
                return False
        ev = threading.Event()
        t = threading.Thread(
            target=_wrap,
            args=(task_id, fn) + args,
            kwargs=kwargs,
            daemon=True,
            name=f"sync-realtime-{task_id}",
        )
        _RUNNERS[task_id] = {"thread": t, "stop_event": ev, "result": None}
        t.start()
        return True


def _wrap(task_id: int, fn: Callable[..., Any], *args, **kwargs) -> None:
    try:
        res = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.exception("realtime sync %s 异常", task_id)
        res = {"success": False, "message": f"实时同步异常: {e}"}
        try:
            from .. import models  # noqa: PLC0415

            models.update_sync_task(task_id, {
                "status": "failed",
                "last_status": "failed",
                "message": f"实时同步异常: {e}",
            })
        except Exception:  # noqa: BLE001
            logger.warning("realtime sync %s 更新失败状态出错", task_id)
    finally:
        with _LOCK:
            if task_id in _RUNNERS:
                _RUNNERS[task_id]["result"] = res


def stop_runner(task_id: int, timeout: float = 15.0) -> Dict[str, Any]:
    """请求停止并等待线程退出。返回线程结果或超时提示。"""
    ev = get_stop_event(task_id)
    ev.set()
    t = None
    with _LOCK:
        if task_id in _RUNNERS:
            t = _RUNNERS[task_id].get("thread")
    if t and t.is_alive():
        t.join(timeout)
    with _LOCK:
        r = _RUNNERS.pop(task_id, None)
        res = (r or {}).get("result")
    if not res:
        res = {"success": True, "message": "实时同步已停止"}
    return res


def clear_runner(task_id: int) -> None:
    with _LOCK:
        _RUNNERS.pop(task_id, None)
