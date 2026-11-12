# -*- coding: utf-8 -*-
"""回归：SQL Server 2008 等仅支持 TLS 1.0 的旧版服务端兼容开关。

背景（2026-11-12）：JDK 11+ 的 java.security 默认禁用 TLSv1/TLSv1.1，
而 SQL Server 登录包始终走 TLS 握手（encrypt=false 也绕不开），现场报
"TLS10 is not accepted by client preferences [TLS13, TLS12]"。

修复口径：显式开关在 JVM 启动阶段放开禁用项；**不锁定 sslProtocol**，
客户端仍优先协商 TLS1.2/1.3，只有对端仅支持 TLS1.0 才回落。
"""
from core import jdbc


def _raise(msg):
    raise RuntimeError(msg)


def test_relax_only_removes_legacy_protocols():
    src = ("SSLv3, TLSv1, TLSv1.1, RC4, DES, MD5withRSA, "
           "DH keySize < 1024, EC keySize < 224, 3DES_EDE_CBC, anon, NULL, "
           "include jdk.disabled.namedCurves")
    out, removed = jdbc.relax_disabled_algorithms(src)
    assert removed == ["TLSv1", "TLSv1.1"]
    assert "TLSv1" not in out and "TLSv1.1" not in out
    for keep in ("SSLv3", "RC4", "DES", "DH keySize < 1024",
                 "include jdk.disabled.namedCurves"):
        assert keep in out


def test_relax_is_idempotent_and_handles_empty():
    assert jdbc.relax_disabled_algorithms("") == ("", [])
    once, _ = jdbc.relax_disabled_algorithms("TLSv1, TLSv1.1, RC4")
    twice, removed = jdbc.relax_disabled_algorithms(once)
    assert twice == once and removed == []


def test_config_env_overrides_system_config(monkeypatch):
    monkeypatch.setenv(jdbc.LEGACY_TLS_ENV, "1")
    assert jdbc.legacy_tls_configured() is True
    monkeypatch.setenv(jdbc.LEGACY_TLS_ENV, "0")
    assert jdbc.legacy_tls_configured() is False


def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv(jdbc.LEGACY_TLS_ENV, raising=False)
    import core.db as db
    monkeypatch.setattr(db, "get_system_config", lambda key, default=None: default)
    assert jdbc.legacy_tls_configured() is False


def test_tls_error_hint_matches_real_driver_error():
    err = ("com.microsoft.sqlserver.jdbc.SQLServerException: ... "
           "Error: (protocol_version) The server selected protocol version "
           "TLS10 is not accepted by client preferences [TLS13, TLS12]")
    hint = jdbc.tls_error_hint(err)
    assert "旧版 TLS" in hint and "重启平台服务" in hint
    assert jdbc.tls_error_hint("Login failed for user 'sa'") == ""


def test_test_connection_appends_hint(monkeypatch):
    from core import native_conn
    err = ("The server selected protocol version TLS10 is not accepted "
           "by client preferences [TLS13, TLS12]")
    monkeypatch.setattr(native_conn, "test_connection", lambda *a, **kw: (
        False, "直连失败（0 ms）: 暂不支持直连类型: sqlserver", None))
    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: _raise(err))
    ok, msg, _ = jdbc.test_connection(
        "sqlserver", "10.0.0.1", 1433, "master", "sa", "x")
    assert ok is False
    assert "旧版 TLS" in msg


def test_default_url_not_pinned_to_tlsv1():
    url = jdbc.build_url("sqlserver", "10.0.0.1", 1433, "master")
    assert "sslProtocol" not in url
    assert "encrypt=false" in url and "socketTimeout=600000" in url


def test_status_shape_without_tls_probe(monkeypatch):
    monkeypatch.delenv(jdbc.LEGACY_TLS_ENV, raising=False)
    import core.db as db
    monkeypatch.setattr(db, "get_system_config", lambda key, default=None: default)
    st = jdbc.legacy_tls_status()
    assert {"configured", "applied", "needs_restart", "jvm_started", "reason"} <= set(st)
    assert st["configured"] is False and st["needs_restart"] is False


def test_sqlserver_backup_channel_wraps_tls_error(monkeypatch):
    import jaydebeapi
    from core import sqlserver_jdbc
    err = "Error: (protocol_version) TLS10 is not accepted by client preferences"
    monkeypatch.setattr(jaydebeapi, "connect", lambda *a, **kw: _raise(err))
    monkeypatch.setattr(jdbc, "_ensure_jvm", lambda: None)
    try:
        sqlserver_jdbc._connect({"host": "10.0.0.1", "port": 1433,
                                 "username": "sa", "password": "x",
                                 "db_name": "master"}, "master")
        assert False, "should raise"
    except RuntimeError as e:
        assert "旧版 TLS" in str(e)
