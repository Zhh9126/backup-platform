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

- **服务端插件化，客户端零安装**：所有驱动/插件/工具只装在平台服务端；用户客户端机器与被管理的数据库服务器**什么都不安装**、无需任何 Agent。
- **完全离线运行**：所有依赖随平台包自带（JDBC 驱动、JRE、原生驱动、备份工具离线包），运行时零联网。部署后运行 `python scripts/check_offline.py` 自检。
- **真实执行，如实失败**：所有备份/恢复/同步均为真实操作，连接失败或依赖缺失时任务**如实失败**并给出明确原因与修复指引，不做任何仿真兜底。
- **迁移前预校验**：类型映射矩阵、数据级试写、字符集冲突、容量预估、主键/外键检查——结构或数据不合适在启动前拦截。

---

## 功能总览

### 1. 备份能力

| 能力 | 说明 |
|---|---|
| 9 个数据库备份引擎 + 文件备份 | MySQL / MariaDB / PostgreSQL / Kingbase / DM / SQL Server / Oracle / Redis / MongoDB + 文件（本地 + 远程 SSH 无 Agent）|
| 备份类型 | 全量 / 增量 / 差异（SQL Server）/ 快照 / 合成全量 / 组合（全量+增量双调度）|
| MySQL 物理增量备份与增量链恢复 | XtraBackup 增量备份；恢复时基备 prepare → 逐层合并增量 → 临时实例校验，自动处理压缩产物与增量链 |
| 达梦物理增量备份 | 联机 `BACKUP INCREMENT BACKUPSET`，增量备份集自动拉回 |
| 调度 | cron 表达式 / 固定间隔（APScheduler）|
| 保留策略 | 按天数 + 按份数双重清理（GFS）|
| 自定义备份/恢复脚本 | 全数据库类型通用，平台注入 `PLATFORM_*` 环境变量，SFTP 拉回产物并计算 sha256 |
| 三级存储 | L1 MinIO（热）/ L2 S3（冷）/ L3 本地导出，备份后自动并行复制 |
| 生命周期 | L1→L2 按龄/按容量下沉、到期清理 |
| 全局重删 | 内容 sha256 索引 + 引用计数 |
| 存储池加密 | AES-256-GCM 信封式，密钥来源：环境变量 / 系统设置托管 / 外部 KMS |
| 备份插件 | 服务端插件市场（XtraBackup / MariaDB Backup / pgBackRest / MongoDB Tools 等），支持离线包安装 |
| 远端工具动态发现 | 数据库服务用户 profile → 登录 shell → 常见目录枚举 → find，不写死路径；支持 `tool_path` 手动兜底 |
| 零安装执行 | SSH 远程优先（用数据库自带工具）→ 平台推送临时副本（/tmp 执行即清理）→ 回退平台服务端执行 |

### 2. 恢复能力

| 能力 | 说明 |
|---|---|
| 一键恢复 | 备份记录 → 目标实例（库级）或目标目录（文件级）|
| 表级并行导入 | MySQL 逻辑备份恢复自动拆分 dump 并行导入（`RESTORE_PARALLEL` 可调）|
| 物理恢复并行化 | XtraBackup `--prepare` 附带 `--parallel` |
| 增量链物理恢复 | 自动识别增量产物，定位全量基备，逐层应用后临时实例校验 |
| Redis 全自动恢复 | SFTP 推送 rdb → 停库 → 替换数据文件 → 自动重启加载 → PING 验证 |
| 恢复校验 | 策略化对最近成功备份做可恢复性校验（Oracle 走 impdp SQLFILE / RMAN RESTORE VALIDATE），生成报告 |
| 文件增量恢复 | 自动构建恢复链（最近全量 → 按时间应用增量）|
| PITR 时间点恢复 | MySQL binlog / PostgreSQL WAL 持续捕获；达梦 dmrman RESTORE/RECOVER（还原到指定目录，自动对齐源实例页大小/簇大小）|

### 3. 数据迁移（一站式：预检查 → 结构迁移 → 全量迁移 → 数据校验）

