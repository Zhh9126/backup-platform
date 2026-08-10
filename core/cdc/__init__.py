# -*- coding: utf-8 -*-
"""
T03/T06 数据库 CDC 守护包：工厂 + 能力探测。

选择策略（MySQL binlog / PostgreSQL WAL / 信创三库 / 仿真兜底）::

    DEMO_MODE=on 或 task.demo_only          → SimulatedCDCDaemon
    db_type ∈ {mysql, mariadb} 且客户端可用 → MySQLBinlogDaemon
    db_type = postgresql   且客户端可用     → PostgresWALDaemon
    db_type = oracle       且驱动可用       → OracleLogMinerDaemon   (T06)
    db_type = kingbase     且客户端可用     → KingbaseWALDaemon      (T06)
    db_type = dameng       且驱动可用       → DamengLogMnrDaemon     (T06)
    其余 / 任何前置不满足                    → SimulatedCDCDaemon + degrade_reason

**任何降级都不报错**，只把原因写进 ``daemon.degrade_reason``，最终落到
``rt_capture_state.degrade_reason`` 并在 UI 上以黄色提示展示。

故障隔离（T06 硬要求）
----------------------
三个信创实现走 ``try/except Exception`` **逐个隔离导入**：任一模块存在语法错误
或驱动导入副作用异常，都只会让该引擎回落仿真，
**不会影响本包 import、不会影响 MySQL / PostgreSQL 既有能力**。
导入失败原因记录在 :data:`ENGINE_IMPORT_ERRORS`，并通过
:func:`probe_clients` 暴露到自检面板。
"""
from __future__ import annotations

from typing import Dict, Tuple

import config
import core.db as db

from .base import CDCDaemon
from .mysql_binlog import MySQLBinlogDaemon, _import_mysql_replication
from .pg_wal import PostgresWALDaemon, _import_psycopg2
from .simulated import SimulatedCDCDaemon

# ----------------------------------------------------------------------
# T06 信创三库：隔离导入（任一模块出错都不得影响本包与既有引擎）
# ----------------------------------------------------------------------
#: engine_name -> 导入失败原因（导入成功的引擎不会出现在此字典中）
ENGINE_IMPORT_ERRORS: Dict[str, str] = {}

try:
    from .oracle_logminer import (OracleLogMinerDaemon,  # noqa: F401
                                  probe_oracle_driver)
except Exception as _exc:  # pragma: no cover - 仅在实现文件损坏时触发
    OracleLogMinerDaemon = None  # type: ignore[assignment]
    ENGINE_IMPORT_ERRORS["oracle"] = f"Oracle LogMiner 实现加载失败: {_exc}"

    def probe_oracle_driver() -> dict:  # type: ignore[misc]
        """占位探测：实现模块加载失败时返回不可用。"""
        return {"installed": False,
                "reason": ENGINE_IMPORT_ERRORS.get("oracle", ""),
                "hint": "请检查 core/cdc/oracle_logminer.py 是否损坏"}

try:
    from .kingbase_wal import (KingbaseWALDaemon,  # noqa: F401
                               probe_kingbase_driver)
except Exception as _exc:  # pragma: no cover
    KingbaseWALDaemon = None  # type: ignore[assignment]
    ENGINE_IMPORT_ERRORS["kingbase"] = f"KingbaseES WAL 实现加载失败: {_exc}"

    def probe_kingbase_driver() -> dict:  # type: ignore[misc]
        """占位探测：实现模块加载失败时返回不可用。"""
        return {"installed": False,
                "reason": ENGINE_IMPORT_ERRORS.get("kingbase", ""),
                "hint": "请检查 core/cdc/kingbase_wal.py 是否损坏"}

try:
    from .dameng_logmnr import (DamengLogMnrDaemon,  # noqa: F401
                                probe_dameng_driver)
except Exception as _exc:  # pragma: no cover
    DamengLogMnrDaemon = None  # type: ignore[assignment]
    ENGINE_IMPORT_ERRORS["dameng"] = f"达梦 DM_LOGMNR 实现加载失败: {_exc}"

    def probe_dameng_driver() -> dict:  # type: ignore[misc]
        """占位探测：实现模块加载失败时返回不可用。"""
        return {"installed": False,
                "reason": ENGINE_IMPORT_ERRORS.get("dameng", ""),
                "hint": "请检查 core/cdc/dameng_logmnr.py 是否损坏"}

__all__ = [
    "CDCDaemon", "MySQLBinlogDaemon", "PostgresWALDaemon", "SimulatedCDCDaemon",
    "OracleLogMinerDaemon", "KingbaseWALDaemon", "DamengLogMnrDaemon",
    "CDC_REGISTRY", "ENGINE_DAEMON_MAP", "ENGINE_IMPORT_ERRORS",
    "create_daemon", "probe_clients", "supported_engines",
]

# 实现注册表（key = engine_key）
CDC_REGISTRY = {
    MySQLBinlogDaemon.engine_key: MySQLBinlogDaemon,
    PostgresWALDaemon.engine_key: PostgresWALDaemon,
    SimulatedCDCDaemon.engine_key: SimulatedCDCDaemon,
}

# db_type → 首选实现
ENGINE_DAEMON_MAP = {
    "mysql": MySQLBinlogDaemon,
    "mariadb": MySQLBinlogDaemon,
    "postgresql": PostgresWALDaemon,
}

# T06：把成功导入的信创实现注册进来（导入失败的自然缺席 → 走仿真兜底）
for _engine, _cls in (("oracle", OracleLogMinerDaemon),
                      ("kingbase", KingbaseWALDaemon),
                      ("dameng", DamengLogMnrDaemon)):
    if _cls is not None:
        CDC_REGISTRY[_cls.engine_key] = _cls
        ENGINE_DAEMON_MAP[_engine] = _cls


