# -*- coding: utf-8 -*-
"""自动合成全量调度封装（落实 CDM "系统内自动合成全量"）。

PDF 要点：永久增量（永远只做增量）→ 系统定期把增量链自动合成全量，
合成产物以原始格式可直接挂载即时恢复，中间增量副本由生命周期策略回收。

本模块提供：
- run_auto_synthesis(): 遍历所有任务，对"存在可合并增量"的任务触发合成全量；
- 配置从 system_config.synthesize_config 读取（默认每周日 03:00 运行）。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("core.synthesize")


def _load_config() -> dict:
    try:
        import json
        import core.db as db
        raw = db.get_system_config("synthesize_config")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        elif isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"enabled": True, "min_incremental": 2, "cron": "0 3 * * 0"}


def run_auto_synthesis() -> dict:
    """对所有任务执行自动合成全量。返回统计。

    仅对满足"增量数量 >= min_incremental"的任务合成，避免无意义的空跑。
    合成后中间增量副本由 lifecycle 策略按 chain_status='merged' 回收。
    """
    from core.engines import synthesize_full_for_task
    import core.models as models

    cfg = _load_config()
    min_inc = int(cfg.get("min_incremental", 2) or 2)
    if not cfg.get("enabled", True):
        logger.info("[synthesize] 自动合成已禁用，跳过")
        return {"skipped": True, "synthesized": 0, "tasks": 0}

    tasks = models.list_tasks(enabled=True) or []
    synthesized = 0
    affected = 0
    for t in tasks:
        tid = t["id"]
        sets = models.list_backup_sets(task_id=tid)
        bases = [s for s in sets
                 if s.get("set_type") in ("full", "synthetic_full")]
        has_chain = False
        for base in bases:
            inc_count = sum(
                1 for s in sets
                if s.get("parent_set_id") == base["id"]
                and s.get("set_type") == "incremental")
            if inc_count >= min_inc:
                has_chain = True
                break
        if not has_chain:
            continue
        try:
            ids = synthesize_full_for_task(tid, logger=logger)
            if ids:
                synthesized += len(ids)
                affected += 1
                logger.info("[synthesize] task=%s 生成 %d 个合成全量", tid, len(ids))
        except Exception:
            logger.exception("[synthesize] task=%s 自动合成失败", tid)
    logger.info("[synthesize] 自动合成完成：%d 个任务，%d 个合成全量",
                affected, synthesized)
    return {"skipped": False, "synthesized": synthesized, "tasks": affected}
