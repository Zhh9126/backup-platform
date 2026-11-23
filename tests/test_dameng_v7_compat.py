"""达梦 DM7/V7 连接兼容回归。

覆盖三处现场误判点：
1. 旧版 dmPython 只认位置参数，关键字参数 TypeError 不应被报成「用户名或密码错误」；
2. DM7 老 build 对无 FROM 的 SELECT 1 兼容不一，需 DUAL 候选探活；
3. 原生与 JDBC 错误码体系不同（-2501 / -7002），双通道摘要必须都透出。

全部 mock，不连真实数据库，不依赖 dmPython / JRE。
"""
import socket
import types

from core import jdbc, native_conn, probe


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeCursor:
    def __init__(self, supported=()):
        self.supported = set(supported)
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        if sql not in self.supported:
            raise RuntimeError("CODE:-2104 第 1 行附近出现错误")

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, supported=()):
        self.cur = _FakeCursor(supported)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def _raise(msg):
    raise RuntimeError(msg)


def test_native_connect_dameng_old_dmpython_positional(monkeypatch):
    """旧版 dmPython 关键字签名不符时按 (user, pw, host:port) 重试。"""
    calls = []

    def _fake_import(db_type):
        assert db_type == "dameng"
        mod = types.SimpleNamespace(__name__="dmPython")

        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            if args:
                return _FakeConn(("SELECT 1",))
            raise TypeError("connect() got an unexpected keyword argument 'server'")

        mod.connect = connect
        return mod

    monkeypatch.setattr(native_conn, "_import_driver", _fake_import)
    conn = native_conn.connect("dameng", "10.0.0.9", 5236, "", "SYSDBA", "pw")
    assert isinstance(conn, _FakeConn)
    assert len(calls) == 2
    assert calls[0][1]["user"] == "SYSDBA"
    assert calls[1][0] == ("SYSDBA", "pw", "10.0.0.9:5236")


def test_native_probe_sqls_candidates():
    """达梦有 DUAL 候选；其他库型保持单条探活不变。"""
    assert native_conn._probe_sqls("dameng") == ("SELECT 1", "SELECT 1 FROM DUAL")
    assert native_conn._probe_sqls("mysql") == ("SELECT 1",)
    assert native_conn._probe_sqls("postgresql") == ("SELECT 1",)


def test_native_test_connection_dameng_dual_fallback(monkeypatch):
    """原生直连：SELECT 1 失败后 FROM DUAL 成功即判连通。"""
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _NoopCtx())
    conn = _FakeConn(("SELECT 1 FROM DUAL",))
    monkeypatch.setattr(native_conn, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(native_conn, "_import_driver",
                        lambda db_type: types.SimpleNamespace(__name__="dmPython"))
    ok, msg, info = native_conn.test_connection(
        "dameng", "10.0.0.9", 5236, "", "SYSDBA", "pw")
    assert ok is True, msg
    assert conn.cur.executed == ["SELECT 1", "SELECT 1 FROM DUAL"]
    assert info["mode"] == "native"


def test_probe_via_driver_candidates():
    """驱动探活通用能力：候选序列逐条尝试，共用一次连接。"""
    mod = types.SimpleNamespace(__name__="dmPython")
    conn = _FakeConn(("SELECT 1 FROM DUAL",))
    ok, msg = probe._probe_via_driver(
        mod, lambda: conn, ("SELECT 1", "SELECT 1 FROM DUAL"), "达梦 连接正常")
    assert ok is True, msg
    assert conn.cur.executed == ["SELECT 1", "SELECT 1 FROM DUAL"]


def test_probe_dameng_passes_candidates(monkeypatch):
    """T06 任务预检的达梦分支同样带 DM7 候选。"""
    captured = {}
    mod = types.SimpleNamespace(__name__="dmPython")
    monkeypatch.setattr(probe, "_try_import", lambda *names: (mod, ""))

    def _fake_via_driver(module, connect, sqls, ok_msg):
        captured["sqls"] = sqls
        return True, "OK"

    monkeypatch.setattr(probe, "_probe_via_driver", _fake_via_driver)
    ok, _ = probe._probe_dameng("10.0.0.9", 5236, "SYSDBA", "pw", "", 8)
    assert ok is True
    assert captured["sqls"] == ("SELECT 1", "SELECT 1 FROM DUAL")


def test_jdbc_test_connection_dameng_both_errors(monkeypatch):
    """双通道都失败时原生 -2501 与 JDBC -7002 必须同时透出。"""
    monkeypatch.setattr(native_conn, "test_connection", lambda *a, **kw: (
        False, "直连失败（1021 ms）: dameng 直连 10.0.0.9:5236 失败: "
               "[CODE:-2501]Invalid username or password", None))
    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: _raise(
        "[CODE:-7002]create SOCKET connection failure"))
    ok, msg, _ = jdbc.test_connection("dameng", "10.0.0.9", 5236, "", "SYSDBA", "pw")
    assert ok is False
    assert "原生" in msg and "JDBC" in msg
    assert "-2501" in msg and "-7002" in msg
    assert "大写" in msg and "5236" in msg and "DM7" in msg


def test_jdbc_test_connection_dameng_dual_fallback(monkeypatch):
    """JDBC 兜底通道对 DM7 同样追加 DUAL 候选。"""
    monkeypatch.setattr(native_conn, "test_connection", lambda *a, **kw: (
        False, "直连失败: SELECT 1 不受支持", None))
    conn = _FakeConn(("SELECT 1 FROM DUAL",))
    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(jdbc, "effective_config", lambda db_type: {
        "probe": "SELECT 1", "class": "dm.jdbc.driver.DmDriver"})
    ok, msg, info = jdbc.test_connection("dameng", "10.0.0.9", 5236, "", "SYSDBA", "pw")
    assert ok is True, msg
    assert conn.cur.executed == ["SELECT 1", "SELECT 1 FROM DUAL"]
    assert info["mode"] == "jdbc"


def test_dameng_error_hint_only_for_known_codes():
    """未知错误不附加猜测提示，避免误导排障。"""
    assert jdbc._dameng_error_hint("其他错误", "") == ""
    assert "大写" in jdbc._dameng_error_hint("[CODE:-2501]", "")
    assert "5236" in jdbc._dameng_error_hint("", "[CODE:-7002]")
