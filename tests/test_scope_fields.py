# -*- coding: utf-8 -*-
"""统一备份粒度字段回归：scope(full_instance/database/table) + tables。

此前顶层字段不在 TASK_FIELDS 白名单，POST /api/tasks 会静默丢弃
（HTTP 200 但配置不生效），各库型没有统一的「全库/单库/单表」入口。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="scope_fields_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

import pytest  # noqa: E402
import core.models as models  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    try:
        _db.execute("DELETE FROM backup_tasks")
    except Exception:
        pass
    yield


def _base(**kw):
    d = {"name": "t-scope", "db_type": "mysql", "host": "127.0.0.1",
         "port": 3306, "username": "root", "password": "x", "db_name": "demo",
         "backup_type": "full", "biz_system": "test",
         "schedule_type": "manual", "interval_minutes": 0, "enabled": 0}
    d.update(kw)
    return d


def _extra(tid):
    t = models.get_task(tid)
    return json.loads(t.get("extra_options") or "{}")


def test_scope_full_instance_sets_use_all_db():
    tid = models.create_task(_base(scope="全库"))
    eo = _extra(tid)
    assert eo["scope"] == "full_instance"
    assert eo["use_all_db"] is True
    assert "tables" not in eo


def test_scope_table_requires_and_keeps_tables():
    tid = models.create_task(_base(scope="single_table", tables=["t1", "t2"]))
    eo = _extra(tid)
    assert eo["scope"] == "table"
    assert eo["tables"] == ["t1", "t2"]
    assert eo["use_all_db"] is False


def test_scope_database_clears_tables():
    tid = models.create_task(_base(scope="单库", extra_options={"tables": ["t1"]}))
    eo = _extra(tid)
    assert eo["scope"] == "database"
    assert eo["use_all_db"] is False
    assert "tables" not in eo


def test_top_level_tables_without_scope():
    tid = models.create_task(_base(tables="t1,t2"))
    assert _extra(tid)["tables"] == "t1,t2"


def test_existing_extra_preserved_on_create():
    tid = models.create_task(_base(scope="table", tables=["t1"],
                                   extra_options={"ssh_host_id": 1,
                                                  "dump_format": "custom"}))
    eo = _extra(tid)
    assert eo["ssh_host_id"] == 1 and eo["dump_format"] == "custom"
    assert eo["tables"] == ["t1"]


def test_update_scope_preserves_existing_extra():
    tid = models.create_task(_base(extra_options={"ssh_host_id": 1,
                                                  "dump_format": "custom"}))
    assert models.update_task(tid, {"scope": "table", "tables": ["t1"]})
    eo = _extra(tid)
    assert eo["ssh_host_id"] == 1 and eo["dump_format"] == "custom"
    assert eo["tables"] == ["t1"]


def test_invalid_scope_is_rejected():
    with pytest.raises(ValueError):
        models.create_task(_base(scope="不存在的粒度"))


def test_scope_table_without_tables_is_rejected():
    with pytest.raises(ValueError):
        models.create_task(_base(scope="table"))


def test_oracle_scope_database_maps_to_schemas():
    tid = models.create_task(_base(db_type="oracle", scope="单库",
                                   schema="BIZ"))
    eo = _extra(tid)
    assert eo["scope"] == "database"
    assert eo["schemas"] == ["BIZ"]
    assert eo["schema"] == "BIZ"
    assert "tables" not in eo


def test_oracle_scope_database_without_schema_rejected():
    # 静默跑成 FULL=Y 整库是危险的语义错误，必须显式拒绝
    with pytest.raises(ValueError):
        models.create_task(_base(db_type="oracle", scope="database"))


def test_dameng_scope_database_without_schema_rejected():
    with pytest.raises(ValueError):
        models.create_task(_base(db_type="dameng", scope="database"))


def test_oracle_scope_table_qualifies_table_names():
    tid = models.create_task(_base(db_type="oracle", scope="table",
                                   schema="BIZ", tables=["t1", "t2"]))
    eo = _extra(tid)
    assert eo["tables"] == ["BIZ.t1", "BIZ.t2"]
    assert "schemas" not in eo  # 达梦 schemas 会吞掉 TABLES 语义
    assert eo["schema"] == "BIZ"
    assert eo["use_all_db"] is False


def test_dameng_scope_table_accepts_qualified_names():
    tid = models.create_task(_base(db_type="dameng", scope="table",
                                   tables=["BIZ.t1", "BIZ.t2"]))
    eo = _extra(tid)
    assert eo["tables"] == ["BIZ.t1", "BIZ.t2"]
    assert eo["schema"] == "BIZ"
    assert "schemas" not in eo


def test_schema_scope_table_without_schema_or_qualified_name_rejected():
    with pytest.raises(ValueError):
        models.create_task(_base(db_type="oracle", scope="table",
                                 tables=["t1"]))


def test_schema_scope_full_instance_clears_stale_schemas():
    tid = models.create_task(_base(db_type="oracle", scope="全库",
                                   extra_options={"schemas": ["BIZ"],
                                                  "tables": ["t1"]}))
    eo = _extra(tid)
    assert eo["use_all_db"] is True
    assert "schemas" not in eo and "tables" not in eo


def test_update_scope_uses_existing_db_type():
    tid = models.create_task(_base(db_type="oracle"))
    with pytest.raises(ValueError):
        models.update_task(tid, {"scope": "database"})
    assert models.update_task(tid, {"scope": "database", "schema": "BIZ"})
    eo = _extra(tid)
    assert eo["scope"] == "database" and eo["schemas"] == ["BIZ"]


def test_mysql_scope_keeps_plain_tables():
    # 非 schema 语义库型不受本次归一影响
    tid = models.create_task(_base(scope="table", schema="BIZ",
                                   tables=["t1"]))
    eo = _extra(tid)
    assert eo["tables"] == ["t1"]
    assert "schemas" not in eo