| 能力 | 说明 |
|---|---|
| 支持链路 | MySQL / MariaDB / PostgreSQL / DM（达梦）/ Oracle 互为源目标（含异构跨库：MySQL→达梦、PG→达梦、MySQL→PG、达梦→PG、MySQL→Oracle、Oracle→MySQL 等）|
| 全库迁移模式 | 源库所有表一次性迁移到目标库 |
| 数据校验 | 逐表行数比对（源 vs 目标），逐表明细 |
| 迁移报告 | 各阶段结果/行数/耗时汇总 |
| 连接通道 | 原生 Python 驱动优先（pymysql / psycopg2 / oracledb thin / dmPython），JDBC 兜底（驱动 jar 随包分发）|

### 4. 数据同步（实时/增量/离线）

| 能力 | 说明 |
|---|---|
| 支持链路 | 与数据迁移一致（MySQL / MariaDB / PostgreSQL / DM / Oracle 互为源目标，原生驱动 + JDBC 双通道）|
| 同步模式 | full（数据迁移·一次性）/ incremental（周期增量）/ realtime（实时轮询）|
| 写入模式 | append / overwrite（仅迁移可用）/ upsert / create_if_not_exists |
| 增量同步 | 指定增量列 + 起始值，watermark 断点记录 |
| 字段映射 | 同名映射 / 手动映射 / 可视化连线；列名归一 origin/upper/lower/camel/underscore |
| 统一类型系统 | 源端类型 → 平台中间类型 → 目标端类型，读写两侧一致转换 |
| 概念区分 | 迁移=一次性任务（支持覆盖写入）；同步=持续性任务（禁用覆盖写入），引擎/预校验/前端四层强制执行 |

**Oracle 异构链路真机验证（129 / Oracle 19c）**：mysql→oracle 17/17 行 PASS、oracle→mysql 25/25 行 PASS。
深度测试命中并修复 Oracle 异构迁移典型坑：oracledb 占位符方言、`_` 开头标识符非法（ORA-00911）、
试写临时表 schema 前缀、datetime 字符串 NLS 转换（ORA-01843，会话级对齐 + 原生绑定）、
`varchar2(n)` 精度解析、空 ORDER BY（ORA-00936）、连接自动重试（跨虚拟机 1521 偶发重置）。

### 5. 迁移前预校验

| 检查项 | 说明 |
|---|---|
| 连通性 / 表存在性 | 源目标连接、源表可读、目标表存在性（overwrite 自动建表提示）|
| 异构类型映射矩阵 | 对标 DTS 结构初始化映射：UNSIGNED 升位、精度降级、ENUM/SET/JSON/BIT 特殊类型、TIME/空间类型目标不支持判定，逐列输出建表类型建议 |
| 主键检查 | upsert/实时同步要求目标表单列主键 |
| 增量列检查 | 增量列存在性与类型（数值/时间）|
| 数据级试写 | 源端采样 N 行 → 目标端按映射建议 DDL 建临时表试写 → 对账 → 清理（采样行数可配，0=跳过）|
| 大表容量预估 | 行数/体积统计、按带宽估时、大表告警 |
| 字符集冲突检测 | 跨库字符集家族判定（utf8mb4/utf8/GB18030/UTF8），4 字节→3 字节告警，不兼容拦截 |
| 外键父表完整性 | 子表依赖的父表不在同步列表 → 告警 |
| 接入点 | 引擎层强制拦截 + REST API + 前端结构化报告（运行前自动弹出）|

### 6. 数据对比

| 能力 | 说明 |
|---|---|
| 主键归并对比 | 对标 pt-table-checksum：keyset 分页双指针归并，O(单页)内存，千万级表可对比 |
| 差异行级定位 | missing_in_target / extra_in_target / changed 三类精确定位，附修复 SQL 指引 |
| 跨名表映射 | tables 支持 {"source": "...", "target": "..."} 跨名对比 |
| 同库/异库对比 | MySQL、PostgreSQL、DM（达梦）、Oracle、Kingbase 互为源目标 |
| 多库型校验和 | MySQL CRC32 / PG hashtext / Oracle·达梦 ORA_HASH / SQL Server CHECKSUM_AGG |
| 大小写不敏感 | 表清单与列名自动归一匹配 |

