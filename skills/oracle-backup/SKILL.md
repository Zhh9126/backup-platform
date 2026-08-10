---
name: oracle-backup
description: >
  Oracle 数据库备份与恢复技能。支持逻辑备份(expdp/exp Data Pump)和物理备份(RMAN 全量+增量)，
  以及归档日志备份和 RMAN PITR 时间点恢复。当用户需要对 Oracle 进行备份恢复或归档日志管理时，
  应使用此技能。信创核心库(adapter_tier=core_self)自研实现。
---

# Oracle 备份技能

## 概述

此技能封装了 Oracle 数据库的完整备份与恢复能力，包括 Data Pump 逻辑导出和 RMAN 物理备份。

### 备份引擎: `OracleEngine` (adapter_tier: core_self)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **逻辑备份-服务端** | `expdp` (Data Pump)，文件存 `DATA_PUMP_DIR` |
| **逻辑备份-客户端** | `exp`，传统导出，文件存本地 |
| **物理全量备份** | RMAN `BACKUP DATABASE` |
| **物理增量备份** | RMAN `BACKUP INCREMENTAL LEVEL 0/1 DATABASE` |
| **归档日志备份** | `archivelog_backup()` — RMAN `BACKUP ARCHIVELOG ALL DELETE INPUT` |
| **PITR 恢复** | `rman_pitr(target_time)` — RMAN `SET UNTIL TIME` + `RESTORE` + `RECOVER` + `ALTER DATABASE OPEN RESETLOGS` |
| **逻辑增量** | `exp INCTYPE=INCREMENTAL/CUMULATIVE` |

## 备份类型（BackupType）

- `FULL` — expdp 全量导出
- `SNAPSHOT` — expdp 一致性导出
- `INCREMENTAL` — exp 增量（INCTYPE=INCREMENTAL）或 RMAN INCREMENTAL
- `DIFFERENTIAL` — exp 累积（INCTYPE=CUMULATIVE）

## 备份模式（BackupMode）

- `LOGICAL` — expdp / exp 导出
- `PHYSICAL` — RMAN 备份

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=oracle） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行任务 |
| POST | `/restore` | 执行恢复 |
| GET | `/records?db_type=oracle` | 查询备份记录 |
| GET | `/plugins?category=oracle` | 查看 Oracle 相关插件 |

## 插件依赖

- **oracle-rman-client** — Oracle RMAN 客户端工具

## 使用示例

### RMAN 物理全量备份
```
db_type=oracle, backup_mode=PHYSICAL, backup_type=FULL
```

### expdp 逻辑导出
需要确保 Oracle 服务端 DATA_PUMP_DIR 可写，导出文件路径以 `server-side:` 前缀区分。

### PITR 恢复流程
1. 确保归档日志完备（先执行 `archivelog_backup()`）
2. 执行 `rman_pitr('2024-01-15 10:30:00')` 恢复到指定时间点
3. 数据库以 RESETLOGS 模式打开

### 安全
密码明文写入连接串 `user/pw@//host:port/service`（Oracle 客户端惯例）。
