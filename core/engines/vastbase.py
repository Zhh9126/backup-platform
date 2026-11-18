# -*- coding: utf-8 -*-
"""海量数据 Vastbase（PostgreSQL 系国产商业发行版）备份/恢复引擎。

Vastbase G100 内核基于 PostgreSQL/openGauss，客户端工具链为
``vs_dump / vs_restore / vsql``（部分版本提供 pg_* 同名工具）。
本引擎继承 PostgreSQLEngine：远端通道会优先按 pg_* 解析，
若现场只提供 vs_* 前缀，请在任务的「工具路径」里补充对应目录。

诚实边界：协议兼容层接入，未真机验证；首次使用建议先跑预检与恢复校验。
"""
from .postgresql import PostgreSQLEngine


class VastbaseEngine(PostgreSQLEngine):
    """Vastbase 备份/恢复引擎（继承 PostgreSQL 引擎）。"""

    db_type = "vastbase"
    display_name = "Vastbase（海量）"
    default_port = 5432