def supported_engines() -> tuple:
    """本期支持真实日志流捕获的 db_type 列表。"""
    return tuple(ENGINE_DAEMON_MAP.keys())


def create_daemon(task: dict, rt_config, repo, logger=None) -> CDCDaemon:
    """按任务与环境创建最合适的 CDC 守护。

    Args:
        task: backup_tasks 行（含明文密码）。
        rt_config: :class:`core.rt_backup.types.RtConfig`。
        repo: :class:`core.rt_backup.repo.LogRepository`。
        logger: 日志器。

    Returns:
        已构造但**未启动**的 CDCDaemon。永不抛异常——任何不满足条件的情形
        都降级为 :class:`SimulatedCDCDaemon` 并写入 ``degrade_reason``。
    """
    logger = logger or db.get_logger("rt.cdc")
    task = dict(task or {})
    engine = (task.get("db_type") or "").lower()
    task_id = task.get("id")

    def _simulated(reason: str) -> CDCDaemon:
        daemon = SimulatedCDCDaemon(task, rt_config, repo, logger=logger)
        if reason:
            daemon.degrade_reason = reason
            logger.info("[rt.cdc] task=%s 使用仿真日志流: %s", task_id, reason)
        return daemon

    # 1) 演示模式：强制仿真，绝不连真实库
    if config.DEMO_MODE == "on":
        return _simulated("DEMO_MODE=on，使用仿真日志流")
    if task.get("demo_only"):
        return _simulated("任务标记为演示（demo_only）")

    # 2) 显式配置 rt_mode=sample 时直接仿真（用于压测 / 演练）
    mode = (getattr(rt_config, "mode", "") or "auto").strip().lower()
    if mode == "sample":
        return _simulated("rt_mode=sample，按配置使用仿真日志流")

    # 3) 按引擎选择真实实现
    daemon_cls = ENGINE_DAEMON_MAP.get(engine)
    if daemon_cls is None:
        import_error = ENGINE_IMPORT_ERRORS.get(engine)
        if import_error:
            # 实现文件损坏：只影响该引擎，其余引擎照常工作（故障隔离）
            return _simulated(f"{import_error}，已降级为仿真日志流")
        return _simulated(
            f"引擎 {engine or '未知'} 暂不支持真实日志流捕获，已降级为仿真日志流")

    ok, reason = daemon_cls.is_available(task)
    if not ok:
        return _simulated(reason)

    daemon = daemon_cls(task, rt_config, repo, logger=logger)
    logger.info("[rt.cdc] task=%s 使用 %s", task_id, daemon.display_name)
    return daemon


def probe_clients() -> dict:
    """环境自检：各 CDC 客户端与可选 Python 包是否可用。

    供「实时备份」页面的环境自检面板与 ``GET /api/rt/capabilities`` 使用。
    本函数不接受任务参数，只探测进程级能力，绝不抛异常。
    """
    candidates = [MySQLBinlogDaemon, PostgresWALDaemon]
    candidates += [cls for cls in (OracleLogMinerDaemon, KingbaseWALDaemon,
                                   DamengLogMnrDaemon) if cls is not None]
    candidates.append(SimulatedCDCDaemon)

    implementations = []
    for cls in candidates:
        try:
            ok, reason = cls.check_client()
        except Exception as exc:  # pragma: no cover - 探测本身绝不能炸自检面板
            ok, reason = False, f"能力探测异常: {exc}"
        implementations.append({
            "key": cls.engine_key,
            "name": cls.display_name,
            "available": bool(ok),
            "reason": "" if ok else reason,
            "clients": list(cls.required_clients),
            "is_simulated": cls.is_simulated,
        })
    # 加载失败的实现也要在自检面板中可见，给出可操作原因
    for engine, message in ENGINE_IMPORT_ERRORS.items():
        implementations.append({
            "key": f"{engine}_unavailable",
            "name": f"{engine} CDC 实现（加载失败）",
            "available": False,
            "reason": message,
            "clients": [],
            "is_simulated": False,
        })

    replication_lib, rep_reason = _import_mysql_replication()
    psycopg2_mod, pg_reason = _import_psycopg2()

    optional_packages = {
        "mysql-replication": {
            "installed": replication_lib is not None,
            "reason": rep_reason,
            "hint": ("" if replication_lib is not None
                     else "pip install mysql-replication 可启用位点精确探测"),
        },
        "psycopg2": {
            "installed": psycopg2_mod is not None,
            "reason": pg_reason,
            "hint": ("" if psycopg2_mod is not None
                     else "pip install psycopg2-binary 可启用精确 LSN 探测"),
        },
    }
    # T06：信创驱动自检（拍板 Q6：只给文字指引，不做安装向导页）
    for name, prober in (("oracledb", probe_oracle_driver),
                         ("ksycopg2", probe_kingbase_driver),
                         ("dmPython", probe_dameng_driver)):
        try:
            optional_packages[name] = prober()
        except Exception as exc:  # pragma: no cover
            optional_packages[name] = {
                "installed": False, "reason": f"驱动探测异常: {exc}", "hint": ""}

    return {
        "demo_mode": config.DEMO_MODE,
        "supported_engines": list(supported_engines()),
        # T06 起 Oracle / Kingbase / Dameng 已具备真实 CDC 实现，不再排期后置
        "deferred_engines": [],
        "engine_import_errors": dict(ENGINE_IMPORT_ERRORS),
        "implementations": implementations,
        "optional_packages": optional_packages,
    }
