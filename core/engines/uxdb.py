# -*- coding: utf-8 -*-
"""优炫数据库（UXDB，优炫软件，PostgreSQL 系国产数据库）备份/恢复引擎。

优炫 UXDB 内核为 PostgreSQL，客户端工具链（pg_dump / pg_restore / psql /
pg_basebackup）与社区版同名同用法，默认端口 5432。
本引擎直接继承 PostgreSQLEngine，仅差异化 db_type / 展示名。

远端 SSH 通道由 core/remote_dump.py 的 _PG_FAMILY_TOOLING["uxdb"] 分支承接；
同步/迁移通道由 core/sync/plugins 注册表复用 PostgreSQLPlugin 承接。

诚实边界：按 PostgreSQL 协议兼容层接入（未真机验证），现场首用前建议先跑
一次「预检 + 小库备份 + 恢复校验」确认版本兼容性。
"""
from .postgresql import PostgreSQLEngine


class UXDBEngine(PostgreSQLEngine):
    """优炫数据库备份/恢复引擎（继承 PostgreSQL 引擎，工具链同名同用法）。"""

    db_type = "uxdb"
    display_name = "UXDB（优炫）"
