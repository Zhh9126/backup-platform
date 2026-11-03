# -*- coding: utf-8 -*-
"""同步插件注册中心。"""
from .base import PluginRegistry
from .mysql import MySQLPlugin
from .postgresql import PostgreSQLPlugin
from .oracle import OraclePlugin
from .dameng import DamengPlugin
from .sqlserver import SQLServerPlugin

registry = PluginRegistry()
registry.register("mysql", MySQLPlugin)
registry.register("mariadb", MySQLPlugin)
registry.register("postgresql", PostgreSQLPlugin)
registry.register("oracle", OraclePlugin)
registry.register("dameng", DamengPlugin)
registry.register("sqlserver", SQLServerPlugin)
# 金仓复用 PG 协议（sys_dump/psql 兼容）；驱动侧 psycopg2 直连可用
registry.register("kingbase", PostgreSQLPlugin)
# 国产数据库协议兼容层：瀚高/优炫（PG 系）、TiDB/OceanBase（MySQL 协议）
registry.register("highgo", PostgreSQLPlugin)
registry.register("uxdb", PostgreSQLPlugin)
registry.register("tidb", MySQLPlugin)
registry.register("oceanbase", MySQLPlugin)

__all__ = ["registry"]
