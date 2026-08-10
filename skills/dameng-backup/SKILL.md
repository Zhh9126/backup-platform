---
name: dameng-backup
description: >
  达梦(DM)数据库备份与恢复技能。支持逻辑备份(dexp)和物理备份(dmrman)，支持 SCHEMAS/OWNER/FULL
  范围控制。当用户需要对达梦数据库进行备份恢复或 schema 级别导出导入时，应使用此技能。
  信创核心库(adapter_tier=core_self)自研实现。
---

# 达梦备份技能

## 概述

达梦数据库（DM8）备份恢复。支持 `dexp` 逻辑导出和 `dmrman` 物理备份，提供灵活的 SCHEMAS/OWNER 范围控制。

### 备份引擎: `DamengEngine` (adapter_tier: core_self)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份-SCHEMAS** | `dexp SCHEMAS=用户1,用户2` 指定 schema 导出 |
| **逻辑备份-OWNER** | `dexp OWNER=用户名` 按所有者导出 |
| **逻辑备份-FULL** | `dexp FULL=Y` 整库导出，输出 `.dmp` 文件 |
| **物理全量备份** | `dmrman BACKUP DATABASE FULL TO <路径>` |
| **恢复** | `dimp`，支持目标 schema 映射，`FULL=Y` 整库导入 |
| **安全** | 密码嵌入 `USERID=用户名/密码@主机:端口`（达梦官方惯例） |

## 备份类型（BackupType）

- `FULL` — 全量备份（INCREMENTAL/DIFFERENTIAL 自动回退为 FULL）
- 物理增量推荐使用 dmrman 进行

## 备份模式（BackupMode）

- `LOGICAL` — dexp 导出 `.dmp`
- `PHYSICAL` — dmrman 物理备份

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=dameng） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=dameng` | 查询备份记录 |
| GET | `/plugins?category=dameng` | 查看达梦相关插件 |

## 插件依赖

- **dmrman** — 达梦物理备份恢复管理器

## 使用示例

### 整库导出
```
db_type=dameng, backup_mode=LOGICAL, backup_type=FULL
```

### dmrman 物理备份
```
db_type=dameng, backup_mode=PHYSICAL, backup_type=FULL
```

### 导入到不同 schema
`dimp` 支持目标 schema 映射，将源 schema 数据导入不同目标。
