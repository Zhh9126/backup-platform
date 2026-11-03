# -*- coding: utf-8 -*-
"""OceanBase（蚂蚁集团，分布式数据库，MySQL 租户模式）备份/恢复引擎。

OceanBase MySQL 租户高度兼容 MySQL 协议与语法，mysqldump 逻辑备份可用。
本引擎继承 MySQLEngine，仅差异化：db_type / 展示名 / 默认端口 2881
（MySQL 租户协议端口；OBProxy 通常 2883），并将物理备份明确置为不支持
（xtrabackup 对 OB 自研存储层不适用；OB 物理备份走其内部备份恢复体系，
零 Agent 推送模式暂未接入——如实拒绝不伪装）。

诚实边界：按 MySQL 协议兼容层接入（未真机验证）；OceanBase 4.x 对
mysqldump 兼容性较好，但部分系统视图有差异，建议优先单库/单表备份。
现场首用前建议先跑预检 + 小库备份验证。
"""
from .mysql import MySQLEngine
from core.engines.base import BackupResult, BackupStatus
from core.engines.base import BackupType


class OceanBaseEngine(MySQLEngine):
    """OceanBase（MySQL 租户）备份/恢复引擎（继承 MySQL 引擎，仅逻辑备份）。"""

    db_type = "oceanbase"
    display_name = "OceanBase（MySQL 租户）"
    default_port = 2881

    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message="OceanBase 不支持 xtrabackup 物理备份（自研分布式存储层"
                    "不适用；OB 物理备份走其内部备份恢复体系，暂未接入）。"
                    "请使用逻辑备份：单库/单表 mysqldump 兼容通道。")

    def _backup_physical_remote(self, ssh_host: dict, backup_type) -> BackupResult:
        return self._backup_physical(backup_type)
