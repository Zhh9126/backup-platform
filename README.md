# 数据备份管理平台

跨平台的**数据库 + 文件**集中备份管理平台，支持 **Oracle、MySQL、PostgreSQL、Kingbase（人大金仓）、DM（达梦）、Redis、MongoDB** 等多种数据库，以及**文件/目录（本地与远程 SSH，无 Agent）**的集中备份、定时调度、保留策略、三级对象存储、数据同步、巡检与健康检查、通知告警与一键恢复。

基于 **Python + Flask** 构建，元数据使用 SQLite（零外部依赖、开箱即用）；备份核心通过调用各数据库官方客户端工具或 SSH/SFTP 实现，能以最小依赖在生产环境稳定运行。

> **当前版本**：`v1.0.0`（社区版）｜ Docker 镜像：`ghcr.io/zhh9126/backup-platform:latest`

> **社区版说明（Community Edition）**
> 本项目当前发布为**社区版**：免费供个人学习、内部部署与中小规模生产环境使用。
> 社区版包含全部备份 / 恢复 / 实时管控 / 数据同步 / 灾备演练等核心能力；企业级增强（大规模集群纳管、多租户、商业支持与定制开发）请联系作者洽谈。
>
> **联系方式**：📧 `1547358466@qq.com`（问题反馈、功能建议、合作洽谈均可来信）

---

## 功能特性

### 备份能力
- **10 种引擎**：Oracle / MySQL / MariaDB / PostgreSQL / Kingbase / DM / SQL Server / Redis / MongoDB / 文件（本地 + 远程 SSH 无 Agent），统一引擎接口（`connect` / `backup` / `restore` / `verify_record` / `synthesize_full`）。
- **10 个备份 Skills**（项目 `skills/` 目录，覆盖各引擎最佳实践）：
  - `mysql-backup`：mysqldump + xtrabackup + binlog PITR
  - `mariadb-backup`：继承 MySQL，mariabackup
  - `postgresql-backup`：pg_dump + pg_basebackup + WAL
  - `oracle-backup`：expdp/exp + RMAN + archivelog PITR
  - `kingbase-backup`：sys_dump + sys_basebackup
  - `dameng-backup`：dexp + dmrman
  - `sqlserver-backup`：BACKUP DATABASE/LOG 官方 T-SQL + RESTORE WITH MOVE
  - `redis-backup`：RDB 快照
  - `mongodb-backup`：mongodump
  - `file-backup`：tar.gz 全量 + 快照增量 + 准 CDP + 恢复链
- **文件/目录备份**：本地与远程（SSH，无需安装 Agent）源，全量 + 增量（按 size+mtime 比对），`tar.gz` 归档；源主机与目标主机独立。增量基于**源快照**（同一路径的多任务共享基准），归档仅含变化文件，与全量一致直接保存到目标目录根下，不会删除目标目录里的其他文件。Windows 下采用**原子写入**（临时文件 + replace），避免防病毒/句柄锁导致空包
- **多种备份策略**：全量（full）、增量（incremental）、差异（differential）、快照（snapshot）、合成全量（synthesized，由永久增量链自动合成）、**组合备份（mixed：全量+增量）**：任务同时配置全量调度与增量调度，调度器分别注册 `task_<id>_full` 和 `task_<id>_incremental` 两个作业；触发执行时按 `run_task_now(task_id, backup_type)` 覆盖备份类型，任务列表直观显示「组合 全…/增…」
- **自定义备份/恢复脚本（全数据库类型通用）**：备份方式选「自定义脚本」即可粘贴 bash 脚本，在数据库服务器（SSH 主机）上执行。平台注入环境变量 `PLATFORM_DB_HOST / PLATFORM_DB_PORT / PLATFORM_DB_USER / PLATFORM_DB_PASSWORD / PLATFORM_DB_NAME / PLATFORM_BACKUP_TYPE / PLATFORM_BACKUP_DIR`（恢复时另有 `PLATFORM_BACKUP_FILE / PLATFORM_RESTORE_DB`）；脚本退出码 0 = 成功，**产物必须写入 `$PLATFORM_BACKUP_DIR`**，平台自动 SFTP 拉回、计算真实大小与 sha256、生成备份记录（支持定时调度与三级存储复制）；可选「恢复脚本」配合使用（备份文件自动推送到目标主机并注入 `PLATFORM_BACKUP_FILE`）。适用于任意自定义备份工具/方案（xtrabackup 定制参数、快照、导出工具、云工具等）

### 管理与可视化
- **Web 可视化管理**：仪表盘、数据库备份、文件备份、数据同步、存储管理、保护策略、备份/恢复记录、数据恢复管理、灾备管理、巡检、智能告警、系统设置等
- **定时调度**：基于 APScheduler，支持 `cron` 表达式与固定间隔两种调度方式
- **保留策略**：按“保留天数”和“保留份数”双重清理，避免备份无限膨胀
- **真实备份承诺（无占位/无仿真）**：所有备份与恢复均为真实执行，不做任何“占位/仿真”兜底；客户端缺失或连接失败时任务如实失败并给出明确原因
- **远端工具路径动态发现**：备份/恢复命令一律通过 `resolve_remote_tool` 动态解析（数据库服务用户 profile → 登录 shell → 常见安装目录 glob 枚举 + find），**绝不写死安装路径**，数据库工具未配置环境变量也能自动找到并加载环境执行

### 三级存储体系（异地容灾）
- **L1 热数据 = MinIO**（S3 兼容对象存储，备份第一落点）
- **L2 冷数据 = S3**（AWS S3 或其兼容服务，备份完成后实时/异步推送）
- **L3 源端本地路径导出**（服务端本地文件系统导出，可离线转移）
- 备份成功后由 `tier_replication` 自动并行复制到各层级；`backup_records.storage_tier` 记录每条备份实际到达的层级（如 `minio+s3+local`）
- 可配置复制策略（`push_l1_minio` / `push_l2_s3` / `push_l3_local` / 时机 / 重试）
- **协议识别优化**：存储目标地址支持 `http(s)://` 前缀自动识别（http 直连 / https 加密），并兼容自建/企业 S3 网关的 `insecure` 证书跳过配置

