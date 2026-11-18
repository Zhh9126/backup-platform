# -*- coding: utf-8 -*-
"""中兴 GoldenDB（MySQL 协议兼容，分布式 OLTP）备份/恢复引擎。

GoldenDB 对外提供 MySQL 兼容协议与 ``mysqldump / mysql`` 工具族兼容入口，
分布式架构下由计算节点（CN）承接 SQL。本引擎继承 MySQLEngine。

诚实方向：
- 逻辑备份走单一 CN/Proxy 入口时得到的是**该入口可见的数据库视图**；
- 物理备份（XtraBackup）需在数据节点（DN）上执行，平台不感知分片拓扑，
  如有 DN 级需求请按节点分别建任务。
未真机验证，首次使用请先跑预检与小库恢复校验。
"""
from .mysql import MySQLEngine


class GoldenDBEngine(MySQLEngine):
    """GoldenDB 备份/恢复引擎（继承 MySQL 引擎）。"""

    db_type = "goldendb"
    display_name = "GoldenDB（中兴）"
    default_port = 3306
