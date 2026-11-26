# -*- coding: utf-8 -*-
"""达梦「JDBC 逻辑导出通道」回归（2026-11-25 现场问题）。

现场：达梦实例跑在 Windows 上（D:\\dmdbms），平台机（Linux）没有 dexp/dimp，
JDBC 却能连上——旧逻辑在预检阶段就报「缺少客户端工具: dexp, dimp」直接终止。

覆盖：
- Windows 工具目录候选（含 ``...\\tool`` → 同级 ``...\\bin`` 的自动修正）；
- 逻辑通道选择（auto 自动落到 JDBC / tool 固定不降级 / jdbc 强制）；
- 备份、恢复、恢复校验对 ``.jsonl`` 产物的分派；
- JDBC 引擎的标识符引用、列类型重建、系统模式排除、模式缺失时的诚实失败；
- 恢复前预检对 JDBC 产物不再要求 dimp。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="dm_jdbc_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core import remote_dump  # noqa: E402
from core.engines.base import BackupResult, BackupStatus, BackupType  # noqa: E402
from core.engines.dameng import DamengEngine  # noqa: E402
from core.engines.dameng_jdbc import DamengJDBCEngine  # noqa: E402


def _task(**kw):
    task = {"id": 0, "name": "dm-task", "db_type": "dameng",
            "host": "10.2.24.12", "port": 5238,
            "username": "SYSDBA", "password": "pw", "db_name": "",
            "backup_mode": "logical", "extra_options": "{}"}
    task.update(kw)
    return task


def _engine(**kw):
    return DamengEngine(_task(**kw), _TMP)


# --------------------------------------------------------------------------- #
# Windows 工具目录候选
# --------------------------------------------------------------------------- #
def test_windows_tool_candidates_task_path_first_and_sibling_bin():
    cands = remote_dump.windows_tool_candidates(
        "dexp", r"D:\dmdbms\tool")
    assert cands[0] == r"D:\dmdbms\tool"
    # 现场最常见的填法：填 tool 目录（里面只有 plugins/templates，没有 dexp.exe）
    assert r"D:\dmdbms\bin" in cands


def test_windows_tool_candidates_include_builtin_dirs_without_duplicates():
    cands = remote_dump.windows_tool_candidates("dexp")
    lowered = [c.lower().rstrip("\\") for c in cands]
    assert len(lowered) == len(set(lowered)), "候选目录不能重复"
    assert any(c.lower().endswith("dmdbms\\bin") for c in cands)


def test_windows_tool_candidates_semicolon_and_quote_tolerant():
    cands = remote_dump.windows_tool_candidates(
        "sqlcmd", r'"C:\a\bin";C:\b\bin')
    assert cands[0] == r"C:\a\bin" and r"C:\b\bin" in cands


# --------------------------------------------------------------------------- #
# 逻辑通道选择
# --------------------------------------------------------------------------- #
def test_auto_route_falls_back_to_jdbc_when_clients_missing():
    eng = _engine()
    eng.check_client = lambda: (False, "缺少客户端工具: dexp, dimp")
    eng._jdbc_probe = lambda: (True, "达梦 JDBC 连接正常（10.2.24.12:5238）")
    route, reason = eng._logical_route()
    assert route == "jdbc" and "JDBC" in reason
    ok, msg = eng.preflight()
    assert ok is True and "JDBC 逻辑导出" in msg


def test_tool_channel_never_silently_degrades():
    eng = _engine(extra_options=json.dumps({"logical_channel": "tool"}))
    eng.check_client = lambda: (False, "缺少客户端工具: dexp, dimp")
    eng._jdbc_probe = lambda: (True, "JDBC ok")
    route, reason = eng._logical_route()
    assert route is None and "固定" in reason
    ok, msg = eng.preflight()
    assert ok is False and "固定" in msg


def test_jdbc_channel_reports_unavailable_honestly():
    eng = _engine(extra_options=json.dumps({"logical_channel": "jdbc"}))
    eng.check_client = lambda: (False, "缺少 dexp")
    eng._jdbc_probe = lambda: (False, "ClassNotFound DmDriver")
    route, reason = eng._logical_route()
    assert route is None and "ClassNotFound" in reason
    ok, msg = eng.preflight()
    assert ok is False and "JDBC" in msg


def test_both_channels_down_yields_actionable_message():
    eng = _engine()
    eng.check_client = lambda: (False, "缺少客户端工具: dexp, dimp")
    eng._jdbc_probe = lambda: (False, "驱动不可用")
    ok, msg = eng.preflight()
    assert ok is False
    assert "JDBC" in msg and ("OpenSSH" in msg or "客户端" in msg)


def test_backup_delegates_to_jdbc_engine_and_annotates_channel():
    eng = _engine()
    eng.check_client = lambda: (False, "缺少 dexp")
    eng._jdbc_probe = lambda: (True, "JDBC ok")

    class _FakeJDBC:
        called = False

        def backup(self, backup_type):
            _FakeJDBC.called = True
            return BackupResult(True, BackupStatus.SUCCESS,
                                backup_path="/tmp/x.jsonl", size_bytes=10,
                                message="JDBC 逻辑备份成功：1 张表 / 2 行")

    eng._jdbc_engine = lambda: _FakeJDBC()
    res = eng.backup(BackupType.FULL)
    assert _FakeJDBC.called and res.success
    assert "JDBC" in res.message and "不含视图" in res.message


def test_backup_fails_fast_when_no_channel():
    eng = _engine()
    eng.check_client = lambda: (False, "缺少 dexp")
    eng._jdbc_probe = lambda: (False, "驱动不可用")
    res = eng.backup(BackupType.FULL)
    assert res.success is False and "通道不可用" in res.message


# --------------------------------------------------------------------------- #
# JDBC 引擎细节
# --------------------------------------------------------------------------- #
class _Cur:
    def __init__(self, table):
        self._table = table
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for key, rows in self._table.items():
            if key in sql:
                self._rows = rows(params) if callable(rows) else rows
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, table):
        self._cur = _Cur(table)
        self.jconn = None
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def test_qi_quotes_every_part():
    eng = DamengJDBCEngine(_task(), _TMP)
    assert eng._qi("AIDBM.T1") == '"AIDBM"."T1"'
    assert eng._qi("T1") == '"T1"'
    # 目录里本来就是带引号的名字 → 原样保留
    assert eng._qi('"lower"."t"') == '"lower"."t"'


def test_col_sql_maps_dameng_types():
    eng = DamengJDBCEngine(_task(), _TMP)
    assert eng._col_sql("VARCHAR", 192, None, None) == "VARCHAR(192)"
    assert eng._col_sql("VARCHAR2", 50, None, None) == "VARCHAR(50)"
    assert eng._col_sql("DECIMAL", 12, 12, 2) == "DECIMAL(12,2)"
    assert eng._col_sql("NUMBER", 10, 10, 0) == "NUMBER(10)"
    assert eng._col_sql("TIMESTAMP", 8, None, None) == "TIMESTAMP"


def test_tables_skips_system_owners_and_internal_tables():
    conn = _Conn({"SELECT owner, table_name FROM all_tables": [
        ("SYS", "SYSOBJ"), ("SYSDBA", "T1"), ("AIDBM", "##HIST"),
        ("AIDBM", "T2"), ("CTISYS", "X")]})
    eng = DamengJDBCEngine(_task(), _TMP)
    tables = eng._tables(conn)
    assert tables == ["SYSDBA.T1", "AIDBM.T2"]


def test_tables_honours_explicit_scope():
    extra = json.dumps({"tables": "AIDBM.T1, AIDBM.T2"})
    eng = DamengJDBCEngine(_task(extra_options=extra), _TMP)
    assert eng._tables(_Conn({})) == ["AIDBM.T1", "AIDBM.T2"]
    extra = json.dumps({"schemas": "aidbm"})
    eng = DamengJDBCEngine(_task(extra_options=extra), _TMP)
    conn = _Conn({"SELECT owner, table_name FROM all_tables": [("AIDBM", "T1")]})
    assert eng._tables(conn) == ["AIDBM.T1"]
    sql, params = conn._cur.executed[-1]
    assert "owner IN (?)" in sql and params == ["AIDBM"]


def test_table_meta_builds_ddl_with_not_null():
    conn = _Conn({"all_tab_columns": [
        ("ID", "INT", 4, 10, 0, "N"),
        ("NAME", "VARCHAR", 192, None, None, "Y"),
        ("AMOUNT", "DECIMAL", 8, 12, 2, "Y")]})
    ddl, cols, jtypes = DamengJDBCEngine(_task(), _TMP)._table_meta(
        conn, "AIDBM.T1")
    assert ddl == ('CREATE TABLE "AIDBM"."T1" ("ID" INT NOT NULL, '
                   '"NAME" VARCHAR(192), "AMOUNT" DECIMAL(12,2))')
    assert cols == ["ID", "NAME", "AMOUNT"] and jtypes[0] == "INT"


def test_restore_rejects_missing_target_schema(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"type":"meta"}\n', encoding="utf-8")
    conn = _Conn({"DBA_OBJECTS": [(0,)], "all_users": [(0,)]})
    eng = DamengJDBCEngine(_task(), _TMP)
    eng._conn = lambda database=None: conn
    res = eng.restore(str(path), target_db="NOPE")
    assert res.success is False
    assert "不存在" in res.message and "CREATE SCHEMA" in res.message


def test_restore_replays_rows_into_target_schema(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "meta", "db_type": "dameng", "tables": 1}),
        json.dumps({"type": "table", "name": "SRC.T1",
                    "ddl": 'CREATE TABLE "SRC"."T1" ("ID" INT, "NAME" VARCHAR(20))',
                    "columns": ["ID", "NAME"]}),
        json.dumps({"type": "row", "table": "SRC.T1", "values": [1, "甲"]}),
        json.dumps({"type": "row", "table": "SRC.T1", "values": [2, "乙"]}),
    ]) + "\n", encoding="utf-8")
    conn = _Conn({"DBA_OBJECTS": [(1,)]})
    eng = DamengJDBCEngine(_task(), _TMP)
    eng._conn = lambda database=None: conn

    captured = []

    class _Cur2(_Cur):
        def executemany(self, sql, rows):
            captured.append((sql, rows))

    conn._cur = _Cur2({"DBA_OBJECTS": [(1,)]})
    res = eng.restore(str(path), target_db="TGT")
    assert res.success is True, res.message
    assert "写入 2 行" in res.message and "TGT" in res.message
    # 表名被重指到目标模式，DDL 也同步改写
    sqls = [s for s, _ in conn._cur.executed]
    assert any('DROP TABLE "TGT"."T1"' in s for s in sqls)
    assert any(s.startswith('CREATE TABLE "TGT"."T1" (') for s in sqls)
    assert captured and 'INSERT INTO "TGT"."T1"' in captured[0][0]
    assert captured[0][1] == [[1, "甲"], [2, "乙"]]


def test_verify_record_counts_tables_and_rows(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "meta"}),
        json.dumps({"type": "table", "name": "A.T1"}),
        json.dumps({"type": "row", "table": "A.T1", "values": [1]}),
        json.dumps({"type": "row", "table": "A.T1", "values": [2]}),
    ]) + "\n", encoding="utf-8")
    res = DamengJDBCEngine(_task(), _TMP).verify_record({"backup_path": str(path)})
    assert res.success is True and res.verified is True
    assert "1 张表 / 2 行" in res.message
    # 坏行 → 诚实失败
    path.write_text('{"type":"table"}\n{not-json}\n', encoding="utf-8")
    bad = DamengJDBCEngine(_task(), _TMP).verify_record({"backup_path": str(path)})
    assert bad.success is False and "损坏" in bad.message


def test_engine_dispatches_jsonl_restore_and_verify(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"type":"table","name":"A.T1","ddl":"CREATE TABLE A.T1 (ID INT)"}\n',
                    encoding="utf-8")
    eng = _engine()
    seen = {}

    class _Fake:
        def restore(self, backup_path, **kwargs):
            seen["restore"] = (backup_path, kwargs)
            return BackupResult(True, BackupStatus.SUCCESS, message="ok")

        def verify_record(self, record, options=None):
            seen["verify"] = record.get("backup_path")
            return BackupResult(True, BackupStatus.SUCCESS, message="ok")

    eng._jdbc_engine = lambda: _Fake()
    res = eng.restore(str(path), target_db="TGT")
    assert res.success and seen["restore"][1]["target_db"] == "TGT"
    res = eng.verify_record({"backup_path": str(path)})
    assert res.success and seen["verify"] == str(path)


# --------------------------------------------------------------------------- #
# 恢复前预检：JDBC 产物不要求 dimp
# --------------------------------------------------------------------------- #
def test_restore_precheck_uses_jdbc_probe_for_jsonl(tmp_path, monkeypatch):
    from core import backup_precheck

    path = tmp_path / "d.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "meta"}),
        json.dumps({"type": "table", "name": "A.T1"}),
        json.dumps({"type": "row", "table": "A.T1", "values": [1]}),
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(DamengJDBCEngine, "probe",
                        lambda self: (True, "达梦 JDBC 连接正常"))
    report = backup_precheck.precheck_restore(
        {"id": 1, "db_type": "dameng", "backup_path": str(path),
         "checksum": "", "task_id": None})
    item = [i for i in report["items"] if i["item"] == "restore_tools"][0]
    assert item["level"] == "pass" and "JDBC" in item["message"]
    assert not any("dimp" in str(i["message"]) for i in report["items"]
                   if i["item"] == "restore_tools")