### 数据同步（DataX/LinkUp 风格 Reader/Writer）
- **Reader → 统一 Java 类型 → Writer**：参考 DataX 架构，抽象 `SourceReader` / `SinkWriter`，源端读出的数据先转为平台统一类型（STRING/LONG/DOUBLE/DECIMAL/BOOLEAN/DATE/TIME/DATETIME/BYTES），再由目标端写回。
- **表级同步**：支持选择源表/目标表，MySQL/MariaDB、PostgreSQL 已实现 Reader/Writer，可扩展更多插件。
- **字段映射可视化**：页面提供左右字段列表，支持「同名映射」「同行映射」「清空映射」「手动点击连线建立映射」，并高亮已映射字段与连线。
- **写入模式**：`append`（追加）、`overwrite`（覆盖）、`upsert`（更新插入，MySQL ON DUPLICATE KEY / PG ON CONFLICT）、`create_if_not_exists`（表不存在则自动建表）。
- **增量同步**：可指定增量列与起始值，自动记录断点并更新任务状态。
- **实时同步（Binlog CDC）**：离线任务由 APScheduler 调度；`realtime` 模式由 MySQL/PG 插件内置 Binlog/逻辑复制监听（**全量快照 + 增量 DML**），后台线程（`core/sync/realtime_runners.py`）统一管理启停与运行状态，平台提供配置生成接口与任务监控。
- **全库迁移模式**：启用后可一次性将源库所有表同步到目标库（参考 pg2mysql 全库迁移能力）。
- **Schema 兼容性校验**（pg2mysql Validator）：执行前先比对源/目标的列类型与长度兼容性，检测超出目标列长度的数据行并列出 ID，不兼容则拒绝执行。
- **迁移后数据校验**（pg2mysql Verifier）：同步完成后跨库逐行比对源/目标数据是否一致，报告缺失行数与 ID。
- **约束管理**：写入前自动禁用外键/约束检查（MySQL `SET FOREIGN_KEY_CHECKS=0`，PG `session_replication_role=replica`），写入后恢复，提升大批量同步性能。
- 失败触发通知，每次运行生成同步记录。

### 数据恢复与灾备
- **数据恢复管理**：一键将历史备份恢复到目标实例（数据库）或目标目录（文件）；文件增量恢复时**自动构建恢复链**（先回最近全量，再按时间顺序应用增量），数据库部署（将数据库部署到目标实例）
- **数据对比（恢复数据 vs 生产库）**：恢复完成后可对恢复库与原生产库做数据级一致性校验——表清单比对 → 行数比对 → 全表校验和（可选，MySQL=CRC32 / PostgreSQL=hashtext / Oracle=ORA_HASH，跨版本通用语法）→ 抽样行逐列比对（默认 100 行，可配）。支持 MySQL/MariaDB、PostgreSQL/Kingbase、Oracle；连接优先 DB-API 直连（pymysql/psycopg2/oracledb），缺失时自动回退 JDBC 桥接；支持手动 / cron / 间隔调度，报告含逐表差异明细（含不一致行的源/目标值对照）。页面：「数据恢复管理 → 数据对比」
- **数据库部署（Deploy）**：将数据库（MySQL / MongoDB 等）自动部署到目标 Linux 主机，上传安装包 → 生成安装脚本 → 执行安装 → 实时日志轮询；支持单机/副本集、认证开关、keyFile 等。MySQL 部署统一用 `mysqld_safe` 启动、显式错开 mysqlx 端口；MongoDB 部署支持无认证/副本集+认证（keyFile + `rs.initiate`）；部署记录含连接测试、日志与状态。页面：「数据库部署」。
- **灾备管理**：数据迁移（原「迁移保护」）、数据同步、容灾链路、克隆服务（创建可独立使用的克隆实例）

### 巡检与健康检查
- 对任务做连通性 + 调度 + 上次状态体检，判定 `pass` / `warn` / `fail`
- **任一任务 `fail` 立即通过通知模块告警**
- 巡检记录可查看明细并导出

### 通知告警
- 渠道：Webhook / 钉钉 / 企业微信 / 飞书 / 邮件
- 成功、失败可分别开关；**通知配置支持 Web UI（系统设置页）**，密码不回显（留空表示不改）

### 主机与连接纳管
- **SSH 主机纳管**（`ssh_hosts` 表）：用于文件备份的远程源/目标，密码 XOR + base64 加密，支持连接测试
- **任务级 SSH 凭据（免纳管）**：任务表单「高级」页可勾选「数据库服务器 SSH 执行通道」，直接填 SSH 账号（默认与数据库同机），密码加密存入任务——**无需预先纳管主机即可远程备份/恢复**；已纳管主机仍按 IP 自动匹配

### 全实例备份（PG / Kingbase / MySQL / MariaDB，库名留空或勾选全部库）
- **统一「逐库文件 + 打包」语义**：枚举库 → 每库一个文件（PG 系 `-Fc` 单库快照 / MySQL 系 `--databases` SQL）→ 打包单个 `.tar.gz`（内含 `manifest.json` 库清单）；PG 系额外附 `dumpall -g` 全局对象
- **默认仅备份业务库（排除系统库）**：MySQL=`mysql/sys/information_schema/performance_schema`（后两者为虚拟库、mysqldump 本不支持导出）、PostgreSQL=`postgres/template0/template1`、Kingbase=`template0/1/2/security/test`；任务表单勾选「全实例时包含系统库」或 `extra_options` 传 `{"include_system_dbs": true}` 可包含
- 恢复端自动识别 tar 产物：恢复全局对象（PG 系）→ 缺失的库自动 CREATE → 逐库恢复（本机与 SSH 远端通道均支持）
- PG 系可选整实例 SQL 模式：`extra_options` 传 `{"all_db_mode": "dumpall"}` 时直接 `dumpall` 输出纯 SQL（大库恢复较慢）

### SQL Server 备份/恢复（Linux + Windows）
- **严格遵循微软官方 T-SQL**：完整备份 `BACKUP DATABASE [db] TO DISK=N'...' WITH COMPRESSION, CHECKSUM, INIT`；差异备份 `WITH DIFFERENTIAL`；日志备份 `BACKUP LOG`（需 FULL 恢复模式）；还原 `RESTORE FILELISTONLY` + `RESTORE DATABASE ... WITH MOVE, REPLACE, RECOVERY`；校验 `RESTORE VERIFYONLY WITH CHECKSUM`
- **零依赖**：sqlcmd 在数据库服务器上（Linux `/opt/mssql-tools/bin` 自动发现），平台经 SSH 远程执行并拉回 `.bak/.diff/.trn`；密码走 sqlcmd 官方环境变量 `SQLCMDPASSWORD`，不进 argv
- **Windows 目标**：`ssh_hosts` 的 os_type 设为 `windows` 即走 cmd 语法（备份目录默认 `C:\MSSQL\backup`，可用 `extra_options.backup_dir` 覆盖）
- 备份目录解析：`extra_options.backup_dir` > 实例默认备份目录（`SERVERPROPERTY('InstanceDefaultBackupPath')`）> 平台默认

