# -*- coding: utf-8 -*-
"""元数据库后端（可插拔 SQLite/PG/MySQL）回归测试。

全部离线可跑：方言翻译、主键映射、upsert 构造、sqlite->sqlite 全量搬移。
真实 PG/MySQL 切换 E2E 见 docs/meta_backend_report_20260923.md（需外部库）。
"""
import sqlite3

import pytest

import config
import core.db as db
import core.meta_migrate as mm


# ------------------------- 方言翻译 -------------------------

def test_translate_ddl_postgresql():
    out = db._translate_schema_ddl(db.SCHEMA, "postgresql")
    assert "AUTOINCREMENT" not in out
    assert "SERIAL PRIMARY KEY" in out
    # 原语句不动（幂等翻译的前提是 sqlite 原样返回）
    assert db._translate_schema_ddl("CREATE TABLE t (a TEXT)", "sqlite") == \
        "CREATE TABLE t (a TEXT)"


def test_translate_ddl_mysql():
    out = db._translate_schema_ddl(db.SCHEMA, "mysql")
    assert "AUTOINCREMENT" not in out
    assert "AUTO_INCREMENT" in out
    # MySQL 保留字 key 加反引号
    assert "`key`" in out
    # MySQL 不支持 CREATE INDEX IF NOT EXISTS
    assert "CREATE INDEX IF NOT EXISTS" not in out


def test_translate_ddl_inline_scripts():
    ddl = ("CREATE TABLE IF NOT EXISTS demo_x ("
           "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
    assert "SERIAL PRIMARY KEY" in db._translate_schema_ddl(ddl, "postgresql")
    assert "AUTO_INCREMENT" in db._translate_schema_ddl(ddl, "mysql")


def test_translate_sql_qmark_and_percent():
    assert db._translate_sql("a=? AND b LIKE ?") == "a=%s AND b LIKE %s"
    # SQL 文本中的字面 % 转义为 %%，驱动还原为 %
    assert db._translate_sql("c LIKE '%x%' AND d=?") == "c LIKE '%%x%%' AND d=%s"


# ------------------------- 主键映射 -------------------------

def test_table_pk_map_covers_schema():
    import re
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", db.SCHEMA))
    assert declared == set(db._TABLE_PK)
    assert db._insert_pk_of("backup_tasks") == "id"
    # 无自增主键表返回 None（不生成 RETURNING）
    assert db._insert_pk_of("system_config") is None


# ------------------------- upsert 构造 -------------------------

def test_upsert_sql_dialects(monkeypatch):
    monkeypatch.setitem(db._ACTIVE, "backend", "postgresql")
    sql = db.upsert_sql("t", ["a", "b"], ["a"], ["b"])
    assert "ON CONFLICT(a) DO UPDATE SET b=excluded.b" in sql
    monkeypatch.setitem(db._ACTIVE, "backend", "mysql")
    sql = db.upsert_sql("t", ["a", "b"], ["a"], ["b"])
    assert "ON DUPLICATE KEY UPDATE b=VALUES(b)" in sql
    monkeypatch.setitem(db._ACTIVE, "backend", "sqlite")
    sql = db.upsert_sql("t", ["a", "b"], ["a"], ["b"])
    assert "ON CONFLICT(a) DO UPDATE" in sql


# ------------------------- sqlite -> sqlite 全量搬移 -------------------------

@pytest.fixture()
def two_sqlite_dbs(tmp_path, monkeypatch):
    """两份独立 meta.db，各含 schema 与少量数据。"""
    db._ACTIVE["backend"] = "sqlite"
    src_path = tmp_path / "src_meta.db"
    dst_path = tmp_path / "dst_meta.db"

    monkeypatch.setattr(config, "META_DB_PATH", str(src_path))
    db.init_schema()
    nid = db.execute(
        "INSERT INTO system_logs(ts, level, source, message) VALUES (?,?,?,?)",
        (db.now_iso(), "info", "test", "row-1"))
    assert nid > 0
    db.set_system_config("k1", "v1")
    src_conn = db.open_sqlite_conn()

    monkeypatch.setattr(config, "META_DB_PATH", str(dst_path))
    db.init_schema()
    dst_conn = db.open_sqlite_conn()
    yield src_conn, dst_conn
    src_conn.close()
    dst_conn.close()


def test_migrate_sqlite_to_sqlite(two_sqlite_dbs):
    src, dst = two_sqlite_dbs
    report = mm.migrate(src, "sqlite", dst, "sqlite")
    tables = {r["table"] for r in report["tables"]}
    assert "system_logs" in tables and "system_config" in tables
    assert mm.count_rows(dst, "sqlite", "system_logs") == \
        mm.count_rows(src, "sqlite", "system_logs")


def test_migrate_row_count_mismatch_raises(two_sqlite_dbs, monkeypatch):
    src, dst = two_sqlite_dbs
    # 跳过目标清空并预置一行，造成行数不一致 -> 报错
    monkeypatch.setattr(mm, "clear_target", lambda *a, **k: None)
    dst.execute("INSERT INTO system_logs(id, ts, level, source, message) "
                "VALUES (999999,'t','info','x','y')")
    dst.commit()
    with pytest.raises(RuntimeError, match="行数不一致"):
        mm.migrate(src, "sqlite", dst, "sqlite", tables=["system_logs"])


def test_backend_status_shape():
    st = db.backend_status()
    assert st["backend"] in ("sqlite", "postgresql", "mysql")
    assert "config_file" in st


def test_open_sqlite_conn_pragmas(tmp_path, monkeypatch):
    p = tmp_path / "p.db"
    monkeypatch.setattr(config, "META_DB_PATH", str(p))
    conn = db.open_sqlite_conn()
    assert isinstance(conn, sqlite3.Connection)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()
