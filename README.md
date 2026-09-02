# 数据备份管理平台

<<<<<<< HEAD
跨平台的**数据库 + 文件**集中备份管理平台，支持 **Oracle、MySQL、PostgreSQL、Kingbase（金仓）、DM（达梦）、Redis、MongoDB** 等多种数据库，以及**文件/目录（本地与远程 SSH，无 Agent）**的集中备份、定时调度、保留策略、三级对象存储、数据同步、巡检与健康检查、通知告警与一键恢复。
=======
<div align="center">
>>>>>>> docs(readme): README 据实重写 + Docker 基础镜像升级 Python 3.14

**跨平台数据库 + 文件 集中备份管理平台**

Oracle · MySQL · MariaDB · PostgreSQL · Kingbase（金仓） · DM（达梦） · SQL Server · Redis · MongoDB · 文件

**备份 · 恢复 · 实时备份(PITR) · 数据迁移 · 数据同步 · 数据对比 · 克隆 · 演练 · 巡检 · 告警 · AI 助手**

[![Version](https://img.shields.io/badge/Version-v1.3.0-0D9488)](#更新日志)
[![License](https://img.shields.io/badge/License-MIT-green)](#许可证)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED)](#docker-部署含离线运行)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows-lightgrey)](#支持的数据库与所需客户端)
[![Framework](https://img.shields.io/badge/Framework-Flask-black)](https://flask.palletsprojects.com/)

[![Docker Pulls](https://img.shields.io/badge/Docker%20Pulls-%E6%9F%A5%E7%9C%8B-2496ED)](https://github.com/Zhh9126/backup-platform/pkgs/container/backup-platform)
[![GitHub Stars](https://img.shields.io/badge/Stars-%E6%AC%A2%E8%BF%8E%E7%82%B9%E6%98%9F-yellow)](https://github.com/Zhh9126/backup-platform/stargazers)
[![GitHub Issues](https://img.shields.io/badge/Issues-%E5%8F%8D%E9%A6%88-red)](https://github.com/Zhh9126/backup-platform/issues)
[![Docker Image](https://img.shields.io/badge/Image-ghcr.io%2Fzhh9126%2Fbackup--platform-2496ED)](https://github.com/Zhh9126/backup-platform/pkgs/container/backup-platform)

</div>

---

- **纯 Python + Flask**，元数据 SQLite（零外部中间件依赖，开箱即用）
- **Agentless**：客户端工具装在**数据库服务器**上即可，平台经 SSH 远程执行并动态发现工具真实路径，不在数据库服务器安装任何备份组件
- **真实执行**：所有备份/恢复均为真实操作，客户端缺失或连接失败时任务**如实失败**并给出明确原因，不做任何仿真兜底
- **离线可用**：Docker 镜像内已烘焙全部依赖与原生驱动，运行时零联网

> **当前版本**：`v1.3.0`（社区版）｜ Docker 镜像：`ghcr.io/zhh9126/backup-platform:latest`

> **社区版说明**：免费供个人学习、内部部署与中小规模生产环境使用。企业级增强（大规模集群纳管、多租户、商业支持与定制开发）请联系作者。
> **联系方式**：📧 `1547358466@qq.com`

---

## 功能总览（已实现）

### 1. 备份能力
| 能力 | 说明 |
|---|---|
| 9 个数据库备份引擎 + 文件备份 | MySQL / MariaDB / PostgreSQL / Kingbase / DM / SQL Server / Oracle / Redis / MongoDB + 文件（本地 + 远程 SSH 无 Agent）|
| 备份类型 | 全量 / 增量 / 差异（SQL Server）/ 快照 / 合成全量 / 组合（全量+增量双调度）|
| 调度 | cron 表达式 / 固定间隔（APScheduler）|
| 保留策略 | 按天数 + 按份数双重清理 |
| 自定义备份/恢复脚本 | 全数据库类型通用，平台注入 `PLATFORM_*` 环境变量，SFTP 拉回产物并计算 sha256 |
| 三级存储 | L1 MinIO（热）/ L2 S3（冷）/ L3 本地导出，备份后自动并行复制 |
| 生命周期 | L1→L2 按龄/按容量下沉、到期清理 |
| 全局重删 | 内容 sha256 索引 + 引用计数，KPI 展示节省空间 |
| 存储池加密 | AES-256-GCM 信封式，密钥来源：环境变量 / 系统设置托管 / 外部 KMS |
| 备份插件 | 服务端插件市场（XtraBackup / MariaDB Backup / pgBackRest / MongoDB Tools 等）|
| 远端工具动态发现 | 数据库服务用户 profile → 登录 shell → 常见目录枚举 → find，不写死路径；支持 `tool_path` 手动兜底 |

### 2. 恢复能力
| 能力 | 说明 |
|---|---|
| 一键恢复 | 备份记录 → 目标实例（库级）或目标目录（文件级）|
| 表级并行导入 | MySQL 逻辑备份恢复自动拆分 dump 并行导入（`RESTORE_PARALLEL` 可调）|
| 物理恢复并行化 | XtraBackup `--prepare` 附带 `--parallel` |
| 恢复校验 | 策略化对最近成功备份做可恢复性校验（Oracle 走 impdp SQLFILE / RMAN RESTORE VALIDATE），生成报告 |
| 文件增量恢复 | 自动构建恢复链（最近全量 → 按时间应用增量）|
| PITR 时间点恢复 | MySQL binlog / PostgreSQL WAL 持续捕获，支持按时间点恢复 |

### 3. 数据迁移（DTS 对标）
| 阶段 | 状态 | 说明 |
|---|---|---|
| 预检查 | ✅ 已实现 | 源/目标连通性、目标库自动创建（MySQL）、源对象统计（表数/行数）|
| 结构迁移 + 全量迁移 | ✅ 已实现 | 按源端 schema 重建目标表 + 全库存量导入（复用同步引擎 `create_if_not_exists` + `full_db_migrate`）|
| 数据校验 | ✅ 已实现 | 逐表行数比对（源 vs 目标），逐表明细 |
| 迁移报告 | ✅ 已实现 | 各阶段结果/行数/耗时汇总 |
| **增量迁移 / 不停机切换** | ⚠️ 未实现 | 由「数据同步」realtime 模式（Binlog CDC）承接，迁移页面有明确指引；反向回切、断点续传、自动断流未实现 |

> 当前迁移引擎支持的数据库：**MySQL / MariaDB → MySQL**、**PostgreSQL → PostgreSQL**（原生驱动直连）。
> **未实现**：Kingbase / DM / Oracle / SQL Server / Redis / MongoDB 作为迁移源或目标；异构迁移（如 Oracle → MySQL）；表级/库级黑白名单过滤。

### 4. 数据同步（实时/离线）
| 能力 | 状态 | 说明 |
|---|---|---|
| 表级同步 | ✅ | MySQL/MariaDB、PostgreSQL 已实现 Reader/Writer（插件注册制）|
| 写入模式 | ✅ | append / overwrite / upsert / create_if_not_exists |
| 增量同步 | ✅ | 指定增量列 + 起始值，断点记录 |
| 实时同步 | ✅ | MySQL Binlog CDC（插件内置监听），PG 逻辑复制预留 |
| 字段映射 | ✅ | 同名映射 / 手动映射 / 可视化连线 |
| 全库迁移模式 | ✅ | 源库所有表一次性同步到目标库 |
| Schema 校验 | ✅ | 列类型/长度兼容性预检 |
| **其他数据库**（Oracle/DM/SQL Server/Redis/MongoDB） | ⚠️ 未实现 | 仅 mysql / mariadb / postgresql 有 Reader/Writer 插件 |

### 5. 数据对比
| 能力 | 状态 |
|---|---|
| 表清单比对 / 行数比对 / 全表校验和 / 抽样行逐列比对 | ✅ |
| 支持 MySQL/MariaDB、PostgreSQL/Kingbase、Oracle | ✅ |
| Redis / MongoDB / SQL Server | ⚠️ 未实现 |

### 6. 实时备份（RT / CDC）
| 能力 | 状态 | 说明 |
|---|---|---|
| MySQL binlog 流式捕获 + PITR | ✅ | `mysqlbinlog --read-from-remote-server --raw --stop-never` |
| PostgreSQL WAL 流式捕获 | ✅ | `pg_receivewal` |
| Oracle LogMiner | ✅ | 日志解析轨道 |
| 达梦 LogMNR | ✅ | 日志解析轨道 |
| Kingbase WAL | ⚠️ 需装客户端 | 缺 sys_receivewal 时降级采样 |
| Redis / MongoDB 实时捕获 | ⚠️ 未实现 | |
| 文件实时捕获 | ✅ | watchdog / polling 双模式 |

### 7. 数据库部署
| 能力 | 状态 | 说明 |
|---|---|---|
| MySQL 8.0.x 一键部署到目标 Linux 主机 | ✅ | 上传安装包 → 生成脚本 → 执行 → 实时日志 |
| MongoDB 部署（副本集 + 认证） | ✅ | keyFile + rs.initiate |
| **PostgreSQL / Oracle / Kingbase / DM / Redis / SQL Server 部署** | ⚠️ 未实现 | 仅 MySQL / MongoDB |

### 8. 克隆服务（VDB）
| 能力 | 状态 | 说明 |
|---|---|---|
| 免审批直通（申请即拉起） | ✅ | `CLONE_AUTO_APPROVE=true` 默认；可切回 ITSM 审批流 |
| 真实克隆引擎 | ✅ | mysql / mariadb / postgresql（本机管理实例建库 + 流式导入）|
| TTL 到期自动销毁 | ✅ | 默认 7 天，可配置 |
| **其他数据库（Oracle/DM/SQL Server 等）** | ⚠️ 未实现 | 明确报错不降级仿真 |
| **基于快照/CoW 的秒级克隆** | ⚠️ 未实现 | 当前为逻辑导入克隆，非存储级快照 |

### 9. 运维管理
| 能力 | 状态 |
|---|---|
| 巡检（连通性/调度/上次状态三维体检，fail 即告警） | ✅ |
| 恢复演练（RTO/RPO 评估，趋势/基线/季度排程） | ✅ |
| 通知告警（Webhook/钉钉/企微/飞书/邮件，成功失败分别开关） | ✅ |
| AI 智能体（对话式运维助手，7 个工具调用，LLM 不可用时本地兜底） | ✅ |
| AI 智能告警（规则分析 + 归因） | ✅ |
| 备份质量监控（超长/超频判定，阈值可配） | ✅ |
| 容灾链路（真实 binlog 位点一致性校验/日志缺口检测） | ✅ |
| ITSM 工单对接（内置适配器，可插拔） | ✅ |
| **多租户 / RBAC** | ⚠️ 未实现（单管理员账号） |
| **集群化部署 / 高可用** | ⚠️ 未实现（单机架构） |

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

## 外部 API 调用（Bearer Token）

平台提供 REST API 供外部系统（监控平台 / CMDB / 自动化脚本）调用。所有接口与页面 API 共用，认证方式为 **Bearer Token**（非浏览器会话）。

### 获取令牌

1. 登录 Web 页面 → 打开浏览器开发者工具（F12）→ 在控制台执行：

```javascript
fetch("/api/tokens", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "外部监控系统"})
}).then(r => r.json()).then(d => console.log(d.token));
```

2. 返回的 `token`（`bk_` 前缀，明文仅此一次展示）妥善保存；平台仅存哈希，丢失只能吊销重建。

### 调用示例

```bash
TOKEN="bk_xxxxxxxxxxxxxxxx"
BASE="http://<平台IP>:8080"

# 列出备份任务
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/tasks"

# 立即执行一次全量备份（task_id=22 为例）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"backup_type":"full"}' \
     "$BASE/api/tasks/22/run"

# 查询最近备份记录
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/records?limit=10"

# 触发巡检
curl -s -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/inspection/run"

# 克隆：从备份记录 103 拉起一个隔离克隆库（免审批直通）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source_record_id":103,"target_env":"test","requested_by":"ops"}' \
     "$BASE/api/clone"

# 一站式数据迁移（预检查 → 结构 → 全量 → 校验）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"OA迁移","src_db_type":"mysql","src_host":"192.168.1.10","src_port":3306,
          "src_username":"root","src_password":"***","src_db_name":"oa",
          "tgt_db_type":"mysql","tgt_host":"192.168.1.20","tgt_port":3306,
          "tgt_username":"root","tgt_password":"***","tgt_db_name":"oa_new"}' \
     "$BASE/api/db-migrate"
```

### 主要可用端点

| 方法 | 端点 | 说明 |
|---|---|---|
| GET | `/api/tasks` | 备份任务列表 |
| POST | `/api/tasks/<id>/run` | 立即执行备份 |
| GET | `/api/records` | 备份记录列表 |
| POST | `/api/restores` | 发起恢复 |
| GET | `/api/restores` | 恢复记录列表 |
| POST | `/api/inspection/run` | 触发巡检 |
| GET | `/api/inspection/records` | 巡检记录 |
| GET/POST | `/api/db-migrate` | 一站式迁移计划（列表/创建并执行）|
| GET | `/api/db-migrate/<id>` | 迁移计划详情与各阶段结果 |
| GET/POST | `/api/clone` | 克隆申请/列表（免审批直通）|
| POST | `/api/clone/<id>/destroy` | 销毁克隆 |
| GET | `/api/rt/points?task_id=<id>` | 实时备份恢复点 |
| GET/POST | `/api/storage/targets` | 存储目标 |
| GET | `/api/dashboard` | 仪表盘统计 |

### 令牌管理

```javascript
// 列出令牌（哈希脱敏，不含明文）
fetch("/api/tokens").then(r => r.json()).then(console.log);
// 吊销令牌
fetch("/api/tokens/3", {method: "DELETE"}).then(r => r.json()).then(console.log);
```

> 令牌具备与登录账号等同的操作权限（包含危险操作如删除/恢复/部署），请妥善保管；泄露时立即吊销。
> 令牌认证的请求不走 CSRF 校验（非浏览器场景）；所有调用在平台日志中留痕。

---

## Docker 部署（含离线运行）

镜像已包含全部 Python 依赖与原生直连驱动（pymysql/psycopg2/oracledb），并附带 JRE + JDBC 驱动 jar 作为可选兜底（如 Oracle 11g），**运行时无需联网、无需外部安装任何依赖**。

### 镜像地址（GHCR，国内可加速拉取）

镜像仓库：**`ghcr.io/zhh9126/backup-platform`**（由 GitHub Actions 在 push `v*` 标签时自动构建发布）。

当前版本 tag（**生产环境推荐固定「版本-日期」tag，勿用 latest**）：

```bash
# 最新版（跟随更新）
ghcr.io/zhh9126/backup-platform:latest
# 社区版固定别名（跟随更新）
ghcr.io/zhh9126/backup-platform:community
# 纯版本号
ghcr.io/zhh9126/backup-platform:1.3.0
# 版本+构建日期（推荐：同版本多次构建可区分、可回滚）
ghcr.io/zhh9126/backup-platform:1.3.0-20260902
```

历史版本 tag 规律：`vX.Y.Z` 发版同时产出 `X.Y.Z` 与 `X.Y.Z-<构建日期YYYYMMDD>`，例如 `1.3.0-20260902`。

### 国内网络加速（拉取 ghcr.io 必看）

国内服务器直连 ghcr.io 易超时，配置镜像加速器（网页打不开属正常，不影响 Docker 后台加速）：

```bash
# /etc/docker/daemon.json
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io"
  ],
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
```

```bash
systemctl daemon-reload && systemctl restart docker
docker info   # 底部出现两个加速地址即生效
```

### 拉取与离线导入

```bash
# 推荐：按版本-日期拉取（可追溯、可回滚）
docker pull ghcr.io/zhh9126/backup-platform:1.3.0-20260902

# 离线环境：先在有网机器导出，拷贝到内网后导入
docker save -o backup-platform-1.3.0.tar.gz ghcr.io/zhh9126/backup-platform:1.3.0-20260902
# （内网机器上）
docker load -i backup-platform-1.3.0.tar.gz
```

### 运行

```bash
docker run -d --name backup-platform \
  -p 8080:8080 \
  -v /data/backup-platform:/data \
  -e WEB_PASSWORD=your_password \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:1.3.0-20260902
```

- `/data` 挂载卷持久化：元数据库（`instance/`）、备份文件（`backups/`）、日志（`logs/`）
- 配置全部走环境变量（`WEB_PORT`、`SECRET_KEY`、`WEB_USERNAME` 等）
- 访问 `http://<主机IP>:8080`，默认账号 `admin / admin123`（**请立即修改**）

### Docker Compose 部署（推荐生产）

`docker-compose.yml`：

```yaml
version: '3.8'
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:1.3.0-20260902
    container_name: backup-platform
    ports:
      - "8080:8080"
    environment:
      - WEB_PASSWORD=your_password
      - SECRET_KEY=change-me-to-random
      - TZ=Asia/Shanghai
    volumes:
      - /data/backup-platform:/data
    restart: unless-stopped
```

```bash
docker compose up -d
```

### 容器内调试

```bash
docker exec -it backup-platform /bin/bash
```

### 常见问题

| 现象 | 处理 |
|---|---|
| 拉取 ghcr.io 超时 | 配置上文国内加速器并重启 Docker |
| `denied` 拉取失败 | 确认 tag 存在；GHCR 包需在 GitHub Packages 设置为 Public |
| 容器起不来 | 检查 `/data` 挂载目录权限 |
| pip 安装超时 | 镜像内已烘焙依赖，运行时不需要 pip |

### 手动构建镜像

```bash
docker build -t backup-platform:local .
docker run --rm -p 8080:8080 backup-platform:local
```

---

## 配置

配置优先级：**代码默认值 < 环境变量 < `config.json`（项目根目录，可选）**。

常用配置项（环境变量）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | Web 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `SECRET_KEY` | 会话签名密钥（**生产务必修改**） | 随机生成并持久化 |
| `WEB_USERNAME` / `WEB_PASSWORD` | 登录账号密码 | `admin` / `admin123` |
| `BACKUP_ROOT` | 备份文件根目录 | `./backups` |
| `SCHEDULER_ENABLED` | 是否启用定时调度 | `true` |
| `DEFAULT_RETENTION_DAYS` / `DEFAULT_RETENTION_COUNT` | 默认保留天数 / 份数 | `30` / `50` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | AI 智能体模型端点（不可达时自动本地兜底） | 见 `config.py` |

---

## 支持的数据库与所需客户端

| 数据库 | 备份客户端 | 恢复客户端 | 说明 |
|---|---|---|---|
| MySQL / MariaDB | `mysqldump`、`mysql` | `mysql` | 密码通过临时选项文件注入，不出现于命令行 |
| PostgreSQL | `pg_dump`、`psql` | `pg_restore` / `psql` | 通过 `PGPASSWORD` 环境变量传密码 |
| Oracle | `expdp` / `impdp`（服务端目录）或 `exp` / `imp`（传统增量） | 同左 | 数据泵导出到数据库服务端 `DIRECTORY` |
| Kingbase 金仓 | `sys_dump`、`ksql` | `sys_restore` / `ksql` | 兼容 PostgreSQL 协议，端口默认 54321 |
| DM 达梦 | `dexp` | `dimp` | 逻辑导出，端口默认 5236 |
| SQL Server | `sqlcmd` | `sqlcmd` | 官方 T-SQL：BACKUP/RESTORE，密码经 `SQLCMDPASSWORD` 环境变量注入；Linux/Windows 均支持 |
| Redis | `redis-cli` | （复制 rdb + 重启） | 通过 `REDISCLI_AUTH` 传密码 |
| MongoDB | `mongodump` | `mongorestore` | 通过 `--password` 传密码 |

> **客户端工具装在数据库服务器上即可**，无需安装到平台机：平台通过 SSH 在数据库服务器执行备份/恢复命令，并**动态发现工具真实路径**（服务运行用户 profile → 登录 shell → 常见安装目录枚举，兼容 Oracle 11g/19c、MySQL 自编译目录、DM、金仓等各种未配环境变量的场景）。平台机自身装有客户端时也可本机执行。

---

## 使用说明（导航结构）

- **概览**：仪表盘（数据库/文件备份任务数、累计备份体积、成功失败统计）
- **备份管理**：数据库备份、文件备份、存储管理（三级存储 + 合成全量）、保护策略、备份插件
- **记录**：备份记录、恢复记录、恢复校验
- **数据恢复管理**：数据恢复、数据库部署
- **灾备管理**：数据迁移、数据同步、容灾链路、克隆服务、恢复演练
- **实时管控**：实时管控时间线（RT / CDP / PITR）
- **运维**：巡检、智能告警、数据价值挖掘、智能体、系统设置

典型操作：

1. **数据库备份**：在「数据库备份」新建任务，填写连接、备份类型、调度、保留策略与存储目标。
2. **文件备份**：在「文件备份」先到「系统设置 → SSH 主机」纳管远程主机，再建文件任务；全量生成 `*_full.tar.gz`，增量基于源快照仅打包变化文件（`*_inc.tar.gz`）。
3. **三级存储**：在「存储管理」新增 MinIO（L1）/ S3（L2）/ 本地导出（L3）目标并"测试连接"；备份完成后自动并行复制。
4. **数据同步**：新建同步任务（源/目标连接 + 表 + 字段映射 + 写入模式 + 同步模式 full/incremental/realtime）。
5. **数据迁移**：在「数据迁移」新建迁移计划（源/目标连接 + 迁移内容勾选），提交后自动执行预检查 → 结构+全量迁移 → 数据校验，实时查看各阶段进度与报告。
6. **克隆服务**：在「克隆服务」选择备份记录一键克隆（免审批直通），就绪后展示连接串，到期自动销毁。
7. **恢复校验**：配置策略定期对最近成功备份做可恢复性校验，查看报告与成功率 KPI。
8. **巡检**：点击"立即巡检"或配置定时巡检，查看 `pass/warn/fail` 明细。
9. **AI 智能体**：对话式助手支持查询任务/记录/存储用量、执行备份/巡检（需确认）、知识库问答。

---

## 生产部署建议

- 使用 `gunicorn` 运行：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`
- 通过 Nginx 反代并启用 HTTPS
- 修改 `SECRET_KEY` 与登录密码
- 将 `BACKUP_ROOT` 指向大容量、有冗余的存储；启用三级对象存储实现异地容灾
- 配置系统服务（systemd）实现开机自启与进程守护

---

## 安全说明

- 数据库连接 / SSH 主机密码以混淆方式存储于 SQLite，Web 接口默认不回显明文
- MySQL 等使用临时选项文件（权限 `600`）承载密码，避免明文出现在进程参数中
- 登录失败暴力破解限流；CSRF 同源校验；全局安全响应头与 CSP
- 备份/恢复文件下载路径穿越防护、PITR 参数注入防护、file 引擎命令注入防护
- **外部调用令牌**仅存 sha256 哈希，明文创建时一次性展示，支持随时吊销；调用在平台日志留痕

---

## 常见问题

**Q：平台机没有安装数据库客户端工具，能否备份？**
可以。平台通过 SSH 到数据库服务器执行备份/恢复命令，并动态发现工具真实路径。仅当远端确实不存在对应工具时任务才会失败，此时可通过「备份插件」页或自定义备份脚本解决。

**Q：如何验证备份真的可以恢复？**
三种方式：(1)「恢复校验」配置策略定期校验；(2)「数据对比」将恢复库与生产库做行数/校验和/抽样比对；(3) 直接对任意备份一键恢复到目标实例。

**Q：逻辑增量备份是否完全可用？**
MySQL 增量依赖 binlog；PostgreSQL / Kingbase / MongoDB 的逻辑增量能力有限，建议配合 WAL 归档 / oplog / 时间点恢复或物理备份；SQL Server 的增量即事务日志备份（`BACKUP LOG`），差异备份用 `WITH DIFFERENTIAL`。本平台逻辑引擎对不支持真正增量的库会回退为全量并在备注中说明。

**Q：如何实现异地备份？**
在「存储管理」配置 MinIO(L1) + S2(L2)，备份完成后自动复制到对象存储实现异地容灾。

**Q：文件备份需要被备份机器装 Agent 吗？**
不需要。文件备份通过 `paramiko` SSH 在远程主机上执行 `find`/`tar`。

---

## 许可证

本项目采用 [MIT License](LICENSE) 开源（社区版免费使用）；企业级增强与商业支持请联系作者。

---

## 联系方式

📧 `1547358466@qq.com`（问题反馈、功能建议、合作洽谈均可来信）

GitHub 仓库：[Zhh9126/backup-platform](https://github.com/Zhh9126/backup-platform)
