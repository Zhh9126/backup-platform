# -*- coding: utf-8 -*-
"""Oracle「拉取库列表」跨版本兼容单测。

背景（2026-11-20 用户反馈）：Oracle 任务里 JDBC 测试连接成功，但「拉取库列表」
提示「未获取到库列表」。两个根因：
1) api/tasks.py 的 _LIST_DB_SKIP 没有 oracle/dameng → 直接短路返回空列表且无错误；
2) 拉取 SQL 用了 ALL_USERS.ORACLE_MAINTAINED（12c 才有的列），11g 报 ORA-00904
   导致 JDBC 兜底整体失败。

修复后：list_sql 支持候选序列（逐条降级），11g 回退全量 all_users + 按内置
schema 名单/前缀过滤。
"""
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="ora_schema_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core import jdbc, native_conn  # noqa: E402


_ORA_ROWS = [("SYS",), ("SYSTEM",), ("MDSYS",), ("APEX_030200",),
             ("OWBSYS",), ("BIZ_APP",), ("C##E2E17",)]


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql):
        self.conn.sqls.append(sql)
        if "oracle_maintained" in sql:
            raise Exception('ORA-00904: "ORACLE_MAINTAINED": invalid identifier')
        self.rows = list(_ORA_ROWS)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.sqls = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def _no_native(*a, **k):
    raise native_conn.DriverUnavailable("thin 模式不支持 11g")


def test_native_falls_back_and_filters_system_schemas(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(native_conn, "connect", lambda *a, **k: conn)
    rows = native_conn.list_databases("oracle", "h", 1521, "orcl", "system", "pw")
    # 两条 SQL 都试过（第一条 ORA-00904 → 降级第二条）
    assert len(conn.sqls) == 2
    assert "oracle_maintained" in conn.sqls[0]
    # 内置/组件 schema 被剔除，业务 schema 保留（含 C## 业务用户，不能一刀切）
    assert rows == ["BIZ_APP", "C##E2E17"]


def test_jdbc_falls_back_and_filters_system_schemas(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(native_conn, "list_databases", _no_native)
    monkeypatch.setattr(jdbc, "connect", lambda *a, **k: conn)
    rows = jdbc.list_databases("oracle", "h", 1521, "orcl", "system", "pw")
    assert len(conn.sqls) == 2
    assert rows == ["BIZ_APP", "C##E2E17"]


def test_first_sql_success_skips_fallback(monkeypatch):
    """12c+ 路径：第一条 SQL 直接成功，不会白跑第二条。"""
    class _OkCursor(_FakeCursor):
        def execute(self, sql):
            self.conn.sqls.append(sql)
            self.rows = [("BIZ_A",)]

    class _OkConn(_FakeConn):
        def cursor(self):
            return _OkCursor(self)

    conn = _OkConn()
    monkeypatch.setattr(native_conn, "connect", lambda *a, **k: conn)
    rows = native_conn.list_databases("oracle", "h", 1521, "orcl", "system", "pw")
    assert rows == ["BIZ_A"]
    assert len(conn.sqls) == 1


def test_all_candidates_fail_raises_real_error(monkeypatch):
    class _BadCursor(_FakeCursor):
        def execute(self, sql):
            self.conn.sqls.append(sql)
            raise Exception("ORA-00942: table or view does not exist")

    class _BadConn(_FakeConn):
        def cursor(self):
            return _BadCursor(self)

    monkeypatch.setattr(native_conn, "connect", lambda *a, **k: _BadConn())
    with pytest.raises(Exception) as ei:
        native_conn.list_databases("oracle", "h", 1521, "orcl", "system", "pw")
    assert "ORA-00942" in str(ei.value)


def test_oracle_and_dameng_are_listable_types():
    """Oracle/达梦必须出现在 _LIST_DB_SKIP（= 支持拉取列表），否则静默返回空列表。"""
    from api import tasks as tasks_api
    assert "oracle" in tasks_api._LIST_DB_SKIP
    assert "dameng" in tasks_api._LIST_DB_SKIP
    assert "oracle" in tasks_api._SCHEMA_LIST_TYPES
    assert "dameng" in tasks_api._SCHEMA_LIST_TYPES


def test_payload_type_is_schemas_for_oracle(monkeypatch):
    from api import tasks as tasks_api
    monkeypatch.setattr(tasks_api, "_fetch_db_list",
                        lambda dt, task: (["SYS", "BIZ_A"], True, None))
    payload, err = tasks_api._db_list_payload({"db_type": "oracle",
                                               "host": "h", "port": 1521})
    assert err is None
    assert payload["type"] == "schemas"
    assert payload["via_jdbc"] is True
    assert payload["databases"] == ["SYS", "BIZ_A"]


def test_oracle_system_schema_helper():
    for name in ("SYS", "system", "MDSYS", "APEX_030200", "OWBSYS_AUDIT",
                 "ORADICT_T", ""):
        assert native_conn.oracle_system_schema(name) is True, name
    for name in ("BIZ_APP", "C##E2E17", "SCOTT"):
        assert native_conn.oracle_system_schema(name) is False, name