### 工具路径手动兜底（可选）
- 平台自动发现数据库服务器上的备份工具（运行进程 / 常见目录 / 包管理器 / `/proc` 等）；
  极端场景探测不到时，任务表单「高级」页可填写**备份命令所在目录**（`extra_options.tool_path`，
  冒号/分号分隔），作为 PATH 前缀注入**远程 SSH 命令与本机回退执行**，并最高优先参与工具探测

### 数据库直连能力（原生驱动，无需 Java）
- **原生直连**（`core/native_conn.py`）：通过纯 Python 驱动直连数据库——连接测试、拉取库列表、数据对比，**不依赖 SSH、本机客户端与 Java/JVM**，离线环境开箱即用。入口：任务表单与「备份插件」页，接口：`/api/jdbc/*`。
- **驱动与覆盖**：MySQL/MariaDB（pymysql，纯 Python）、PostgreSQL（psycopg2）、Kingbase（协议兼容 PG，复用 psycopg2）、Oracle（oracledb 瘦客户端，纯 Python，免装 Instant Client，12.1+；11g 请走 JDBC 兜底）、DM 达梦（dmPython，随达梦客户端提供）。
- **JDBC 可选兜底**（`core/jdbc.py`）：仅当原生驱动缺失或服务端不支持瘦模式直连（如 Oracle 11g）时启用，需本机 JDK/JRE 与 `drivers/` 下驱动 jar（均可选，不装不影响直连功能）。
- **「原有连接方式优先」**：直连仅作为连接测试 / 拉库列表的兜底通道与显式入口，备份执行仍走原有（SSH / 本机客户端）方式。
- **JVM 全局单例**（仅 JDBC 兜底时）：classpath 在首次启动时一次性加载 `drivers/` 全部 jar；自动探测 JDK（`JAVA_HOME` / 常见安装路径 / `.jdks` / PATH 推导）。

### 一键恢复
- 选择历史备份记录恢复到目标实例（数据库）或目标目录（文件）
- **表级并行导入（MySQL 逻辑备份）**：恢复时自动解压并按表边界（`DROP/CREATE TABLE`）拆分 dump，N 路并发导入（`RESTORE_PARALLEL`，默认 4，环境变量可调），大库恢复显著提速；任一段失败会汇总失败段信息，无法拆分时自动回退单线程
- **物理恢复并行化**：XtraBackup `--prepare` / 合成全量自动附带 `--parallel=<RESTORE_PARALLEL>`，加速 redo 应用

### 恢复校验（Restore Verify）
- **验证“备份是否真的可恢复”**：按策略关联备份任务，定期或手动对最近一次成功备份做可恢复性校验，生成校验报告。
- **Oracle 深度校验（真实恢复验证）**：逻辑备份推回服务端执行 `impdp SQLFILE` 真实解析 dump 全部 DDL（不落数据）；物理备份执行 RMAN `RESTORE DATABASE VALIDATE` 并**真实从备份片抽取数据文件**到暂存目录作恢复证据，完成后清理。
- 策略支持 `manual` / `cron` / `interval` 三种调度，可配置恢复池（recovery_pool）与克隆保留时长（clone_retention_min）。
- 报告含状态、耗时、消息、是否已清理；仪表盘提供成功率 KPI。接口：`/api/restore-verify-*`、`/api/restore-test-reports`。

### 副本管理与底层备份优化（CDM 能力）
- **永久增量 + 自动合成全量**：以 `chain_id` 串联增量链，当增量份数达到阈值（默认 ≥2）由调度器自动合成一份新全量（`synthesize_full`），并标记被合并的增量为 `merged`，实现副本闭环回收，减少恢复链长度。
- **重删统计**：合成全量时统计 `dedup_saved_bytes`（被增量叠加后节省的体量），在备份集与副本视图中展示。
- **副本层级**：备份集支持 `full` / `incremental` / `synthesized` 类型，支持按链追溯与按需回收。
- 自动合成全量默认每周日 03:00 触发，也可在「存储管理 / 合成全量」页手动触发。接口：`/api/synthesize`。

### 全局重删与存储池加密（白皮书 §2.4 / §2.6）
- **全局重删（Content-Defined / 内容寻址）**：备份落盘后统一做后处理，按内容 sha256 建全局索引（`dedup_index`），相同块只保留一份，记录引用计数与累计节省空间；仪表盘提供「全局重删比」与「累计节省空间」KPI 卡片。接口：`/api/dedup/stats`、`/api/dedup/scan`。
- **存储池加密（AES-256-GCM，信封式）**：备份落盘后可选加密，每文件随机 nonce、文件头含 magic + salt + nonce + tag；密钥来源优先级为 `环境变量 BACKUP_POOL_KEY` > `系统设置中托管的密钥（system_config）` > `config.py 默认值`。
- **密钥托管 / 接入 KMS**：在「系统设置 → 存储池加密密钥（KMS）」卡片中，可选「本地密钥库」（平台托管主密钥，存于数据库、页面不回显明文）或「外部 KMS」（AWS / 阿里云 / 腾讯云 / 自托管 / HashiCorp Vault，运行时从 KMS 拉取主密钥，不可达时失败安全回退到本地回退密钥）。保存后自动跑加密自检（AES-256-GCM 加密→解密闭环），并可「测试 KMS 连通性」。接口：`GET/POST /api/pool-crypto`、`POST /api/pool-crypto/test`。
- **任务级开关**：数据库备份与文件备份的任务表单均提供「存储池加密」开关（`extra_options.encrypt_pool`），开启后该任务落盘产物为密文；缺密钥时按失败安全策略明文跳过加密（不阻断备份）。
- 注：全局重删与存储池加密在文件引擎落盘后的统一 `_post_process`（先加密、后重删）阶段触发；数据库备份的加密由引擎层调用同一 `crypto_pool`。

