---
name: mysql-backup
description: >
  MySQL 数据库备份与恢复技能。支持逻辑备份(mysqldump)和物理备份(xtrabackup全量+增量)，
  以及 binlog 备份与 PITR(时间点恢复)。当用户需要对 MySQL 数据库进行备份、恢复、PITR
  或备份任务管理时，应使用此技能。依赖 percona-xtrabackup 插件进行物理备份。
---

# MySQL 备份技能

## 概述

此技能封装了 MySQL 数据库的完整备份与恢复能力，通过备份管理平台 API（`http://localhost:8080`）操作。

### 备份引擎: `MySQLEngine` (adapter_tier: peripheral_api)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份** | `mysqldump`，支持单库/分表/全实例/仅结构/仅数据，`--single-transaction` 保证一致性 |
| **物理全量备份** | `xtrabackup --backup`，支持 zstd 压缩 |
| **物理增量备份** | `xtrabackup --incremental-basedir`，基于上次全量做增量 |
| **增量链合成** | `xtrabackup --prepare --incremental-dir` 合并增量到全量 |
| **Binlog 备份** | `mysqlbinlog --read-from-remote-server` 远程抽取，附带 `.meta.json` |
| **PITR 恢复** | 基于目标时间点生成 binlog 重放脚本 |
| **跨主机恢复** | SFTP + SSH 远程恢复 |

## 备份类型（BackupType）

- `FULL` — 全量备份（逻辑或物理）
- `INCREMENTAL` — 增量备份（物理模式需要先有全量基础）
- `DIFFERENTIAL` — 差异备份（自动回退到全量）

## 备份模式（BackupMode）

- `LOGICAL` — mysqldump 导出 `.sql` / `.sql.gz`
- `PHYSICAL` — xtrabackup 物理复制

## API 端点

平台 API 基础路径: `http://localhost:8080/api`

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（指定 db_type=mysql, backup_mode, backup_type） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 手动执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=mysql` | 查询备份记录 |
| GET | `/plugins?category=mysql` | 查看 MySQL 相关插件 |

## 插件依赖

- **percona-xtrabackup-80** — MySQL 8.0 物理备份
- **percona-xtrabackup-24** — MySQL 5.7 及以下物理备份
- **percona-toolkit** — 辅助工具集

使用插件管理页面 `/plugins` 进行一键安装。

## 使用示例

### 创建全量逻辑备份任务
通过 API POST `/tasks`，设置 `db_type=mysql`, `backup_mode=LOGICAL`, `backup_type=FULL`。

### 创建物理增量备份任务
先确保已有全量物理备份记录，然后创建 `INCREMENTAL` 类型任务（`backup_mode=PHYSICAL`）。

### PITR 恢复
1. 先调用 `backup_binlog()` 抽取 binlog
2. 调用 `pitr_target_time(target_dt)` 生成重放脚本
3. 先恢复全量备份，再重放 binlog 到目标时间点

### 安全注意事项
- 密码通过临时 `.cnf` 文件（0600 权限）传递给 mysqldump/xtrabackup
- 使用 `--defaults-extra-file` 引用，命令中不出现明文密码
- 执行完成后自动删除临时 `.cnf`
