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
