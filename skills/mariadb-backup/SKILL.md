---
name: mariadb-backup
description: >
  MariaDB 数据库备份与恢复技能。完全继承 MySQL 引擎，支持逻辑备份(mysqldump)和物理备份(mariabackup)，
  以及 binlog 备份与 PITR。当用户需要对 MariaDB 数据库进行备份、恢复或任务管理时，应使用此技能。
---

# MariaDB 备份技能

## 概述

此技能封装了 MariaDB 数据库的备份与恢复能力。MariaDB 引擎 (`MariaDBEngine`) 完全继承自 `MySQLEngine`，独立 db_type 便于精确归类，后续可独立扩展 mariadb-dump 等特性。

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份** | `mysqldump`（同 MySQL），支持单库/分表/全实例 |
| **物理全量备份** | `mariabackup --backup`（兼容 xtrabackup） |
| **物理增量备份** | `mariabackup --incremental-basedir` |
| **Binlog 备份** | `mysqlbinlog` 远程抽取 |
| **PITR 恢复** | 基于目标时间点生成 binlog 重放脚本 |
| **跨主机恢复** | SFTP + SSH 远程恢复 |

## 备份类型（BackupType）

- `FULL` — 全量备份
- `INCREMENTAL` — 增量备份
- `DIFFERENTIAL` — 差异备份（回退到全量）

## 备份模式（BackupMode）

- `LOGICAL` — mysqldump 导出
- `PHYSICAL` — mariabackup 物理复制

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=mariadb） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=mariadb` | 查询备份记录 |

## 插件依赖

- **mariabackup** — MariaDB 物理备份工具（推荐 MariaDB 10.2.6+ 内置版本）

## 使用示例

创建备份任务时指定 `db_type=mariadb` 即可，其余操作与 MySQL 完全相同。

```
POST /api/tasks
{
  "db_type": "mariadb",
  "backup_mode": "PHYSICAL",
  "backup_type": "FULL",
  "host": "192.168.1.100",
  "port": 3306,
  ...
}
```