### 7. 实时备份（RT / CDP）

| 能力 | 说明 |
|---|---|
| MySQL binlog 流式捕获 + PITR | `mysqlbinlog --read-from-remote-server --raw --stop-never` |
| PostgreSQL WAL 流式捕获 | `pg_receivewal` |
| Oracle LogMiner / 达梦 LogMNR | 日志解析轨道 |
| Kingbase WAL | 缺 sys_receivewal 时降级采样 |
| 文件实时捕获 | watchdog / polling 双模式 |

### 8. 数据库部署

| 能力 | 说明 |
|---|---|
| 支持部署 | MySQL 8.0 / PostgreSQL / MongoDB（副本集+认证）/ Kingbase / Redis / DM / Oracle |
| 部署流程 | 上传安装包 → 创建部署任务 → 异步执行（SFTP 推送 + 远程脚本）→ 实时日志 |
| Redis 部署 | 源码编译安装，启动脚本含 --daemonize，环境变量写入 profile.d |
| Kingbase 静默安装 | ISO 挂载 → InstallAnywhere 静默安装（自动处理临时目录/非 root/授权文件/家目录空间检查）|
| 参数配置 | base_dir / data_dir / 端口 / 密码 / 字符集 / 模式等，均由任务配置 |

### 9. 克隆服务（VDB）

| 能力 | 说明 |
|---|---|
| 免审批直通 | `CLONE_AUTO_APPROVE=true` 默认；可切回 ITSM 审批流 |
| 逻辑导入克隆 | mysql / mariadb / postgresql（备份产物流式导入到目标实例新库）|
| **PostgreSQL 秒级克隆（COW）** | `clone_mode=template`：`CREATE DATABASE ... TEMPLATE` 写时复制，实测 0.28s 完成整库克隆 |
| **MySQL 快照克隆（COW）** | `clone_mode=snapshot`：LVM 快照 + 独立端口实例；VG 空间预检，非 LVM 明确报错不降级 |
| **Oracle schema 克隆** | `clone_mode=schema`：在线 expdp 源 schema → impdp REMAP_SCHEMA 到新 schema（129 真机验证 44s）|
| TTL 到期自动销毁 | 默认 7 天，可配置；Oracle 克隆清理走 DROP USER CASCADE |

### 10. 运维管理

| 能力 | 说明 |
|---|---|
| 巡检 | 连通性/调度/上次状态三维体检，fail 即告警 |
| 恢复演练 | RTO/RPO 评估，趋势/基线/季度排程 |
| 通知告警 | Webhook/钉钉/企微/飞书/邮件，成功失败分别开关 |
| AI 智能体 | 对话式运维助手，工具调用，LLM 不可用时本地兜底 |
| AI 智能告警 | 规则分析 + 归因 |
| 备份质量监控 | 超长/超频判定，阈值可配 |
| 容灾链路 | binlog 位点一致性校验/日志缺口检测 |
| ITSM 工单对接 | 内置适配器，可插拔 |
| 多租户 / RBAC | ❌ 未实现（单管理员账号）|
| 集群化 / 高可用 | ❌ 未实现（单机架构）|

---

## 快速开始

```bash
# 1. 初始化元数据数据库
python init_db.py

# 2. 启动平台（同时启动后台调度器）
python run.py
```

浏览器访问 `http://<服务器IP>:8080`，默认账号 `admin / admin123`（**请立即修改**）。

> 备份任务执行前，请先在「系统设置 → SSH 主机」纳管数据库服务器（或使用任务级 SSH 凭据）。

---

## 离线环境部署

平台面向**完全离线环境**设计：运行时不安装任何东西，一切依赖随离线包自带。

1. **构建离线包**：PyInstaller 打包（Python 依赖全内置）+ `drivers/`（JDBC 驱动 jar）+ `jdk/`（JRE，达梦/Oracle JDBC 通道必需）+ 备份工具离线包。
2. **部署后自检**：`python scripts/check_offline.py` —— 五层检查（Python 依赖 / JDBC jar / JVM / dmPython / 外部工具），缺失项给出处置指引。
3. **备份工具**：仅支持离线包上传安装（备份插件页 SFTP 上传），不依赖在线源。

