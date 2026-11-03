# -*- coding: utf-8 -*-
"""TiDB（PingCAP，分布式 MySQL 协议兼容数据库）备份/恢复引擎。

TiDB 高度兼容 MySQL 协议与语法，mysqldump 逻辑备份可用（官方推荐
dumpling，但 mysqldump 大部分场景兼容）。本引擎继承 MySQLEngine，仅差异化：
db_type / 展示名 / 默认端口 4000，并将物理备份明确置为不支持
（xtrabackup 直接读写 InnoDB 数据文件，对 TiDB 分布式存储层不适用；
TiDB 官方物理备份工具为 br，零 Agent 推送模式暂未接入——如实拒绝不伪装）。

诚实边界：按 MySQL 协议兼容层接入（未真机验证）；全实例逻辑备份
（mysqldump --all-databases）在 TiDB 上可能遇到系统库兼容差异，建议
优先使用单库/单表备份。现场首用前建议先跑预检 + 小库备份验证。
"""
from .mysql import MySQLEngine
from core.engines.base import BackupResult, BackupStatus
from core.engines.base import BackupType


class TiDBEngine(MySQLEngine):
    """TiDB 备份/恢复引擎（继承 MySQL 引擎，仅逻辑备份通道）。"""

    db_type = "tidb"
    display_name = "TiDB"
    default_port = 4000

    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message="TiDB 不支持 xtrabackup 物理备份（分布式存储层不适用，"
                    "官方物理备份工具为 br，暂未接入）。请使用逻辑备份："
                    "单库/单表 mysqldump 兼容通道。")

    def _backup_physical_remote(self, ssh_host: dict, backup_type) -> BackupResult:
        return self._backup_physical(backup_type)
