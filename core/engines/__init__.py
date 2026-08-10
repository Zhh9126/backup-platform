# -*- coding: utf-8 -*-
"""数据库备份引擎注册表：集中注册各类型引擎，供调度器与 API 按 db_type 获取。

适配层契约（AdapterContract）
----------------------------
所有引擎（无论"核心库自研"还是"外围 API 集成"）向上统一暴露以下 5 类方法，
屏蔽底层差异，供上层服务门面（Phase2 service_facade）统一调用：
    1. backup(backup_type)        -> BackupResult      # 备份
    2. restore(backup_path, ...)  -> BackupResult      # 恢复
    3. clone_to_test(...)         -> BackupResult      # 克隆/拉起测试库（VDB）
    4. verify(backup_path)        -> dict              # 校验（完整性/可恢复性）
    5. list_sets()                -> list              # 列出该任务的备份集
具体签名见 base.BackupEngine；本 Phase 仅在基类补充 synthesize_full()/list_sets() 契约，
其余方法在后续 Phase 逐步落地实现。
"""
from typing import Protocol

from core.engines.base import (BackupEngine, BackupType, BackupMode,
                               BackupStatus, BackupResult)
from core.engines.mysql import MySQLEngine
from core.engines.mariadb import MariaDBEngine
from core.engines.postgresql import PostgreSQLEngine
from core.engines.oracle import OracleEngine
from core.engines.kingbase import KingbaseEngine
from core.engines.dameng import DamengEngine
from core.engines.redis import RedisEngine
from core.engines.mongodb import MongoEngine
from core.engines.file import FileBackupEngine

ENGINE_REGISTRY = {
    "mysql": MySQLEngine,
    "mariadb": MariaDBEngine,
    "postgresql": PostgreSQLEngine,
    "oracle": OracleEngine,
    "kingbase": KingbaseEngine,
    "dameng": DamengEngine,
    "redis": RedisEngine,
    "mongodb": MongoEngine,
    "file": FileBackupEngine,
}

ENGINE_DISPLAY = {k: cls.display_name for k, cls in ENGINE_REGISTRY.items()}

# ------------------------- 适配层分级（Adapter Tier） -------------------------
# core_self      : 核心库（信创）以自研适配器为主，强同步/准同步、物理备份能力完备
# peripheral_api : 外围引擎以 API 集成封装为主（逻辑导出 + 远程调用）
_CORE_SELF = ("oracle", "kingbase", "dameng")
_PERIPHERAL_API = ("mysql", "mariadb", "postgresql", "redis", "mongodb", "file")
for _name in _CORE_SELF:
    if _name in ENGINE_REGISTRY:
        ENGINE_REGISTRY[_name].adapter_tier = "core_self"
for _name in _PERIPHERAL_API:
    if _name in ENGINE_REGISTRY:
        ENGINE_REGISTRY[_name].adapter_tier = "peripheral_api"


def get_adapter_tier(db_type: str) -> str:
    """返回指定 db_type 的适配层分级；未知类型默认归为外围 API 集成。"""
    cls = ENGINE_REGISTRY.get(db_type)
    return getattr(cls, "adapter_tier", "peripheral_api") if cls else "peripheral_api"


class AdapterContract(Protocol):
    """适配层统一契约（5 类方法签名，供服务门面与类型检查引用）。"""

    def backup(self, backup_type: BackupType) -> BackupResult: ...

    def restore(self, backup_path: str, **kwargs) -> BackupResult: ...

    def clone_to_test(self, **kwargs) -> BackupResult: ...

    def verify(self, backup_path: str) -> dict: ...

    def list_sets(self) -> list: ...


def get_engine(db_type: str, task: dict, storage_root: str, logger=None):
    cls = ENGINE_REGISTRY.get(db_type)
    if not cls:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    return cls(task, storage_root, logger)


def supported_types() -> list:
    return list(ENGINE_REGISTRY.keys())


def synthesize_full_for_task(task_id: int, target_storage_tier: int = None,
                             logger=None) -> list:
    """遍历任务的 BackupSet 增量链，合并为合成全量。

    对每个"全量/合成全量"基集，收集其后续增量（parent_set_id 指向它且
    set_type=incremental），调用对应引擎的 synthesize_full() 完成合并，
    并登记一个新的 set_type=synthetic_full 备份集（parent_set_id 指向链头）。
    返回新生成的合成全量 BackupSet id 列表。
    """
    import config
    import core.models as models
    task = models.get_task(task_id)
    if not task:
        return []
    engine = get_engine(task["db_type"], task, config.BACKUP_ROOT, logger)
    sets = models.list_backup_sets(task_id=task_id)
    base_sets = [s for s in sets if s.get("set_type") in ("full", "synthetic_full")]
    new_ids = []
    for base in base_sets:
        chain = [base] + [s for s in sets
                          if s.get("parent_set_id") == base["id"]
                          and s.get("set_type") == "incremental"]
        if len(chain) < 2:
            continue  # 无增量可合并
        res = engine.synthesize_full(sets=chain,
                                     target_storage_tier=target_storage_tier,
                                     target_record_id=base.get("record_id"))
        if res.success and res.backup_path:
            new_id = models.create_backup_set({
                "task_id": task_id,
                "record_id": base.get("record_id"),
                "set_type": "synthetic_full",
                "storage_tier": target_storage_tier or base.get("storage_tier", 1),
                "object_key": res.backup_path,
                "parent_set_id": base["id"],
                "verified": 1 if res.verified else 0,
                "size_bytes": res.size_bytes or 0,
                "checksum": res.checksum or "",
            })
            new_ids.append(new_id)
            if logger:
                logger.info("[synthesize] task=%s 合成全量 #%s (合并 %d 个增量)",
                            task_id, new_id, len(chain) - 1)
    return new_ids
