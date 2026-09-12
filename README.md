<div align="center">

# AIDBM

<p align="center"><img src="static/img/aidbm-logo.png" alt="AIDBM" width="260"></p>

**AI 原生智能数据库灾备管理平台**（AI-Native Database Backup & Disaster Recovery Management）

区别于传统备份工具，**AIDBM** 是国内少有的 AI 原生智能数据库灾备管理平台，以 AI 技术赋能传统数据备份、迁移、容灾场景，
解决**人工运维效率低、备份失效难发现、故障排查慢、异构数据适配难**等行业痛点，
真正实现数据灾备的**智能化、自动化、安全化、全域化**。

Oracle · MySQL · MariaDB · PostgreSQL · Kingbase（金仓） · DM（达梦） · SQL Server · Redis · MongoDB · 文件

**备份 · 恢复 · PITR · 数据迁移 · 数据同步 · 数据对比 · 预校验 · 克隆 · 演练 · 巡检 · AI 告警**

[![Version](https://img.shields.io/badge/Version-v1.4.5-0D9488)](#更新日志)
[![License](https://img.shields.io/badge/License-MIT-green)](#许可证)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED)](#docker-部署含离线运行)
[![Framework](https://img.shields.io/badge/Framework-Flask-black)](https://flask.palletsprojects.com/)

</div>

---

## 平台特色

- **AI 原生**：AI 助手可自然语言驱动备份/巡检/查询，自动做根因分析、修复建议与告警降噪；预校验与类型映射由 AI 辅助，把"备份失效"和"异构不兼容"挡在发生之前。
- **服务端插件化，客户端零安装**：所有驱动/插件/工具只装在平台服务端；用户客户端机器与被管理的数据库服务器**什么都不安装**、无需任何 Agent。
- **完全离线运行**：所有依赖随平台包自带（JDBC 驱动、JRE、原生驱动、备份工具离线包），运行时零联网。部署后运行 `python scripts/check_offline.py` 自检。
- **真实执行，如实失败**：所有备份/恢复/同步均为真实操作，连接失败或依赖缺失时任务**如实失败**并给出明确原因与修复指引，不做任何仿真兜底。
- **迁移前预校验**：类型映射矩阵、数据级试写、字符集冲突、容量预估、主键/外键检查——结构或数据不合适在启动前拦截。
- **可插拔数据库类型**：除内置 10 种引擎外，界面就能"新增一种数据库"——填脚本模板 + 能力声明，即刻获得与内置引擎同等的备份/恢复/调度/存储能力，无需改代码。
- **多用户与权限管控**：24 个权限点 × 3 档内置角色（admin / operator / viewer），菜单按权限自动收敛，API 层二次校验，适配多人协作与运维审计要求。

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
| 可插拔数据库类型 | 「数据库类型」页界面新增：填脚本模板（备份/增量/全实例/恢复/校验/列库/连通性）+ 能力声明，运行时渲染为 `CustomDBEngine` 注入引擎注册表，与内置引擎同一调度链路；`{{KEY}}` 占位渲染为 `${PLATFORM_KEY}`，脚本不落明文密码 |
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

### 2.1 备份后自动校验（记录列表的「校验✓」验的是什么）

每次备份成功后平台自动执行**产物级**两级校验（无需人工触发），结果写入 `backup_records.verified` / `verify_msg`，
在「备份记录」页以「校验✓」徽章展示，鼠标悬停可看本次结论原文：

| 级别 | 验什么 | 说明 |
|---|---|---|
| **L1 完整性** | sha256 校验和 + 历史比对 | 计算产物 sha256 并落库（>2GB 跳过以免拖慢主流程）；与同任务上一条成功记录比对，一致则标注「与上次一致（疑似源未变更）」，用于发现"任务一直成功但数据没变"的假象 |
| **L2 可用性** | 产物格式头探测 | 按库型读产物头部识别：gzip/zstd 魔数、MySQL `-- MySQL dump`/`CREATE`/`INSERT`、PG `PostgreSQL`/`pg_dump` 等；其他类型至少确认存在、非空、可读 |

**注意**：这是"产物级"校验，证明备份文件已生成且格式可识别，**不等于真的恢复了一遍**。
要验证"能不能真的恢复出来"，请用：

- **恢复校验**页：按策略定期把备份真实恢复到临时库/临时实例并比对，产出恢复测试报告；
- **克隆服务**：把备份拉起为虚拟数据库（VDB）供真实查询验证；
- **数据恢复**页：直接恢复到指定目标库并核对行数。

对应实现：`core/scheduler.py::_verify_backup`（L1+L2 判定）与各引擎 `verify_record()`（深度校验，
如 Oracle `impdp SQLFILE`、`RMAN RESTORE VALIDATE`）。校验未通过不会删除备份，记录照常保留。

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
| 异构类型映射矩阵 | 对标 DTS 结构初始化映射：UNSIGNED 升位、精度降级、ENUM/SET/JSON/BIT 特殊类型、TIME/空间类型目标不支持判定，逐列输出建表类型建议。覆盖 7 个源库 × 7 个目标库的偏门类型（数组 / JSONB / jsonpath / RANGE / ENUM / SET / RAW / ROWID / XMLTYPE / SDO_GEOMETRY / UNIQUEIDENTIFIER / HIERARCHYID / SQL_VARIANT / ROWVERSION / ST_GEOMETRY / VECTOR 等），每列给出 `ok` / `warn` / `fail` 级别与原因（`fail` 为预校验直接拦截项）|
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

### 10. 运维运营

| 能力 | 说明 |
|---|---|
| 巡检 | 连通性/调度/上次状态三维体检，fail 即告警；巡检记录可导出、可调度 |
| 恢复演练 | RTO/RPO 评估，趋势/基线/季度排程 |
| 通知告警 | Webhook/钉钉/企微/飞书/邮件，成功失败分别开关 |
| AI 智能助手 | 对话式运维助手，支持平台内工具调用（查任务/查报告/触发操作），LLM 不可用时本地规则兜底 |
| AI 智能告警 | 规则分析 + 故障归因，降低告警噪音 |
| 备份质量监控 | 超长/超频判定，阈值可配 |
| 容灾链路 | binlog 位点一致性校验/日志缺口检测，健康灯（red/yellow/green）|
| ITSM 工单对接 | 内置适配器，可插拔；克隆/迁移审批工单联动 |
| 保护策略 | 策略 CRUD + 任务绑定（等级/并行度/RPO/RTO 目标）|
| 生命周期管理 | 冷热分级存储：状态概览 / 策略配置 / 手动触发归档降级 |
| 多租户 / RBAC | ❌ 未实现（单管理员账号）|
| 集群化 / 高可用 | ❌ 未实现（单机架构）|

### 11. 主机纳管与数据库部署

| 能力 | 说明 |
|---|---|
| SSH 主机纳管 | 增删改查 + 连接测试；备份/恢复/同步/克隆的远端执行通道 |
| 数据库一键部署 | 达梦/金仓/Redis/MySQL/PG/Oracle 静默安装：挂载 ISO → 安装 → 初始化 → 启动 → 验证 |
| 依赖插件管理 | 备份依赖客户端（xtrabackup/dexp 等）一键安装/卸载/查询 |
| 环境自检 | `tools/check_env.py` 一键核对驱动/JDBC/JRE（服务端集中安装，客户端零安装）|

### 12. 数据服务与高级能力

| 能力 | 说明 |
|---|---|
| 数据价值挖掘 | 备份数据脱敏导出（Data Mining / Anonymized Export），备份库变现为测试数据源 |
| 全局重删 | 参照鼎甲迪备全局重删：块级去重，节省存储 |
| 自动合成全量 | 增量链自动合并为合成全量（永久增量体系）|
| 对象级恢复 | 从备份中精准提取指定表/对象，不必整库恢复 |
| 存储目标管理 | 多存储后端 CRUD + 连接测试 + 三级复制（本地/二级/三级）|

### 13. 可插拔数据库类型

| 能力 | 说明 |
|---|---|
| 界面新增数据库类型 | 「数据库类型」页填写 `db_type` / 显示名 / 默认端口 / 分类 / 图标 / 支持模式，保存即生效（无需重启）|
| 脚本模板 | 备份（全量 / 增量 / 全实例）、恢复、校验、列库、连通性测试共 7 个模板位；`{{KEY}}` 占位渲染为 `${PLATFORM_KEY}`，密码等敏感值经 `PLATFORM_PARAM_<KEY>` 环境变量注入，**脚本文件不落明文** |
| 能力声明 | `backup_modes` / `supports_incremental` / `supports_full_instance` / `supports_sync` / `client_tools` / `skip_client_check`，驱动前端表单与调度行为 |
| 同一调度链路 | 渲染为 `CustomDBEngine` 实例注入引擎注册表，cron 调度、保留策略、三级存储、全局重删、恢复校验、告警全部自动复用 |
| 动态类型发现 | `/api/meta` 的 `db_types` / `db_type_meta` / `default_ports` 运行时合并自定义类型，任务表单下拉无需改代码 |
| 安全与治理 | 删除被备份任务引用的适配器会被拒绝（提示引用任务数）；支持停用/启用、远端连通性测试、从内置模板复制 |

### 14. 用户与角色管理（RBAC）

| 能力 | 说明 |
|---|---|
| 多用户登录 | `users` 表 + PBKDF2-HMAC-SHA256（20 万次迭代，标准库实现，无第三方依赖，可离线打包）|
| 权限点 | 24 个，覆盖概览 / 备份 / 恢复 / 数据对比 / 部署 / 灾备 / 运维 / 系统 |
| 内置角色 | `admin`（全部）/ `operator`（备份·恢复·同步·克隆·对比·部署·巡检·告警·日志·运维分析，21 项）/ `viewer`（只读，4 项）|
| 附加权限 | 单用户可在角色基础上叠加权限点（`permissions` 字段，逗号分隔）|
| 菜单按权限显示 | 侧边栏 25 个菜单带 `data-perm`，登录后按 `hasPerm()` 自动隐藏；API 层 `permission_required()` 二次校验，前端绕过仍返回 403 |
| 账号治理 | 管理员重置密码 / 停用 / 删除（拒绝删除内置 admin 与最后一个启用的 admin）；用户自助改密（校验旧密码）；`must_change_password` 首登改密 |
| 兼容升级 | `users` 表为空时自动用 `config.WEB_USERNAME/WEB_PASSWORD` 种子首个 admin；旧会话与外部 API Token 调用方均不受影响 |

---

## 快速开始

```bash
# 1. 初始化元数据数据库
python init_db.py

# 2. 启动平台（同时启动后台调度器）
python run.py
```

浏览器访问 `http://<服务器IP>:8080`，默认账号 `admin / admin123`（**请立即修改**）。

> 首次启动会用 `config.WEB_USERNAME/WEB_PASSWORD` 自动创建首个 `admin` 用户（标记 `must_change_password`，建议登录后立即在「用户管理」改密）。
> 需要多人协作时，在「用户管理」新增账号并分配角色（admin / operator / viewer），菜单会按权限自动收敛。

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
docker pull ghcr.io/zhh9126/backup-platform:v1.4.5   # 固定版本（可回滚）
```

> v1.4.4 起镜像已内置全部运行依赖与备份工具（Python 原生驱动 / JDBC+JRE / xtrabackup / mariabackup 等），`docker run` 开箱即用，无需再外部挂载任何工具目录。

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
docker save ghcr.io/zhh9126/backup-platform:v1.4.5 -o backup-platform.tar
# 内网机器导入
docker load -i backup-platform.tar
```

### 运行

```bash
docker run -d --name backup-platform \
  -p 8080:8080 \
  -v backup-data:/data \
  -e BACKUP_ROOT=/data/backups \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:latest
```

> **备份文件存在哪？** 镜像默认把备份产物写到容器内 `/data/backups`（布局 `<该路径>/<数据库类型>/<任务ID_任务名>/`）。
> **必须把 `/data` 挂载到宿主机持久化卷**，否则备份会写进容器可写层（`docker inspect` 的 UpperDir 里能看到，重建容器即丢失）。
> 平台「备份存储管理」页顶部的**本地备份存储位置**卡片会显示实际路径、磁盘用量与持久化风险提示，并支持在线修改（界面配置优先级高于环境变量，重启后仍生效）。

### Docker Compose（推荐生产）

```yaml
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:latest
    ports: ["8080:8080"]
    environment:
      - BACKUP_ROOT=/data/backups
    volumes:
      - ./data:/data          # 元数据库 / 备份产物 / 日志统一持久化
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
| `BACKUP_ROOT` | 备份产物根目录（界面「备份存储管理 → 本地备份存储位置」可实时修改并持久化，**界面配置优先级更高**） | `./backups` |
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

**以上不是上限——数据库类型可插拔**：除内置引擎外，可在「数据库类型」页界面新增任意一种数据库（GaussDB / OceanBase / TiDB / 自研库等），只需提供备份/恢复脚本模板与能力声明，即可获得与内置引擎**完全相同的**调度、保留策略、三级存储、重删、告警与恢复校验能力；脚本经 SSH 在数据库服务器执行，**客户端零安装**约束不变。

---

## 使用说明（导航结构）

- **概览**：仪表盘（任务数、累计备份体积、成功失败统计）
- **备份管理**：数据库备份、文件备份、存储管理（三级存储 + 合成全量）、保护策略、备份插件、**数据库类型（可插拔）**
- **记录**：备份记录、恢复记录、恢复校验
- **数据恢复管理**：数据恢复、数据库部署
- **灾备管理**：数据迁移、数据同步、数据对比、容灾链路、克隆服务、恢复演练
- **实时管控**：实时管控时间线（RT / CDP / PITR）
- **运维**：巡检、智能告警、数据价值挖掘、智能体、**系统设置、用户管理（RBAC）**

> 菜单按当前账号权限自动收敛：`viewer` 仅见只读入口，`operator` 不见系统设置 / 用户管理 / 数据库类型，`admin` 全量可见。

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
10. **新增数据库类型**：在「数据库类型」页点「新增」，填 `db_type` / 显示名 / 默认端口与 7 个脚本模板（可从内置模板复制改），保存后到「数据库备份」新建任务即可选到该类型；右上「测试」可先做远端连通性验证。
11. **新增用户**：在「用户管理」页点「新增用户」，选角色（admin / operator / viewer）并勾选附加权限；保存后该用户登录即按权限看到对应菜单。

---

## 生产部署建议

- 使用 `gunicorn` 运行：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`
- 元数据 SQLite 建议定期备份（`instance/meta.db`）
- 备份产物目录与数据库服务器网络隔离，配置三级存储异地容灾
- 离线环境部署完成后**首先运行** `python scripts/check_offline.py` 自检

## 安全说明

- 密码 AES 加密存储（元数据库中不含明文）
- 备份密码通过临时选项文件 / 环境变量注入，不出现在命令行（防 ps 泄露）
- 平台登录密码 PBKDF2-HMAC-SHA256 加盐哈希（20 万次迭代），元数据库不存明文
- 多用户权限双层校验：菜单按权限隐藏 + API 层 `permission_required()` 强制校验（前端绕过仍 403）
- 可插拔适配器脚本模板不落明文密码：敏感参数经 `${PLATFORM_PARAM_*}` 环境变量注入
- API Token 仅存哈希；会话 Cookie HttpOnly
- 默认账号请立即修改（首登会标记 `must_change_password`）；生产环境建议限制来源 IP

## 更新日志

### v1.4.5（2026-09-12）

- **可插拔数据库类型**：新增「数据库类型」页，界面即可新增/编辑/停用/删除一种数据库（填脚本模板 + 能力声明即可用于备份/恢复）。适配器 → `db_adapters` 表 → 运行时渲染为 `CustomDBEngine` 注入引擎注册表，与 10 个内置引擎走**完全相同的调度链路**（cron/保留策略/三级存储/重删/告警全部复用）；`/api/meta` 动态返回类型与端口，任务表单无需改代码即可选到新类型。
  - 脚本模板 `{{KEY}}` 占位统一渲染为 `${PLATFORM_KEY}` shell 变量引用，**脚本文件不落明文密码**；`PLATFORM_*` 由任务参数注入
  - 删除被备份任务引用的适配器会被拒绝并提示任务数；支持「从模板复制」与远端连通性测试
- **用户与角色管理（RBAC）**：新增「用户管理」页，多用户登录 + 24 个权限点 + 3 档内置角色（`admin` / `operator` / `viewer`）。
  - 密码 PBKDF2-HMAC-SHA256（20 万次迭代，标准库实现，无第三方依赖，离线可打包），格式 `pbkdf2_sha256$<iter>$<salt>$<hash>`
  - 侧边栏 25 个菜单按权限自动隐藏；`permission_required()` 装饰器在 API 层二次校验（前端隐藏失败仍会 403 兜底）
  - 用户可叠加附加权限（`permissions` 字段）；支持管理员重置密码、用户自助改密、`must_change_password` 首登改密提醒
  - 兼容升级：`users` 表为空时自动用 `config.WEB_USERNAME/WEB_PASSWORD` 种子首个 admin；旧会话（字符串 user）自动兼容；外部 API Token 调用方不受影响
- **本地备份存储位置（L1 落点）可见可配置**：生效优先级「界面配置（`system_config.backup_root`）> 环境变量 `BACKUP_ROOT` / `config.json` > 程序目录 `backups` 兜底」，实时日志与实时文件目录跟随根目录；存储管理页新增卡片展示实际路径、来源与磁盘用量，`GET/PUT /api/storage/local-root` 支持在线改路径（路径校验 + 写探测 + 可选迁移历史备份）；启动与运维日志明确提示持久化风险（安装目录内 / 容器未挂卷告警）。
- **失败可定位的日志体系**：日志目录可写性兜底（配置目录不可写时降级并标识来源）、`platform.log` / `error.log` 大小轮转（20MB × 10）、`faulthandler` 段错误兜底（`crash.log`）、启动横幅打印运行形态 / PID / 日志 / 备份 / 元数据库路径；全链路脱敏（口令、令牌、连接串）；每次备份 / 恢复 / 校验生成**独立详细日志**（`logs/oper/`，含完整执行命令与输出）。
- **数据迁移与同步：全库字段类型保真映射（本次重点）**：类型映射升级为三层架构——预检矩阵（`core/sync/type_matrix.py`）→ 列元数据真实精度（各插件 `list_columns`）→ 建表 DDL 方言。覆盖 **7 种数据库（PostgreSQL / 金仓 / MySQL / MariaDB / Oracle / 达梦 / SQL Server）× 7 个目标库** 的偏门类型：
  - PG 系：数组 `integer[]` / `text[]`、`jsonb` / `json` / `jsonpath`、RANGE 族、`hstore` / `tsvector` / `citext` / `ltree`、`inet` / `cidr` / `macaddr`、`character varying`、`timestamp with time zone`、`interval`
  - MySQL 系：`ENUM` / `SET`（同库原写法透传）、`YEAR`、`BIT(n)`、空间族、`VECTOR(n)`、`TINYINT(1)`、无符号整数升位
  - Oracle：`RAW` / `LONG RAW` / `LONG`、`ROWID` / `UROWID`、`XMLTYPE`、`SDO_GEOMETRY`、`ANYDATA` 族、`BFILE`、`PLS_INTEGER` / `BINARY_INTEGER`、`INTERVAL YEAR TO MONTH / DAY TO SECOND`
  - SQL Server：`UNIQUEIDENTIFIER`、`HIERARCHYID`、`GEOGRAPHY` / `GEOMETRY`、`SQL_VARIANT`、`ROWVERSION` / `TIMESTAMP`、`DATETIME2` / `DATETIMEOFFSET` / `SMALLDATETIME`、`MONEY` / `SMALLMONEY`
  - 达梦 / 金仓：`TEXT` / `LONGVARCHAR`、`IMAGE`、`ST_GEOMETRY`、`BIGDATETIME`、`SERIAL` / `INT2` / `INT4` / `INT8`
  - 修复阻断型缺陷：带修饰类型名（`decimal(12,3)` / `varchar(100)`）精确比较失败导致全部落兜底 `VARCHAR(255)`（数值列退化为字符串列）；TEXT 与 BLOB 共用协议类型码 252 被误判为二进制（中文数据损坏）；多词类型（`character varying` / `timestamp with time zone` / `long raw`）被截断；`integer[]` 数组映射落空返回 `None`；SQL Server 的 `TIMESTAMP`（ROWVERSION 别名）覆盖全局时间戳语义；`PLS_INTEGER` / `int2` / `serial` 别名触发 KeyError；`DATETIMEOFFSET` / `BIGDATETIME` / `INT4RANGE` 等源类型名被原样输出到目标库（建表直接失败）
  - 建表端新增跨源兜底 `matrix_suggest()`：异库特有类型经统一矩阵翻译，不再一律落 `VARCHAR(255)` / `NVARCHAR(4000)` / `VARCHAR2(4000)`；MySQL 侧修正 `INT UNSIGNED`、`INT4RANGE`、`TIMESTAMP WITH TIME ZONE` 三处会生成非法 DDL 的路径
  - 冒烟验证：**1281 组映射组合，目标方言语义违规 0**；长度与精度保真断言全通过（`varchar(100)→VARCHAR(100)`、`decimal(12,3)→DECIMAL(12,3)` / `NUMBER(12,3)` / `NUMERIC(12,3)`）
- **MariaDB 打通**：连通性探测注册 `mariadb`（协议兼容复用 MySQL 实现，此前 `src_db_type=mariadb` 预检查直接失败「未知数据库类型」）；迁移计划与数据同步（含全库迁移 `full_db_migrate`）双向真机实测通过，中文与二进制数据逐值一致；`db_adapters` 表迁移（存量库幂等补齐）。已知边界：实时同步（Binlog CDC）当前仍限定 MySQL 源。
- **物理备份与恢复加固**：
  - **目录形态产物校验**：物理备份（xtrabackup / mariabackup / pg_basebackup）未打包时产物是**目录**，此前按「文件」校验会被误报「文件不存在」（MariaDB / MySQL 物理全量与增量均命中）；现按目录级校验，空目录 / 零字节直接判失败，并以备份标志文件（`xtrabackup_checkpoints` / `mariabackup_checkpoints` / `backup-my.cnf` / `backup_label` / `PG_VERSION`）作为「数据库可识别的可恢复产物」证据，指纹（文件数 / 总字节 / 标志文件）写入校验说明
  - **空增量层清理**：增量基不存在时自动退化为全量，残留的空增量目录会被恢复链识别为增量层并导致 apply 报 `cannot open .../xtrabackup_checkpoints`；现自动清理空目录，且恢复链构建时剔除无效增量层（空目录 / 无 checkpoints）
  - **物理恢复校验改为整实例级**：物理恢复是数据目录级，任务表单的 `target_db` 属逻辑恢复语义常不成立；校验改为先查目标库、不存在则回退实例级（库数 + 业务表数），业务表数为 0 时如实判失败，消除「查不到表仍报通过」的假通过

> 本次更新的完整说明（含逐库类型映射对照表、验证记录与已知边界）见 [readme_20260912.md](readme_20260912.md)。

### v1.4.4（2026-09-09）

- **Docker 镜像自足**：备份工具（xtrabackup / mariabackup 等）烘焙进镜像，启动即用、零外部挂载
- **fix(db)**：新建数据同步任务时 `sync_tasks` 缺 `precheck_sample_rows` 列，导致全新目标库预检失败

### v1.4.3（2026-09-09）

- **fix(probe)**：MySQL / PostgreSQL / Redis / MongoDB 连通性探测改为原生驱动优先
- **fix(mysql)**：8.0 dump 跨版本恢复兼容——目标为 5.7 / MariaDB 时自动降级 `utf8mb4_0900_*` 排序规则

> 更早版本（v1.0.0 ~ v1.4.2）发布历史见仓库 Git Tags。

## 许可证

MIT（社区版免费供个人学习、内部部署与中小规模生产环境使用。企业级增强请联系作者：📧 `1547358466@qq.com`）
