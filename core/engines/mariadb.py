# -*- coding: utf-8 -*-
"""
MariaDB 备份引擎实现。

MariaDB 与 MySQL 在协议和客户端二进制（mysqldump / mysql）层面高度兼容，
但作为独立的 db_type，便于：
  1. 在任务列表、仪表盘按数据库类型精确归类（避免混入 MySQL）；
  2. 后续若 MariaDB 走 mariadb-dump 工具，可在此处独立扩展。

实现策略：复用 MySQL 引擎的所有方法，仅替换 db_type / display_name。
"""
from core.engines.mysql import MySQLEngine


class MariaDBEngine(MySQLEngine):
    """MariaDB 备份引擎（兼容 MySQL 客户端协议）。"""

    db_type = "mariadb"
    display_name = "MariaDB"
    required_clients = ["mysqldump", "mysql"]  # MariaDB 自带 mysqldump/mysql 客户端
