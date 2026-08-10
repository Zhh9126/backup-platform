# -*- coding: utf-8 -*-
"""
保护策略服务（ProtectionPolicyService）。

把"分层分级保护"从口头约定变成可计算的对象：
- 按保护等级（core / important / general）提供默认 RPO/RTO 与备份/复制/保留策略；
- 提供 resolve(task) 解析"生效策略"：任务显式绑定的策略优先，否则按等级取默认；
- 供调度（Phase1 决定并行度/避峰）、复制链路（tier_replication）与生命周期复用。

本期先实现解析能力，被调用处后续接入，不改动既有备份/复制主流程。
"""
from typing import Optional

import core.models as models


class ProtectionPolicyService:
    """分层分级保护策略解析服务。

    保护等级语义：
    - core（核心）：RPO≈0 近实时强同步/准同步，物理备份为主，跨站点强一致复制。
    - important（重要）：分钟级 RPO，逻辑+增量，跨站点异步复制。
    - general（一般）：逻辑备份 + 定时异地，小时级 RPO。
    """

    LEVELS = ("core", "important", "general")

    # 各等级默认策略（JSON 子字段均为 dict，便于调度/复制直接读取）
    DEFAULTS = {
        "core": {
            "level": "core",
            "rpo_target_min": 0,
            "rto_target_min": 15,
            "backup_strategy": {
                "type": "full",
                "mode": "physical",
                "frequency": "PT15M",
                "incremental": True,
                "parallel": 4,
                "sync_mode": "strong",
            },
            "link_strategy": {
                "replication": "sync",
                "cross_site": True,
                "consistency": "strong",
            },
            "retention": {
                "days": 90,
                "count": 200,
                "lifecycle": {"l1_to_l2_days": 1, "l2_to_l3_days": 7},
            },
        },
        "important": {
            "level": "important",
            "rpo_target_min": 15,
            "rto_target_min": 60,
            "backup_strategy": {
                "type": "full",
                "mode": "logical",
                "frequency": "PT1H",
                "incremental": True,
                "parallel": 2,
                "sync_mode": "async",
            },
            "link_strategy": {
                "replication": "async",
                "cross_site": True,
                "consistency": "eventual",
            },
            "retention": {
                "days": 30,
                "count": 100,
                "lifecycle": {"l1_to_l2_days": 3, "l2_to_l3_days": 15},
            },
        },
        "general": {
            "level": "general",
            "rpo_target_min": 240,
            "rto_target_min": 240,
            "backup_strategy": {
                "type": "full",
                "mode": "logical",
                "frequency": "P1D",
                "incremental": False,
                "parallel": 1,
                "sync_mode": "async",
            },
            "link_strategy": {
                "replication": "async",
                "cross_site": False,
                "consistency": "eventual",
            },
            "retention": {
                "days": 14,
                "count": 50,
                "lifecycle": {"l1_to_l2_days": 7, "l2_to_l3_days": 30},
            },
        },
    }

    def default_policy(self, level: str) -> dict:
        """返回某等级的默认策略 dict（字段与 protection_policies 表一致）。"""
        if level not in self.LEVELS:
            level = "general"
        d = dict(self.DEFAULTS[level])
        # 深拷贝可变子结构，避免调用方修改污染类常量
        d["backup_strategy"] = dict(d["backup_strategy"])
        d["link_strategy"] = dict(d["link_strategy"])
        d["retention"] = dict(d["retention"])
        d["enabled"] = True
        return d

    def resolve(self, task) -> dict:
        """解析任务的生效保护策略。

        Args:
            task: 任务 dict（来自 models.get_task）或任意含 policy_id / protection_level
                  属性的对象。

        Returns:
            生效策略 dict：优先返回任务显式绑定的 ProtectionPolicy；
            其次按任务的 protection_level 取默认；
            若均无，回退到 general 默认策略。
        """
        if isinstance(task, dict):
            policy_id = task.get("policy_id")
            level = task.get("protection_level")
        else:
            policy_id = getattr(task, "policy_id", None)
            level = getattr(task, "protection_level", None)

        # 1) 任务显式绑定的策略优先
        if policy_id:
            pol = models.get_protection_policy(policy_id)
            if pol:
                return pol

        # 2) 否则按保护等级取默认
        if not level or level not in self.LEVELS:
            level = "general"
        return self.default_policy(level)

    def resolve_rpo_rto(self, task) -> (int, int):
        """便捷方法：返回 (RPO 分钟, RTO 分钟)。"""
        pol = self.resolve(task)
        return int(pol.get("rpo_target_min") or 0), int(pol.get("rto_target_min") or 0)

    def is_valid_level(self, level: Optional[str]) -> bool:
        return level in self.LEVELS


# 便捷单例，供调度/复制等处直接调用
policy_service = ProtectionPolicyService()
