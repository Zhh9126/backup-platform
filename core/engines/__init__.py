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
from core.engines.opengauss import OpenGaussEngine
from core.engines.panwei import PanweiEngine
from core.engines.oracle import OracleEngine
from core.engines.kingbase import KingbaseEngine
from core.engines.dameng import DamengEngine
from core.engines.sqlserver import SQLServerEngine
from core.engines.redis import RedisEngine
from core.engines.mongodb import MongoEngine
from core.engines.neo4j import Neo4jEngine
from core.engines.file import FileBackupEngine
from core.engines.object_storage import ObjectStorageEngine
from core.engines.synology import SynologyEngine
from core.engines.qdrant import QdrantEngine
from core.engines.chroma import ChromaEngine
from core.engines.highgo import HighGoEngine
from core.engines.gaussdb import GaussDBEngine
from core.engines.vastbase import VastbaseEngine
from core.engines.greatdb import GreatDBEngine
from core.engines.goldendb import GoldenDBEngine
from core.engines.jdbc_logical import JDBCLogicalEngine
from core.engines.uxdb import UXDBEngine
from core.engines.tidb import TiDBEngine
from core.engines.oceanbase import OceanBaseEngine


def _register_vm_engine():
    """惰性注册虚拟机备份引擎。

    为什么不用顶层 import：core.vm.engine 依赖 core.engines.base，
    而本模块（core.engines.__init__）在 import core.vm 时又会回来
    触发本模块加载 → 循环导入。这里改为运行时按需绑定，首次用到才加载。
    """
    if "vm" in ENGINE_REGISTRY:
        return ENGINE_REGISTRY.get("vm")
    try:
        from core.vm.engine import VMBackupEngine
    except Exception:
        return None
    ENGINE_REGISTRY["vm"] = VMBackupEngine
    ENGINE_DISPLAY["vm"] = VMBackupEngine.display_name
    VMBackupEngine.adapter_tier = "peripheral_api"
    return VMBackupEngine


ENGINE_REGISTRY = {
    "mysql": MySQLEngine,
    "mariadb": MariaDBEngine,
    "postgresql": PostgreSQLEngine,
    "oracle": OracleEngine,
    "kingbase": KingbaseEngine,
    "opengauss": OpenGaussEngine,
    "panwei": PanweiEngine,
    "dameng": DamengEngine,
    "sqlserver": SQLServerEngine,
    "redis": RedisEngine,
    "mongodb": MongoEngine,
    "neo4j": Neo4jEngine,
    "file": FileBackupEngine,
    "object_storage": ObjectStorageEngine,
    "synology": SynologyEngine,
    "qdrant": QdrantEngine,
    "chroma": ChromaEngine,
    # 国产数据库协议兼容层（PG 系 / MySQL 系复用既有引擎）
    "highgo": HighGoEngine,
    "uxdb": UXDBEngine,
    "tidb": TiDBEngine,
    "oceanbase": OceanBaseEngine,
    # 2026-11-13：从「模板目录」提升为内置引擎（复用 PG/MySQL 工具族）
    "gaussdb": GaussDBEngine,
    "vastbase": VastbaseEngine,
    "greatdb": GreatDBEngine,
    "goldendb": GoldenDBEngine,
    # 2026-11-13：通用 JDBC 逻辑备份（长尾库型兜底：Db2/HANA/Vertica/Teradata 等）
    "jdbc_generic": JDBCLogicalEngine,
}

ENGINE_DISPLAY = {k: cls.display_name for k, cls in ENGINE_REGISTRY.items()}
ENGINE_DISPLAY["vm"] = "虚拟机"

# ------------------------- 适配层分级（Adapter Tier） -------------------------
# core_self      : 核心库（信创）以自研适配器为主，强同步/准同步、物理备份能力完备
# peripheral_api : 外围引擎以 API 集成封装为主（逻辑导出 + 远程调用）
_CORE_SELF = ("oracle", "kingbase", "dameng")
_PERIPHERAL_API = ("mysql", "mariadb", "postgresql", "sqlserver",
                   "redis", "mongodb", "neo4j", "file", "vm", "object_storage", "synology",
                   "qdrant", "chroma", "highgo", "uxdb", "tidb", "oceanbase")
for _name in _CORE_SELF:
    if _name in ENGINE_REGISTRY:
        ENGINE_REGISTRY[_name].adapter_tier = "core_self"
for _name in _PERIPHERAL_API:
    if _name in ENGINE_REGISTRY:
        ENGINE_REGISTRY[_name].adapter_tier = "peripheral_api"


