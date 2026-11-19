# -*- coding: utf-8 -*-
"""Oracle 11g/19c 探测通道兼容性单测（纯 mock，不连真实库）。

背景（现场缺陷）：python-oracledb 的 thin 模式**不支持 Oracle 11.2 及更早版本**
（DPY-3010）。旧实现把这条「通道不适用」当成「连接失败」，导致：
  1. 11g 现场界面显示「连接失败」——用户误判平台不支持 11g；
  2. 更严重的是它掩盖了 JDBC 通道的真实报错（如密码错 ORA-01017）；
  3. `_probe_via_jdbc` 恒传空库名，Oracle 走 JDBC 时报 ORA-12261。

覆盖点：
  - thin_version_blocked 的判别边界
  - native_conn.connect 对 11g 抛 DriverUnavailable（而非 RuntimeError）
  - _probe_oracle：thin 被版本挡住 → 自动切 JDBC，并标注通道
  - _probe_oracle：真实失败（ORA-01017）不被兜底掩盖
  - _probe_via_jdbc：库名/service 必须透传给 jdbc.connect
  - jdbc.test_connection：11g 双通道失败时以 JDBC 真实错误为主
"""
import pytest

from core import native_conn, probe, jdbc


# ---------------------------------------------------------------------------
# thin_version_blocked
# ---------------------------------------------------------------------------

DPY_3010 = (
    "DPY-6005: cannot connect to database (CONNECTION_ID=abc).\n"
    "DPY-3010: connections to this database server version are not supported "
    "by python-oracledb in thin mode"
)


def test_thin_blocked_detects_dpy3010():
    assert native_conn.thin_version_blocked(DPY_3010) is True
    assert native_conn.thin_version_blocked("DPY-3019: unsupported") is True
    assert native_conn.thin_version_blocked(
        "connections to this database server version are not supported "
        "by python-oracledb in thin mode") is True


def test_thin_blocked_ignores_real_db_errors():
    # 密码错/服务名错是真实失败，不能当成「通道不适用」而切换通道
    assert native_conn.thin_version_blocked(
        "ORA-01017: invalid username/password; logon denied") is False
    assert native_conn.thin_version_blocked(
        "DPY-6001: Cannot connect to database. Service orcl is not registered "
        "with the listener at host \"192.168.220.133\" port 1521.") is False
    assert native_conn.thin_version_blocked("") is False
    assert native_conn.thin_version_blocked(None) is False


# ---------------------------------------------------------------------------
# native_conn.connect：11g 抛 DriverUnavailable
# ---------------------------------------------------------------------------

class _FakeOracleModule:
    """模拟 python-oracledb：连 11.2 服务端时抛 DPY-3010。"""

    def __init__(self, error):
        self.error = error
        self.calls = 0
        self.__name__ = "oracledb"

    def makedsn(self, host, port, service_name=None):
        return "%s:%s/%s" % (host, port, service_name)

    def connect(self, **kwargs):
        self.calls += 1
        raise RuntimeError(self.error)


def test_connect_11g_raises_driver_unavailable(monkeypatch):
    fake = _FakeOracleModule(DPY_3010)
    monkeypatch.setattr(native_conn, "_import_driver", lambda db_type: fake)

    with pytest.raises(native_conn.DriverUnavailable) as ei:
        native_conn.connect("oracle", "192.168.220.133", 1521, "orcl11g",
                            "system", "oracle")
    msg = str(ei.value)
    assert "11.2" in msg and "DPY-3010" in msg
    # 必须只尝试一次就切换通道，不做无意义的 3 次重试
    assert fake.calls == 1


def test_connect_password_error_not_masked(monkeypatch):
    """真实数据库错误（ORA-01017）应按普通 ConnectionError 抛出，不当通道问题。"""
    fake = _FakeOracleModule("ORA-01017: invalid username/password; logon denied")
    monkeypatch.setattr(native_conn, "_import_driver", lambda db_type: fake)

    with pytest.raises(Exception) as ei:
        native_conn.connect("oracle", "192.168.220.133", 1521, "orcl11g",
                            "system", "bad")
    assert not isinstance(ei.value, native_conn.DriverUnavailable)
    assert "ORA-01017" in str(ei.value)


# ---------------------------------------------------------------------------
# probe._probe_oracle：通道切换
# ---------------------------------------------------------------------------

