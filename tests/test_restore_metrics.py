# -*- coding: utf-8 -*-
"""恢复后数据量实测（core/restore_metrics）单元测试。

纯单测：不连真实库、不写元数据库——连接与 SSH 通道都用假实现替换，
验证「口径正确 + 采不到就如实失败，不猜数」这一硬约束。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import restore_metrics as rm  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0
        self.sql = ""

    def execute(self, sql, *a, **kw):
        self.sql = sql
        self._i = 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.cur = _FakeCursor(rows)

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_parse_row_mysql_tab():
    t, r, s = rm._parse_row("3\t1500\t1048576")
    assert (t, r, s) == (3, 1500, 1048576)


def test_parse_row_pg_pipe():
    t, r, s = rm._parse_row("2|42|2048")
    assert (t, r, s) == (2, 42, 2048)


def test_parse_row_garbage():
    assert rm._parse_row("") == (None, None, None)
    assert rm._parse_row("only-one") == (None, None, None)


def test_sql_for_mysql_scopes_schema():
    sql = rm._sql_for("mysql", "bizdb", "")
    assert "table_schema='bizdb'" in sql
    assert "table_type='BASE TABLE'" in sql


def test_sql_for_pg_uses_pg_class():
    sql = rm._sql_for("postgresql", "bizdb", "public")
    assert "pg_class" in sql and "nspname='public'" in sql


def test_sql_for_oracle_uses_owner():
    sql = rm._sql_for("oracle", "ORCL", "C##E2E")
    assert "all_tables" in sql and "owner='C##E2E'" in sql


def test_sql_for_oracle_without_owner_excludes_system():
    sql = rm._sql_for("oracle", "ORCL", "")
    assert "NOT IN" in sql and "'SYS'" in sql


def test_sql_literal_escapes_quote():
    assert rm._lit("a'b") == "'a''b'"


def test_measure_unsupported_type_is_honest():
    out = rm.measure("redis", "1.2.3.4", 6379, "0", "u", "p")
    assert out["ok"] is False
    assert out["tables"] is None and out["rows"] is None
    assert "暂不支持" in out["note"]


def test_measure_direct_mysql(monkeypatch):
    conn = _FakeConn([(3, 1500, 1048576)])

    def _connect(db_type, host, port, db, user, password, timeout=10):
        assert db_type == "mysql" and db == "bizdb" and host == "10.0.0.9"
        return conn

    import core.native_conn as nc
    monkeypatch.setattr(nc, "connect", _connect)
    out = rm.measure("mysql", "10.0.0.9", 3306, "bizdb", "u", "p")
    assert out["ok"] is True
    assert out["mode"] == "direct"
    assert out["tables"] == 3 and out["rows"] == 1500
    assert out["size_bytes"] == 1048576
    assert conn.closed is True
    assert "估算" in out["note"]


def test_measure_direct_failure_falls_back_to_ssh(monkeypatch):
    import core.native_conn as nc
    import core.remote_dump as rd

    def _boom(*a, **kw):
        raise ConnectionError("1045 access denied")

    monkeypatch.setattr(nc, "connect", _boom)
    monkeypatch.setattr(rd, "resolve_remote_tool", lambda *a, **kw: "/usr/bin/mysql")
    monkeypatch.setattr(rd, "remote_exec_capture",
                        lambda *a, **kw: {"returncode": 0, "stdout": "5\t900\t2048\n",
                                          "stderr": ""})
    out = rm.measure("mysql", "10.0.0.9", 3306, "bizdb", "u", "p",
                     ssh_host={"host_key": "root@10.0.0.9:22"})
    assert out["ok"] is True
    assert out["mode"] == "ssh"
    assert out["tables"] == 5 and out["rows"] == 900 and out["size_bytes"] == 2048


def test_measure_ssh_failure_reports_reason(monkeypatch):
    import core.native_conn as nc
    import core.remote_dump as rd

    monkeypatch.setattr(nc, "connect",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("unreachable")))
    monkeypatch.setattr(rd, "resolve_remote_tool", lambda *a, **kw: "/usr/bin/mysql")
    monkeypatch.setattr(rd, "remote_exec_capture",
                        lambda *a, **kw: {"returncode": 1, "stdout": "",
                                          "stderr": "Access denied"})
    out = rm.measure("mysql", "10.0.0.9", 3306, "bizdb", "u", "p",
                     ssh_host={"host_key": "root@10.0.0.9:22"})
    assert out["ok"] is False
    assert out["tables"] is None
    assert "Access denied" in out["note"]


def test_measure_ssh_client_missing_reports_reason(monkeypatch):
    import core.native_conn as nc
    import core.remote_dump as rd

    monkeypatch.setattr(nc, "connect",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("unreachable")))
    monkeypatch.setattr(rd, "resolve_remote_tool", lambda *a, **kw: None)
    out = rm.measure("mysql", "10.0.0.9", 3306, "bizdb", "u", "p",
                     ssh_host={"host_key": "root@10.0.0.9:22"})
    assert out["ok"] is False
    assert "客户端" in out["note"]


def test_measure_from_task_resolves_host_from_ssh_host():
    task = {"db_type": "mysql", "host": "src-host", "port": 3306,
            "db_name": "srcdb", "username": "u", "password": "p"}
    captured = {}

    def _measure(**kw):
        captured.update(kw)
        return {"ok": True, "tables": 1, "rows": 1, "size_bytes": 1,
                "mode": "direct", "note": ""}

    import core.restore_metrics as self_mod
    orig = self_mod.measure
    self_mod.measure = _measure
    try:
        out = self_mod.measure_from_task(
            task=task, target_host_info={"host_key": "root@192.168.1.8:22"},
            target_db="newdb", target_port=3307)
    finally:
        self_mod.measure = orig
    assert out["ok"] is True
    assert captured["host"] == "192.168.1.8"
    assert captured["port"] == 3307
    assert captured["database"] == "newdb"
    assert captured["user"] == "u" and captured["password"] == "p"


def test_measure_from_task_falls_back_to_task_host():
    task = {"db_type": "mysql", "host": "src-host", "port": 3306,
            "db_name": "srcdb", "username": "u", "password": "p"}
    captured = {}

    def _measure(**kw):
        captured.update(kw)
        return {"ok": False}

    import core.restore_metrics as self_mod
    orig = self_mod.measure
    self_mod.measure = _measure
    try:
        self_mod.measure_from_task(task=task)
    finally:
        self_mod.measure = orig
    assert captured["host"] == "src-host"
    assert captured["database"] == "srcdb"


def test_measure_from_task_schema_from_extra_options():
    task = {"db_type": "oracle", "host": "h", "port": 1521, "db_name": "ORCL",
            "username": "u", "password": "p",
            "extra_options": {"schema": "C##E2E"}}
    captured = {}

    def _measure(**kw):
        captured.update(kw)
        return {"ok": True}

    import core.restore_metrics as self_mod
    orig = self_mod.measure
    self_mod.measure = _measure
    try:
        self_mod.measure_from_task(task=task)
    finally:
        self_mod.measure = orig
    assert captured["schema"] == "C##E2E"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
