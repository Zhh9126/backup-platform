# -*- coding: utf-8 -*-
"""瀚高数据库（HighGo，瀚高股份，PostgreSQL 系国产商业发行版）备份/恢复引擎。

瀚高数据库内核为 PostgreSQL，客户端工具链（pg_dump / pg_restore / psql /
pg_basebackup）与社区版同名同用法，默认端口 5866（企业版安全模式）。
因此本引擎直接继承 PostgreSQLEngine，仅差异化 db_type / 展示名 / 默认端口。

远端 SSH 通道由 core/remote_dump.py 的 _PG_FAMILY_TOOLING["highgo"] 分支承接；
同步/迁移通道由 core/sync/plugins 注册表复用 PostgreSQLPlugin 承接。

诚实边界：按 PostgreSQL 协议兼容层接入（未真机验证），现场首用前建议先跑
一次「预检 + 小库备份 + 恢复校验」确认版本兼容性（瀚高安全版部分系统表有差异）。
"""
from .postgresql import PostgreSQLEngine


class HighGoEngine(PostgreSQLEngine):
    """瀚高数据库备份/恢复引擎（继承 PostgreSQL 引擎，工具链同名同用法）。"""

    db_type = "highgo"
    display_name = "HighGo（瀚高）"
    # 瀚高安全版默认端口 5866（企业版）；任务可覆盖
    default_port = 5866
