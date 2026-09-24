<div align="center">

# AIDBM

<p align="center"><img src="static/img/aidbm-logo.png" alt="AIDBM" width="260"></p>

**AI 原生智能数据库灾备管理平台**（AI-Native Database Backup & Disaster Recovery Management）

区别于传统备份工具，**AIDBM** 是国内少有的 AI 原生智能数据库灾备管理平台，以 AI 技术赋能传统数据备份、迁移、容灾场景，
解决**人工运维效率低、备份失效难发现、故障排查慢、异构数据适配难**等行业痛点，
真正实现数据灾备的**智能化、自动化、安全化、全域化**。

Oracle · MySQL · MariaDB · PostgreSQL · Kingbase（金仓） · DM（达梦） · SQL Server · Redis · MongoDB · Neo4j · 文件

**备份 · 恢复 · PITR · 数据迁移 · 数据同步 · 数据对比 · 预校验 · 克隆 · 演练 · 巡检 · AI 告警**

[![Version](https://img.shields.io/badge/Version-v1.4.14-0D9488)](#更新日志)
[![License](https://img.shields.io/badge/License-MIT-green)](#许可证)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED)](#docker-部署含离线运行)
[![Framework](https://img.shields.io/badge/Framework-Flask-black)](https://flask.palletsprojects.com/)

</div>

---

> **部署到离线环境（不可联网、不可随意改代码）前请先阅读**：
> [产品功能固化清单](docs/feature_manifest_20260917.md)（功能全集、各引擎真实验证状态、能力边界）
> · [离线交付与运维手册](docs/offline_ops_manual_20260917.md)（安装、上线检查清单、排障地图、不改代码可调整项）
> · [产品设计说明书](docs/product_design_spec_20260916.md)（领域模型与产品化差距清单 G1–G13）

> **二次开发 / 排障前先读**：[代码图谱](docs/code_graph.md)（由 `scripts/gen_code_graph.py` 自动生成，零第三方依赖）——分层架构与模块依赖、枢纽模块与循环/越层依赖体检、**332 条 REST 路由索引**、页面→JS→接口链路、元数据库表访问热点、关键业务链路，以及「想改 X 先看哪些文件」。重生成：`python scripts/gen_code_graph.py`（结构漂移用 `--diff`，CI 卡口用 `--strict`）。

## 平台特色

- **AI 原生**：AI 助手可自然语言驱动备份/巡检/查询，自动做根因分析、修复建议与告警降噪；预校验与类型映射由 AI 辅助，把"备份失效"和"异构不兼容"挡在发生之前。
- **服务端插件化，客户端零安装**：所有驱动/插件/工具只装在平台服务端；用户客户端机器与被管理的数据库服务器**什么都不安装**、无需任何 Agent。
- **完全离线运行**：所有依赖随平台包自带（JDBC 驱动、JRE、原生驱动、备份工具离线包），运行时零联网。部署后运行 `python scripts/check_offline.py` 自检。
- **真实执行，如实失败**：所有备份/恢复/同步均为真实操作，连接失败或依赖缺失时任务**如实失败**并给出明确原因与修复指引，不做任何仿真兜底。
  > **唯一的例外（如实说明）**：CDC 链路保留了仿真降级通道（`core/cdc/simulated.py`），仅在 ① `DEMO_MODE=on`、② 任务标记 `demo_only`、③ `rt_mode=sample` 三种**用户显式要求演示**的情况下被选中；因能力不足（引擎未注册 / 依赖缺失 / 客户端缺失）的自动降级自 v1.4.10 起**默认禁止**（`RT_ALLOW_SIMULATED_FALLBACK`，默认 false），此类情况任务直接置 FAILED 并写 error 日志，不再产出仿真数据。
- **迁移前预校验**：类型映射矩阵、数据级试写、字符集冲突、容量预估、主键/外键检查——结构或数据不合适在启动前拦截。
- **可插拔数据库类型**：除内置 10 种引擎外，界面就能"新增一种数据库"——填脚本模板 + 能力声明，即刻获得与内置引擎同等的备份/恢复/调度/存储能力，无需改代码。
- **多用户与权限管控**：24 个权限点 × 3 档内置角色（admin / operator / viewer），菜单按权限自动收敛，API 层二次校验，适配多人协作与运维审计要求。

---

## 功能总览

### 1. 备份能力

| 能力 | 说明 |
|---|---|
| 10 个数据库备份引擎 + 文件备份 | MySQL / MariaDB / PostgreSQL / Kingbase / DM / SQL Server / Oracle / Redis / MongoDB / Neo4j + 文件（本地 + 远程 SSH 无 Agent）|
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
| 虚拟机备份（整机） | PVE / KVM(libvirt) / ESXi / Hyper-V 纳管 → 保护策略 → 恢复点（PITR）→ 原地还原 / 克隆新 VM / 自动恢复验证（SureBackup 式）；复用平台既有调度、保留、存储分层与告警 |
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
| 多租户 / RBAC | 已实现|
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
| 数据价值挖掘 | 资产盘点 → 敏感发现与国标分级（GB/T 43697-2024）→ 价值评估与冷数据治理 → 合规概览（PIPL/等保）→ 脱敏导出，把冷备份变成可治理、可举证、可外发的数据资产 |
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
docker pull ghcr.io/zhh9126/backup-platform:v1.4.14   # 固定版本（可回滚）
# 当前最新版本：v1.4.14（2026-09-24）
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
docker save ghcr.io/zhh9126/backup-platform:v1.4.14 -o aidbm-v1.4.14.tar
# 内网机器导入
docker load -i aidbm-v1.4.14.tar
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

### 升级（旧版本 → 新版本）

```bash
# 1. 导入新版本镜像（离线环境在有网机器 docker save 后拷入）
docker load -i aidbm-v1.4.14.tar

# 2. 停止并删除旧容器（备份数据在 backup-data 卷中，不受影响）
docker stop backup-platform
docker rm backup-platform

# 3. 用新版本镜像启动（命令与首次部署完全一致，仅换镜像 tag）
docker run -d --name backup-platform   -p 8080:8080   -v backup-data:/data   -e BACKUP_ROOT=/data/backups   --restart unless-stopped   ghcr.io/zhh9126/backup-platform:v1.4.14

# 4.（可选）删除旧版本镜像释放空间
docker rmi ghcr.io/zhh9126/backup-platform:v1.4.13
```

> 升级不丢数据：元数据库、备份产物、日志都在 `backup-data` 卷（`/data`），
> 换容器不影响。**删除旧镜像前确认容器已停止并移除**，否则会报
> `conflict: unable to remove ... container is using`。

### 卸载

```bash
# 1. 停止并删除容器
docker stop backup-platform
docker rm backup-platform

# 2. 删除镜像
docker rmi ghcr.io/zhh9126/backup-platform:v1.4.14

# 3.（谨慎）删除数据卷 —— 会清掉全部备份产物与元数据，操作前务必
#    先把备份文件拷贝转移！
docker volume inspect backup-data      # 先查看卷在宿主机的实际位置
docker volume rm backup-data           # 确认不再需要数据后再执行
```

> 卷的实际宿主机路径可用 `docker volume inspect backup-data`（Mountpoint 字段）查看，
> 离线环境建议直接从该目录 tar 备份后再做删除操作。

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
| Neo4j | neo4j-admin database dump / backup、APOC 导出 | neo4j-admin database load / restore、apoc.import.* | 三通道：离线 dump、企业版在线 backup（不停机、可增量）、APOC 在线导出（Cypher/JSON/CSV）；默认端口 7687 |

### 虚拟机备份支持矩阵（/vm）

| 虚拟化平台 | 接入方式 | 备份通道 | 增量能力 | 克隆新 VM |
|---|---|---|---|---|
| Proxmox VE | HTTPS API（账号密码 / API Token） | `vzdump`；PBS 存储时走 PBS 备份 | 备份存储为 PBS 时支持块级增量，否则按全量执行（如实说明，不伪造增量） | 支持（可隔离网络、改 MAC、自动开机、TTL 自动销毁） |
| KVM / libvirt | SSH + `virsh` | 快照/检查点 + 磁盘镜像导出 | 支持增量（`virsh backup-begin` 可用时） | 支持 |
| VMware ESXi | SSH + `vim-cmd` | 关机/快照后打包 VM 目录（vmdk） | 依赖快照链，不支持时回退全量 | 支持 |
| Hyper-V | SSH + PowerShell | `Export-VM` / 检查点导出 | 依赖检查点，不支持时回退全量 | 支持 |

设计要点：**不新建平行调度体系**——每台受保护 VM 对应一条 `backup_tasks(db_type='vm')`，复用平台调度、保留策略、三级存储、生命周期与告警；恢复点（PIT）单独建链（全量 / 增量 / 合成全量），支持按时间点选择还原，并可对恢复点做自动恢复验证（拉起隔离 VM 后销毁）。

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

### v1.4.13（2026-09-22）

- **「组合 / 增量任务保存后无法编辑」根因修复（用户反馈，本次重点）**
  - **现象**：任务是「组合（全量+增量）」或「增量」类型时，点「编辑」页面**毫无反应**、也不报错；全量任务正常。
  - **根因三层叠加**：① 页面真实元素 id 是 `t_incremental_days` / `t_incremental_time`，旧 JS 按前缀拼成 `t_inc_days`；② `BKP.$` 对不存在的元素返回**万能代理对象**，参与 `if (el)` 判断恒为真，代码继续往下走；③ 该代理对象上调用 `querySelectorAll(...).forEach(...)` 得到 `undefined` → 抛 `TypeError`，而这条链路**没有任何 try/catch**，静默打断 `openTaskModal` —— 用户侧就是"点了没反应"。
  - **修复**：`bkp-core.js` 让代理对象对 `querySelectorAll` 返回 `[]`、`querySelector` 返回 `null`（源头止血）；`app.js` 新增 `_mixedIds` / `_firstRealEl` / `_checkboxesOf` / `_daysFromCron`，用 `document.getElementById` 逐个取候选 id（含 `_inc_` → `_incremental_` 兼容），缺失时**从 cron 星期段反推按天勾选**；`OpenTaskModal` / `openFileTaskModal` / `editTask` / `editFileTask` **全部加 try/catch + toast**（同类问题今后会弹红色提示而非静默）。顺带补齐 `tasks.html` 缺失的 `t_ssh_host`（高级选项里"指定纳管主机"此前不生效）。
- **新增「备份产物定期清理」**：备份一直积累会占满磁盘，而手工删风险极大。现在支持按**每个任务自己的保留天数/份数**自动回收，并提供 **dry-run 预览**。
  - **安全设计（三层护栏）**：① **keep_min 兜底**——每任务最后 N 份成功备份无条件保留，永远不会清空最后一个可用备份；② **`_is_managed_path` 路径护栏**——只允许删备份根目录之内的文件，越界一律拒绝；③ **未配置保留天数的任务不会被清理**（不会被误伤）。
  - **审计友好**：清理后默认把记录标记为 `expired_cleanup`（**保留审计轨迹**），可选才物理删行；"产物已不在磁盘"的幽灵记录只对齐账本，**不虚报释放空间**。
  - **落地**：`core/backup_cleanup.py` + `api/backup_cleanup.py`（配置 / preview / preview\-\<id\> / run）+ `core/scheduler.py` 新增 `_register_cleanup()`（job `backup_cleanup`，cron 默认 `10 3 * * *`，**start 与 reload 两条路径均已注册**，顺带修掉此前 reload 会丢 gfs/ferry job 的缺陷）+ 数据库备份页工具栏黄色按钮「定期清理」与弹窗（开关 / cron / 最少保留 / 是否删记录 / 扫描预览 / 立即清理）+ `expired_cleanup`、`expired_gfs` 状态徽章。
  - **验证**：`tests/test_backup_cleanup.py` **13 passed**（含 UI 契约静态回归）；本机手工跑过一次真实清理（24 条记录标记 `expired_cleanup`，其中 2 条真实删除了文件）。
  - 默认：开关 **开启**、每任务最少保留 **1 份**、cron `10 3 * * *`；任务表单「保留天数」默认 30 天（`retention_count` 字段当前未在表单开放，需由管理员经接口设置）。
- **交付《AIDBM 用户操作指导手册（详细版）》**（`docs/AIDBM_用户操作指导手册_详细版_20260922.md`，1668 行 / 26 章 + 4 附录）：逐 UI 事实编写（表单按**元素 id → 中文标签**抽取、取值范围取自 `<select>` option），覆盖全部 26 个功能页；含 **12 个场景剧本**（新库首日保护、误删单表、整库误 UPDATE 回到 10 分钟前、可停机/不停机搬迁、磁盘占满、交审计材料、季度演练、克隆测试库、备份失败怎么查、外包脱敏数据、值班例行清单）、11 类错误排障对照表、20 条 FAQ、cron 与关键字段 id 速查。快速版 `docs/AIDBM_用户操作指导手册_20260922.md` 保留给新人首日上手。
- **完整说明见 [readme_20260922.md](readme_20260922.md)**。

### v1.4.12（2026-09-19）

- **批量修复「引用了但从未导入/定义」的运行时崩溃（9 组、11 处）**
  - **背景**：用户连续反馈两处 `NameError`（AI 助手页 `needs_confirm`、操作日志页 `contract`），属同一类"用了但没导入"缺陷。本轮改为新增基于 `symtable` 的全仓静态检查（186 个文件）一次扫出全部同类隐患，逐个核实后修复，检查器复跑 **0 处残留**。
  - **用户可见崩溃**：① 操作日志页 `contract` 未导入（`api/logs.py`）；② AI 告警页 `contract` 未导入（`api/ai_alert.py`，同一隐患尚未爆出）；③ AI 助手按自然语言点名执行任务时 `_apply_intent_tiebreak` **全项目从未定义**（必崩）——已按 `_INTENT_TOKENS` 既有设计补齐：命中"物理/逻辑/增量/全量/实时"等强意图词的任务整体前移，命中唯一时可直接执行，命中多个或只有"备份"这类弱词则交回用户澄清。
  - **功能级致命（此前 100% 失败）**：① 跨主机全实例恢复（PG/金仓）脚本里 `{db}` 被当成 Python 变量，构造命令即 `NameError`；② MySQL/MariaDB 全实例 tar 引用未定义的 `task`（且调用方未传真实风味），GTID 跳过分支必崩；③ PG 系远端 `dumpall` 全实例函数签名缺 `tool_path`，调用方传了该参数 → `TypeError`，函数体又引用它 → `NameError`，两条路都断。
  - **其余**：金仓 JDBC 探测兜底参数名笔误、实时同步轮询缺 `os`/`traceback`、MongoDB 恢复解压缺 `subprocess`、自定义引擎恢复校验把 `shlex.quote` 写成 `shlex_quote`、巡检报告详情缺 `json`。
  - **验证**：静态检查 186 文件 0 隐患；10 个改动模块导入冒烟通过；`test_ai_agent`/`test_ai_alert`/`test_ai_alert_taskdetail`/`test_api_contract`/`test_custom_backup` **145 passed / 8 failed**（8 项与改动前完全同一批旧断言，零回归）；新增意图消歧用例 6/6 通过。
- **两天完整说明见 [readme_20260919.md](readme_20260919.md)**（含 v1.4.11 同期入库的对象存储备份、无 Agent 能力体系、API 契约与离线文档、代码图谱生成器、质量门禁五项新增能力）。

### v1.4.11（2026-09-19）

- **文件备份：单个文件源「备份失败 + 原因空白」（用户反馈，问题 1）**
  - **根因**：源路径填单个文件时，引擎仍按目录处理（`cd` + `tar -C` 目录），且远端打包的 stderr 被 `2>/dev/null` 吞掉，导致失败消息是「源打包失败: 」——冒号后面什么都没有，无法定位。
  - **修复**：新增源路径**真实探测**（一次 SSH 用 `-d/-f/-e` 判定目录/文件/其它/不存在）；单文件源改用 `-C <父目录> <文件名>` 打包并在归档内独立存根；打包失败必带 **rc + 真实 stderr + 实际执行的命令**（stderr 为空时显式说明「命令未输出任何错误信息」并打印命令原文），杜绝空白原因。
  - **顺带修掉三个同类隐患**：① 源路径不存在/不支持时**提前失败**，不再产出「成功但空包」的假备份；② `exclude_patterns`（排除规则）此前界面有、代码从未生效，现已贯通本地/远端四种传输组合；且快照会记录当次规则，规则变更后不再把"规则变了"误判成"文件被删除"而静默漏备；③ 远端打包改为**流式落盘**，不再把整个归档攒进内存（大目录会 `MemoryError`，而 `MemoryError` 的报错文本是空的）。
- **MySQL 全实例备份：`mysqldump: Got errno 28 on write` 且失败文案误导（用户反馈，问题 2）**
  - **磁盘写满不再只看得见一句天书**：逐库 dump 失败时识别 ENOSPC，直接给出**哪个路径满了、各自还剩多少、怎么处置**（清理分区 / 改产物根目录 / 用 `BP_WORK_DIR` 指定更大的临时工作目录）；关键错误行会自动从 mysqldump 的无关警告（如 `column statistics not supported`）中挑出来，不再被噪声盖住。
  - **不再默认用 `/tmp` 当中转**：临时工作目录改到**产物所在分区**的 `.bp_work`（`/tmp` 常是小分区或 tmpfs，大实例一写就满），可用 `BP_WORK_DIR` 环境变量指定。
  - **备份前空间预检（真实生效）**：用数据库自身数据估算本次落盘量，明显不够就**在动手前终止**，不让用户跑几分钟才失败。估算口径为「逻辑统计 vs InnoDB 物理文件大小逐库取大者」——只用 `information_schema` 的 `DATA_LENGTH` 会严重低估（InnoDB 统计是采样值且刷新滞后，实测刚灌 4MB 的表只报 16KB）；物理大小按版本兼容查询（MySQL 8.0+ 用 `innodb_tablespaces`，5.6/5.7 与 MariaDB 用 `innodb_sys_tablespaces`，PG 系用 `pg_database_size()` 本身就是真实占用）。
  - **第二道防线**：逐库 dump 之间检查磁盘余量，若已放不下下一个库就**就地终止并说明进度**（已完成 n/N 个库），而不是把设备写满才炸。
  - **失败文案按原因分流**：未纳管 SSH 时的兜底提示不再一律写「请纳管 SSH 备份机」——磁盘满给出清空间指引、客户端缺失给出工具安装/`tool_path` 指引、连接认证失败给出地址端口账号口令核对指引，其余才提 SSH。
- **新增开发者工具：代码图谱生成器**（`scripts/gen_code_graph.py`，纯标准库、零第三方依赖、只 `ast.parse` 不执行被分析代码）：一次扫描产出 `docs/code_graph.md`（人读）+ `docs/code_graph.json`（机器读），覆盖分层架构与模块依赖、枢纽模块、循环/越层依赖体检、332 条 REST 路由索引（METHOD+URL+handler）、页面→JS→接口链路、元数据库表访问热点、关键业务链路与「想改 X 先看哪些文件」。人工注解（链路/指引）每次生成都做存在性校验，失效即标 ⚠；`--diff` 看结构漂移，`--strict` 可在 CI 中拦截不可信数据（当前 ERROR 0）。用于替代"改一个功能要先通读几万行代码"。
- **可重复执行的回归脚本**：新增 `tests/e2e_v1411_fixes.py`（真实执行、不仿真，10 项断言：单文件源 / 目录源 / 排除规则生效 / 源不存在提前失败且原因非空 / 目标分区写满报真实 Errno 28；MySQL 全实例成功产物校验含中文 / 2MB 工作目录**备份前**终止 / 连接失败原因分流 / 估算失效时第二道防线），实测 **10/10 通过**（免密 MySQL 实例 + tmpfs 真实制造 ENOSPC）。运行：`.venv/bin/python tests/e2e_v1411_fixes.py`（可用 `--mysql-host/--mysql-port/--mysql-pwd` 指定实例）。
- **回归**：文件备份端到端 **13/13 通过**（单文件→远端、目录+排除规则、源不存在、单文件→本地、打包前文件被删五类场景，真实 SSH 环境 192.168.220.137）；全实例端到端 **12/12 通过**（含 2MB tmpfs 上真实制造的 ENOSPC，验证「备份前终止」与写满时的诊断均生效）。全量 pytest 与 `git archive HEAD` 快照同命令基线对比 **170 failed / 292 passed / 1 skipped / 30 errors，与基线完全一致，零回归**（failed 项均为既有用例间共享临时库导致的存量冲突）。
- **完整说明见 [readme_20260919.md](readme_20260919.md)**（同批入库的还有对象存储备份、无 Agent 能力体系、API 契约与代码图谱生成器）。

### v1.4.10（2026-09-17）

- **MySQL 本机（备份平台与数据库同机）全实例备份失败——四个真实缺陷修复（用户反馈，本次重点）**
  - **根因（内存）**：本机全实例备份用 `capture_output=True` 把**整个** mysqldump 产物读进内存后才写盘。平台与数据库同机时内存被数据库占用，Python 抛 `MemoryError`——而 `MemoryError` 的 `str()` **是空字符串**，最终上报成「MySQL 全实例备份失败: 」这种看不到任何原因的消息；用户侧表现就是"备份跑了 5 分多钟，最后突然失败，产物 0 字节"。已改为 **stdout 直连文件流式落盘**，内存占用恒为管道缓冲大小、与产物大小无关。真实 122MB 库实测：平台 RSS 160→182MB（**仅 +22MB**），4.2 秒成功，包内 `.sql` 未压缩 128,006,222 字节。
  - **凭据被本机配置覆盖**：平台机的 `/root/.my.cnf` 优先级高于 `MYSQL_PWD`，导致任务里密码填错也能"备份成功"（实际连的是 my.cnf 里的账号）。全实例的枚举库 / mysqldump / 恢复灌入三处已统一加 `--no-defaults`，密码错误现在会**如实失败**并提示 `ERROR 1045 Access denied`。
  - **失败文案误导**：未纳管 SSH 时旧消息写「请在数据库服务器上纳管 SSH 主机」，而同机场景本机客户端才是正解。现在数据库地址即平台本机（或 SSH 目标即本机）时**直接走本机**，失败只报本机真实原因。
  - **空原因消息兜底**：异常统一用 `str(e) or type(e).__name__`，杜绝「失败: 」后面一片空白；枚举库失败时透出真实的连接/认证 stderr。
- **Oracle 11g 端到端真实测试通过**（192.168.220.168，实例 orcl11g）：逻辑备份 expdp、物理备份 RMAN（294MB 备份片）、恢复（impdp 真实导入，数据状态真实回退）、实时（LogMiner 真实捕获 redo/undo SQL）、恢复校验（RMAN VALIDATE + 真实抽取数据文件 / impdp SQLFILE）五类全通过，报告见 `docs/oracle_11g_e2e_report_20260917.md`。配套修复：11g 不能用 oracledb thin（DPY-3010）改走 JDBC 兜底、SSH 握手抖动重试（"Error reading SSH protocol banner" 不再直接判失败）、`rt_supervisor` 陈旧锁只判心跳不判进程存活导致守护静默不启动。
- **实时保护链路去仿真**：CDC 因"能力不足 / 客户端缺失"（如金仓缺 `sys_receivewal`、达梦缺 dmPython）**不再静默降级为仿真日志流**——仿真恢复点不能作为 RPO/RTO 依据，现默认置 FAILED 并写 error 日志（开关 `RT_ALLOW_SIMULATED_FALLBACK`，默认 false）。仅 `DEMO_MODE=on` / `demo_only` / `rt_mode=sample` 三种**用户显式要求**的演示场景仍允许仿真。
- **对象存储密钥解密**：`storage_targets.secret_key` 落库为密文（`enc:`），此前直连 MinIO/S3 未解密导致 `AccessDenied`；统一在存储后端工厂入口解密，覆盖生命周期、分层复制与手工复制等全部调用方。
- **文档同步**：`feature_manifest` §3.2 / §9、`offline_ops_manual` §5.3、`product_design_spec` 按真实状态更新（含远程通道大实例产物仍会进内存的已知边界）。
- **回归**：`test_backup_restore_all` 18 failed（既有基线，与改动前一致）、`test_rt_journal` 48 passed，零回归。

### v1.4.9（2026-09-16）

- **数据迁移 / 同步真实端到端测试与六个缺陷修复（本次重点）**：对 MySQL 8.0.40 / MySQL 5.7.44 / MariaDB 10.11.19 / PostgreSQL 14.12 四实例实跑 **9 条真实链路**（4 条迁移 + 5 条同步，含单表与全库 `full_db_migrate`）。核验口径为直接查目标库行数、样本值与 `information_schema` 的主键 / 自增 / 列类型 / 默认值，**不采信平台状态**。
  - **自增丢失**：MySQL/MariaDB 源的自增只存在于 `INFORMATION_SCHEMA.EXTRA`（`COLUMN_DEFAULT` 为 NULL），目标表因此丢自增，后续写入主键冲突；改为还原 `AUTO_INCREMENT`，非主键自增列补 `UNIQUE KEY`。
  - **PG → MySQL 整表建表失败**：PG serial 的 `nextval('seq'::regclass)` 被直接拼进 MySQL DDL 报 1064；新增 `_mysql_default()`，序列默认值转自增、函数默认值换算、`'x'::type` 字面量剥除类型转换，无法换算的原生表达式不生成 DEFAULT（宁可缺默认值也不让整表建不起来）。
  - **时间默认值精度**：`DATETIME(6) DEFAULT CURRENT_TIMESTAMP` 报 1067，改为与列精度一致的 `CURRENT_TIMESTAMP(6)`。
  - **跨源类型名归一化缺失**：PG 的 `CHARACTER VARYING` 取首词后匹配不到分支而落到兜底 `TEXT`，`varchar(50)/(100)` 长度丢失；新增异源类型名归一化表。
  - **"假成功"掩盖失败**：全库迁移 success 只看行级错误数，整表建表失败时"读 0 写 0 错误 0"被判成功，直到校验阶段才抛难定位的错误；改为有失败表一律判失败。
  - **元数据契约**：`ColumnMeta` 的 `is_primary` / `auto_increment` 由插件间动态属性正规化为字段。
  - **目标侧保真实测**：`id int(11) auto_increment`（真实插入得 id=6）、`varchar(50)`、`decimal(12,3) DEFAULT 0.000`、`datetime DEFAULT CURRENT_TIMESTAMP`、主键 `PRIMARY KEY (id)` 全部保留。
- **首页运营态势补齐四项此前标注未实现的指标**：恢复演练通过率（只计已出结果的演练，pending/running 不计入分母）、副本异地复制成功率、失败率环比、保护对象覆盖率；`_insights()` 的保护态势与近 7 天趋势改为 **SQL 全表聚合**，避免"最近 500 条采样"把多数任务误判为从未备份。
- **备份恢复链路稳健性**：跨主机恢复支持 zstd/gzip 产物解压（目标机无解压工具时在平台侧解压，遵守数据库服务器零安装）、远端 mysql 绝对路径注入、剥离 `mysqldump --databases` 自带 `CREATE DATABASE/USE`（否则数据被写回源库名）、导入前清 GTID 避免 1840、远端真实错误回传；新增 `_run_with_stdin` 保证 BLOB 与二进制列无损；Oracle 连接串口令加引号与 expdp `DIRECTORY` 必填（ORA-39145）；SQL Server `RESTORE ... WITH MOVE` 位置修正；分层复制支持目录型产物打包。
- **新增 `scripts/api_audit.py`**：自动发现全部 `/api` 路由并逐个真实请求，抓 5xx、非 JSON 响应、200 但业务失败、写接口错误处理不当与鉴权缺失五类缺陷（与压测脚本互补：压测看并发吞吐，本脚本看正确性）。
- **文档**：新增设计思想、产品设计说明书（含 G1–G13 产品化差距清单）与 Acronis / Yak Ops 两份对标分析。
- **回归**：`test_link_sources_contract` + `test_rt_journal` **59 passed**；全量 pytest 用 `git archive HEAD` 快照做同命令基线对比，**171 failed / 291 passed → 170 failed / 292 passed**，零回归且净修复 1 条（`/rt-timeline` 用例过期，已改为断言跳转目标的承载页）。

> 完整测试矩阵、缺陷根因与核验记录见 [readme_20260916.md](readme_20260916.md) 第八章。

### v1.4.8（2026-09-16）

- **Neo4j 图数据库备份引擎（第 10 个数据库引擎）**：三通道真实备份——官方离线 `dump`（社区版/企业版，Docker 场景按官方做法 `docker stop` → 临时容器 dump → `docker start`，避免抢 `database_lock`）、企业版在线 `backup`（不停机、`--type=DIFF` 永久增量，只拉回新增产物）、APOC 在线导出（Cypher / JSON / CSV，跨版本迁移与审计）；恢复通道一一对应（`load` / `restore` / `apoc.import.*`）。连接探测 cypher-shell 优先、回退官方 HTTP 事务接口；能力按**真实情况**声明（无物理备份通道、不支持全实例与同步）。
- **虚拟机备份（整机，/vm）**：新增 `core/vm` 子系统——PVE / KVM(libvirt) / ESXi / Hyper-V 四种平台纳管 → 保护策略 → 恢复点（PITR，全量/增量/合成全量 + change_token 永久增量）→ 原地还原 / 克隆新 VM（隔离网络、改 MAC、TTL 自动销毁）/ **SureBackup 式自动恢复验证**（隔离网络真实拉起 VM 做心跳·端口·HTTP 健康检查，不通过不罢休）→ RPO 偏离报告。复用平台既有调度、记录、保留、存储分层与告警，虚拟化平台侧零安装零 agent。
  > **注意：本模块尚未在真实虚拟化环境做端到端验证（无可用 PVE/ESXi/Hyper-V/libvirt 环境），暂不建议生产使用，待测试优化。**
- **页面结构收敛**：「实时备份（PITR 准 CDP）」与「CDC 变更捕获与回放」合并为 `/realtime` 一个入口（页内标签页）；「数据库类型」并入「数据库备份」页标签页；`/cdc`、`/rt-timeline`、`/db-adapters` 旧入口保留 302 重定向并带深链参数。
- **修复**：数据库类型面板按钮全部失效（面板内联脚本早于 `bkp-core.js` 执行，`BKP` 未定义导致初始化中断）；CDC 面板 `BKP.api` 调用签名误用与按钮选择器串绑；Neo4j 无 JDBC 通道被误判为连接故障；静态资源版本号更新。

> 本次更新的完整说明（含三通道对比、VM 数据模型与支持矩阵、缺陷排查记录与后续计划）见 [readme_20260916.md](readme_20260916.md)。

### v1.4.7（2026-09-15）

- **大库备份提速 + 断点续传（本次重点）**：10GB 级库备份从"必然超时重跑"变为可稳定完成。
  - **根因定位**：SSH 数据通道旧实现 `out += sess.recv(65536)`——bytes 不可变，每轮追加都要把已有全部数据 memcpy 一遍（O(n²)）。累积到 1.4GB 时单次追加要复制 1.4GB，实测吞吐掉到约 600KB/s（与现场现象吻合）；叠加"每轮最多 64KB + 无数据固定 sleep 50ms"（等效限速约 1.3MB/s）。
  - **修复**：统一重写为 `_ssh_exec_stream`——流式接收、256KB 块、仅在无数据时 sleep 2ms、SSH 窗口 2MB→16MB / 单包 32KB→128KB；新增 `_ssh_exec_pipe_to_file()` 边收边写盘（内存恒定，不再把 10GB 产物攒在内存里）。
  - **服务端零安装提速**：限速改由平台侧实现（`rate_kbps`），不再依赖数据库服务器安装 `pv`。
  - **实测**：300MB 数据流旧实现 17 分钟仍未传完，新实现秒级完成（详见 `docs/backup_perf_20260915.md`）。
- **断点续传（大库必备）**：MySQL/MariaDB 逻辑备份改为两段式——远端后台 dump 落盘（`setsid nohup`，SSH 断开也继续）+ 平台按 offset 增量拉取（边导出边传）。中断 / 超时 / 平台重启后**从已传字节继续，不重跑 dump**；断点元数据默认保留 12h，成功后自动清理远端暂存与本地半成品。实测：`kill -9` 中断在 208KB，重跑自动续传完成，产物解压 270MB / 52 万行完整一致。
- **任务级高级选项（超时 / 重试 / 续传）**：新增 5 个字段（执行超时、空闲超时、失败重试间隔、失败重试次数、断点续传开关），存 `extra_options`。
  - **执行超时默认 0 = 不限**（旧版写死 3600s，10GB 库必然被砍掉后从头重跑）。
  - **空闲超时默认 1800s**：连续这么久没有任何数据才判失败，区分"大表读得慢"与"真卡死"。
  - **失败重试间隔默认 60s**、重试次数默认 3（旧版 5s 指数退避，且重试＝从头重跑）。
  - 全局默认可用环境变量覆盖：`BACKUP_CMD_TIMEOUT` / `BACKUP_IDLE_TIMEOUT` / `BACKUP_RETRY_INTERVAL` / `BACKUP_RESUME_ENABLED` / `BACKUP_RESUME_TTL` / `BACKUP_REMOTE_STAGE`。
- **断点续传扩展到全部产物形态（固定产物）**：物理备份 tar / Oracle Data Pump `.dmp` / SQL Server `.bak` / 达梦备份集 / 自定义脚本产物，此前因"不产生 rc 标记文件"而未接入，现按同一模式打通：
  - **结束判定差异**：逻辑备份有 rc 标记；固定产物改用**「远端文件连续 N 秒不再增长」**判定写盘结束（`BACKUP_STABLE_SECS`）。
  - **不重跑昂贵备份**：远端路径改为**确定性**（按任务固定）+ `.bkdone` 完成标记 → 拉回中断后重试**跳过备份执行**（xtrabackup / pg_basebackup / RMAN 可能跑数小时）直接从断点续传；成功拉回后仍照原规则清理，数据库服务器**零残留**。
  - 覆盖：MySQL/MariaDB 物理（xtrabackup / mariabackup）、PG 系与金仓 `*_basebackup`、Oracle RMAN 备份片与 expdp、SQL Server FULL/DIFF/LOG、达梦联机/dmrman、自定义脚本产物（详见 `readme_20260915.md` §3.4）。
- **可观测性**：SSH 传输与 SFTP 拉取每 30s 输出进度（已传大小 + 速率 MB/s）到操作日志，大库备份不再"黑屏干等"。
- **修复**：`_sftp_pull_incremental` 中 `has_rc` / `stable_secs` 两个变量未定义（上一轮中断留下的半成品，必然 `NameError`），随本次续传改造一并补齐并参数化。
- **修复**：`remote_has_tool` 另起 SSH 连接失败时会被误判成"没装 zstd"→ 改用当前连接 `command -v zstd` 探测，避免大库备份白白丢掉压缩。

> 本次更新的完整说明（含 O(n²) 根因分析、逐项实测数据、任务级参数表与已知边界）见 [readme_20260915.md](readme_20260915.md)。

### v1.4.6（2026-09-15）

- **全面压力测试与高并发加固（本次重点）**：新增 `scripts/stress_test_full.py` 一键全链路压测（6 阶段：批量建任务 → 批量触发备份 → 落库对账 → 全接口压测 → 数据返回校验 → 混合读写 + 写接口并发，结果可落盘 JSON）。**1200 个备份任务 / 并发 150 实测：全程 0 个 5xx、0 条平台 ERROR 日志、无 SQLite "database is locked"、无请求超时**（详见 `docs/stress_test_report_20260915.md`）。
  - 压测数据：批量建任务 QPS 159.5 / P95 1298ms；批量触发备份 1200 次全部 success；全接口（自动发现 112 个 GET）× 5 轮 QPS 81.8、P95 1271ms；混合读写 30s、2637 次请求 QPS 84.2；写接口并发 QPS 208.4；数据返回校验 18/18 通过；平台 RSS 峰值 360MB、线程峰值 172。
  - 落库对账（B2 阶段）：每条触发都留记录，success 记录 `size_bytes > 0`、`sha256` 齐全、产物文件真实存在，杜绝"假成功"。
- **修复压测暴露的 5 个并发缺陷**：
  1. **调度器 reload 并发竞态（最严重）**：`reload_scheduler()` 遍历 job 快照后逐个 `remove_job()`，并发建/删任务必抛 `JobLookupError` → **HTTP 500**（60 并发即触发 11 次）。改为「序号 + 执行锁」**请求合并**（并发 N 次 reload 只真正重建 1~2 次）并对 `remove_job` 容错。
  2. **内置 Web 服务未开多线程**：`app.run` 缺 `threaded`，批量提交排队超时。新增 `config.WEB_THREADED`（默认开，环境变量 `WEB_THREADED=0` 可关）。
  3. **SQLite 无 busy 超时**：并发写元库风险。`get_conn()` 增加 `timeout=30` + `PRAGMA busy_timeout=30000` + `synchronous=NORMAL`。
  4. `GET /api/tasks/{id}/list-tables` 目标库连不上时返回 500 → 改为 **400 + error 文案**（属配置/环境问题，不再误报为服务端异常）。
  5. `GET /api/records` 不支持分页（固定 500 条）→ 支持 `limit`（上限 500 保护）。
- **性能调优结论**：备份执行吞吐约 13~15 个/秒，瓶颈是单次备份固定开销（元库写锁 / 日志 / 产物复制与 sha256），**非并发度**（`max_concurrent_backups` 2→16 后 QPS 仍 14.5，无提升），生产建议 `gunicorn -w 4 --threads 8` 承载。
- **全类型自定义备份/恢复脚本 + 行级 CDC**（详见 `docs/custom_backup_guide.md`）：任务 `backup_mode=custom` 可自定义脚本（经 SSH/SFTP 在数据库服务器执行，产物拉回并计算真实 size/sha256），统一入口 `base.py run_backup()/run_restore()`；新增行级变更数据捕获（`core/cdc/rowlevel.py`、`api/cdc.py`）与对应页面/单测 `tests/test_custom_backup.py`。

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
- **MariaDB 打通**：连通性探测注册 `mariadb`（协议兼容复用 MySQL 实现，此前 `src_db_type=mariadb` 预检查直接失败「未知数据库类型」）；迁移计划与数据同步（含全库迁移 `full_db_migrate`）双向真机实测通过，中文与二进制数据逐值一致；`db_adapters` 表迁移（存量库幂等补齐）；实时同步（Binlog CDC）已支持 MySQL / MariaDB 源。
- **遗留问题优化（5 项闭环，真实环境验证）**：
  - **MariaDB 源实时同步放开**：Binlog CDC 源库判定由「仅 `mysql`」扩为 `mysql` / `mariadb`（同协议族：`SHOW MASTER STATUS`、ROW 事件、CRC32 校验、TableMap 事件兼容）；MariaDB 10.11 → MySQL 8.0 端到端实测通过（快照 3 行 + 源插入 1 行 + 目标内容一致 + 任务 `success`）。顺带修复**停止响应**：原先 `for event in stream` 阻塞读要等源库产生下一个事件才回到停止检查点（实测 40s+），现停止信号置位后由看门狗线程 `shutdown(SHUT_RDWR)` 打断底层 socket（pymysql 的 `close()` 不 `shutdown`，Linux 下无法唤醒已阻塞的 `recv`）——MariaDB 源停止响应 **0.0s**
  - **预检查先自动建库**：`target_database` 检查项前置到连通性检查之前（MySQL / MariaDB 目标库不存在时自动建库），不再以 `1049 Unknown database` 提前判失败（与「数据迁移」页行为对齐）；非 MySQL 目标给出明确的人工建库诊断
  - **同步任务状态显示修正**：`status` / `last_status` 双写 + 列表「终态优先」归一 + 运行态每 3s 自动刷新，不再长期显示 `never`
  - **原 fail 组合改为可落地方案**：带时区时间戳 → `DATETIME(6)`（按 UTC 归一、保留微秒、规避 `TIMESTAMP` 的 2038 上限）、`INTERVAL` → `VARCHAR(64)`、`TIME` → `VARCHAR2(16)`、`ENUM` / `SET` → `VARCHAR2(最长枚举值)`；MySQL / Oracle 建表端同步跟随，类型矩阵无解项由 **32 → 7**（其余为自定义类型并提示人工确认）
  - **空间类型跨库提示补全**：统一为「需人工确认」的 warn，并在映射说明中给出目标库扩展与转换要求（PG / 金仓 PostGIS、达梦 DMGEO、Oracle Spatial）及 SRID、几何结构确认项
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
