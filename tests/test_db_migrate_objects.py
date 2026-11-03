# -*- coding: utf-8 -*-
"""KDTS 对标：迁移对象配置与视图方言适配的纯逻辑单测。"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(ROOT, "tests", ".tmp_mig_obj", "meta.db")
os.makedirs(os.path.dirname(os.environ["META_DB_PATH"]), exist_ok=True)

from core.db_migrate import DbMigrationEngine, _sql_quote


def test_adapt_view_mysql_to_pg():
    sql = "SELECT `id`, `e2e_kdts_src`.`biz_order`.`name` FROM `e2e_kdts_src`.`biz_order` WHERE `amt` > 100"
    out = DbMigrationEngine._adapt_view_sql(sql, "mysql", "pg", "e2e_kdts_src")
    assert "`" not in out
    assert "e2e_kdts_src." not in out and '"e2e_kdts_src".' not in out
    assert '"biz_order"."name"' in out
    assert '"amt" > 100' in out


def test_adapt_view_pg_to_mysql():
    sql = 'SELECT "id", "name" FROM "biz_order" WHERE "amt" > 100'
    out = DbMigrationEngine._adapt_view_sql(sql, "pg", "mysql", "")
    assert '"' not in out
    assert "`id`" in out


def test_sql_quote_escapes():
    assert _sql_quote("业务订单表") == "'业务订单表'"
    assert _sql_quote("it's") == "'it''s'"


def test_extra_options_parse_in_row_dict():
    row = {"migrate_types": '["structure"]',
           "extra_options": '{"exclude_tables":["t1"],"migrate_objects":{"views":true}}',
           "phases_json": "{}"}
    from core.db_migrate import _row_to_dict
    d = _row_to_dict(row)
    assert d["migrate_types"] == ["structure"]
    assert d["extra_options"]["exclude_tables"] == ["t1"]
    assert d["extra_options"]["migrate_objects"]["views"] is True
