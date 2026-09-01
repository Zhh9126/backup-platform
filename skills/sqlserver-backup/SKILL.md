---
name: sqlserver-backup
description: >
  SQL Server 数据库备份与恢复技能。基于微软官方 T-SQL 备份体系：
  完整备份(BACKUP DATABASE)、差异备份(WITH DIFFERENTIAL)、事务日志备份
  (BACKUP LOG)，还原采用 RESTORE FILELISTONLY + RESTORE DATABASE WITH MOVE,
  REPLACE, RECOVERY，校验采用 RESTORE VERIFYONLY WITH CHECKSUM。
  当用户需要对 SQL Server 数据库进行备份、恢复或校验时，应使用此技能。
  支持 Linux 与 Windows 目标（经 SSH 执行 sqlcmd）。
---

# SQL Server 备份技能

## 概述

此技能封装了 SQL Server 数据库的完整备份与恢复能力，通过备份管理平台 API（`http://localhost:8080`）操作。

### 备份引擎: `SQLServerEngine` (adapter_tier: peripheral_api)

## 官方语法对照（Microsoft Learn：BACKUP/RESTORE (Transact-SQL)）

| 操作 | T-SQL |
|------|-------|
| **完整备份** | `BACKUP DATABASE [db] TO DISK = N'<path>.bak' WITH NAME=..., COMPRESSION, CHECKSUM, STATS=10, INIT` |
| **差异备份** | `BACKUP DATABASE [db] TO DISK = N'<path>.diff' WITH DIFFERENTIAL, COMPRESSION, CHECKSUM, INIT` |
| **日志备份** | `BACKUP LOG [db] TO DISK = N'<path>.trn' WITH COMPRESSION, CHECKSUM, INIT` |
| **还原** | `RESTORE FILELISTONLY` 获取逻辑文件名 → `RESTORE DATABASE [db] FROM DISK=N'...' WITH MOVE ..., REPLACE, RECOVERY` |
| **校验** | `RESTORE VERIFYONLY FROM DISK = N'...' WITH CHECKSUM` |

## 支持的能力

| 能力 | 说明 |
|------|------|
| **完整备份（FULL）** | `BACKUP DATABASE ... TO DISK`，启用 COMPRESSION + CHECKSUM，产物 `.bak` |
| **差异备份（DIFFERENTIAL）** | `WITH DIFFERENTIAL`，基于最近完整备份的差集，产物 `.diff` |
| **日志备份（INCREMENTAL）** | `BACKUP LOG`，事务日志链备份（需恢复模式为 FULL/BULK_LOGGED），产物 `.trn` |
| **恢复（RESTORE）** | SFTP 推送备份到服务器 → `RESTORE FILELISTONLY` 解析逻辑文件名 → `RESTORE DATABASE ... WITH MOVE, REPLACE, RECOVERY` |
| **恢复校验** | SHA256 + 服务器端 `RESTORE VERIFYONLY WITH CHECKSUM` |
| **库清单** | `sys.databases`（排除 master/tempdb/model/msdb 与离线库） |

## 前置条件

- **数据库服务器**安装 SQL Server（任意版本）+ `sqlcmd`（Linux 位于 `/opt/mssql-tools/bin`，Windows 随实例安装）——均为数据库自带，**平台不装任何客户端**
- 平台通过 SSH 连接数据库服务器（任务级 SSH 凭据或已纳管主机）
- 密码通过 sqlcmd 官方环境变量 `SQLCMDPASSWORD` 注入，不进命令行参数
- 日志备份要求数据库恢复模式为 FULL 或 BULK_LOGGED（简单模式下 SQL Server 会报错）

## 任务配置要点

| 配置 | 说明 |
|------|------|
| `db_type` | `sqlserver`（默认端口 1433） |
| `db_name` | **必填**（单库备份；master/tempdb/model/msdb 为系统库勿作业务备份） |
| `backup_type=full` | 完整备份 `.bak` |
| `backup_type=differential` | 差异备份 `.diff`（需先有完整备份基准） |
| `backup_type=incremental` | 日志备份 `.trn`（需 FULL 恢复模式） |
| `extra_options.backup_dir` | 可选，服务器端备份目录（默认取 `SERVERPROPERTY('InstanceDefaultBackupPath')`，Linux 通常 `/var/opt/mssql/backup`，Windows `C:\MSSQL\backup`） |
| SSH 主机 `os_type=windows` | Windows 目标自动切换 cmd 语法 |

## 恢复语义

- 默认恢复到**本任务实例的同名库**（`WITH REPLACE, RECOVERY`，覆盖已有同名库）
- 恢复到指定库名：恢复时指定 `target_db`（`WITH MOVE` 自动重定向数据/日志文件）
- 差异/日志备份的**序列恢复**（完整 NORECOVERY → 差异 → 日志 → RECOVERY）可按顺序手动执行各产物的恢复实现

## API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/tasks` | 创建任务（db_type=sqlserver） |
| `POST /api/tasks/<id>/run` | 立即执行备份 |
| `POST /api/restore` | 从备份记录恢复（可指定 target_db） |
| `GET /api/tasks/<id>/list-databases` | 拉取库清单（已排除系统库） |

## 环境要求与兼容性

- **Linux**：SQL Server 2017/2019/2022 on Linux，`mssql-tools` 自带 sqlcmd
- **Windows**：SQL Server 2008–2022（BACKUP/RESTORE 语法各版本通用），目标主机启用 OpenSSH 后将 SSH 主机 `os_type` 设为 `windows`
- sqlcmd 自动发现失败时（冷门安装路径），任务 `extra_options.tool_path` 手动填写 sqlcmd 所在目录
