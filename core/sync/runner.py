# -*- coding: utf-8 -*-
"""数据同步调度入口。

注意：本目录（core/sync/ 是包）会遮蔽同名的 core/sync.py 旧模块——
旧模块里的 run_sync 因此从未被 import 到，调度器 core.sync.run_sync
此前一直 AttributeError 静默失败（2026-10 体检发现并修复）。
run_sync 现定居本文件，并由 core/sync/__init__.py 导出。
"""
from datetime import datetime
from typing import Optional

import core.db as db
from core import models, notifier
from .engine import run_sync_task_with_task

_logger = db.get_logger("sync")


def run_sync(sync_task_id: int) -> Optional[dict]:
    """执行一次数据同步，返回生成的同步记录（供调度器调用）。"""
    task = models.get_sync_task(sync_task_id, include_secret=True)
    if not task:
        _logger.warning("同步任务不存在: %s", sync_task_id)
        return None

    started = db.now_iso()
    rec_id = models.create_sync_record({
        "sync_task_id": sync_task_id,
        "started_at": started,
        "status": "running",
    })

    _logger.info("开始数据同步 task=%s(%s) %s -> %s",
                 task["id"], task.get("name"),
                 task.get("src_db_type"), task.get("tgt_db_type"))

    res = {}
    try:
        res = run_sync_task_with_task(task)
        status = "success" if res.get("success") else "failed"
        message = res.get("message", "")
        rows = res.get("total_write", 0)

        # 更新任务状态
        models.set_sync_status(
            sync_task_id,
            datetime.now().isoformat(timespec="seconds"),
            status,
            message,
        )
    except Exception as e:
        _logger.exception("数据同步异常 sync=%s", sync_task_id)
        status = "failed"
        message = f"同步异常: {e}"
        rows = 0
        models.set_sync_status(
            sync_task_id,
            datetime.now().isoformat(timespec="seconds"),
            status,
            message,
        )

    finished = db.now_iso()
    # 可观测对账：读取量 = 写入 + 过滤 + 失败，任何一项缺失即链路黑箱
    db.execute(
        "UPDATE sync_records SET finished_at=?, status=?, rows_synced=?, "
        "read_rows=?, error_rows=?, watermark=?, message=? WHERE id=?",
        (finished, status, rows,
         res.get("total_read", 0),
         res.get("errors", 0),
         (str(res.get("next_value")) if res.get("next_value") is not None
          else None),
         message, rec_id),
    )

    if status == "failed":
        text = (f"同步任务: {task.get('name')}\n源: {task.get('src_db_type')} {task.get('src_host')}\n"
                f"目标: {task.get('tgt_db_type')} {task.get('tgt_host')}\n"
                f"状态: 失败\n说明: {message}")
        notifier.Notifier(None, _logger).notify(
            "failure", f"[失败] 数据同步 {task.get('name')}", text)
        db.add_log("ERROR", "sync", f"sync={sync_task_id} {task.get('name')} -> 失败")
    else:
        db.add_log("INFO", "sync", f"sync={sync_task_id} {task.get('name')} -> {status}")

    return models.get_sync_record(rec_id)
