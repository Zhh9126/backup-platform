# -*- coding: utf-8 -*-
"""万里数据库 GreatDB（MySQL 协议兼容，国产分布式 OLTP）备份/恢复引擎。

GreatDB 通信协议与 MySQL 完全兼容，客户端工具链为 ``mysqldump / mysql /
mysqlbinlog``（部分版本提供 greatdump，参数与 mysqldump 一致）。
本引擎继承 MySQLEngine，仅差异化 db_type / 展示名 / 默认端口。

诚实边界：协议兼容层接入，未真机验证；分布式版本存在多个计算节点，
备份请指向**单一计算节点或负载均衡入口**（全实例备份语义按单节点数据面理解）。
"""
from .mysql import MySQLEngine


class GreatDBEngine(MySQLEngine):
    """GreatDB 备份/恢复引擎（继承 MySQL 引擎）。"""

    db_type = "greatdb"
    display_name = "GreatDB（万里）"
    default_port = 3306