def test_probe_oracle_falls_back_to_jdbc_on_11g(monkeypatch):
    """thin 被版本挡住 → 自动走 JDBC，且消息标注通道（界面展示依据）。"""
    monkeypatch.setattr(probe, "_try_import",
                        lambda *names: (_FakeOracleModule(DPY_3010), ""))
    monkeypatch.setattr(probe, "_probe_via_driver",
                        lambda *a, **k: (False, "连接失败: " + DPY_3010))
    captured = {}

    def _fake_jdbc(db_type, h, p, u, pw, t, db_name=""):
        captured.update(db_type=db_type, db_name=db_name)
        return True, "oracle 连接正常（JDBC）"

    monkeypatch.setattr(probe, "_probe_via_jdbc", _fake_jdbc)

    ok, msg = probe._probe_oracle("192.168.220.133", 1521, "system", "oracle",
                                  "orcl11g", 8)
    assert ok is True
    assert "JDBC" in msg
    assert "11g" in msg or "11.2" in msg
    # service 名必须透传给 JDBC（否则 URL 变 //host:port/ → ORA-12261）
    assert captured["db_name"] == "orcl11g"


def test_probe_oracle_real_failure_not_fallback(monkeypatch):
    """密码错这类真实失败不能被兜底掩盖，也不再尝试 JDBC。"""
    monkeypatch.setattr(probe, "_try_import",
                        lambda *names: (_FakeOracleModule("x"), ""))
    monkeypatch.setattr(probe, "_probe_via_driver",
                        lambda *a, **k: (False, "连接失败: ORA-01017: invalid username/password"))
    called = {"n": 0}

    def _fake_jdbc(*a, **k):
        called["n"] += 1
        return True, "should not be used"

    monkeypatch.setattr(probe, "_probe_via_jdbc", _fake_jdbc)

    ok, msg = probe._probe_oracle("192.168.220.133", 1521, "system", "bad",
                                  "orcl11g", 8)
    assert ok is False
    assert "ORA-01017" in msg
    assert called["n"] == 0


def test_probe_oracle_thin_ok_on_19c(monkeypatch):
    """12.1+ 仍走 thin（不应无谓启动 JVM）。"""
    monkeypatch.setattr(probe, "_try_import",
                        lambda *names: (_FakeOracleModule("x"), ""))
    monkeypatch.setattr(probe, "_probe_via_driver",
                        lambda *a, **k: (True, "Oracle 连接正常（oracledb）"))
    called = {"n": 0}
    monkeypatch.setattr(probe, "_probe_via_jdbc",
                        lambda *a, **k: (called.__setitem__("n", 1), (True, ""))[1])

    ok, msg = probe._probe_oracle("192.168.220.129", 1521, "system", "oracle",
                                  "orcl11g", 8)
    assert ok is True
    assert "oracledb" in msg
    assert called["n"] == 0


def test_probe_oracle_jdbc_db_error_is_failure(monkeypatch):
    """JDBC 已与实例交互（ORA-xxxxx）→ 明确失败，不能报「无法确认」。"""
    monkeypatch.setattr(probe, "_try_import",
                        lambda *names: (_FakeOracleModule(DPY_3010), ""))
    monkeypatch.setattr(probe, "_probe_via_driver",
                        lambda *a, **k: (False, "连接失败: " + DPY_3010))
    monkeypatch.setattr(probe, "_probe_via_jdbc",
                        lambda *a, **k: (False, "JDBC 连接失败: ORA-01017"))
    monkeypatch.setattr(probe, "_first_client", lambda *names: "")

    ok, msg = probe._probe_oracle("192.168.220.133", 1521, "system", "bad",
                                  "orcl11g", 8)
    assert ok is False
    assert "ORA-01017" in msg


# ---------------------------------------------------------------------------
# _probe_via_jdbc：库名必须透传
# ---------------------------------------------------------------------------

def test_probe_via_jdbc_passes_db_name(monkeypatch):
    seen = {}

    class _Cur:
        def execute(self, sql):
            seen["sql"] = sql

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    def _fake_connect(db_type, host, port, db, user, password, timeout=None):
        seen.update(db_type=db_type, host=host, port=port, db=db, user=user)
        return _Conn()

    monkeypatch.setattr(jdbc, "connect", _fake_connect)
    ok, msg = probe._probe_via_jdbc("oracle", "192.168.220.133", 1521,
                                    "system", "oracle", 8, "orcl11g")
    assert ok is True
    assert seen["db"] == "orcl11g"
    assert "DUAL" in seen["sql"]


# ---------------------------------------------------------------------------
# jdbc.test_connection：错误归因
# ---------------------------------------------------------------------------

def test_jdbc_test_connection_11g_reports_jdbc_error(monkeypatch):
    """11g：原生通道「不适用」，最终失败消息必须来自 JDBC（真原因）。"""
    monkeypatch.setattr(native_conn, "test_connection",
                        lambda *a, **k: (False, "thin 不支持 11.2（DPY-3010）", None))

    def _boom(*a, **k):
        raise RuntimeError("ORA-01017: invalid username/password; logon denied")

    monkeypatch.setattr(jdbc, "connect", _boom)

    ok, msg, info = jdbc.test_connection("oracle", "192.168.220.133", 1521,
                                         "orcl11g", "system", "bad")
    assert ok is False
    assert "ORA-01017" in msg
    assert "DPY-3010" not in msg
