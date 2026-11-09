# -*- coding: utf-8 -*-
"""回归：离线包全新部署时 SQL Server JDBC 逻辑备份必须开箱可用。

背景（2026-11-09）：全新离线部署的 instance/ 为空，没有页面注册的
jdbc_user_registry.json；若 DRIVER_CONFIG 未内置 sqlserver，逻辑备份会报
「暂不支持 JDBC 连接类型: sqlserver」，而物理备份又需 SSH，两条通道都断。
驱动 jar 本就随镜像交付，因此必须内置注册。
"""
from core import jdbc


def test_sqlserver_is_builtin_driver():
    assert "sqlserver" in jdbc.DRIVER_CONFIG
    cfg = jdbc.DRIVER_CONFIG["sqlserver"]
    assert cfg["class"] == "com.microsoft.sqlserver.jdbc.SQLServerDriver"
    assert cfg["jar"] == "mssql-jdbc-13.4.0.jre11.jar"
    assert all(tok in cfg["url"] for tok in ("{host}", "{port}", "{db}"))
    assert "databaseName" in cfg["url"]


def test_sqlserver_jar_is_shipped():
    jar, cls, url, probe, list_sql, flt = jdbc._resolve_config("sqlserver")
    assert jar.exists() and jar.stat().st_size > 100000
    assert cls == "com.microsoft.sqlserver.jdbc.SQLServerDriver"
    assert "sys.databases" in list_sql


def test_empty_registry_still_resolves(monkeypatch):
    monkeypatch.setattr(jdbc, "_load_user_registry", lambda: {})
    cfg = jdbc.effective_config("sqlserver")
    assert cfg["jar"] == "mssql-jdbc-13.4.0.jre11.jar"
    assert "sqlserver" in jdbc.effective_db_types()
    jar, *_ = jdbc._resolve_config("sqlserver")
    assert jar.exists()


def test_user_registry_still_overrides_builtin(monkeypatch):
    monkeypatch.setattr(jdbc, "_load_user_registry", lambda: {
        "sqlserver": {"jar": "custom-mssql.jar",
                      "class": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
                      "url": "jdbc:sqlserver://{host}:{port}",
                      "probe": "SELECT 1", "list_sql": "SELECT 1",
                      "filter": []}})
    assert jdbc.effective_config("sqlserver")["jar"] == "custom-mssql.jar"


def test_registration_template_and_default_port():
    assert "sqlserver" in jdbc.KNOWN_DRIVER_TEMPLATES
    assert jdbc.DEFAULT_PORTS.get("sqlserver") == 1433


def test_connection_reports_real_jdbc_error(monkeypatch):
    """原生直连不适用时必须透出 JDBC 真实错误，不能报成「不支持 sqlserver」。"""
    from core import native_conn
    monkeypatch.setattr(native_conn, "test_connection", lambda *a, **kw: (
        False, "直连失败（0 ms）: 暂不支持直连类型: sqlserver", None))
    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: (
        _raise("Login failed: cannot open database 'sa'")))
    ok, msg, _ = jdbc.test_connection("sqlserver", "10.0.0.1", 1433, "sa", "sa", "x")
    assert ok is False
    assert "暂不支持直连类型" not in msg
    assert "cannot open database 'sa'" in msg
    assert "JDBC" in msg


def test_connection_keeps_native_error_when_real_failure(monkeypatch):
    """原生通道真实失败（端口不通）仍优先返回原生错误，行为不回退。"""
    from core import native_conn
    monkeypatch.setattr(native_conn, "test_connection", lambda *a, **kw: (
        False, "目标 10.0.0.1:1433 端口不可达", None))
    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: _raise("jdbc boom"))
    ok, msg, _ = jdbc.test_connection("sqlserver", "10.0.0.1", 1433, "", "sa", "x")
    assert ok is False
    assert "端口不可达" in msg


def test_list_databases_falls_back_to_jdbc(monkeypatch):
    from core import native_conn
    monkeypatch.setattr(native_conn, "list_databases", lambda *a, **kw: (
        _raise_value("暂不支持直连类型: sqlserver")))

    class _Conn:
        def cursor(self):
            return self

        def execute(self, sql):
            assert "sys.databases" in sql

        def fetchall(self):
            return [("master",), ("aidbm_test",)]

        def close(self):
            pass

    monkeypatch.setattr(jdbc, "connect", lambda *a, **kw: _Conn())
    assert jdbc.list_databases("sqlserver", "10.0.0.1", 1433, "", "sa", "x") == [
        "master", "aidbm_test"]


def _raise(msg):
    raise RuntimeError(msg)


def _raise_value(msg):
    raise ValueError(msg)


def test_sqlserver_socket_timeout_is_milliseconds(monkeypatch):
    """SQL Server JDBC 的 socketTimeout 单位是毫秒，15 会让连接 20ms 超时。"""
    monkeypatch.setattr(jdbc, "_load_user_registry", lambda: {})
    url = jdbc.effective_config("sqlserver")["url"]
    assert "socketTimeout=600000" in url
    assert "socketTimeout=15" not in url
    assert "socketTimeout=600000" in jdbc.KNOWN_DRIVER_TEMPLATES["sqlserver"]["url"]
    assert "socketTimeout=600000" in jdbc.build_url(
        "sqlserver", "10.0.0.1", 1433, "master")


def test_legacy_registry_timeout_is_repaired(monkeypatch):
    """旧容器卷里已保存的 15ms 注册项也要自动升级，不能让用户手工改 JSON。"""
    monkeypatch.setattr(jdbc, "_load_user_registry", lambda: {
        "sqlserver": {"jar": "mssql-jdbc-13.4.0.jre11.jar",
                      "class": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
                      "url": ("jdbc:sqlserver://{host}:{port};databaseName={db};"
                              "loginTimeout=8;socketTimeout=15"),
                      "probe": "SELECT 1", "list_sql": "SELECT 1",
                      "filter": []}})
    assert "socketTimeout=600000" in jdbc.effective_config("sqlserver")["url"]
    assert "socketTimeout=600000" in jdbc.build_url(
        "sqlserver", "10.0.0.1", 1433, "")


def test_other_db_types_timeout_untouched(monkeypatch):
    monkeypatch.setattr(jdbc, "_load_user_registry", lambda: {})
    assert "socketTimeout=15000" in jdbc.effective_config("mysql")["url"]


def test_list_tables_sqlserver_branch():
    """任务表单「拉表清单」必须支持 sqlserver（真机 E2E 暴露的缺口）。"""
    from core import data_compare

    class _Cur:
        def execute(self, sql, *args):
            self.sql = sql

        def fetchall(self):
            return [("t_e2e",), ("Orders",)]

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    names = data_compare._list_tables(_Conn(), "sqlserver", "aidbm_db", "")
    assert names == ["t_e2e", "Orders"]
    try:
        data_compare._list_tables(_Conn(), "sqlserver", "aidbm_db", "bad';DROP")
        raise AssertionError("非法 schema 应被拒绝")
    except ValueError:
        pass
