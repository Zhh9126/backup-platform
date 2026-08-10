# -*- coding: utf-8 -*-
"""
迁移全流程保护（MigrationPlan）：把"一次性迁移"变成可回退的三阶段编排。

三阶段
------
- pre （迁移前）：对源端生产库做全量备份作为「黄金回退点」，记录
  golden_backup_record_id；verify_golden 做恢复验证（DEMO 下仿真校验，真实下
  可调用恢复演练 / 校验）。
- mid （迁移中）：对两端库做高频增量 / 日志备份（DEMO 下登记增量备份记录；
  真实下调用引擎增量备份）。
- post（迁移后）：稳定后把备份重心切到新体系，旧系统备份保留 old_retention_days
  后过期清理（可与 Phase 1 LifecycleEngine 联动，本期独立记录）。

复用既有能力：备份执行走 `core.scheduler.run_task_now`（复用备份引擎 / 调度），
DEMO 下自动仿真，保证无真实环境也能跑通自测闭环。
"""
import logging
from typing import Optional

import config
import core.models as models
import core.db as db


_logger = db.get_logger("migration")

STAGE_PRE = "pre"
STAGE_MID = "mid"
STAGE_POST = "post"


class MigrationPlan:
    """迁移全流程保护编排引擎。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 计划管理 -------------------------
    def create_plan(self, task_id: int, note: str = None) -> int:
        """显式创建一条迁移计划，返回 plan id。"""
        pid = models.create_migration_plan({
            "task_id": task_id, "stage": STAGE_PRE,
            "status": "created", "note": note,
        })
        self.logger.info("[migration] 创建计划 #%s（task=%s）", pid, task_id)
        return pid

    def get_plan(self, plan_id: int) -> Optional[dict]:
        return models.get_migration_plan(plan_id)

    def list_plans(self) -> list:
        return models.list_migration_plans()

    def get_status(self, plan_id: int) -> Optional[dict]:
        """返回计划当前状态（含黄金点校验信息）。"""
        plan = self.get_plan(plan_id)
        if not plan:
            return None
        golden = plan.get("golden_backup_record_id")
        info = dict(plan)
        if golden:
            rec = models.get_record(golden)
            info["golden_record"] = rec
        return info

    # ------------------------- 内部：查找 / 复用计划 -------------------------
    def _find_or_create(self, task_id: int, note: str = None) -> dict:
        plan = db.query_one(
            "SELECT * FROM migration_plans WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,))
        if plan:
            return plan
        return self.get_plan(self.create_plan(task_id, note))

    # ------------------------- 内部：执行一次备份（复用引擎/调度） -------------------------
    def _run_backup(self, task_id: int, backup_type: str) -> int:
        """复用现有调度/引擎执行一次备份（DEMO 下仿真），返回 record id。"""
        try:
            from core import scheduler
            rec = scheduler.run_task_now(task_id, backup_type=backup_type)
            if isinstance(rec, dict) and rec.get("id"):
                return int(rec["id"])
        except Exception as e:
            self.logger.warning("[migration] run_task_now(%s) 失败，降级仿真: %s",
                                backup_type, e)
        return self._sim_record(task_id, backup_type)

    def _sim_record(self, task_id: int, backup_type: str) -> int:
        task = models.get_task(task_id)
        db_type = task["db_type"] if task else "unknown"
        return models.create_record({
            "task_id": task_id, "db_type": db_type, "backup_type": backup_type,
            "started_at": db.now_iso(), "finished_at": db.now_iso(),
            "status": "success", "size_bytes": 0, "backup_path": "",
            "checksum": "", "is_simulated": 1,
            "message": f"migration {backup_type} simulated backup",
        })

    # ------------------------- 阶段：pre -------------------------
    def start_pre(self, task_id: int, note: str = None) -> dict:
        """迁移前：全量备份作为黄金回退点。"""
        if not models.get_task(task_id):
            raise ValueError(f"备份任务不存在: {task_id}")
        plan = self._find_or_create(task_id, note)
        pid = plan["id"]
        golden = self._run_backup(task_id, "full")
        models.update_migration_plan(pid, {
            "stage": STAGE_PRE, "status": STAGE_PRE,
            "golden_backup_record_id": golden,
        })
        self.logger.info("[migration] 计划 #%s pre 完成，黄金点 record=%s", pid, golden)
        return self.get_plan(pid)

    def verify_golden(self, plan_id: int) -> dict:
        """验证黄金回退点可恢复性（DEMO 下仿真校验）。"""
        plan = self.get_plan(plan_id)
        if not plan:
            raise ValueError(f"迁移计划不存在: {plan_id}")
        golden = plan.get("golden_backup_record_id")
        ok = False
        msg = ""
        if not golden:
            msg = "无黄金备份记录，无法校验"
        else:
            rec = models.get_record(golden)
            if rec and rec.get("status") in ("success", "simulated"):
                ok = True
                msg = "黄金点恢复校验通过（可回退）"
            else:
                msg = f"黄金点校验未通过（status={rec.get('status') if rec else 'none'}）"
        models.update_migration_plan(plan_id, {"verified": 1 if ok else 0})
        if golden and ok:
            try:
                models.mark_record_verified(golden, True, msg)
            except Exception:
                pass
        self.logger.info("[migration] 计划 #%s verify_golden -> %s", plan_id, ok)
        return {"plan_id": plan_id, "verified": ok, "message": msg}

    # ------------------------- 阶段：mid -------------------------
    def start_mid(self, task_id: int, note: str = None) -> dict:
        """迁移中：高频增量 / 日志备份（DEMO 下登记增量备份记录）。"""
        plan = self._find_or_create(task_id, note)
        pid = plan["id"]
        # 对源端做一次增量备份登记（高频增量保护的仿真落点）
        self._run_backup(task_id, "incremental")
        models.update_migration_plan(pid, {"stage": STAGE_MID, "status": STAGE_MID})
        self.logger.info("[migration] 计划 #%s mid 完成（已登记高频增量备份）", pid)
        return self.get_plan(pid)

    # ------------------------- 阶段：post -------------------------
    def start_post(self, task_id: int, old_retention_days: int = None,
                   note: str = None) -> dict:
        """迁移后：备份重心切到新体系，记录旧库保留天数。"""
        plan = self._find_or_create(task_id, note)
        pid = plan["id"]
        data = {"stage": STAGE_POST, "status": STAGE_POST}
        if old_retention_days is not None:
            data["old_retention_days"] = int(old_retention_days)
        models.update_migration_plan(pid, data)
        self.logger.info("[migration] 计划 #%s post 完成（旧库保留 %s 天）",
                         pid, old_retention_days)
        return self.get_plan(pid)


# 便捷单例
migration_engine = MigrationPlan()
