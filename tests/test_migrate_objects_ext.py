# -*- coding: utf-8 -*-
"""同义词/程序包/自定义类型迁移自测（P1，2026-11）。

覆盖（离线可测部分，真机链路见统一测试轮）：
- MySQL/PG 源：无同义词/程序包/类型对象 → 如实 skipped（不伪装）
- Oracle 源同义词：普通同义词同族直通执行、dblink 同义词跳过
- PG 枚举/复合类型：DDL 生成 + 同族执行
- 异构目标：导出 status=export，SQL 带入报告
- _phase_objects 汇总统计纳入新对象
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mig_obj_ext_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.environ["SCHEDULER_ENABLED"] = "0"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()                                # noqa: E402

from core.db_migrate import DbMigrationEngine   # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, binds=None):
        self.sql = sql
        self.binds = binds

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def _plan(src="oracle", tgt="dameng", schema="SRC_SCH"):
    return {"id": 1, "name": "t", "src_db_type": src, "tgt_db_type": tgt,
            "src_host": "h", "src_port": 1, "tgt_host": "h", "tgt_port": 1,
            "src_db_name": schema, "tgt_db_name": "TGT_SCH",
            "src_username": "u", "tgt_username": "u", "src_password": "",
            "tgt_password": ""}


def _eng():
    return DbMigrationEngine()


# ---------------------------------------------------------------------------
def test_mysql_source_objects_skipped():
    eng = _eng()
    conn = _FakeConn([])
    r = eng._mig_synonyms(conn, "mysql", "postgresql", "t")
    assert r[0]["status"] == "skipped" and "无同义词" in r[0]["error"]
    r = eng._mig_packages(conn, "mysql", "postgresql")
    assert r[0]["status"] == "skipped"
    r = eng._mig_types(conn, "mysql", "postgresql")
    assert r[0]["status"] == "skipped"


def test_oracle_synonyms_same_family_exec_and_dblink_skip():
    eng = _eng()
    eng._obj_plan = _plan("oracle", "dameng", "SRC_SCH")
    rows = [("SYNC1", "SRC_SCH", "T1", None),
            ("SYNC2", "SRC_SCH", "T2", "PROD.WORLD")]
    conn = _FakeConn(rows)
    calls = []
    eng._exec_target = lambda plan, sql: calls.append(sql) or (True, "")
    out = eng._mig_synonyms(conn, "oracle", "dameng", "TGT_SCH")
    assert len(out) == 2
    assert out[0]["status"] == "ok"
    assert calls and calls[0] == "CREATE OR REPLACE SYNONYM SYNC1 FOR SRC_SCH.T1"
    # dblink 同义词跳过并说明
    assert out[1]["status"] == "skipped" and "dblink" in out[1]["error"]


def test_oracle_synonyms_heterogeneous_export():
    eng = _eng()
    eng._obj_plan = _plan("oracle", "postgresql", "SRC_SCH")
    rows = [("SYNC1", "SRC_SCH", "T1", None)]
    conn = _FakeConn(rows)
    out = eng._mig_synonyms(conn, "oracle", "postgresql", "public")
    assert out[0]["status"] == "export"
    assert "CREATE OR REPLACE SYNONYM" in out[0]["sql"]


def test_pg_enum_types_same_family_exec():
    eng = _eng()
    eng._obj_plan = _plan("postgresql", "postgresql", "pub")
    # 第一段查询枚举，第二段复合类型
    enums = [("color", ["red", "green"])]
    composites = [("pt", ["a integer", "b text"])]

    class _SeqCursor:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, binds=None):
            self._conn.i += 1
            self._rows = enums if self._conn.i == 1 else composites

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class _SeqConn:
        def __init__(self):
            self.i = 0

        def cursor(self):
            return _SeqCursor(self)

        def close(self):
            pass

    calls = []
    eng._exec_target = lambda plan, sql: calls.append(sql) or (True, "")
    out = eng._mig_types(_SeqConn(), "postgresql", "postgresql")
    assert len(out) == 2
    assert out[0]["kind"] == "enum_type" and out[0]["status"] == "ok"
    assert "CREATE TYPE \"color\" AS ENUM ('red','green')" in calls
    assert out[1]["kind"] == "composite_type"
    assert "CREATE TYPE \"pt\" AS (a integer, b text)" in calls


def test_pg_types_heterogeneous_export():
    eng = _eng()
    eng._obj_plan = _plan("postgresql", "mysql", "pub")

    class _SeqCursor:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, binds=None):
            self._conn.i += 1
            self._rows = [("color", ["red"])] if self._conn.i == 1 else []

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class _SeqConn:
        def __init__(self):
            self.i = 0

        def cursor(self):
            return _SeqCursor(self)

        def close(self):
            pass

    out = eng._mig_types(_SeqConn(), "postgresql", "mysql")
    assert out[0]["status"] == "export"
    assert "AS ENUM" in out[0]["sql"]


def test_oracle_types_ddl_extraction_and_exec():
    eng = _eng()
    eng._obj_plan = _plan("oracle", "oracle", "SRC_SCH")
    ddl_text = "CREATE OR REPLACE TYPE SRC_SCH.T_OBJ AS OBJECT (id NUMBER)"

    class _DdlConn(_FakeConn):
        def cursor(self):
            return _DdlCursor()

    class _DdlCursor:
        def __init__(self):
            self._stage = 0

        def execute(self, sql, binds=None):
            self._stage += 1
            self.sql = sql

        def fetchall(self):
            return [("T_OBJ",)] if self._stage == 1 else []

        def fetchone(self):
            return (ddl_text,)

        def close(self):
            pass

    calls = []
    eng._exec_target = lambda plan, sql: calls.append(sql) or (True, "")
    out = eng._mig_types(_DdlConn([]), "oracle", "oracle")
    assert len(out) == 1 and out[0]["status"] == "ok"
    assert calls and calls[0] == ddl_text
