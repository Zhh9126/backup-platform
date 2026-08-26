# -*- coding: utf-8 -*-
"""同步插件注册中心。"""
from .base import PluginRegistry
from .mysql import MySQLPlugin
from .postgresql import PostgreSQLPlugin

registry = PluginRegistry()
registry.register("mysql", MySQLPlugin)
registry.register("mariadb", MySQLPlugin)
registry.register("postgresql", PostgreSQLPlugin)

__all__ = ["registry"]
