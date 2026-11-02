# 备份插件五道关卡审计与对齐升级路线（2026-11-02）

> 背景：按"备份插件 = 翻译官 + 执行者"五道关卡方法论，对 AIDBM 引擎/Agent 通道逐关审计。
> 原则沿用产品既定约束：**零 Agent 安装（Agent 通道为可选项）、完全离线、真实执行不仿真、不虚报**。

## 一、五道关卡审计表

| 关卡 | 能力点 | AIDBM 现状 | 结论 |
|---|---|---|---|
| 1. 接口标准与生命周期 | CheckEnv | `base.py preflight()`（引擎级依赖检查）+ `core/backup_precheck.py` 13 项预检（配置/SSH/源库连通/工具就绪/归档模式/磁盘空间） | ✅ 达标 |
| | Backup/Restore | 统一入口 `base.run_backup()/run_restore()`；`BackupType/BackupResult/BackupStatus` 全引擎枚举一致 | ✅ 达标 |
| | Cancel | `core/cancel.py` 协作式取消事件；**本轮闭环**：`_ssh_exec_stream` 块边界每秒检查 → 关会话抛 CancelledError；Agent 通道「断连即杀」（进程组级 killpg） | ✅ 达标（本轮） |
| | QueryProgress | 进度心跳（30s：已收字节/速率/耗时）进操作日志与实时日志；结构化百分比进度 API 缺失 | 🟡 P1 |
| | QueryStatus | `backup_records.status/last_status` 双写、终态优先 | ✅ 达标 |
| 2. 数据流管道与子进程隔离 | 流式管道 | `exec-stream` chunked + stderr 独立读线程（防管道死锁）；mysqldump stdout 直连文件流式（MemoryError 修复）；`_ssh_exec_pipe_to_file` 边收边落盘（内存恒定） | ✅ 达标 |
| | 子进程隔离 | **本轮**：Agent Popen `start_new_session=True` 独立进程组，断连/超时 killpg 连孙进程（rman/xtrabackup）一并终止，不留孤儿 | ✅ 达标（本轮） |
| | 限速/断点续传 | 平台侧 `rate_kbps`（不依赖远端 pv）；`sftp_put_resumable`/.part 断点续传；rsync delta 增量拉取；源端文件级去重 | ✅ 达标 |
| | 传输完整性 | sha256 端到端对账（file_put/拉取双端） | ✅ 达标 |
| 3. Catalog 元数据 | 备份记录元数据 | `backup_records`：size/checksum(sha256)/manifest.json（全实例逐库清单） | ✅ 达标 |
| | 深度校验 | `verify_record`：mysqldump 头/PGDMP 魔数/RMAN RESTORE VALIDATE+数据文件抽取/impdp SQLFILE | ✅ 达标（业界少有） |
| | 恢复点安全检测 | `backup_anomaly` 三查（体积突变/熵值/结构嗅探），只提示不拦截 | ✅ 达标 |
| | 结构化位点目录（SCN/LSN/GTID、增量链 relation） | 位点散在产物内部（xtrabackup_check_points 等）与消息/rt 模块，记录表无结构化列 | ❌ P1 |
| 4. 恢复花式操作 | 跨主机/异名恢复 | `target_host_id/target_db` 全库型支持 | ✅ 达标 |
| | PITR | MySQL binlog 多段+zstd（实测）、PG 归档 PITR（133 实测）、Oracle `rman_pitr`（代码就绪，129 磁盘待清理收口）、达梦 dmrman PITR 未做 | 🟡 P1 |
| | 恢复演练 | `drill real_restore`（真恢复临时库→逐表行数校验→评分→销毁，MySQL/MariaDB） | ✅ 达标 |
| | 克隆服务 | `clone_service` 免审批直通 VDB | ✅ 达标 |
| | 恢复选项细化（跳过已存在/仅还原日志/Mongo oplog 时点） | 未做 | ❌ P2 |
| 5. 健壮性与工程化 | 临时目录治理 | `FULL_INSTANCE_WORK_DIR`(.bp_work)、远端 /tmp/platform_* 用后清理、BP_WORK_DIR 可覆盖 | ✅ 达标 |
| | 脱敏与安全 | 密码走 env（MYSQL_PWD/SQLCMDPASSWORD/KINGBASE_PASSWORD）不进 argv；openGauss `-W` 例外为平台限制（已如实记录）；Agent HMAC 强制 + AES-GCM 黑盒 payload | ✅ 达标 |
| | 静默重试/容错 | SSH 握手 3 次重试、impdp 12514 自动重试、通道异常分类（瞬时/非瞬时）、启动自愈（running 僵尸记录清理） | ✅ 达标 |
| | 空间预检 | 平台侧全实例逐库预检 + ENOSPC 精准诊断；**Agent 通道备份前远端落盘空间预检未覆盖** | 🟡 P0 |
| | 审计 | operation 日志全程 + Agent `_audit` + 锁定/解锁 WARNING | ✅ 达标 |

## 二、本轮已落地（2026-11-02）

