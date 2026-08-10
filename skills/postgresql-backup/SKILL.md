---
name: postgresql-backup
description: >
  PostgreSQL 数据库备份与恢复技能。支持逻辑备份(pg_dump)和物理备份(pg_basebackup)，建议配合 WAL 归档实现增量恢复
  和 PITR。当用户需要对 PostgreSQL 数据库进行备份、恢复或流式复制时，应使用此技能。
---

# PostgreSQL 备份技能

## 概述

此技能封装了 PostgreSQL 数据库的备份与恢复能力。支持纯文本和自定义格式导出、物理全量复制，通过 WAL 归档建议实现增量恢复。

### 备份引擎: `PostgreSQLEngine` (adapter_tier: peripheral_api)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份-纯文本** | `pg_dump -Fp` 输出 `.sql` 文件 |
| **逻辑备份-自定义** | `pg_dump -Fc` 输出 `.dump` 压缩格式 |
| **物理全量备份** | `pg_basebackup -Ft -z --checkpoint=fast`，tar.gz 格式 |
| **恢复（.dump）** | `pg_restore -c -C`，清空后重建 |
| **恢复（.sql）** | `psql -f`，直接导入 |
| **跨主机恢复** | SSH + SFTP 远程恢复 |

## 备份类型（BackupType）

- `FULL` — 全量备份（INCREMENTAL/DIFFERENTIAL 自动回退为 FULL）
- 增量备份需通过 WAL 归档 + 流式物理备份实现（平台会给出提示建议）

## 备份模式（BackupMode）

- `LOGICAL` — pg_dump 导出
- `PHYSICAL` — pg_basebackup 物理复制

## 安全

密码通过 `PGPASSWORD` 环境变量注入，绝不出现于命令行参数中。

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=postgresql） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=postgresql` | 查询备份记录 |
| GET | `/plugins?category=postgresql` | 查看 PostgreSQL 相关插件 |

## 插件依赖

- **pgbackrest** — 企业级 PostgreSQL 备份工具，支持增量/PITR/WAL 归档

## 使用示例

### 逻辑备份
```
db_type=postgresql, backup_mode=LOGICAL, backup_type=FULL
```

### 物理全量备份
```
db_type=postgresql, backup_mode=PHYSICAL, backup_type=FULL
```

### 增量/PITR
平台不支持开箱即用的 WAL 增量备份，推荐安装 pgbackrest 插件，配置 WAL 归档后手动配置流式复制。
