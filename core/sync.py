# -*- coding: utf-8 -*-
"""
数据同步兼容入口。

新版同步引擎采用 DataX/LinkUp 风格：Source Reader → 统一 Java 类型 → Sink Writer，
支持表级同步、字段映射、写入模式（append/overwrite/upsert/create_if_not_exists）、
增量同步与实时同步（Flink CDC 预留）。

本模块保留旧的 `run_sync(sync_task_id)` 调度入口，负责：
- 解密连接密码
- 创建/更新 sync_records
- 调用 core.sync.engine 执行同步
- 失败时触发通知与日志
"""
from datetime import datetime
from typing import Optional

import config
import core.db as db
from core import models, notifier
from core.sync.engine import run_sync_task_with_task

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
    db.execute(
        "UPDATE sync_records SET finished_at=?, status=?, rows_synced=?, message=? WHERE id=?",
        (finished, status, rows, message, rec_id),
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