### 备份插件系统（Backup Plugins）
- **服务端插件市场**：以 manifest 驱动的插件目录，集中管理物理备份所需客户端（如 Percona XtraBackup、MariaDB Backup、pgBackRest、MongoDB Database Tools、Redis Tools、Percona Toolkit）。
- **Linux Only / 离线下载优先**：部署目标为 Linux，安装策略优先级 `fallback_download > package_manager > manual_only`；缺客户端时在调度前 `preflight` 阶段如实失败并提示前往插件页安装（不做仿真兜底）。
- 支持一键安装本机所需（`POST /api/plugins/batch-install`）、安装进度轮询、卸载（仅清理本平台离线安装目录）。页面：「备份插件」。
- **压缩管线增强（zstd）**：逻辑备份压缩自动探测 zstd 版本，≥1.4 时启用 `-T0` 多线程并行 + `--long=27` 长距离匹配（约 128MB 窗口），大备份集压缩率与吞吐双提升；低版本自动回退标准参数。

### 备份质量监控
- 仪表盘 KPI 监控**超长备份**与**超频备份**：超长判定支持 `fixed`（固定分钟）、`speed`（数据量/期望速度，默认 500 GB/h，浮动容忍默认 20%）、`both`（任一即超长）三种规则；超频判定为默认 5 分钟内同任务 ≥3 次。
- 阈值（`backup_quality_thresholds`）可在仪表盘 / 系统设置通过 Web UI 配置。
- 备份时长以 `time.monotonic()` 亚秒级精度统计，避免秒级舍入误差。接口：`/api/settings/backup-quality-thresholds`、`/api/records/overrun-stats`。详见 `docs/backup_quality_monitoring.md`。

### 实时管控与 CDP / 时间点恢复（RT & CDC）
- **实时备份（RT Backup）**：基于 watcher（watchdog / polling）的准实时文件与数据库变更捕获，配合 journal 记录变更流水；支持 PITR（时间点恢复）与快照。
- **CDC（变更数据捕获）**：针对 Oracle（LogMiner）、达梦（LogMinr）、PostgreSQL（WAL）、MySQL（binlog）的日志解析，支撑准 CDP 级恢复。
- **位点落盘增强**：捕获位点（`rt_capture_state`）除每 tick 周期落盘外，健康查询（health）同步刷新，页面看到的即最新位点；封存粒度下限降至 5s（`rt_interval_sec` 可配），RPO 恢复点更密集。
- **RPO 秒级监控告警**：恢复点新鲜度（`rpo_actual_sec`）超过任务目标（`rpo_target_sec`）时自动写入系统日志告警（source=`rt.monitor:<task_id>`，限频 `RT_RPO_ALERT_MIN_SEC` 默认 300s），恢复后自动复位。
- 引擎位于 `core/rt_backup/` 与 `core/cdc/`；实时管控任务可在「容灾链路」中被引用并做日志间隙填补、备端一致性校验——引用 `rt_task` 源的链路自动接入**真实 binlog 位点**（源库 `SHOW MASTER STATUS` vs 已捕获位点），缺口与一致性按真实滞后判定，无真实位点时回退仿真。页面：「实时管控时间线」。

### 演练 / 容灾 / 克隆 / 迁移 / ITSM
- **恢复演练（Drills）**：对备份做恢复演练并生成趋势与基线，支持季度演练排程，验证 RTO/RPO。页面：「恢复演练」。
- **数据迁移 / 容灾链路 / 克隆服务**：在「灾备管理」分组下提供数据迁移（原迁移保护）、容灾链路编排、可独立使用的克隆实例；「数据同步」也已归入「灾备管理」分组。
- **ITSM**：工单/事件对接，备份失败自动建单。
- **数据价值挖掘**：对备份数据进行价值分析与可视化。页面：「数据价值挖掘」。

### AI 能力
- **AI 智能体（Agent）**：对话式运维助手（`core/ai_agent/`），基于 ReAct 循环（最多 3 轮工具调用），可调用 7 个工具：
  - `list_tasks`（列出备份任务）、`list_recent_records`（最近备份记录）、`get_storage_usage`（存储用量）、`list_alert_predictions`（AI 预测告警）、`get_inspection_report`（巡检报告）、`run_backup_task`（执行备份，需确认）、`run_inspection`（执行巡检，需确认）。
  - 工具结果会被格式化为面向用户的 Markdown 表格/文本；查询类工具无需确认直接执行，危险操作返回 `confirm_required` 由用户二次确认。
  - **本地兜底（LLM 不可用时）**：当模型端点不可达时，仍可做本地意图识别——知识库纯问答（RPO/RTO/全量/增量/物理/逻辑备份等）、查询类工具直接执行并格式化返回、危险操作返回确认请求，确保"每条提问都有信息"。页面：「智能体」。
- **AI 智能告警**：基于规则的智能告警分析与归因。页面：「智能告警」。

---

## 支持的数据库与所需客户端

| 数据库 | 备份客户端 | 恢复客户端 | 说明 |
|---|---|---|---|
| MySQL / MariaDB | `mysqldump`、`mysql` | `mysql` | 密码通过临时选项文件注入，不出现于命令行 |
| PostgreSQL | `pg_dump`、`psql` | `pg_restore` / `psql` | 通过 `PGPASSWORD` 环境变量传密码 |
| Oracle | `expdp` / `impdp`（服务端目录）或 `exp` / `imp`（传统增量） | 同左 | 数据泵导出到数据库服务端 `DIRECTORY` |
| Kingbase 电科金仓 | `sys_dump`、`ksql` | `sys_restore` / `ksql` | 兼容 PostgreSQL 协议，端口默认 54321 |
| DM 达梦 | `dexp` | `dimp` | 逻辑导出，端口默认 5236 |
| SQL Server | `sqlcmd` | `sqlcmd` | 官方 T-SQL：BACKUP/RESTORE，密码经 `SQLCMDPASSWORD` 环境变量注入；Linux/Windows 均支持，端口默认 1433 |
| Redis | `redis-cli` | （复制 rdb + 重启） | 通过 `REDISCLI_AUTH` 传密码 |
| MongoDB | `mongodump` | `mongorestore` | 通过 `--password` 传密码 |

> **客户端工具装在数据库服务器上即可**，无需安装到平台机：平台通过 SSH 在数据库服务器执行备份/恢复命令，并**动态发现工具真实路径**（服务运行用户 profile → 登录 shell → 常见安装目录枚举，兼容 Oracle 11g/19c、MySQL 自编译目录、DM、金仓等各种未配环境变量的场景）。平台机自身装有客户端时也可本机执行。

