# -*- coding: utf-8 -*-
"""
openGauss（华为开源数据库）备份/恢复引擎。

openGauss 兼容 PostgreSQL 协议，自带 gs_dump / gs_restore / gsql /
gs_dumpall / gs_basebackup，用法分别与 pg_dump / pg_restore / psql /
pg_dumpall / pg_basebackup 一致。远程 SSH 通道由 core/remote_dump.py 的
_PG_FAMILY_TOOLING["opengauss"] 分支承接（GAUSSHOME/LD_LIBRARY_PATH 注入、
omm 用户探测）；本引擎主要解决：

1. 本机通道工具名映射（pg_* → gs_*），复用 PostgreSQL 引擎全部逻辑；
2. 远端物理备份参数差异：gs_basebackup 不支持 --checkpoint，需显式
   -X stream，且必须 -w 禁止交互式密码输入；
3. 远端工具探测用户为数据库 OS 用户 omm。

要求：
- 密码不出现在进程参数中，统一通过 PGPASSWORD 环境变量注入
  （gs_dump/gsql 等 PG 系客户端兼容读取）。
"""
from .postgresql import PostgreSQLEngine


# 本机通道工具名映射：PG 生态名 → openGauss 实际工具名
_TOOL_MAP = {
    "pg_dump": "gs_dump",
    "pg_restore": "gs_restore",
    "psql": "gsql",
    "pg_dumpall": "gs_dumpall",
    "pg_basebackup": "gs_basebackup",
}


class OpenGaussEngine(PostgreSQLEngine):
    """openGauss 备份/恢复引擎（复用 PostgreSQL 引擎逻辑 + 工具名映射）。"""

    db_type = "opengauss"
    display_name = "openGauss"
    required_clients = ["gs_dump", "gs_restore", "gsql"]
    physical_bundled_tools = ["gs_basebackup"]
    # openGauss 客户端工具在数据库服务器 omm 用户 profile 的 PATH/GAUSSHOME 中
    tool_check_user = "omm"
    # 远端物理备份：工具与默认端口/用户与 PG 不同
    _phys_tool = "gs_basebackup"
    _phys_default_port = 26000
    _phys_default_user = "omm"
    _phys_label = "gs_basebackup"

    # ------------------------------------------------------------------
    # 工具解析：把 PG 生态工具名翻译为 openGauss 工具名后走统一解析器
    # ------------------------------------------------------------------
    def _resolve_local_tool(self, name: str):
        return super()._resolve_local_tool(_TOOL_MAP.get(name, name))

    def _remote_phys_backup(self, ssh_host: dict) -> dict:
        """gs_basebackup 参数差异：不支持 --checkpoint，需 -X stream -w，
        以 omm 用户探测工具，注入 GAUSSHOME/LD_LIBRARY_PATH 环境。"""
        from core import remote_dump
        return remote_dump.remote_physical_backup(
            self.task, ssh_host,
            tool=self._phys_tool, default_port=self._phys_default_port,
            default_user=self._phys_default_user,
            extra_args_key="pg_basebackup_extra_args", tool_label=self._phys_label,
            base_flags=["-Ft", "-z", "-X", "stream", "-w"],
            check_user="omm",
        )