---

## 外部 API 调用（Bearer Token）

平台提供 REST API 供外部系统调用，认证方式为 **Bearer Token**。

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

# 数据同步任务迁移前预校验
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://<服务器IP>:8080/api/sync-tasks/9/precheck

# 数据迁移（预检查 → 结构 → 全量 → 校验 一站式）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"plan_id":1}' \
  http://<服务器IP>:8080/api/migration/run
```

---

## Docker 部署（含离线运行）

### 镜像地址

```bash
docker pull ghcr.io/zhh9126/backup-platform:latest
docker pull ghcr.io/zhh9126/backup-platform:v1.4.0   # 固定版本（可回滚）
```

### 服务端环境自检（部署后先跑一遍）

```bash
python tools/check_env.py
# 一键核对：Python 直连驱动（8 项）/ JDBC 驱动包 / JRE，输出缺失项与离线安装指引
# 按"服务端集中安装、客户端零安装"设计，所有数据库驱动都在镜像内预装
```

### 国内加速

```json
// /etc/docker/daemon.json
{"registry-mirrors": ["https://docker.m.daocloud.io", "https://dockerproxy.com"]}
```

### 离线导入

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

### 手动构建镜像

```bash
docker build -t backup-platform:local .
```

---

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `BACKUP_ROOT` | 备份产物根目录 | `./backups` |
| `SCHEDULER_ENABLED` | 是否启用定时调度 | `true` |
| `DEFAULT_RETENTION_DAYS` / `DEFAULT_RETENTION_COUNT` | 默认保留天数 / 份数 | `30` / `50` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | AI 智能体模型端点（不可达时自动本地兜底） | 见 `config.py` |

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
| Redis | redis-cli --rdb | （替换 rdb + 自动重启） | REDISCLI_AUTH 传密码 |
| MongoDB | mongodump | mongorestore | --archive 流式拉回 |

**数据迁移/同步连接通道**：原生 Python 驱动优先（pymysql / psycopg2 / oracledb thin），JDBC 兜底（驱动 jar 随包，达梦/金仓/Oracle 全覆盖）。部署后运行 `python tools/check_env.py` 一键自检（服务端集中安装、客户端零安装）。

---

## 使用说明（导航结构）

- **概览**：仪表盘（任务数、累计备份体积、成功失败统计）
- **备份管理**：数据库备份、文件备份、存储管理（三级存储 + 合成全量）、保护策略、备份插件
- **记录**：备份记录、恢复记录、恢复校验
- **数据恢复管理**：数据恢复、数据库部署
- **灾备管理**：数据迁移、数据同步、数据对比、容灾链路、克隆服务、恢复演练
- **实时管控**：实时管控时间线（RT / CDP / PITR）
- **运维**：巡检、智能告警、数据价值挖掘、智能体、系统设置

典型操作：

1. **数据库备份**：在「数据库备份」新建任务，填写连接、备份类型、调度、保留策略与存储目标。
2. **文件备份**：在「文件备份」先纳管远程主机，再建文件任务；全量生成 `*_full.tar.gz`，增量基于源快照仅打包变化文件。
3. **数据同步**：新建同步任务（源/目标连接 + 表 + 字段映射 + 写入模式 + 同步模式），运行前自动执行迁移前预校验，也可点「预检」单独查看报告。
4. **数据对比**：在「数据对比」新建对比任务（源/目标连接 + 表清单，支持跨名映射），报告逐行展示差异定位与修复指引。
5. **数据库部署**：在「数据库部署」上传安装包，选择目标主机一键部署，实时查看安装日志。
6. **克隆服务**：选择备份记录一键克隆（免审批直通），就绪后展示连接串，到期自动销毁。
7. **恢复校验**：配置策略定期对最近成功备份做可恢复性校验，查看报告与成功率 KPI。
8. **巡检**：点击"立即巡检"或配置定时巡检，查看 pass/warn/fail 明细。
9. **AI 智能体**：对话式助手支持查询任务/记录/存储用量、执行备份/巡检（需确认）、知识库问答。

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
