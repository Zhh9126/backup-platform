# -*- coding: utf-8 -*-
"""保护目标驱动收口（P0-2）：把 RPO/RTO/cross_site 目标从"展示字段"变成
"会告警的闭环"。

三条规则（对应对标文档 P0-2）：
1. RPO 目标 ≤5 分钟 → 必须开启实时保护（CDP/rt_enabled），否则 fail 级缺口；
2. 有效备份间隔 > RPO 目标 → warn（备份频率撑不住目标）；
3. 策略要求异地容灾（link_strategy.cross_site）→ 未配置异地/对象存储目标
   则 warn（数据只在本机，异地容灾目标落空）。

诚实边界：本模块只做"目标 → 校验 → 告警"，不自动改配置（自动改配置列入
后续版本）；评估为只读，可在任意入口调用（任务列表/首页关注/告警任务）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

logger = logging.getLogger("core.goals")


def _policy_targets(task: dict) -> tuple:
    """(rpo_target_min, rto_target_min, policy)——策略绑定任务取策略值。"""
    try:
        from core.policy import policy_service
        rpo, rto = policy_service.resolve_rpo_rto(task)
        pol = policy_service.resolve(task) or {}
        return int(rpo or 0), int(rto or 0), pol
    except Exception:  # noqa: BLE001
        return 0, 0, {}


def _effective_interval_minutes(task: dict) -> int:
    """估算有效备份间隔（分钟）。cron 支持常见模式，复杂表达式返回 0（跳过规则2）。"""
    st = (task.get("schedule_type") or "").lower()
    if st == "interval":
        try:
            return int(task.get("interval_minutes") or 0)
        except (TypeError, ValueError):
            return 0
    if st == "cron":
        expr = (task.get("cron_expr") or "").strip()
        m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", expr)
        if m:
            return int(m.group(1))          # */N * * * * → 每 N 分钟
        if re.match(r"^\d+(\s*,\s*\d+)*\s+\*\s+\*\s+\*\s+\*$", expr):
            return 60                        # 每小时第 X 分
        if re.match(r"^\d+\s+\d+\s+\*\s+\*\s+\*$", expr):
            return 1440                      # 每天一次
        return 0                             # 复杂表达式：不评估
    return 0                                  # none/手动：规则2不适用


def _has_offsite(task: dict) -> bool:
    """是否配置了异地/非本机存储目标（对象存储/远程备份机/磁带均算）。"""
    backend = (task.get("storage_backend") or "local").lower()
    if backend not in ("local", "disk", ""):
        return True
    try:
        extra = task.get("extra_options") or {}
        if isinstance(extra, str):
            extra = json.loads(extra)
        if isinstance(extra, dict) and (extra.get("remote_host")
                                        or extra.get("storage_targets")):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def evaluate_task_goals(task: dict) -> List[Dict]:
    """评估单任务保护目标缺口。返回 [{level, code, message}]。"""
    gaps: List[Dict] = []
    if not task:
        return gaps
    rpo_min, _rto_min, pol = _policy_targets(task)
    rt_on = bool(task.get("rt_enabled"))

    # 规则1：RPO 近实时（0）或 ≤5 分钟必须开 CDP
    if (rpo_min == 0 or 0 < rpo_min <= 5) and not rt_on:
        rpo_txt = "近实时(≈0)" if rpo_min == 0 else f"{rpo_min} 分钟"
        gaps.append({
            "level": "fail", "code": "rpo_no_cdp",
            "message": (f"RPO 目标 {rpo_txt} 但未开启实时保护（CDP）："
                        "仅靠定时备份无法满足目标，请在「实时保护」开启"),
        })

    # 规则2：备份间隔 > RPO 目标
    interval = _effective_interval_minutes(task)
    if rpo_min > 0 and interval > 0 and interval > rpo_min:
        gaps.append({
            "level": "warn", "code": "interval_gt_rpo",
            "message": (f"备份间隔约 {interval} 分钟，大于 RPO 目标 "
                        f"{rpo_min} 分钟：请缩短备份周期（或绑定更高保护策略）"),
        })

    # 规则3：cross_site 要求异地副本
    link_strategy = {}
    try:
        link_strategy = json.loads(pol.get("link_strategy") or "{}") \
            if isinstance(pol.get("link_strategy"), str) \
            else (pol.get("link_strategy") or {})
    except Exception:  # noqa: BLE001
        link_strategy = {}
    if link_strategy.get("cross_site") and not _has_offsite(task):
        gaps.append({
            "level": "warn", "code": "cross_site_no_offsite",
            "message": ("策略要求异地容灾（cross_site）但任务未配置异地/对象"
                        "存储目标：数据仅存本机，异地容灾目标落空"),
        })
    return gaps


def protection_gaps() -> Dict:
    """全任务目标缺口汇总（供首页关注/任务列表告警）。"""
    import core.models as models
    tasks = models.list_tasks(include_secret=False) or []
    gaps = []
    fail_n = warn_n = 0
    for t in tasks:
        for g in evaluate_task_goals(t):
            gaps.append({"task_id": t.get("id"), "task_name": t.get("name"),
                         "db_type": t.get("db_type"),
                         "rpo_target_min": _policy_targets(t)[0],
                         **g})
            if g["level"] == "fail":
                fail_n += 1
            else:
                warn_n += 1
    return {"gaps": gaps, "fail_count": fail_n, "warn_count": warn_n,
            "total": len(gaps)}
