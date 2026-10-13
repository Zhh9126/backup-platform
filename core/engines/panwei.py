# -*- coding: utf-8 -*-
"""
磐维（PanWeiDB，中国移动 × 华为，openGauss 系商业发行版）备份/恢复引擎。

磐维分集中式与分布式两个形态，二者均兼容 openGauss 工具链：
gs_dump / gs_restore / gsql / gs_dumpall / gs_basebackup 与 openGauss
同名同用法，GAUSSHOME/LD_LIBRARY_PATH 环境约定一致，sha256 认证同样
不读 PGPASSWORD（须 -W 显式传密码）。因此本引擎直接继承
OpenGaussEngine，仅差异化：

1. db_type / 展示名 / 默认端口（磐维集中式常用默认 12800，可在任务中改）；
2. 物理备份默认端口/用户（磐维安装 OS 用户通常为 omm/panwei，探测失败
   时会回退 root bash -lc 与目录 glob，不会误报缺失）；
3. 分布式形态说明：gs_dump 经 CN 协调器导出全库数据，备份/恢复语义与
   集中式一致；gs_probackup（磐维管理平台底层）暂不直连，物理备份走
   gs_basebackup（-X fetch，与 openGauss 相同参数集）。

远端 SSH 通道由 core/remote_dump.py 的 _PG_FAMILY_TOOLING["panwei"]
分支承接；本机通道复用 PostgreSQL 引擎逻辑 + 工具名映射（pg_* → gs_*）。
"""
from .opengauss import OpenGaussEngine, _TOOL_MAP


class PanweiEngine(OpenGaussEngine):
    """磐维（PanWeiDB）备份/恢复引擎（继承 openGauss 引擎，工具链同名同用法）。"""

    db_type = "panwei"
    display_name = "PanWeiDB（磐维）"
    required_clients = ["gs_dump", "gs_restore", "gsql"]
    physical_bundled_tools = ["gs_basebackup"]
    # 磐维数据库服务器 OS 用户通常为 omm（openGauss 系安装器默认），
    # 探测失败时 remote_dump 会回退 root bash -lc 与常见目录 glob。
    tool_check_user = "omm"
    # 物理备份：gs_basebackup，磐维集中式常用默认端口 12800
    _phys_tool = "gs_basebackup"
    _phys_default_port = 12800
    _phys_default_user = "omm"
    _phys_label = "gs_basebackup"
    # remote_dump 分发 / dump_format 解析 / logical_full 全实例按 panwei 走 gs_* 工具族
    _dump_db_type = "panwei"
    # gs_dump 库名为位置参数（不支持 PG 的 -d 短选项写法）——继承自 openGauss
    _dump_dbname_positional = True
    # sha256 认证不读 PGPASSWORD，本机/远端命令一律 -W 显式传密码——继承自 openGauss
    _pass_via_w = True
