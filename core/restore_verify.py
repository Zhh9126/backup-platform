# -*- coding: utf-8 -*-
"""恢复校验执行器。

提供恢复校验策略的调度入口与立即执行入口：
- run_restore_verify_policy(policy_id): 对指定策略执行一次校验，生成测试报告。
"""
import os
import time
import logging
from typing import Optional

import config
import core.db as db
from core import models
from core.engines import get_engine
from core.engines.base import BackupStatus

logger = logging.getLogger(__name__)


def _latest_success_record(task_id: int) -> Optional[dict]:
    """获取任务最近一次成功/仿真的备份记录。"""
    row = db.query_one(
        "SELECT * FROM backup_records WHERE task_id=? AND status IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, BackupStatus.SUCCESS.value, BackupStatus.SIMULATED.value),
    )
    return row


def _cleanup_temp_dir(temp_dir: str) -> None:
    """清理恢复校验产生的临时目录/文件。"""
    if not temp_dir or not os.path.isdir(temp_dir):
        return
    try:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        logger.warning("清理恢复校验临时目录失败 %s: %s", temp_dir, e)


def run_restore_verify_policy(policy_id: int) -> dict:
    """执行一次恢复校验策略。

    返回测试报告 id。
    """
    policy = models.get_restore_verify_policy(policy_id)
    if not policy:
        raise ValueError(f"恢复校验策略不存在: {policy_id}")
    task_id = policy["task_id"]
    task = models.get_task(task_id)
    if not task:
        raise ValueError(f"策略关联的任务不存在: {task_id}")

    record = _latest_success_record(task_id)
    if not record:
        # 没有可校验的备份，也生成失败报告
        report_id = models.create_restore_test_report({
            "policy_id": policy_id,
            "task_id": task_id,
            "record_id": None,
            "db_type": task.get("db_type"),
            "status": BackupStatus.FAILED.value,
            "duration_sec": 0,
            "message": "没有可用的成功备份记录用于恢复校验",
            "cleaned": 1,
        })
        models.set_restore_verify_status(
            policy_id, db.now_iso(), BackupStatus.FAILED.value, report_id)
        return {"report_id": report_id, "success": False, "message": "没有可用的成功备份记录"}

    record_id = record["id"]
    db_type = task.get("db_type") or record.get("db_type") or "unknown"
    report_id = models.create_restore_test_report({
        "policy_id": policy_id,
        "task_id": task_id,
        "record_id": record_id,
        "db_type": db_type,
        "status": BackupStatus.RUNNING.value,
        "duration_sec": 0,
        "message": "开始恢复校验",
        "cleaned": 0,
    })

    started = time.monotonic()
    temp_dir = None
    try:
        engine = get_engine(task.get("db_type"), task, config.BACKUP_ROOT, logger)
        # 将恢复池作为临时目标，供引擎在校验时使用
        options = {"recovery_pool": policy.get("recovery_pool") or ""}
        result = engine.verify_record(dict(record), options=options)
        duration = round(time.monotonic() - started, 3)
        status = BackupStatus.SUCCESS.value if result.success else BackupStatus.FAILED.value
        message = result.message or ("校验通过" if result.success else "校验失败")
        models.update_restore_test_report(report_id, {
            "status": status,
            "duration_sec": duration,
            "message": message,
            "finished_at": db.now_iso(),
        })
        models.set_restore_verify_status(
            policy_id, db.now_iso(), status, report_id)
        return {
            "report_id": report_id,
            "success": result.success,
            "message": message,
            "duration_sec": duration,
        }
    except Exception as e:
        duration = round(time.monotonic() - started, 3)
        msg = f"恢复校验异常: {e}"
        logger.exception("恢复校验失败 policy_id=%s", policy_id)
        models.update_restore_test_report(report_id, {
            "status": BackupStatus.FAILED.value,
            "duration_sec": duration,
            "message": msg,
            "finished_at": db.now_iso(),
        })
        models.set_restore_verify_status(
            policy_id, db.now_iso(), BackupStatus.FAILED.value, report_id)
        return {"report_id": report_id, "success": False, "message": msg}
    finally:
        _cleanup_temp_dir(temp_dir)