1. **协作式取消闭环**（第一关 Cancel 收口）：
   - 平台 `core/engines/file.py::_ssh_exec_stream`：主循环每秒检查取消事件，命中即关会话抛 `CancelledError`（调度器标记 cancelled，断点保留可续传）。
   - Agent `agent/backup_agent.py::_exec_stream`（v1.1.1）：select 同时监视子进程 stdout 与客户端 socket；客户端断连（平台取消/网络中断）**秒级发现并 killpg 终止整个进程组**——此前 RMAN/物理备份在无人接收时会继续空跑占满磁盘。
   - 平台 `core/agent_client.py::AgentChannel.close()`：关闭时**强制断开底层 TCP**（仅丢迭代器不断连接，Agent 永远检测不到断连——E2E 实测暴露的真缺口）；`AgentSessionChannel.exec_command` 支持透传 agent 侧流式超时。
   - 覆盖 SSH 通道与 Agent 通道全部流式备份/恢复路径（`_ssh_exec_pipe*` 均委托 `_ssh_exec_stream`）。
2. Agent 版本 1.1.0 → 1.1.1；活动子进程注册表 `AgentState.active_procs`（可观测基础）。

**E2E 实测 `/root/e2e/e2e_cancel_kill.py` 7/7 PASS**（本机 Agent 通道）：
T1 流式正常完成 rc=0；T2 断连即杀（读块后掐断，3s 内远端进程组消失）；T3 平台 cancel → 2.1s 抛 CancelledError 且远端进程组退出；T4 agent 侧 timeout=3 → killpg rc=-9（3.1s 生效）。回归：test_agent_exec+test_backup_precheck+test_fix_2026_001 共 35 passed；前端 JS 门禁 18 文件全绿。

> 注意：Agent 变更需**重新安装/重推 Agent**（黑盒 payload 按当前源码重建）才在目标机（170/171）生效；本轮按用户要求**不打离线包**。
> 已知边界（如实）：agent 通道上，若远端命令长时间无 stdout（RMAN 本地备份静默期），平台侧取消需等到下一块数据到达才生效（HTTP 流读阻塞）；SSH 通道不受此限（paramiko 非阻塞轮询）。

## 三、升级排期

| 优先级 | 事项 | 对应关卡 | 说明 |
|---|---|---|---|
| P0 | Agent 通道远端落盘空间预检（备份前 stat 目标目录可用空间 vs 预估产物） | 5 | 复用平台 ENOSPC 预检逻辑，经 /exec 探测即可 |
| P0 | 取消场景 E2E（长备份中途停止 → 记录 cancelled + 远端进程组退出 + 无孤儿） | 1/2 | 本轮改动回归 |
| P1 | `backup_records.catalog_json` 结构化位点列（SCN/LSN/GTID/增量链 relation/备份集版本） | 3 | MySQL xtrabackup_check_points、PG timeline、Oracle SCN 摘要 |
| P1 | 结构化进度：任务级 percent 字段（引擎解析工具百分比输出，RMAN/impdp/xtrabackup 各自正则） | 1 | QueryProgress 对齐鼎甲"进度百分比" |
| P1 | 达梦 dmrman PITR（137 在线后真机）；Oracle PITR 129 磁盘清理后收口真机 | 4 | 不得在真机验证前对外承诺 |
| P1 | Agent rlimit（子进程地址空间/文件大小硬顶，防失控） | 2 | py3.6 兼容 resource 模块 |
| P2 | 恢复选项细化：跳过已存在对象、仅还原日志文件、Mongo oplog 时点恢复 | 4 | 对标鼎甲恢复选项 |
| P2 | 单任务多通道并行（RMAN 多通道/库级并行） | 2 | 与限速/进度联动设计后实施 |

## 四、分库型对齐矩阵（远程执行通道统一，Agent 通道为通用 exec+file，全库型可走）

| 库型 | 逻辑备份 | 物理/热备 | 恢复 | 实时 | PITR | 真机验证 |
|---|---|---|---|---|---|---|
| MySQL/MariaDB | mysqldump ✅ | XtraBackup/mariabackup 推送式 ✅ | ✅ | binlog ✅ | ✅（多段+zstd 实测） | ✅ |
| PostgreSQL/金仓/openGauss/磐维 | pg_dump/sys_dump/gs_dump ✅ | pg_basebackup/sys_basebackup/gs_basebackup ✅ | ✅ | 流复制/WAL ✅ | ✅（PG 实测；金仓/openGauss 代码就绪） | ✅ |
| 达梦 | dexp/dimp ✅ | disql 联机备份 ✅ | ✅ | LogMiner ✅ | dmrman 🟡 P1 | ✅（除 PITR） |
| Oracle 11g/19c | expdp/impdp ✅ | RMAN 推送式 ✅ | ✅ | LogMiner ✅ | rman_pitr 🟡（129 收口中） | ✅ |
| SQL Server | — | T-SQL BACKUP ✅ | ✅ | — | 日志恢复随 FULL 模式 | ✅ |
| Redis/MongoDB | RDB/mongodump ✅ | — | ✅（Redis 恢复未实测，如实） | — | — | 🟡 |
| 文件/对象存储/VM | tar/rsync/源端去重 ✅ | 镜像备份 ✅ | ✅ | — | — | ✅ |

> 分库型共用同一插件骨架（引擎基类 + remote 通道 + Agent 通道 + 预检 + 取消 + 进度心跳），新增库型只需实现引擎层命令翻译——这正是"翻译官"框架的收益。