---

## 前端技术选型说明

平台前端采用 **Jinja2 服务端模板 + Bootstrap 5 + 原生 JS（IIFE + `BKP` 工具集）**，
并针对复杂交互页面提供 **Preact + htm 免构建方案**（试点：`static/js/data_compare_preact.js`）：

- Preact / htm 的 ESM 文件已**本地化**于 `static/vendor/preact/`（约 16KB），配合浏览器原生
  import map 加载，**零 npm 依赖、零构建步骤、完全离线可用**，契合平台离线独立打包的部署形态；
- 写法上与 React（组件 + Hooks）几乎一致，未来若升级完整 React + Vite 工具链可平滑迁移；
- 页面引入方式：模板内声明 import map + `<script type="module">`（参考 `templates/data_compare.html`）；
- 传统简单页面（表格 + 表单 + 模态框）仍推荐原生 JS，避免过度工程化。

---

## 目录结构

```
备份管理平台/
├── run.py                 # 启动入口（Web + 调度）
├── init_db.py             # 初始化元数据数据库
├── app.py                 # Flask 应用与页面路由
├── config.py              # 全局配置（环境变量 / config.json 覆盖）
├── auth.py                # 登录鉴权
├── requirements.txt       # Python 依赖
├── core/
│   ├── db.py              # SQLite 封装、建表、加密、工具
│   ├── models.py          # 任务 / 记录 / 恢复 / 日志 数据访问
│   ├── storage.py         # 存储管理、SFTP 上传、保留策略
│   ├── storage_backends/  # 三级存储驱动抽象层
│   │   ├── base.py        # StorageBackend 抽象基类
│   │   ├── local.py       # 本地文件系统（L3）
│   │   ├── minio.py       # MinIO 热数据（L1，S3 兼容 SDK）
│   │   └── s3.py          # S3 冷数据（L2，S3 兼容 SDK）
│   ├── tier_replication.py  # 三级复制引擎（备份后并行复制到各层级）
│   ├── lifecycle.py       # 生命周期：L1→L2 按龄/按容量下沉 + 到期清理
│   ├── notifier.py        # 通知（webhook/钉钉/企微/飞书/邮件）
│   ├── ssh_hosts.py       # SSH 主机纳管（文件备份远程源/目标）
│   ├── jdbc.py            # 直连统一入口（原生驱动优先，JDBC 可选兜底）
│   ├── native_conn.py     # 原生 Python 直连驱动（pymysql/psycopg2/oracledb/dmPython，无 Java）
│   ├── sync/              # 数据同步引擎（Reader/Writer/Plugin 架构）
│   │   ├── engine.py      # 同步执行器（task → SyncEngine）
│   │   ├── realtime_runners.py  # 实时同步（Binlog CDC）后台线程管理
│   │   ├── source.py / sink.py  # Reader/Writer 抽象（已合并入 plugins/base.py）
│   │   ├── type_mapper.py # 统一 Java 类型映射
│   │   └── plugins/       # 数据库同步插件
│   │       ├── base.py    # BasePlugin / SourceReader / SinkWriter
│   │       ├── mysql.py   # MySQL/MariaDB 插件
│   │       └── postgresql.py  # PostgreSQL 插件
│   ├── inspection.py      # 巡检与健康检查引擎
│   ├── scheduler.py       # APScheduler 调度与单次执行入口（含恢复校验/合成全量注册）
│   ├── restore_verify.py  # 恢复校验执行器（策略→报告）
│   ├── global_dedup.py    # 全局重删（内容 sha256 索引，dedup_index）
│   ├── crypto_pool.py     # 存储池加密（AES-256-GCM 信封式，密钥来源：环境变量/系统设置/KMS）
│   ├── synthesize.py      # 自动合成全量引擎（永久增量链闭环 + 重删统计）
│   ├── policy.py          # 备份保护策略引擎
│   ├── drill.py           # 恢复演练引擎（趋势/基线/排程）
│   ├── migration.py / disaster_link.py / clone_service.py  # 迁移保护 / 容灾链路 / 克隆
│   ├── restore_extras.py  # 跨主机恢复 / 克隆辅助工具（mysql_clone_to_test 等）
│   ├── itsm.py            # ITSM 工单对接
│   ├── data_mining.py     # 数据价值挖掘
│   ├── ai_agent/          # AI 智能体（agent/session/executor/tools）
│   ├── ai_alert.py        # AI 智能告警
│   ├── rt_backup/         # 实时备份（watchers/journal/PITR/supervisor）
│   ├── cdc/               # 变更数据捕获（Oracle/达梦/PG/MySQL 日志解析）
│   ├── plugins/           # 备份插件系统（manifests + 安装状态）
│   ├── plugin_catalog.py / plugin_installer.py  # 插件目录 + 安装器
│   ├── skills/            # 9 个备份 Skill 文档（mysql/mariadb/postgresql/oracle/kingbase/dameng/redis/mongodb/file）
│   └── engines/           # 各数据库 + 文件备份引擎（统一接口）
│       ├── base.py        # 引擎抽象基类与结果对象
│       ├── mysql.py / postgresql.py / oracle.py / kingbase.py
│       ├── dameng.py / redis.py / mongodb.py
│       └── file.py        # 文件/目录备份（本地 + 远程 SSH 无 Agent）
├── api/                   # REST API 蓝图
│   ├── storage.py         # 存储目标 CRUD / 测试连接 / 复制 / 复制策略
│   ├── hosts.py           # SSH 主机 CRUD + 连接测试
│   ├── jdbc.py            # 直连连接测试 / 拉库列表 / JDBC 驱动管理
│   ├── sync.py            # 同步任务 / 记录
│   ├── inspection.py      # 巡检执行 / 记录 / 排程
│   ├── system.py          # 系统设置 / 通知配置（UI）/ 备份质量阈值
│   ├── restore_verify.py  # 恢复校验策略 / 报告 / 立即校验 / 统计
│   ├── dedup.py           # 全局重删统计（/api/dedup/stats、/api/dedup/scan）
│   ├── synthesize.py      # 自动合成全量状态 / 手动触发
│   ├── plugins.py         # 插件市场 / 安装 / 卸载 / 推荐 / 批量安装
│   ├── drills.py          # 恢复演练任务 / 趋势 / 基线 / 排程
│   ├── migration.py / link.py / clone.py  # 迁移保护 / 容灾链路 / 克隆
│   ├── itsm.py / datamining.py  # ITSM / 数据价值挖掘
│   ├── ai_agent.py / ai_alert.py  # AI 智能体 / AI 智能告警
│   ├── rt.py              # 实时管控
│   └── tasks.py / records.py / restore.py / policy.py / lifecycle.py / ...
├── templates/             # 前端页面（Bootstrap）
├── static/                # CSS / JS
├── skills/                # 9 份备份 Skill 操作指南（Markdown）
├── drivers/               # JDBC 驱动 jar（可选兜底通道，供 core/jdbc.py 加载；直连无需）
├── docs/                  # 设计文档（mermaid 架构图、备份质量监控等）
├── backups/               # 备份文件落盘目录（运行时生成）
└── instance/              # SQLite 元数据库（运行时生成）
```

