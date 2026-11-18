# -*- coding: utf-8 -*-
"""通用 JDBC 逻辑引擎 + 国产库内置化回归（2026-11-13）。

本轮三件事的代码契约：
1. GaussDB / Vastbase / GreatDB / GoldenDB 从"模板"提升为**内置引擎**
   （复用 PG / MySQL / openGauss 工具族）；
2. 通用 JDBC 逻辑备份引擎 `jdbc_generic` 落地：只要 core.jdbc 有驱动配置
   （Db2 / ClickHouse / YashanDB / TDengine …），get_engine 就不再报
   "不支持的数据库类型"，而是用 JDBC 引擎兜底；
3. JDBC 恢复必须按方言引用标识符 —— MySQL 系反引号、其余双引号
   （真机教训：统一双引号在 MySQL 上恢复直接语法错误）。
"""
from core import jdbc, remote_dump
from core.engines import ENGINE_REGISTRY, engine_meta_map, get_engine
from core.engines.goldendb import GoldenDBEngine
from core.engines.gaussdb import GaussDBEngine
from core.engines.greatdb import GreatDBEngine
from core.engines.jdbc_logical import JDBCLogicalEngine
from core.engines.vastbase import VastbaseEngine


def test_four_types_promoted_to_builtin_engines():
    assert ENGINE_REGISTRY["gaussdb"] is GaussDBEngine
    assert ENGINE_REGISTRY["vastbase"] is VastbaseEngine
    assert ENGINE_REGISTRY["greatdb"] is GreatDBEngine
    assert ENGINE_REGISTRY["goldendb"] is GoldenDBEngine
    meta = engine_meta_map()
    for t in ("gaussdb", "vastbase", "greatdb", "goldendb"):
        assert meta.get(t), f"{t} 缺少能力元信息"
        assert meta[t].get("builtin") is True


def test_tool_family_mapping_present():
    """远端工具族/OS 用户别名必须覆盖新库型，否则备份跑起来才报 127。"""
    pg = remote_dump._PG_FAMILY_TOOLING
    assert pg["gaussdb"]["dump_tool"] == "gs_dump"
    assert pg["vastbase"]["dump_tool"] == "pg_dump"
    assert pg["gaussdb"]["default_port"] == 8000
    assert pg["vastbase"]["default_port"] == 5432


def test_jdbc_driver_config_has_four_types():
    types = set(jdbc.effective_db_types())
    for t in ("gaussdb", "vastbase", "greatdb", "goldendb"):
        assert t in types, f"{t} 缺 JDBC 驱动配置"


def test_jdbc_engine_registered_as_builtin():
    assert ENGINE_REGISTRY["jdbc_generic"] is JDBCLogicalEngine
    meta = engine_meta_map()["jdbc_generic"]
    assert meta["backup_modes"] == ["logical"]


def test_get_engine_falls_back_to_jdbc_for_long_tail():
    """Db2 没有内置引擎但有 JDBC 驱动 → 必须落到通用 JDBC 引擎而非报错。"""
    eng = get_engine("db2", {"id": 1, "db_type": "db2", "host": "127.0.0.1",
                             "port": 50000, "db_name": "SAMPLE"}, "/tmp")
    assert isinstance(eng, JDBCLogicalEngine)
    assert eng._driver_type() == "db2"


def test_identifier_quoting_by_dialect():
    mysql_eng = JDBCLogicalEngine({"db_type": "mysql", "host": "h"}, "/tmp")
    assert mysql_eng._qi("t1") == "`t1`"
    pg_eng = JDBCLogicalEngine({"db_type": "postgresql", "host": "h"}, "/tmp")
    assert pg_eng._qi("t1") == '"t1"'
    db2_eng = JDBCLogicalEngine({"db_type": "db2", "host": "h"}, "/tmp")
    assert db2_eng._qi("S.T") == '"S.T"'


def test_mysql_family_flag_matches_quoting():
    for t in ("mysql", "mariadb", "greatdb", "goldendb", "tidb", "oceanbase"):
        eng = JDBCLogicalEngine({"db_type": t, "host": "h"}, "/tmp")
        assert eng._qi("c") == "`c`", t
    for t in ("postgresql", "kingbase", "gaussdb", "vastbase", "db2", "clickhouse"):
        eng = JDBCLogicalEngine({"db_type": t, "host": "h"}, "/tmp")
        assert eng._qi("c") == '"c"', t