def get_adapter_tier(db_type: str) -> str:
    """返回指定 db_type 的适配层分级；未知类型默认归为外围 API 集成。"""
    cls = ENGINE_REGISTRY.get(db_type)
    if cls:
        return getattr(cls, "adapter_tier", "peripheral_api")
    # 适配器动态注册（custom_api）—— 走 db_adapters 兼容查询
    try:
        from core import db_adapters
        spec = db_adapters.get_by_dbtype(db_type, include_disabled=True)
        if spec:
            return "custom_api"
    except Exception:
        pass
    return "peripheral_api"


def supported_types() -> list:
    """返回所有可用数据库类型键：内置 + 已启用的自定义适配器。"""
    out = list(ENGINE_REGISTRY.keys())
    try:
        from core import db_adapters
        for t in db_adapters.enabled_types():
            if t not in out:
                out.append(t)
    except Exception:
        pass
    return out


# 内置引擎能力覆盖：engine_meta_map 的默认值偏乐观（全部 True），
# 部分引擎并不支持物理备份/全实例/同步，这里按各引擎**真实能力**逐项声明，
# 供前端下拉与能力提示诚实展示（新增类型时请同步登记）。
_ENGINE_META_OVERRIDE = {
    "panwei": {
        "icon": "bi-hdd-network",
        "description": "磐维 PanWeiDB（openGauss 系，中国移动 × 华为）：gs_dump/gs_restore/gsql/gs_basebackup；"
                       "集中式与分布式均适用（分布式经 CN 协调器导出）；暂不支持数据同步",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True,
        "supports_full_instance": True,
        "supports_sync": False,
    },
    "neo4j": {
        "icon": "bi-diagram-3",
        "description": "图数据库：离线 dump / 企业版在线 backup / APOC 在线导出 三通道",
        "backup_modes": ["logical"],
        # 增量仅企业版「在线 backup」通道原生支持（--type=DIFF），
        # 其余通道由引擎诚实回退为全量并给出说明。
        "supports_incremental": True,
        "supports_full_instance": False,
        "supports_sync": False,
    },
    "synology": {
        "icon": "bi-hdd-network",
        "category": "storage",
        "description": "NAS 存储（群晖 DSM）：FileStation Web API 文件级全量 + "
                       "size/mtime 增量，HTTP Range 断点续传，DSM 侧零安装；"
                       "恢复支持回推 DSM 共享目录（FileStation.Upload + 回读大小校验）；"
                       "SAN Manager 裸 LUN 不直接读取，iSCSI 场景请备份业务主机挂载点",
        "backup_modes": ["logical"],
        "supports_incremental": True,
        "supports_full_instance": False,
        "supports_sync": False,
    },
    "object_storage": {
        "icon": "bi-cloud-arrow-up",
        "description": "对象存储桶级保护：MinIO / 阿里云 OSS / 腾讯云 COS / S3 兼容；"
                       "对象级索引 + 永远增量 + 版本捕获，桶侧零安装",
        "backup_modes": ["logical"],
        "supports_incremental": True,
        "supports_full_instance": False,
        "supports_sync": False,
    },
    # ---- 2026-11-13：按各引擎**真实能力**补齐（此前全部落到乐观默认值，
    #      「数据库类型」页面把 redis/file/mongodb 也标成「物理 + 全实例 + 增量」）----
    "mysql": {
        "icon": "bi-database-fill",
        "description": "mysqldump 逻辑 + XtraBackup 物理（免安装推送），支持增量/全实例/数据同步",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "mariadb": {
        "icon": "bi-database-fill",
        "description": "mysqldump/mariabackup，MariaDB 10.x 全版本；支持增量/全实例/同步",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "postgresql": {
        "icon": "bi-database-fill",
        "description": "pg_dump 逻辑 + pg_basebackup 物理，支持 PITR 与数据同步",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "oracle": {
        "icon": "bi-database-fill-exclamation",
        "description": "expdp 逻辑 + RMAN 物理（11g/19c），LogMiner 实时与事务级还原",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "kingbase": {
        "icon": "bi-database-fill-gear",
        "description": "金仓 KingbaseES：sys_dump 逻辑 + sys_basebackup 物理（V8/V9 全版本）",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "opengauss": {
        "icon": "bi-database-fill-gear",
        "description": "openGauss：gs_dump 逻辑 + gs_basebackup 物理；物理增量未实现（诚实回退全量）",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": False, "supports_full_instance": True, "supports_sync": True,
    },
    "dameng": {
        "icon": "bi-database-fill-gear",
        "description": "达梦 DM8：dexp/dimp 逻辑 + dmrman 物理；支持实时（归档日志）",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "sqlserver": {
        "icon": "bi-database-fill",
        "description": "SQL Server：BACKUP DATABASE 物理（SSH）+ JDBC 逻辑（免 SSH）/ 差异 / 日志",
        "backup_modes": ["logical", "physical"],
        "supports_incremental": True, "supports_full_instance": True, "supports_sync": True,
    },
    "redis": {
        "icon": "bi-lightning-charge",
        "description": "Redis RDB 快照（redis-cli --rdb），支持跨主机恢复；无增量/全实例语义",
        "backup_modes": ["logical"],
        "supports_incremental": False, "supports_full_instance": False, "supports_sync": False,
    },
    "mongodb": {
        "icon": "bi-node-plus",
        "description": "MongoDB：mongodump --archive 归档 + mongorestore",
        "backup_modes": ["logical"],
        "supports_incremental": False, "supports_full_instance": True, "supports_sync": False,
    },
    "file": {
        "icon": "bi-folder2-open",
        "description": "文件/目录备份：tar 打包 + 增量 + 源端去重 + 镜像恢复（Windows 通道同名）",
        "backup_modes": ["logical"],
        "supports_incremental": True, "supports_full_instance": False, "supports_sync": False,
    },
    "qdrant": {
        "icon": "bi-boxes",
        "description": "向量库 Qdrant：snapshot API 全量快照 + 恢复",
        "backup_modes": ["logical"],
        "supports_incremental": False, "supports_full_instance": False, "supports_sync": False,
    },
    "chroma": {
        "icon": "bi-boxes",
        "description": "向量库 Chroma：集合级导出/导入（HTTP API）",
        "backup_modes": ["logical"],
        "supports_incremental": False, "supports_full_instance": False, "supports_sync": False,
    },
    "highgo": {
        "icon": "bi-database-fill-gear",
        "description": "瀚高 HighGo（PG 协议兼容层）：pg_dump/pg_restore 通道",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "uxdb": {
        "icon": "bi-database-fill-gear",
        "description": "优炫 UXDB（PG 协议兼容层）：pg_dump/pg_restore 通道",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "tidb": {
        "icon": "bi-diagram-2-fill",
        "description": "TiDB（MySQL 协议兼容层）：mysqldump 逻辑；物理需官方 br（未接入）",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "oceanbase": {
        "icon": "bi-diagram-2-fill",
        "description": "OceanBase MySQL 租户（协议兼容层）：mysqldump 逻辑",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    # ---- 2026-11-13：从模板目录提升为内置引擎（复用 PG/MySQL 工具族）----
    "gaussdb": {
        "icon": "bi-database-fill-gear",
        "description": "GaussDB（华为，openGauss 血缘）：gs_dump / gs_restore / gs_basebackup 通道",
        "backup_modes": ["logical", "physical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "vastbase": {
        "icon": "bi-database-fill-gear",
        "description": "Vastbase（海量数据，PG 系）：pg_dump / pg_restore 通道",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "greatdb": {
        "icon": "bi-database-fill",
        "description": "GreatDB（万里，MySQL 协议兼容）：mysqldump / mysql 通道",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
    "jdbc_generic": {
        "icon": "bi-plug",
        "description": "通用 JDBC 逻辑备份（长尾库型兜底）：JDBC 元数据 + SELECT 导出为 JSONL，回放建表与批量 INSERT；需平台侧 JRE + 驱动 jar",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": False, "supports_sync": False,
    },
    "goldendb": {
        "icon": "bi-diagram-2-fill",
        "description": "GoldenDB（中兴，MySQL 协议兼容，分布式）：按 CN/Proxy 入口 mysqldump 逻辑备份",
        "backup_modes": ["logical"], "supports_incremental": False,
        "supports_full_instance": True, "supports_sync": True,
    },
}


def engine_meta_map() -> dict:
    """返回 db_type → 元信息，给前端下拉/分类展示用。"""
    import config
    out = {}
    for t, cls in ENGINE_REGISTRY.items():
        out[t] = {
            "db_type": t,
            "display_name": getattr(cls, "display_name", t),
            "category": "builtin",
            "icon": "bi-hdd-stack",
            "default_port": config.DEFAULT_PORTS.get(t),
            "description": "",
            "backup_modes": ["logical", "physical"],
            "supports_incremental": True,
            "supports_full_instance": True,
            "supports_sync": True,
            "adapter_tier": getattr(cls, "adapter_tier", "peripheral_api"),
            "builtin": True,
            "source": "builtin",
        }
        out[t].update(_ENGINE_META_OVERRIDE.get(t) or {})
    try:
        from core import db_adapters
        for spec in db_adapters.list_adapters(include_disabled=False):
            out[spec["db_type"]] = db_adapters.engine_meta(spec)
    except Exception:
        pass
    return out


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
        # 虚拟机引擎：core.vm 与本模块互相依赖，只能运行时惰性注册。
        # 必须在这里兜住——调度器定时触发、手动备份、还原/克隆/恢复验证
        # 全部经由本函数取引擎，漏掉会导致 db_type='vm' 的任务一调度就抛
        # 「不支持的数据库类型」。
        if db_type == "vm":
            cls = _register_vm_engine()
    if not cls:
        # 适配器未注册 → 尝试惰性注册（动态类型首次调度时）
        try:
            from core import db_adapters
            if db_adapters.ensure_registered(db_type):
                cls = ENGINE_REGISTRY.get(db_type)
        except Exception:
            pass
    if not cls:
        # 2026-11-13：长尾库型兜底——只要 core.jdbc 里有驱动配置（Db2 / ClickHouse /
        # YashanDB / TDengine …），就用通用 JDBC 逻辑引擎接住，而不是直接报
        # 「不支持的数据库类型」。这样"上传一张 jar 就能备份任意关系型库"成立。
        try:
            from core import jdbc as _jdbc
            if str(db_type).lower() in {t.lower() for t in _jdbc.effective_db_types()}:
                cls = JDBCLogicalEngine
                ENGINE_REGISTRY.setdefault(str(db_type).lower(), JDBCLogicalEngine)
                ENGINE_DISPLAY.setdefault(str(db_type).lower(),
                                          "%s（JDBC 逻辑）" % str(db_type).upper())
        except Exception:
            pass
    if not cls:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    return cls(task, storage_root, logger)


# supported_types() 已在引擎注册动态化改造中上移到模块中部（含适配器合并）。
# 保留旧位置仅为兼容引用——> 真正的实现已在前文。


def synthesize_full_for_task(task_id: int, target_storage_tier: int = None,
                             logger=None) -> list:
    """遍历任务的 BackupSet 增量链，合并为合成全量。

    对每个"全量/合成全量"基集，收集其后续增量（parent_set_id 指向它且
    set_type=incremental），调用对应引擎的 synthesize_full() 完成合并，
    并登记一个新的 set_type=synthetic_full 备份集（parent_set_id 指向链头）。

    永久增量链闭环（参考 CDM 设计）：
    - 合成全量继承链头的 chain_id（无则生成新链），使整条"永远增量"链可追溯；
    - 合并完成后，被覆盖的中间增量备份集标记 chain_status='merged'，
      交由副本生命周期策略统一回收（合成后中间副本释放）；
    - 估算 dedup_saved_bytes = Σ增量大小 − 合成全量大小（合成即去重收益）。
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
            # 永久增量链：沿用链头 chain_id，无则新建
            chain_id = base.get("chain_id") or (
                "chn_%s_%d" % (task_id, int(base["id"])))
            syn_size = res.size_bytes or 0
            inc_sum = sum(int(s.get("size_bytes") or 0)
                          for s in chain if s.get("set_type") == "incremental")
            dedup_saved = max(inc_sum - syn_size, 0)

            # 合成后做真实可恢复校验（鼎甲迪备 §3.2 强调"合成产物可直接挂载即时恢复"）：
            # 用引擎自带的 verify_record 对合成产物做存在性/完整性/checksum 校验，
            # 失败则合成全量不可信，标记为未通过，避免静默造假。
            verified = 0
            verify_msg = ""
            try:
                rec = {
                    "backup_path": res.backup_path,
                    "checksum": res.checksum or "",
                    "db_type": task.get("db_type"),
                    "size_bytes": syn_size,
                }
                vres = engine.verify_record(rec, options={})
                verified = 1 if vres.success else 0
                verify_msg = vres.message or ""
            except Exception as e:
                verified = 0
                verify_msg = f"合成后校验异常: {e}"

            # 真实合并 vs 逻辑重链：用 chain_status 区分，前端可诚实展示
            chain_status = "synthesized_real" if not res.simulated else "synthesized_sim"

            new_id = models.create_backup_set({
                "task_id": task_id,
                "record_id": base.get("record_id"),
                "set_type": "synthetic_full",
                "storage_tier": target_storage_tier or base.get("storage_tier", 1),
                "object_key": res.backup_path,
                "parent_set_id": base["id"],
                "verified": verified,
                "size_bytes": syn_size,
                "dedup_saved_bytes": dedup_saved,
                "checksum": res.checksum or "",
                "chain_id": chain_id,
                "chain_status": chain_status,
            })
            # 标记被合并的中间增量：副本生命周期可回收
            for s in chain:
                if s.get("set_type") == "incremental":
                    models.update_backup_set(
                        s["id"],
                        {"chain_id": chain_id, "chain_status": "merged"})
            if logger:
                mode = "物理合并" if not res.simulated else "逻辑重链(缺客户端)"
                logger.info(
                    "[synthesize] task=%s 合成全量 %s (%s, 合并 %d 个增量, "
                    "去重 %.1fMB, 校验%s)",
                    task_id, new_id, mode, len(chain) - 1,
                    dedup_saved / 1048576.0, "通过" if verified else "未通过")
            new_ids.append(new_id)
    return new_ids