---

## 安装

```bash
pip install -r requirements.txt
```

- 必选：`Flask`、`APScheduler`、`paramiko`、`pymysql`、`psycopg2-binary`、`oracledb`（原生直连驱动，无 Java 依赖）
- 可选（按需）：
  - `minio`（三级存储 MinIO / S3 驱动，需 `boto3`）
  - `PyYAML`（config.yaml 支持）
  - `jpype1`、`jaydebeapi`（JDBC 可选兜底通道，另需本机 JDK/JRE 与 `drivers/` 下的驱动 jar；离线环境无 Java 时无需安装）
  - `dmPython`（达梦直连，非 PyPI，随达梦客户端 `drivers/python` 目录提供）

> 启用三级对象存储时，请安装 `minio` 与 `boto3`：`pip install minio boto3`

---

## 配置

配置优先级：**代码默认值 < 环境变量 < `config.json`（项目根目录，可选）**。

常用配置项（环境变量）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | Web 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `SECRET_KEY` | 会话签名密钥（**生产务必修改**） | `dev-secret-...` |
| `WEB_USERNAME` / `WEB_PASSWORD` | 登录账号密码 | `admin` / `admin123` |
| `BACKUP_ROOT` | 备份文件根目录 | `./backups` |
| `DEMO_MODE` | 已废弃（自 2026-08-14 起强制按 `off` 处理，全部真实备份/恢复） | `off` |
| `SCHEDULER_ENABLED` | 是否启用定时调度 | `true` |
| `DEFAULT_RETENTION_DAYS` / `DEFAULT_RETENTION_COUNT` | 默认保留天数 / 份数 | `30` / `50` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | AI 智能体模型端点（不可达时自动本地兜底） | 见 `config.py` |

`DEMO_MODE` 说明：自 2026-08-14 起平台**不再支持仿真/占位备份**——所有备份与恢复均为真实执行，客户端或连接缺失时任务如实失败并给出原因（保留该配置仅为兼容旧配置文件）。

---

## 快速开始

```bash
# 1. 初始化元数据数据库
python init_db.py

# 2. 启动平台（同时启动后台调度器）
python run.py
```

> 也可使用 `start.sh` 一键启动（自动注入 `BACKUP_ROOT`，检测到 JDK 时附带注入，运行 `run.py`）。

浏览器访问 `http://<服务器IP>:8080`，使用默认账号 `admin / admin123` 登录。

> 备份任务执行前，请先在「系统设置 → SSH 主机」纳管数据库服务器（或保证平台机可直接访问目标库）；客户端工具装在数据库服务器上即可，平台会自动发现工具路径并远程执行。

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
ghcr.io/zhh9126/backup-platform:1.2.1
# 版本+构建日期（推荐：同版本多次构建可区分、可回滚）
ghcr.io/zhh9126/backup-platform:1.2.1-20260901
```

历史版本 tag 规律：`vX.Y.Z` 发版同时产出 `X.Y.Z` 与 `X.Y.Z-<构建日期YYYYMMDD>`，例如 `1.1.1-20260901`。历次更新明细见 `readme_20260901.md` 等按日期归档的更新说明。

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
docker pull ghcr.io/zhh9126/backup-platform:1.2.1-20260901

# 离线环境：先在有网机器导出，拷贝到内网后导入
docker save -o backup-platform-1.2.1.tar.gz ghcr.io/zhh9126/backup-platform:1.2.1-20260901
# （内网机器上）
docker load -i backup-platform-1.2.1.tar.gz
```

### 运行

```bash
docker run -d --name backup-platform \
  -p 8080:8080 \
  -v /data/backup-platform:/data \
  -e WEB_PASSWORD=your_password \
  --restart unless-stopped \
  ghcr.io/zhh9126/backup-platform:1.2.1-20260901
```

- `/data` 挂载卷持久化：元数据库（`instance/`）、备份文件（`backups/`）、日志（`logs/`）
- 配置全部走环境变量（`WEB_PORT`、`SECRET_KEY`、`WEB_USERNAME` 等，见上文「配置」）
- 访问 `http://<主机IP>:8080`，默认账号 `admin / admin123`（**请立即修改**）

### Docker Compose 部署（推荐生产）

`docker-compose.yml`：

