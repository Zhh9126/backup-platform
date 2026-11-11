# -*- coding: utf-8 -*-
"""三粒度备份兼容性回归：单表真实生效、兼容层全实例通道可用。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.engines.base import parse_table_list  # noqa: E402


def test_parse_table_list_forms():
    assert parse_table_list(["t1", "t2"]) == ["t1", "t2"]
    assert parse_table_list('["t1","t2"]') == ["t1", "t2"]
    assert parse_table_list("t1, t2 ,t1") == ["t1", "t2"]
    assert parse_table_list(None) == []
    assert parse_table_list({"a": 1}) == []


def test_logical_full_covers_protocol_compatible_engines():
    import core.logical_full as lf
    for name in ("postgresql", "kingbase", "opengauss", "panwei",
                 "highgo", "uxdb", "mysql", "mariadb", "tidb", "oceanbase"):
        assert name in lf.TOOLING, "%s 全实例本机通道缺 TOOLING" % name
        assert name in lf.SYSTEM_DBS, "%s 缺系统库清单" % name
    assert "highgo" in lf.PG_FAMILY and "uxdb" in lf.PG_FAMILY


def test_remote_dump_whitelists_cover_compatible_engines():
    src = open(os.path.join(ROOT, "core", "remote_dump.py"),
               encoding="utf-8").read()
    assert '("postgresql", "kingbase", "opengauss", "panwei", "highgo", "uxdb")' in src
    assert '("mysql", "mariadb", "tidb", "oceanbase")' in src
    assert 'if db_type == "mysql":' not in src


def test_pg_and_kingbase_local_dump_appends_table_flag():
    for rel in ("core/engines/postgresql.py", "core/engines/kingbase.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert 'cmd += ["-t", _tb]' in src, "%s 单表备份未真实追加 -t" % rel
        assert "parse_table_list" in src


def test_scope_fields_no_longer_silently_dropped():
    """顶层 scope/tables 必须经归一进 extra_options，而非被白名单丢弃。"""
    src = open(os.path.join(ROOT, "core", "models.py"), encoding="utf-8").read()
    assert "_normalize_scope_fields(dict(data))" in src
    assert "def _normalize_scope_fields" in src
    api = open(os.path.join(ROOT, "api", "tasks.py"), encoding="utf-8").read()
    assert "except ValueError as ve:" in api


def _dm_engine():
    from core.engines.dameng import DamengEngine
    e = DamengEngine.__new__(DamengEngine)
    e.task = {"db_name": "BIZ"}
    return e


def test_dameng_dexp_table_scope_is_single_mode():
    """170 实测: SCHEMAS + TABLES 同时给出会 rc=1「不支持多种导出模式」。"""
    args = _dm_engine()._scope_args({"schema": "BIZ", "tables": ["t1", "t2"]})
    assert args == ["TABLES=BIZ.t1,BIZ.t2"]
    assert not any(a.startswith(("SCHEMAS=", "OWNER=", "FULL=")) for a in args)


def test_dameng_dexp_table_scope_keeps_qualified_names():
    args = _dm_engine()._scope_args({"tables": ["BIZ.t1"]})
    assert args == ["TABLES=BIZ.t1"]


def test_dameng_dexp_table_scope_prefix_from_schemas():
    args = _dm_engine()._scope_args({"schemas": ["BIZ", "OTHER"],
                                    "tables": "t1,t2"})
    assert args == ["TABLES=BIZ.t1,BIZ.t2"]
