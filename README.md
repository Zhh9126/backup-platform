<div align="center">

# 数据备份管理平台

**跨平台数据库 + 文件 集中备份管理平台**

Oracle · MySQL · MariaDB · PostgreSQL · Kingbase（金仓） · DM（达梦） · SQL Server · Redis · MongoDB · 文件

**备份 · 恢复 · PITR · 数据迁移 · 数据同步 · 数据对比 · 预校验 · 克隆 · 演练 · 巡检 · 告警**

[![Version](https://img.shields.io/badge/Version-v1.3.3-0D9488)](#更新日志)
[![License](https://img.shields.io/badge/License-MIT-green)](#许可证)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED)](#docker-部署含离线运行)
[![Framework](https://img.shields.io/badge/Framework-Flask-black)](https://flask.palletsprojects.com/)

</div>

---

## 平台特色

- **服务端插件化，客户端零安装**：所有驱动/插件/工具（JDBC 驱动、mysqldump、dexp、sqlcmd 等）只装在平台服务端；用户客户端机器与被管理的数据库服务器**什么都不安装**、无需任何 Agent。
- **完全离线运行**：所有依赖随平台包自带（JDBC jar、JRE、预编译驱动、外部备份工具离线包），运行时零联网。部署后运行 `python scripts/check_offline.py` 自检。
- **真实执行，如实失败**：所有备份/恢复/同步均为真实操作，连接失败或依赖缺失时任务**如实失败**并给出明确原因与修复指引，不做任何仿真兜底。
- **迁移前预校验**：字段类型映射矩阵、数据级试写、字符集冲突、容量预估、主键/外键检查——结构或数据不合适在启动前拦截，杜绝迁移到一半才失败。
- **真实差异定位**：数据对比采用主键归并（对标 pt-table-checksum），逐行精确定位 missing/extra/changed 差异并生成修复 SQL，零误报。

---

## 功能总览（真实实测状态）

> 状态说明：✅ 真机实测通过 ｜ ⚠️ 部分实现/待完善 ｜ ❌ 未实现（如实标注，不做虚假宣传）

### 1. 数据备份

| 能力 | 状态 | 说明 |
|---|---|---|
| MySQL/MariaDB 逻辑备份 | ✅ | mysqldump，压缩/并行，累计 400+ 次真机执行 |
| MySQL/MariaDB 物理备份 | ✅ | XtraBackup（含 zstd 压缩），远端工具缺失时平台推送临时副本执行后清理 |
| **MySQL 增量物理备份 + 增量链恢复** | ✅ | FULL→INCREMENT→恢复 全链闭环实测，增量数据验证恢复 |
| PostgreSQL 逻辑/物理备份 | ✅ | pg_dump / pg_basebackup，恢复实测通过 |
| 达梦逻辑备份（dexp/dimp） | ✅ | 跨机 SSH + 平台 JDBC 双通道实测 |
| 达梦物理备份 + 增量（INCREMENT） | ✅ | 联机 BACKUP [INCREMENT] BACKUPSET，增量体积 143KB vs 全量 2.1MB |
| 达梦 PITR 时间点恢复 | ⚠️ | dmrman RESTORE/RECOVER 链路已通，页大小对齐后最终验证进行中 |
| Oracle / 金仓 / SQL Server 备份恢复 | ✅ | 各有真机成功记录（expdp/RMAN、sys_dump、BACKUP/RESTORE DATABASE） |
| Redis / MongoDB 备份 | ⚠️ | 引擎就绪（依赖已随包内置），待真机实测 |
| 文件备份（本地 + 远程 SSH） | ✅ | 无 Agent，全量/增量（watchdog / polling） |
| 实时备份（CDP） | ✅ | MySQL binlog 流式 / PG WAL / Oracle LogMiner / 达梦 LogMNR 轨道 |
| 调度 / 保留策略（GFS）/ 合成全量 | ✅ | cron / 间隔调度；按天数+份数清理；增量链合并 |
| 三级存储 + 生命周期 + 全局重删 + 加密 | ✅ | MinIO(L1)/S3(L2)/本地(L3)，自动下沉与清理 |

### 2. 数据恢复

| 能力 | 状态 | 说明 |
|---|---|---|
| MySQL 逻辑/物理恢复 | ✅ | 恢复实测 13+3 次；物理恢复含 prepare + 临时实例校验 |
| MySQL 增量链恢复 | ✅ | 基备 prepare(--apply-log-only) → 逐层 --incremental-dir 合并 → 临时实例校验 |
| PostgreSQL 恢复 | ✅ | 逻辑恢复实测 11 次，数据完整一致 |
| 达梦逻辑恢复（dimp） | ✅ | 跨机恢复实测 |
| Oracle / 金仓 / SQL Server 恢复 | ✅ | 各有真机成功记录 |
| 表级并行导入 / 恢复校验 / 文件增量恢复链 | ✅ | RESTORE_PARALLEL 可调；策略化可恢复性校验 |

### 3. 数据迁移与数据同步

> **概念区分（业界定义）**：数据迁移 = 一次性任务（存量搬迁，完成即止，支持覆盖写入）；数据同步 = 持续性任务（常驻运行保持两端一致，禁用覆盖写入）。平台在术语、引擎约束、预校验、前端四个层面严格执行该区分。

| 链路 | 预校验 | 全量迁移 | 实时同步（轮询） |
|---|---|---|---|
| MySQL → 达梦 | ✅ | ✅ | ✅ |
| PG → 达梦 | ✅ | ✅ | ✅ |
| MySQL → PG | ✅ | ✅ | ✅ |
| 达梦 → PG | ✅ | ✅ | ✅ |
| MySQL→MySQL / PG→PG（同构） | ✅ | ✅ | ✅ |

配套能力：
| 能力 | 状态 | 说明 |
|---|---|---|
| 异构类型映射引擎 | ✅ | 对标阿里 DTS《结构初始化数据类型映射》：UNSIGNED 升位、精度降级、ENUM/SET/JSON/BIT 特殊处理，逐列输出建表类型建议 |
| 数据级试写预检 | ✅ | 源端采样 N 行 → 目标端按映射建议 DDL 建临时表试写 → 对账 → 清理，数据级问题提前拦截 |
| 字符集冲突检测 | ✅ | utf8mb4(4字节) → GB18030/latin1 拦截；按库型区分 MySQL utf8(3字节) 与真 UTF-8 |
| 大表容量预估 | ✅ | 行数/体积统计 + 按带宽估时 + 大表告警（≥1 亿行 / ≥10GB） |
| 外键父表完整性 | ✅ | 子表依赖的父表不在同步列表 → 告警 |
| 列名归一（field_ide） | ✅ | origin/upper/lower/camel/underscore，建表 DDL 与写入一致生效 |
| Binlog CDC 实时同步 | ✅ | mysql-replication 库（随离线包内置） |

### 4. 数据对比

| 能力 | 状态 | 说明 |
|---|---|---|
| 主键归并对比（pk_chunk） | ✅ | 对标 pt-table-checksum：keyset 分页双指针归并，O(单页)内存，千万级表可对比 |
| 差异行级定位 | ✅ | missing_in_target / extra_in_target / changed 三类精确定位，零误报 |
| 修复 SQL 生成 | ✅ | 逐条差异输出 INSERT/DELETE/UPDATE 修复指引 |
| 跨名表映射 | ✅ | tables 支持 {"source": "T1", "target": "T1_CMP"} 跨名对比 |
| 校验和（多库型） | ✅ | MySQL CRC32 / PG hashtext / Oracle·达梦 ORA_HASH / SQL Server CHECKSUM_AGG |
| 实测矩阵 | ✅ | mysql-mysql（4 处差异精确定位）、MySQL↔达梦（跨库）、达梦-达梦（跨名） |

### 5. 迁移前预校验体系（对标 DTS 预检查）

| 检查项 | 状态 |
|---|---|
| 源/目标连通性、表存在性 | ✅ |
| 列兼容性（类型映射矩阵 + 列缺失/多列） | ✅ |
| 主键（upsert/实时必需）与增量列 | ✅ |
| 数据级试写 / 大表容量 / 字符集冲突 / 外键完整性 | ✅ |
| 任务性质校验（迁移 vs 同步，概念冲突拦截） | ✅ |

接入点：引擎层强制拦截（skip_precheck 可跳过）+ REST API（`POST /api/sync-tasks/<id>/precheck`）+ 前端结构化报告弹窗。

### 6. 其他功能

| 功能 | 状态 | 说明 |
|---|---|---|
| 数据库部署 | ⚠️ | MySQL 8.0 / MongoDB 一键部署实测；PG 部署验证通过；其余库型待实测 |
| 克隆服务（VDB） | ✅ | 免审批直通，MySQL/MariaDB/PG 逻辑克隆，TTL 到期自动销毁 |
| 巡检 / 恢复演练（RTO/RPO） | ✅ | 三维体检、趋势/基线/季度排程 |
| 通知告警 | ✅ | Webhook/钉钉/企微/飞书/邮件 |
| AI 智能体 / AI 告警 | ✅ | 对话式运维助手，LLM 不可用时本地兜底 |
| 容灾链路 / ITSM 对接 | ✅ | binlog 位点一致性校验；内置适配器可插拔 |
| 多租户 / RBAC | ❌ | 单管理员账号 |
| 集群化 / 高可用 | ❌ | 单机架构 |

---

## 真机测试矩阵（截至 2026-09-06）

所有 ✅ 均为真机实测（非纸面推断），测试环境：本机 MySQL 8.0 + 192.168.220.137（达梦/PG）+ 192.168.220.140（MySQL/PG）跨机链路。

| 类别 | 实测项 |
|---|---|
| 备份 | MySQL 逻辑/物理/增量物理、PG 逻辑/物理、达梦 逻辑/物理/增量物理、Oracle、金仓、SQL Server、文件全量/增量 |
| 恢复 | MySQL 逻辑/物理/增量链、PG 逻辑、达梦 逻辑、Oracle、金仓、SQL Server |
| 迁移 | MySQL→达梦、PG→达梦、MySQL→PG、达梦→PG（全量+数据比对） |
| 同步 | 上述四链路实时轮询（源插入→目标秒级可见） |
| 对比 | mysql-mysql、MySQL↔达梦、达梦-达梦（跨名映射，差异精确定位零误报） |
| 预校验 | 五大项 + 数据级试写 + 字符集冲突真实拦截（utf8mb4→GB18030 案例） |

> 详细测试记录与修复清单见 [readme_20260901.md](readme_20260901.md)（22 章测试日志，含每个 Bug 的现象/根因/修法）。

---

## 快速开始

```bash
# 1. 初始化元数据数据库
python init_db.py

# 2. 启动平台（同时启动后台调度器）
python run.py
```

浏览器访问 `http://<服务器IP>:8080`，默认账号 `admin / admin123`（**请立即修改**）。

> 备份任务执行前，请先在「系统设置 → SSH 主机」纳管数据库服务器（或使用任务级 SSH 凭据）；客户端工具装在数据库服务器上即可，平台会自动发现工具路径并远程执行。

---

## 离线环境部署

平台面向**完全离线环境**设计：运行时不安装任何东西，一切依赖随离线包自带。

1. **构建离线包**：PyInstaller 打包（Python 依赖全内置）+ `drivers/`（JDBC jar）+ `jdk/`（JRE，达梦/Oracle JDBC 通道必需）+ 外部备份工具离线包。
2. **部署后自检**：`python scripts/check_offline.py` —— 五层检查（Python 依赖 / JDBC jar / JVM / dmPython / 外部工具），缺失项给出处置指引。
3. **外部备份工具**：仅支持离线包上传安装（备份插件页 SFTP 上传），不依赖在线源。

---

## 外部 API 调用（Bearer Token）

平台提供 REST API 供外部系统（监控平台 / CMDB / 自动化脚本）调用，认证方式为 **Bearer Token**。

### 获取令牌

登录 Web 页面 → 浏览器控制台（F12）执行：

```javascript
fetch("/api/tokens", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "外部监控系统"})
}).then(r => r.json()).then(d => console.log(d.token));
```

返回的 `token`（`bk_` 前缀）明文仅此一次展示，平台仅存哈希。

### 调用示例

```bash
TOKEN="bk_xxxxxxxx"

# 列出备份任务
curl -s -H "Authorization: Bearer $TOKEN" http://<服务器IP>:8080/api/tasks

# 立即执行一次全量备份
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"backup_type":"full"}' \
  http://<服务器IP>:8080/api/tasks/22/run

# 查询最近备份记录
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://<服务器IP>:8080/api/records?limit=10"

# 触发巡检
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://<服务器IP>:8080/api/inspection/run

# 数据迁移（预检查 → 结构 → 全量 → 校验 一站式）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"plan_id":1}' \
  http://<服务器IP>:8080/api/migration/run

# 数据同步任务迁移前预校验（连通性/类型矩阵/数据级试写等）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://<服务器IP>:8080/api/sync-tasks/9/precheck
```

---

## Docker 部署（含离线运行）

### 镜像地址（GHCR，国内可加速拉取）

```bash
# 最新版（跟随更新）
docker pull ghcr.io/zhh9126/backup-platform:latest
# 纯版本号 / 版本+构建日期（推荐：可追溯、可回滚）
docker pull ghcr.io/zhh9126/backup-platform:v1.3.3
```

### 国内网络加速

```json
// /etc/docker/daemon.json
{"registry-mirrors": ["https://docker.m.daocloud.io", "https://dockerproxy.com"]}
```

### 离线环境

```bash
# 有网机器导出
docker save ghcr.io/zhh9126/backup-platform:v1.3.3 -o backup-platform.tar
# 内网机器导入
docker load -i backup-platform.tar
```

### 运行

```bash
docker run -d --name backup-platform \
  -p 8080:8080 \
  -v backup-data:/app/instance \
  -v backup-files:/app/backups \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:latest
```

### Docker Compose（推荐生产）

```yaml
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:latest
    ports: ["8080:8080"]
    volumes:
      - ./instance:/app/instance
      - ./backups:/app/backups
    restart: unless-stopped
```

---

## 支持的数据库与所需客户端

> 以下工具装在**数据库服务器**上即可（数据库自带），平台经 SSH 远程执行并动态发现工具路径；平台机自身装有客户端时也可本机执行（零安装回退）。

| 数据库 | 备份 | 恢复 | 说明 |
|---|---|---|---|
| MySQL / MariaDB | mysqldump、xtrabackup | mysql、xtrabackup | 密码经临时选项文件注入；物理恢复在平台侧 prepare + 临时实例校验 |
| PostgreSQL | pg_dump、pg_basebackup | pg_restore / psql | PGPASSWORD 环境变量传密码 |
| Oracle | expdp / RMAN | impdp / RMAN | 数据泵 DIRECTORY；RMAN 支持增量 |
| Kingbase 金仓 | sys_dump、sys_basebackup | sys_restore / ksql | 兼容 PG 协议，默认端口 54321 |
| DM 达梦 | dexp、disql(联机 BACKUP) | dimp、dmrman(PITR) | 原生驱动缺失时自动降级 JDBC 通道（驱动随包） |
| SQL Server | sqlcmd | sqlcmd | BACKUP/RESTORE DATABASE；SQLCMDPASSWORD 注入 |
| Redis | redis-cli --rdb | （复制 rdb + 重启） | REDISCLI_AUTH 传密码 |
| MongoDB | mongodump | mongorestore | --archive 流式拉回 |

**数据迁移/同步连接通道**：原生 Python 驱动（pymysql / psycopg2 / oracledb thin）优先，JDBC 兜底（驱动 jar 随包，达梦/金仓/Oracle 全覆盖）。

---

## 使用说明（导航结构）

- **概览**：仪表盘（任务数、累计体积、成功失败统计）
- **备份管理**：数据库备份、文件备份、存储管理（三级存储 + 合成全量）、保护策略、备份插件
- **记录**：备份记录、恢复记录、恢复校验
- **数据恢复管理**：数据恢复、数据库部署
- **灾备管理**：数据迁移、数据同步、容灾链路、克隆服务、恢复演练
- **数据对比**：同库/异库对比，主键归并差异定位与修复 SQL
- **实时管控**：RT / CDP / PITR 时间线
- **运维**：巡检、智能告警、数据价值挖掘、智能体、系统设置

---

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `BACKUP_ROOT` | 备份产物根目录 | `./backups` |
| `SCHEDULER_ENABLED` | 是否启用定时调度 | `true` |
| `DEFAULT_RETENTION_DAYS` / `_COUNT` | 默认保留天数 / 份数 | `30` / `50` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | AI 智能体端点（不可达时本地兜底） | 见 `config.py` |

---

## 生产部署建议

- 使用 `gunicorn` 运行：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`
- 元数据 SQLite 建议定期备份（`instance/meta.db`）
- 备份产物目录与数据库服务器网络隔离，配置三级存储异地容灾
- 离线环境部署完成后**首先运行** `python scripts/check_offline.py` 自检

## 安全说明

- 密码 AES 加密存储（元数据库中不含明文）
- 备份密码通过临时选项文件 / 环境变量注入，不出现在命令行（防 ps 泄露）
- API Token 仅存哈希；会话 Cookie HttpOnly
- 默认账号请立即修改；生产环境建议限制来源 IP

## 许可证

MIT（社区版免费供个人学习、内部部署与中小规模生产环境使用。企业级增强请联系作者：📧 `1547358466@qq.com`）
