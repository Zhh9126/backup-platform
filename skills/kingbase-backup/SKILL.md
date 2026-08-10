---
name: kingbase-backup
description: >
  人大金仓(KingbaseES)数据库备份与恢复技能。支持逻辑备份(sys_dump)和物理备份(sys_basebackup)，
  建议配合 WAL 归档实现增量恢复。当用户需要对金仓数据库进行备份恢复或迁移时，应使用此技能。
  信创核心库(adapter_tier=core_self)自研实现。
---

# 金仓备份技能

## 概述

人大金仓 KingbaseES 数据库备份恢复。工具链兼容 PostgreSQL 风格（`sys_dump` ≈ `pg_dump`，`sys_basebackup` ≈ `pg_basebackup`）。

### 备份引擎: `KingbaseEngine` (adapter_tier: core_self)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份-纯文本** | `sys_dump -Fp` 输出 `.sql` 文件 |
| **逻辑备份-自定义** | `sys_dump -Fc` 输出 `.dump` 压缩格式 |
| **物理全量备份** | `sys_basebackup -Ft -z --checkpoint=fast` |
| **恢复（.dump）** | `sys_restore -c -C` 清空后重建 |
| **恢复（.sql）** | `ksql -f` 直接导入 |
| **数据库列表** | `ksql` 查询 `sys_database`（过滤 template 库） |

## 备份类型（BackupType）

- `FULL` — 全量备份（INCREMENTAL/DIFFERENTIAL 自动回退为 FULL）
- 增量备份推荐使用 WAL 归档

## 备份模式（BackupMode）

- `LOGICAL` — sys_dump 导出
- `PHYSICAL` — sys_basebackup 物理复制

## 安全

密码通过 `PGPASSWORD` 环境变量注入，绝不出现于命令行参数。

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=kingbase） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=kingbase` | 查询备份记录 |
| GET | `/plugins?category=kingbase` | 查看金仓相关插件 |

## 插件依赖

- **kingbase-tools** — 人大金仓官方工具集（sys_dump, sys_restore, ksql, sys_basebackup）

## 使用示例

### 逻辑全量备份
```
db_type=kingbase, backup_mode=LOGICAL, backup_type=FULL
```

### 物理全量备份
```
db_type=kingbase, backup_mode=PHYSICAL, backup_type=FULL
```

### 特殊性
与 PostgreSQL 高度兼容的 API，工具名称前缀 `sys_` 替代 `pg_`，`ksql` 替代 `psql`。
