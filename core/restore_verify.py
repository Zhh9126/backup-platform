# -*- coding: utf-8 -*-
"""恢复验证：文件/工具检查为部分通过，隔离恢复副本查询成功才为通过。"""
import inspect
import json
import logging
import time

import config
from core import db, models
from core.engines import get_engine
from core.recovery_evidence import check_artifact, prove_clone

logger = logging.getLogger(__name__)


def _latest_success_record(task_id):
    return db.query_one(
        "SELECT * FROM backup_records WHERE task_id=? AND status='success' ORDER BY id DESC LIMIT 1",
        (task_id,))


def run_restore_verify_policy(policy_id, clone_request_id=None):
    policy = models.get_restore_verify_policy(policy_id)
    if not policy:
        raise ValueError(f"恢复校验策略不存在: {policy_id}")
    task = models.get_task(policy["task_id"], include_secret=True)
    if not task:
        raise ValueError(f"策略关联的任务不存在: {policy['task_id']}")
    # A lease covers a policy regardless of whether the caller is cron or HTTP.
    from core.operation_lease import operation_lease
    with operation_lease("restore-verify:%s" % policy_id) as ensure_owned:
        record = _latest_success_record(policy["task_id"])
        report_id = models.create_restore_test_report({
            "policy_id": policy_id, "task_id": policy["task_id"],
            "record_id": record["id"] if record else None, "db_type": task.get("db_type"),
            "status": "running", "message": "开始恢复校验", "cleaned": 0,
            "verification_level": "unknown", "evidence_json": "{}",
        })
        started = time.monotonic()
        evidence, status, level = {}, "failed", "failed"
        try:
            if not record:
                raise ValueError("没有可用的真实成功备份记录")
            evidence = check_artifact(record)
            engine = get_engine(task.get("db_type"), task, config.BACKUP_ROOT, logger)
            options = {"recovery_pool": policy.get("recovery_pool") or ""}
            parameters = inspect.signature(engine.verify_record).parameters
            result = (engine.verify_record(dict(record), options=options)
                      if "options" in parameters else engine.verify_record(dict(record)))
            if not result.success or result.simulated:
                raise ValueError(result.message or "备份产物校验失败")
            status, level = "partial", "artifact"
            evidence["engine_check_passed"] = True
            try:
                runtime = prove_clone(record, task, clone_request_id)
            except Exception as exc:
                if clone_request_id is not None:
                    raise
                runtime = {"runtime_verified": False, "reason": str(exc)[:300]}
            evidence.update(runtime)
            ensure_owned()
            if evidence.get("runtime_verified"):
                status, level = "success", "runtime"
                message = "隔离恢复副本查询通过"
            else:
                message = "部分通过：%s；%s" % (result.message or "备份产物检查通过",
                                              runtime.get("reason") or "尚未完成隔离恢复查询验证")
        except Exception as exc:
            status, level = "failed", "failed"
            message = "恢复校验失败: %s" % exc
            logger.warning("恢复校验 policy=%s: %s", policy_id, message)
        duration = round(time.monotonic() - started, 3)
        models.update_restore_test_report(report_id, {
            "status": status, "duration_sec": duration, "message": message,
            "verification_level": level, "evidence_json": json.dumps(evidence, ensure_ascii=False),
            "finished_at": db.now_iso(),
        })
        models.set_restore_verify_status(policy_id, db.now_iso(), status, report_id)
        return {"report_id": report_id, "success": status == "success", "status": status,
                "verification_level": level, "evidence": evidence, "message": message,
                "duration_sec": duration}
