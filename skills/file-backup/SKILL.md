---
name: file-backup
description: >
  文件/目录备份与恢复技能。支持全量 tar.gz 归档、基于快照的增量备份、准CDP实时捕获、
  跨主机恢复和恢复链自动构建。支持 4 种源/目标组合(local/remote)。当用户需要对文件系统目录
  进行备份恢复或实时保护时，应使用此技能。
---

# 文件备份技能

## 概述

文件系统级备份恢复引擎。支持全量打包、快照增量对比、准 CDP 实时捕获等高级特性。

### 备份引擎: `FileBackupEngine` (adapter_tier: peripheral_api)

## 支持的能力

| 能力 | 说明 |
|------|------|
| **全量备份** | tar.gz 归档，4 种源/目标组合 |
| **增量备份** | 基于快照(`snapshot.json`)对比，比较 `size + mtime`（容差 5s），仅打包变化文件 |
| **准 CDP 实时** | `capture_increment()` + `ensure_base_full()` 实时捕获文件变化 |
| **恢复链构建** | `_build_restore_chain()` 自动查找全量基准 + 所有中间增量，支持跨任务匹配 |
| **恢复** | 按时间排序依次解压全量 + 增量链到目标目录，支持 `chain_override` 精确链 |
| **路径安全** | `_restore_filter` 过滤路径穿越（拒绝 `/` 开头或含 `..` 的路径） |

## 4 种源/目标组合

| 组合 | 场景 |
|------|------|
| local → local | 本机文件打包到本地 |
| local → remote | 本机文件发送到远程 |
| remote → local | 远程文件拉取到本地 |
| remote → remote | 远程文件传输到另一远程机 |

## 备份类型（BackupType）

- `FULL` — 全量 tar.gz 打包
- `INCREMENTAL` — 基于快照的增量（仅打包变化 + 删除清单）
- `SNAPSHOT` — 快照状态

## 备份模式（BackupMode）

- `PHYSICAL` — tar.gz 文件归档（唯一模式）

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/tasks` | 创建文件备份任务（db_type=file） |
| GET | `/tasks` | 查询任务列表 |
| POST | `/tasks/<id>/run` | 执行备份任务 |
| POST | `/restore` | 执行恢复（自动构建恢复链） |
| GET | `/records?db_type=file` | 查询备份记录 |
| GET | `/rt-timeline` | 查看准 CDP 实时时间轴 |

## 使用示例

### 全量备份本地目录到远程
```
db_type=file, backup_mode=PHYSICAL, backup_type=FULL
source_path=/data/app
target_host=192.168.1.100
target_path=/backup/data-app
```

### 增量备份
必须先存在同源的全量备份记录（快照基准），系统自动对比变化。

### 准 CDP 实时保护
1. 创建文件实时备份任务
2. 系统自动维护全量基准 + 持续捕获增量
3. 在 `/rt-timeline` 页面查看时间轴

### 恢复链
恢复时自动构建：全量基准 → 增量1 → 增量2 → ... → 目标时间点

### 快照命名空间
- 普通任务: `file_snapshots/<md5>/snapshot.json`
- 实时任务: `file_snapshots/rt/<md5>/snapshot.json`