```yaml
version: '3.8'
services:
  backup-platform:
    image: ghcr.io/zhh9126/backup-platform:1.2.1-20260901
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
| 拉取 ghcr.io 超时 | 配置上文国内加速器并重启 Docker（网页打不开不影响加速） |
| `denied` 拉取失败 | 确认 tag 存在；GHCR 包需在 GitHub Packages 设置为 Public |
| 容器起不来 | 检查 `/data` 挂载目录权限（容器内 root 运行，一般无需干预） |
| pip 安装超时 | 镜像内已烘焙依赖，运行时不需要 pip；勿使用清华源 |

### 手动构建镜像

```bash
docker build -t backup-platform:local .
docker run --rm -p 8080:8080 backup-platform:local
```

---

## 使用说明（导航结构）

平台侧边栏分组如下：

- **概览**：仪表盘（数据库备份任务数 / 文件备份任务数 / 累计备份体积 / 成功失败统计）
- **备份管理**
  - 数据库备份：原有「任务管理」页，管理各数据库备份任务（文件任务已在此排除）
  - 文件备份：文件/目录备份（本地与远程 SSH，无 Agent）
  - 存储管理：三级存储目标（MinIO/S3/本地）配置、容量、复制策略、合成全量
  - 保护策略：备份保护策略管理
  - 备份插件：服务端插件市场（物理备份客户端安装/卸载）
- **记录**
  - 备份记录：数据库/文件的历史备份、校验值、下载、触发三级复制
  - 恢复记录：历次恢复操作的记录
  - 恢复校验：恢复校验策略与报告（验证备份可恢复性）
- **数据恢复管理**
  - 数据恢复：选择备份记录恢复到目标实例/目录
  - 数据库部署：将数据库（MySQL / MongoDB 等）自动部署到目标主机
- **灾备管理**
  - 数据迁移：数据迁移全流程保护（原「迁移保护」，黄金回退点 + 恢复验证 / 高频增量 / 重心切换与旧库保留）
  - 数据同步：库到库的同步任务（已从「备份管理」分组移入，作为迁移/容灾的数据源）
  - 容灾链路：容灾链路管理（引用数据同步 / 实时保护任务，提供智能选路、日志间隙填补、备端一致性校验）
  - 克隆服务：创建可独立使用的克隆实例
  - 恢复演练：恢复演练任务、趋势、基线、季度排程
- **实时管控**
  - 实时管控时间线：RT 实时备份与变更捕获（CDP / PITR）
- **运维**
  - 巡检：手动/定时体检，判定 `pass` / `warn` / `fail`，`fail` 即告警
  - 智能告警：基于规则的智能告警
  - 数据价值挖掘：备份数据价值挖掘分析
  - 智能体：AI 对话式运维助手（工具调用执行备份/恢复/巡检）
  - 系统设置：调度器状态、通知配置（Web UI）、备份质量阈值、SSH 主机纳管、平台信息与日志、存储池加密密钥（KMS）托管

典型操作：

1. **数据库备份**：在「数据库备份」新建任务，填写连接、备份类型、调度、保留策略与存储目标。
2. **文件备份**：在「文件备份」先到「系统设置 → SSH 主机」纳管远程主机，再建文件任务（源/目标可分别选本地或远程）。全量备份生成 `*_full.tar.gz`；增量备份基于**源快照**（同一路径的多任务共享基准），仅打包变化文件，在目标目录根下生成 `*_inc.tar.gz`（与全量归档同级），不会覆盖或删除目标目录里的其他备份或文件。若增量任务找不到历史快照（如全量在修复前执行），会自动回退为全量。
3. **三级存储**：在「存储管理」分别新增 MinIO（L1）、S3（L2）、本地导出（L3）目标并“测试连接”；备份完成后自动复制到各层级。
4. **数据同步**：在「数据同步」新建同步任务，填写源/目标连接，选择源表与目标表；在「字段映射」标签页使用「同名映射」或手动点击字段建立映射；选择写入模式（append/overwrite/upsert/create_if_not_exists）与同步模式（full/incremental/realtime）；点击「运行」执行离线同步，或点击「生成 Flink 配置」下发到 Flink CDC 集群做实时同步。
5. **巡检**：在「巡检」点击“立即巡检”，查看各项 `pass/warn/fail` 明细；可配置定时巡检。
6. **数据恢复与灾备**：在「数据恢复」选择备份记录恢复到目标实例/目录；**文件增量恢复会自动先回全量、再按时间顺序应用增量**。在「数据库部署」上传安装包并部署 MySQL / MongoDB 等到目标 Linux 主机（支持副本集、认证、mysqlx 端口错开等），实时查看部署日志。在「灾备管理」进行数据迁移、数据同步、容灾链路、克隆服务、恢复演练操作。
7. **恢复校验**：在「记录 → 恢复校验」新建策略并关联到某个备份任务，选择调度方式（`manual`/`cron`/`interval`）；点击「立即校验」对最近一次成功备份做可恢复性校验，查看报告与成功率 KPI。
8. **副本优化**：平台对支持增量备份的引擎自动按 `chain_id` 维护永久增量链；当增量份数达到阈值（默认 ≥2）由调度器在每周日 03:00 自动合成新全量并回收旧增量（副本闭环），合成时计算重删节省量，可在「存储管理 → 合成全量」手动触发或查看状态。
9. **备份插件**：在「备份管理 → 备份插件」查看已安装 / 市场，对物理备份所需客户端（XtraBackup / MariaDB Backup / pgBackRest 等）一键安装；安装后对应数据库任务的物理备份即可启用。缺客户端时调度前 `preflight` 会提示前往插件页安装。
10. **备份质量监控**：在「系统设置 → 备份质量阈值」配置超长（固定时长 / 速度阈值 / 两者任一）与超频（同任务单位时间次数）判定规则，仪表盘实时展示超长 / 超频 KPI。
11. **存储池加密与重删**：在「系统设置 → 存储池加密密钥（KMS）」选择「本地密钥库」或「外部 KMS」并保存（保存后自动跑加密自检）；在数据库/文件备份任务表单勾选「存储池加密」，该任务落盘即为 AES-256-GCM 密文。仪表盘新增「全局重删比 / 累计节省空间 / 存储池加密任务数」三张 KPI 卡，实时反映重删与加密覆盖情况。
12. **组合备份（全量+增量）**：新建数据库/文件备份任务时选择「组合（全量+增量）」，分别设置全量调度（cron/interval）与增量调度（cron/interval）；调度器会注册两个独立作业，任务列表的调度单元格显示「组合 全…/增…」，执行时按所选类型运行对应备份。
13. **AI 智能体**：在「智能体」直接与对话式助手交互，提问「列出所有备份任务」「查询存储用量」「最近备份有没有失败？」「什么是RPO？」等；执行备份/巡检类操作时需二次确认。即使 AI 模型端点不可达，平台也会以本地兜底（知识库问答 + 工具直查）返回信息，不会空响应。

---

## 三级存储体系说明

| 层级 | 类型 | 角色 | 说明 |
|---|---|---|---|
| L1 | MinIO | 热数据（第一落点） | 备份文件首先写入此处 |
| L2 | S3 | 冷数据归档 | 从 L1 实时/异步推送，用于异地容灾与长期归档 |
| L3 | 本地路径导出 | 离线转移 | 服务端本地文件系统导出，可作为离线介质 |

- 复制由 `tier_replication.replicate_to_tiers()` 在备份成功后**并行**执行（L3 为复制终态，不参与自动流转）。
- 复制策略字段：`push_l1_minio` / `push_l2_s3` / `push_l3_local`（默认均启用）、`timing`、`max_retries`、`retry_interval`。
- 生命周期（`lifecycle.py`）：目前实现 L1(MinIO)→L2(S3) 按龄/按容量下沉与全局到期清理；L3 为终态。

---

## 通知配置

通知渠道支持 Webhook / 钉钉 / 企业微信 / 飞书 / 邮件，成功与失败可分别开关。**推荐在「系统设置 → 通知配置」中通过 Web UI 配置**（密码不回显，留空表示不改）。

也可在 `config.json` 中配置 `NOTIFY_DEFAULTS`（DB 配置优先于此处默认值）：

```json
{
  "NOTIFY_DEFAULTS": {
    "enabled": true,
    "on_success": false,
    "on_failure": true,
    "channels": [
      {"type": "webhook", "url": "https://example.com/hook"},
      {"type": "dingtalk", "url": "https://oapi.dingtalk.com/robot/send?access_token=xxx"},
      {"type": "wechat", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"},
      {"type": "feishu", "url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
      {"type": "email", "smtp_host": "smtp.example.com", "smtp_port": 465,
       "use_tls": true, "smtp_user": "alert@example.com", "smtp_password": "xxx",
       "from_addr": "alert@example.com", "to": ["ops@example.com"]}
    ]
  }
}
```

---

## 生产部署建议

- 使用 `gunicorn` 运行：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`
- 通过 Nginx 反代并启用 HTTPS
- 修改 `SECRET_KEY` 与登录密码，必要时接入企业统一认证
- 将 `BACKUP_ROOT` 指向大容量、有冗余的存储；启用三级对象存储实现异地容灾
- 配置系统服务（systemd）实现开机自启与进程守护

---

## 安全说明

- 数据库连接 / SSH 主机密码以混淆方式存储于 SQLite，Web 接口默认不回显明文；生产环境建议结合密钥文件 / 环境变量管理
- MySQL 等使用临时选项文件（权限 `600`）承载密码，避免明文出现在进程参数中
- Redis 通过 `REDISCLI_AUTH` 环境变量传密码
- 请在生产环境务必修改默认登录账号与 `SECRET_KEY`（平台支持 `SECRET_KEY` 随机化并持久化，重启不丢失会话密钥）
- **登录安全加固**：登录失败**暴力破解限流**（多次失败临时锁定）；CSRF 校验提供**同源兜底**（Origin/Referer 校验）；全局注入**安全响应头与 CSP**，缓解 XSS / 点击劫持
- **接口鉴权与注入防护**：`restore_verify` 系列路由全量鉴权；修复 PITR 参数注入、备份/恢复文件下载**路径穿越**、file 引擎命令注入等风险点

---

## 常见问题

**Q：平台机没有安装数据库客户端工具，能否备份？**
可以。平台通过 SSH 到数据库服务器执行备份/恢复命令，并**动态发现工具真实路径**（数据库服务运行用户的 profile → 登录 shell → 常见安装目录枚举，兼容 Oracle 11g/19c、自编译 MySQL、DM、金仓等未配环境变量的场景），工具路径绝不写死。仅当远端确实不存在对应工具时任务才会失败，此时可通过「备份插件」页或自定义备份脚本解决。

**Q：如何验证备份真的可以恢复？**
三种方式：(1)「数据恢复管理 → 恢复校验」配置策略，定期对最近成功备份做可恢复性校验（Oracle 逻辑备份走 impdp SQLFILE 真实解析 DDL、物理备份走 RMAN RESTORE VALIDATE + 真实抽取数据文件）；(2)「数据恢复管理 → 数据对比」将恢复库与生产库做行数/校验和/抽样比对；(3)「记录」页对任意备份一键恢复到目标实例。

**Q：逻辑增量备份是否完全可用？**
MySQL 增量依赖 binlog；PostgreSQL / Kingbase / MongoDB 的逻辑增量能力有限，建议配合 WAL 归档 / oplog / 时间点恢复或物理备份；达梦增量建议使用 `dmrman` 物理备份；SQL Server 的增量即事务日志备份（`BACKUP LOG`，需恢复模式为 FULL/BULK_LOGGED），差异备份用 `WITH DIFFERENTIAL`。本平台逻辑引擎对不支持真正增量的库会回退为全量并在备注中说明。

**Q：如何实现异地备份？**
两种方式：(1) 任务“存储后端”设为 `SFTP`，填写远程主机/路径（需 `paramiko`）；(2) 在「存储管理」配置 MinIO(L1) + S3(L2)，备份完成后自动复制到对象存储实现异地容灾。

**Q：文件备份需要被备份机器装 Agent 吗？**
不需要。文件备份通过 `paramiko` SSH 在远程主机上执行 `find`/`tar`，无需在被备份机器安装任何 Agent；源与目标可分别选择本地或远程。

**Q：三级存储的层级是如何定义的？**
L1 = MinIO（热数据，第一落点）、L2 = S3（冷数据归档）、L3 = 源端本地路径导出（离线转移）。备份先写 L1，再由 `tier_replication` 并行复制到 L2/L3，`backup_records.storage_tier` 记录实际到达层级（如 `minio+s3+local`）。

**Q：巡检判定规则是什么？**
对任务做连通性 + 调度 + 上次状态体检：连通性失败或最近一次备份失败 ⇒ `fail`；无法判定连通性 / 从未运行 / 未配置调度 ⇒ `warn`；均正常 ⇒ `pass`。任一任务 `fail` 会立即告警。

---

## 备份 Skills 文档

`skills/` 目录提供 10 份面向运维人员的备份操作指南（Markdown）：

| Skill | 关键能力 |
|---|---|
| `mysql-backup` | `mysqldump` 逻辑 + XtraBackup 物理 + binlog PITR |
| `mariadb-backup` | 继承 MySQL，`mariabackup` 物理 |
| `postgresql-backup` | `pg_dump` 逻辑 + `pg_basebackup` 物理 + WAL 归档 |
| `oracle-backup` | `expdp`/`exp` 逻辑 + RMAN 物理 + archivelog PITR |
| `kingbase-backup` | `sys_dump` 逻辑 + `sys_basebackup` 物理 |
| `dameng-backup` | `dexp` 逻辑 + `dmrman` 物理 |
| `sqlserver-backup` | `BACKUP DATABASE/LOG` 官方 T-SQL + `RESTORE WITH MOVE, REPLACE, RECOVERY` + `VERIFYONLY` |
| `redis-backup` | RDB 快照复制 |
| `mongodb-backup` | `mongodump` |
| `file-backup` | `tar.gz` 全量 + 快照增量 + 准 CDP + 恢复链 |

---

## 许可证

本项目仅供学习与内部交付使用。
