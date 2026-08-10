---
name: redis-backup
description: >
  Redis 数据库备份与恢复技能。通过 redis-cli --rdb 拉取 RDB 快照，支持 SCP 远程恢复。
  无原生增量能力，增量依赖 AOF 或主从复制。当用户需要对 Redis 进行快照备份或迁移恢复时，
  应使用此技能。
---

# Redis 备份技能

## 概述

Redis 快照备份，仅支持逻辑快照（RDB 文件）。无物理备份概念，增量需依赖 AOF 持久化或主从复制。

### 备份引擎: `RedisEngine` (adapter_tier: peripheral_api)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **快照备份** | `redis-cli --rdb` 拉取 RDB 文件到本地 |
| **跨主机恢复** | `scp` 将 `.rdb` 复制到目标机器数据目录，需手动重启 Redis 加载 |
| **密码保护** | 通过 `REDISCLI_AUTH` 环境变量传递，不出现于命令行 |

## 备份类型（BackupType）

- `SNAPSHOT` — RDB 快照（唯一支持类型）
- `FULL` / `INCREMENTAL` / `DIFFERENTIAL` 均自动回退为 SNAPSHOT

## 备份模式（BackupMode）

- `LOGICAL` — RDB 快照拉取（唯一支持模式）

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建备份任务（db_type=redis） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行快照任务 |
| POST | `/restore` | 执行恢复（SCP + 手动重启） |
| GET | `/records?db_type=redis` | 查询备份记录 |
| GET | `/plugins?category=redis` | 查看 Redis 相关插件 |

## 插件依赖

- **redis-tools** — Redis 命令行工具集（redis-cli, redis-server）

## 使用示例

### RDB 快照备份
```
db_type=redis, backup_mode=LOGICAL, backup_type=SNAPSHOT
```

### 恢复流程
1. 将 `.rdb` 文件 SCP 到目标 Redis 主机的数据目录
2. 停止目标 Redis 服务
3. 替换 RDB 文件
4. 重新启动 Redis 服务

### 增量备份说明
Redis 不原生支持增量备份。生产环境建议：
- 开启 AOF 持久化 + 定期 RDB 快照
- 建立主从复制，从节点做快照备份
