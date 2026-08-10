---
name: mongodb-backup
description: >
  MongoDB 数据库备份与恢复技能。通过 mongodump 进行逻辑全量备份，支持 gzip 压缩和
  mongorestore 恢复。增量备份建议配合 oplog 重放实现 PITR。当用户需要对 MongoDB 进行
  备份恢复或数据迁移时，应使用此技能。
---

# MongoDB 备份技能

## 概述

MongoDB 逻辑备份恢复，使用 `mongodump` / `mongorestore` 工具链。

### 备份引擎: `MongoEngine` (adapter_tier: peripheral_api)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **全量备份** | `mongodump --out <dir>`，目录输出 |
| **压缩** | `mongodump --gzip` 压缩输出 |
| **认证** | `--authenticationDatabase` 支持 |
| **恢复-整库** | `mongorestore --drop` 先删后恢复 |
| **恢复-指定库** | 支持指定目标库恢复 |

## 备份类型（BackupType）

- `FULL` — 全量 dumpp（唯一支持）
- `INCREMENTAL` / `DIFFERENTIAL` 自动回退为 FULL

## 备份模式（BackupMode）

- `LOGICAL` — mongodump（唯一支持）
- 无物理备份支持

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=mongodb） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行 dump 任务 |
| POST | `/restore` | 执行 mongorestore |
| GET | `/records?db_type=mongodb` | 查询备份记录 |
| GET | `/plugins?category=mongodb` | 查看 MongoDB 相关插件 |

## 插件依赖

- **mongodb-database-tools** — MongoDB 官方数据库工具集（mongodump, mongorestore）

## 使用示例

### 全量 dump
```
db_type=mongodb, backup_mode=LOGICAL, backup_type=FULL
```

### 恢复
```
mongorestore --drop --dir <dump目录> --uri <连接串>
```

### 增量/PITR
平台不原生支持 oplog 增量。建议：
1. 定期全量 mongodump
2. 配合 oplog 时间窗口做 PITR
3. 安装 mongodb-database-tools 插件使用 `mongo` shell 操作 oplog

### 安全
密码以 `--password` 参数明文传入命令行（MongoDB 官方惯例），建议使用 `--uri` 连接串方式。
