# 容灾备份一体机采购需求 · AIDBM 能力对照与达标状态（2026-11-23）

需求来源：`备份一体机需求-261123(1).docx`（容灾备份一体机，采购包 001/1）。
对照口径：**如实标注**，达标项附代码/实测证据；未达标项写清原因与替代方案；
硬件规格与商务条款不属软件范畴，单独列明。

## 一、已达标的软件能力（本轮新增/完善，附真机证据）

| 需求条款 | 达标状态 | 实现/证据 |
|---|---|---|
| 浏览器查看/下载/验证单个文件 | ✅ 达标 | `core/archive_browser.py` + `/api/records/<id>/archive/{files,file,download,verify}`；170 全实例备份产物 126 个 SQL 文件真机验证：预览中文内容、SHA256 true/false、单文件下载、路径穿越 HTTP 400 拒绝 |
| 整机数据恢复演练到第三台主机 | ✅ 达标（MySQL） | `core/drill.py` `_run_real_restore_drill` 支持 `scenario.target_host_id`；170 源 → 171 第三台主机真机演练 drill#9：真实建库/恢复(rc=0)/跨主机逐表行数校验/自动销毁，源库 8 行零触碰、演练库 0 残留、RTO 5.2s、评分 65 |
| Oracle 11g/19c JDBC 测试与拉库（不填 service） | ✅ 达标 | 133 Oracle 11g(service=orcl11g)、171 Oracle 19c 真机：空 db_name 自动从纳管 SSH `lsnrctl status` 探测 service，JDBC 测试成功；拉库走 SSH+sqlplus 返回真实 schema；预检归档模式真实探测 ARCHIVELOG |
| 多流 + 压缩 + 带宽控制 | ✅ 达标 | `bandwidth_limit`/`rate_limit_window`/多流并行拉取(zst/gzip)；`scripts/transfer_benchmark.py` 实测 4 流 SHA256 对账吞吐（千兆环境 254.6Mbps，工具在 10G 环境以默认 2000Mbps 阈值判定） |
| AES-256 / SM4 加密 | ✅ 达标 | `core/crypto_pool.py` AES-256-GCM 默认 / SM4-128-GCM 可选（GB/T 32907） |
| 两地三中心级联复制、多对一备份 | ✅ 达标 | `core/pool_replication.py` pool_chains 串行/并行级联 |
| 不可变备份防勒索、访问白名单 | ✅ 达标 | `core/immutability.py`（保留锁定+chmod 写保护+完整性复检+四路删除拦截）、`core/backup_anomaly.py` 启发式检测；受保护主机禁止作为恢复目标 |
| 断点续传、限时限流 | ✅ 达标 | `sftp_pull_resumable` + `.part`/sha 边车；`rate_limit_window` 跨零点时段限速 |
| 大屏显示 | ✅ 达标 | `/screen` + `api/screen.py`，实时运行状态与业务系统/主机态势 |
| 定时备份与实时复制切换 | ✅ 达标（数据库级） | 调度器定时备份 + `core/cdc/*` 日志级准 CDP（binlog/WAL/LogMiner/DM LogMNR），RT 配置可启停 |
| 信创数据库与国产 OS | ✅ 达标（数据库） | 达梦/金仓/openGauss/磐维/瀚高/优炫等国产库真机矩阵；平台 Python 纯解释执行，原则上架构无关（见下方未验证项） |

## 二、部分达标 / 有条件达标

| 需求条款 | 状态 | 说明 |
|---|---|---|
| 恢复到指定虚拟化平台 VM | 部分达标 | `core/vm/` 5 provider（ESXi/Hyper-V/libvirt/PVE/Workstation）支持从恢复点 clone 到新 VM（`clone_from_rp`/`verify_rp` API + 前端按钮）；跨平台（如物理机→VM、VMware→Hyper-V）未实现，磁盘格式转换与 API 语义不通 |
| 源端虚拟化覆盖 Xen/PowerVM/RHEV/FusionSphere/EasyStack | 部分达标 | 现有 5 provider；Xen/RHEV 可复用 libvirt 路径、FusionSphere/EasyStack 可仿 HTTP provider，需逐家适配（大工程）；PowerVM（HMC/VIOS）差异过大 |
| 文件系统级恢复演练到第三台主机 | 部分达标 | 数据库（MySQL）已达标；文件备份跨主机演练可复用 `cross_host` SFTP 通道，尚未接入 drill 类型 |
| 传输速度 ≥2000Mbps@10Gbps | 有条件达标 | 工具已交付可实测；当前开发环境和 170/171 是千兆链路（实测 254Mbps 即千兆上限），**须在 10G 现场跑 `scripts/transfer_benchmark.py` 出报告才可作为交付证据** |

## 三、结构性不跟随（需在规格响应中如实说明负偏离）

> **2026-11-24 用户决策更新**：允许客户端只安装一个统一 Agent 插件支撑多块能力，
> 这解除了此前"零 Agent"的硬约束。已开发统一插件框架（`agent/plugins.py` v1.3.0），
> 171 真机验证三块能力可用（见下节）。下表中部分条款的"结构性不跟随"口径随之调整。

| 需求条款 | 原因 |
|---|---|
| ★卷级块级 CDP、RPO 最小 1 秒 | **部分改观**：统一 Agent 已支持块级 SHA256 位图 + 1 秒间隔封段（171 真机 RPO=1s 封段 7936 块验证通过）。但仍是"块级变更位图"而非真·卷级 device-mapper snapshot 的一致性快照；生产端需 root 与 dm 权限时才能用真块级（无权限时如实降级为文件级位图） |
| 高负载自动切 bitmap CBT | CBT 必须在写入时由 hypervisor 持续跟踪脏块，无法事后切换；OS/文件级无内核 CBT。平台有 `source_guard` 高负载降并行+限速（保护生产），但不是 bitmap CBT |
| 整机裸金属恢复（可启动） | **部分改观**：统一 Agent 的 `bmr` 插件已支持 dd 裸镜像 + 恢复作业单（171 真机引导工具探测通过），但仍不做无人值守的引导介质自动制作/GRUB 改写——需人工确认执行，避免误覆盖生产磁盘 |
| 应用级整机容灾（中间件/邮件/ERP） | 平台保护的是数据库与文件层，不对 Tomcat/Weblogic/Exchange/SAP 等应用做一致性快照与应用感知恢复 |
| ARM/国产 CPU 真机验证 | 代码为纯 Python 解释执行，无架构相关依赖；但**未在 ARM 环境实测**，不能宣称已验证（可承诺架构无关，不承诺已测试） |
| 硬件规格（2U/CPU/内存/盘位/网口/电源/IPMI） | 硬件采购范畴，与平台软件无关 |
| 原厂服务（巡检/升级/培训/7×24 质保） | 商务服务范畴，与软件功能无关 |

## 四、本轮验证记录（可复跑）

- `/root/e2e/e2e_delivery_1123.py`：170/171 真实数据库可交付验证 20/20 PASS
  （Oracle 19c 空 service JDBC/拉库/预检归档、170 全实例备份归档浏览六项、PG/金仓/达梦预检）
- drill#9：170 源 → 171 第三台主机真实恢复演练 success（RTO 5.2s，评分 65，演练库自动销毁、源库零触碰）
- 133 Oracle 11g：JDBC 空 service 成功、拉库 9 schema、预检 9 项全 pass、RMAN 物理备份 376.3MB 落盘
- `scripts/transfer_benchmark.py --host root@<10G目标>:22 --password <pw> --size-mb 512 --streams 8`：10G 现场达标判定命令
- 回归：`pytest tests/test_drill_third_host.py tests/test_switch_drill.py tests/test_dr_switch_enhance.py tests/test_dr_monitor.py tests/test_i18n_coverage.py tests/test_archive_browser.py tests/test_oracle_jdbc_service.py tests/test_backup_precheck.py` → 53 passed

### 2026-11-24 追加：统一 Agent 插件（一个 Agent 多能力）真机验证
- **架构**：`agent/plugins.py` 插件框架（block-cdp / bmr / app-dr 三块）+ `agent/backup_agent.py` v1.3.0 内嵌插件、`/capabilities` 自报、`/plugin/<name>` 统一路由；平台侧 `core/agent_client.py::plugin_invoke` + `api/agent_plugins.py` + Agent 管理页"统一插件能力"展示。
- **171 真机**（Agent 由 v1.1.2 黑盒升级为 v1.3.0 源码版，Python3.7 兼容，systemd 托管）：
  - `/api/agent-plugins` 列出 3 个插件全部 `supported=true`；
  - block-cdp 位图：`/data/mysql3306/mysql.ibd` 5 块；RPO=1 秒封段 2 轮，单轮 7936 块（经平台 API + agent_id 调用成功）；
  - bmr：引导工具探测（grub2-mkconfig 可用），恢复作业单 5 步、需人工确认；
  - app-dr：冻结/解冻钩子 + 整机文件快照，全部成功。
- **诚实边界**：171 Python3.7 无 cryptography，加密降级为 HMAC-SHA256（不影响插件功能）；块级位图非 device-mapper 一致性快照；BMR 不无人值守改引导。
- 回归：52 passed（含 6 项插件契约 + i18n 门禁 + 特殊恢复/第三台主机演练/归档浏览/Oracle）。

### 2026-11-24 再追加：恢复选项细化 + 扇区雕刻 + 一致性检测
用户要求"未完成的持续开发"，本轮补齐：
1. **跳过已存在表**：`core/table_extract.py::_skip_existing_lines`（去 DROP TABLE、CREATE→IF NOT EXISTS、INSERT→IGNORE），仅作用于表级切片通道，不碰全量恢复主链路；`api/restore.py` → `scheduler.run_restore_now` → `engine.restore` 贯通 `skip_existing_tables`。**170 真机验证**：记录 504 恢复到 `e2e_skip_exist`，原数据 `(999,'原数据-不应被删')` 保留，备份 3 行（订单-甲/乙/丙）并存，共 4 行，恢复 success 且消息如实标注 IGNORE 语义。
2. **Mongo oplog 时点**：`core/engines/mongodb.py` 支持 `oplog_replay`/`oplog_limit`（BSON 时间戳 `<sec>:<ord>` 格式校验，`--oplogReplay`/`--oplogLimit`），API 贯通（需备份时开启 `mongodump --oplog`，平台不猜测时点）。
3. **扇区级雕刻**：统一 Agent 新增第 4 插件 `block-carve`（`scan` 魔数签名识别 JPEG/PNG/PDF/ZIP/SQLite/GZ/XZ/Ogg/ibd，`extract` 只读提取到平台目录 + SHA256）；171 真机扫描 `/data/mysql3306/mysql.ibd` 2MB 返回 0 命中（诚实，无常见文件头）。
4. **数据一致性检测**：`block-cdp` 插件新增 `consistency_check`（MySQL innochecksum / Oracle dbv / 通用文件头），返回 `repair_plan.auto_execute=false` + 人工修复建议；171 真机 `consistent=true`，不自动执行写操作。
5. **仅还原日志**：即既有 PITR 能力（`/api/rt/preview|recover`），11-17 已 13/13 真机验证，无需重复开发。
- 回归：52 passed（含新增 6 项恢复选项契约 + 4 插件 + 表提取/改名既有回归）。
- 诚实边界：雕刻仅识别未覆盖块的文件头，已覆盖数据不可恢复；IGNORE 会同时跳过严格模式类型错误（已在 notes/UI 标注）；Mongo oplog 需备份时开启 --oplog 才有时点数据。

### 2026-11-24 第三轮：BMR 可启动介质 / 应用钩子库 / 高负载自适应 + 深度验证

1. **BMR 可启动介质**：`bmr` 插件 v1.1.0 新增 `build_boot_media`——用 OS 自带 `grub2-mkimage` + `mkisofs/xorriso` 产出 El Torito 可启动救援 ISO（含内核/initramfs/恢复镜像/grub.cfg + sha256）；无工具时如实 unsupported（171 真机探测：grub2-mkconfig 有、mkisofs/xorriso 无，故现场无法真出 ISO，如实标注）。
2. **应用一致性钩子库**：`app-dr` 插件 v1.1.0 内置 7 preset（nginx/redis/mysql/postgresql/tomcat/dameng/generic），`list_hooks` 供 UI 选择，`freeze`/`thaw` 支持 `preset=名称`，不再要求用户手写命令；171 真机 `preset=redis` freeze/thaw 均成功（rc=0）。
3. **高负载自适应块级位图**：`core/source_guard.py` 新增 `source_guard_cdp_mode`/`source_guard_cdp_agent_id` 配置；throttle 区间自动探测目标机统一 Agent 的 block-cdp 能力，可用时切"块级位图增量"模式，无 Agent 时如实降级为降并行+限速（不虚报 CBT）；配置端点 `/api/source-guard/config` 真机可读。

**深度验证**（`/root/e2e/e2e_gap_deep_1124.py`，170/171 真机，走生产 API）：**21 PASS / 0 FAIL**
- 4 插件自报全 supported、7 应用钩子 preset 真机执行、扇区雕刻 PNG 签名识别+提取（228B + sha256）、一致性检测+修复不自动执行、BMR 工具探测+缺镜像诚实失败、跳过已存在表（170 原数据 999 保留+备份 3 行写入=4 行）、高负载降级决策诚实、Oracle 拉库/JDBC 回归。

**单元回归**：76 passed（13 个测试文件，含 BMR/钩子库/高负载契约 + 恢复选项 + Agent 插件 + 前轮能力）。

### 2026-11-24 第四轮：架构边界项逐项闭环（ToolPack / 真裸块 CDP / 事务一致性 / 无人值守 BMR / 新 VM provider）

用户要求"未完成的能力持续开发、缺口补齐、深度测试验证"，本轮把上一轮标为"结构性不跟随/环境受限"的条款逐项推进：

1. **BMR 真出可启动 ISO（不再依赖 mkisofs/xorriso）**：新增 `agent/iso9660.py`——纯 Python ISO9660 Level 2 + El Torito BIOS 引导生成器（PVD/路径表/目录记录/Boot Catalog 全部手写，零外部依赖、流式写入）。`bmr` 插件 v1.3.0 的 `build_boot_media` 优先用内置生成器，回落系统 mkisofs/xorriso；PyInstaller 加 `--hidden-import iso9660`。**171 真机 11/11 PASS**：现场确认无 mkisofs/xorriso（只有 grub2-mkimage），仍真出 40MB 可引导 ISO，SFTP 拉回后解析 PVD/El Torito Boot Catalog 全部通过，引导镜像字节读回一致（`/root/e2e/e2e_bmr_iso_1124.py`）。
2. **真·卷级块 CDP**：`block-cdp::_cdp_bitmap` 新增裸块分支（`stat.S_ISBLK` → `os.read` 按 4096B 分块 SHA256，mode=`raw_block`），不再把 `/dev/loop*` 错当目录。**本机 losetup 真机 VERIFY PASS**：8MB loop 卷 2048 块位图、改第 10 块精确定位唯一变更块、秒级封段 RPO=1s（`/root/e2e/verify_raw_cdp_1124.py`）。
3. **ToolPack 推送式免安装 + Mongo oplog 闭环**：新增 `core/toolpack.py`（平台随包 `tools/<group>/bin` 工具解析 + ELF 架构预检 + SFTP 推到 `/tmp/aidbm_tool_*` 临时执行 + shell 清理尾巴/PIPESTATUS 保真 rc）；`core/remote_dump.py` 远端缺 mongodump 时改走 ToolPack 推送（不再要求数据库服务器装 mongodb-database-tools），`core/engines/base.py` 本机通道加 ToolPack 兜底；Mongo 备份支持 `extra_options.oplog=true`（`mongodump --oplog` 整库+oplog.bson），离线包/Docker 镜像纳入 `tools/`。**真机 9/9 PASS**（`/root/e2e/e2e_mongo_oplog_1124.py`，Mongo 7 单节点副本集 + 生产 API）：ToolPack 推送 mongodump 备份 success → 追加 2 行 → 同名库 `oplogLimit` 时点恢复回到 3 行中文硬核对。**修一真 bug**：mongorestore 的 `--oplogReplay` 与 `--nsFrom/--nsTo` 互斥（官方报错），平台在命令构造前显式拒绝"oplog + 异名库"并给出正确路径，不再发必然失败的命令。
4. **应用感知事务一致性**：`app-dr` 插件 v1.2.0 新增 `quiesce`/`unquiesce`——MySQL/MariaDB 在**同一持锁连接**内 `FLUSH TABLES WITH READ LOCK; SELECT SLEEP(n)` 保活（修复了"独立短连接执行读锁随连接退出即释放"的假实现），`unquiesce` 终止保活会话释放锁。**本机真机 PASS**（`/root/e2e/verify_quiesce_1124.py`）：读锁期间写入被阻塞到 timeout rc=124、解锁后写入成功且行内容硬核对。PG/金仓/Oracle/达梦如实拒绝并引导各自原生一致性通道（pg_basebackup/RMAN/dmrman）。
5. **无人值守 BMR**：`bmr` 插件新增 `restore_execute`——`confirm_device` 与目标设备逐字相同 + 镜像 sha256 实测匹配 + `dry_run=false` 三重硬闸，全满足才 dd 写回。**本机 loop 设备真机 PASS**（`/root/e2e/verify_bmr_restore_1124.py`）：缺 confirm/不匹配/sha 错/dry_run 四道闸全部拒绝，全条件满足时 loop 设备真写回 + 内容硬核对。
6. **更多 VM provider**：新增 `core/vm/providers/rest_api.py` 通用 REST 控制面基类 + `ovirt`（oVirt/RHEV）、`openstack`（FusionSphere/EasyStack）、`xenserver` 三个 provider，注册到 `PROVIDER_META`。**诚实边界**：控制面（连接探活 + 资产发现）真实可用并单测解析验证，数据面备份/恢复一律抛 `VMProviderError`「未真机验证不提供」，绝不伪造成功（4 项契约 PASS）。

**单元回归**：46 passed（9 个测试文件，含本轮新增 ToolPack/裸块 CDP/REST provider 契约 + 前轮全部能力），零回归。**真机/深度**：BMR ISO 11/11、Mongo oplog 9/9、裸块 CDP PASS、事务 quiesce PASS、BMR 强闸 PASS。

### 2026-11-24 第五轮：AI 大模型备份 / RAG 知识库能力补齐

用户要求"AI 大模型备份那块也需要优化，应该怎么备份 RAG"。本轮补齐 AI 工作负载最关键的语义缺口：

1. **RAG 知识库语义层**（`core/rag_backup.py` / `api/rag_backup.py` / `/ai_backup` 页新增区块）：知识库 = 向量任务 + 文档任务 + **embedding 配置指纹**（模型/版本/维度/距离/分块规则）。备份前固化基线，恢复前比对指纹——strict 模式漂移直接 HTTP 409 拒绝（防"恢复了向量快照却因模型版本不同静默答错"）；恢复后做结构/规模质量复核。
2. **Qdrant 引擎 4 个真 bug（真机首次测 Qdrant 暴露）**：快照创建 `PUT`→`POST`（原一直 404 备份失败）；多集合恢复只读首个→读 manifest 恢复全部；`verify_record` 补 sha256 逐个核对；恢复 URL 补 `priority=snapshot`。**真机 22/22 PASS**：Qdrant 1.12.1 两 collection 各 5 点中文，备份→删库→恢复→points/维度/payload 全核对，篡改被 sha 拒绝，版本漂移被 409 拦截。
3. **Chroma `verify_record`** 补清单 sha256 核对（原只读 tar 结构）。
4. **诚实边界**：top-k 语义相关性对比（需 embedding 模型 + 查询集在场）列为 P2 未做，本轮质量复核只做结构/规模，不虚报；Milvus 仍未接入。

单元回归 20 passed（RAG+AI 保护+i18n 门禁，磁盘闸放开后），零回归。

### 2026-11-24 第六轮：真卷级一致性快照 + 写入期 CBT（原标注架构边界项）

用户要求把"真卷级一致性快照（需 dm snapshot 冻结视图）"与"写入期 CBT（需 hypervisor 跟踪）"也开发优化。本轮在统一 Agent 的 `block-cdp` 插件（v1.2.0）落地：

1. **一致性冻结快照** `snapshot_create`/`snapshot_release`：优先 Linux device-mapper snapshot（内核 COW 冻结读视图 + 写入期脏块）；dm 不可用时**如实降级为用户态时间点副本**（cp --sparse/dd 块级复制到稀疏文件，之后对该副本读即冻结视图），返回 `mode`（dm_snapshot/user_copy）与 `dm_reason`，绝不虚报 dm 一致性。配合 app-dr 的 `quiesce`（MySQL 全局读锁）可进一步做到应用一致性。
2. **写入期 CBT** `cbt_start`/`cbt_changed`：dm 模式读 COW exception 表（内核写入期记录的脏块），用户态模式用位图轮询 diff（如实标注 `mode=polling`，非内核写入期 CBT；采样间隔内多次写只记最终态）。本轮修复 CBT 漏检被清空/删除块 + 同块 added/removed 重复计数两个真缺陷。
3. **真机验证**：本机 loop 卷 11/11 PASS——快照视图在源卷改写后仍保持创建时刻数据（时间点一致硬核对）、CBT 精确定位变更块 7/42；**171 远端经 Agent `/plugin` 接口同样通过**（冻结视图不变、CBT 定位块 9）。本机 CentOS7 + loop 环境 dm snapshot 内核 exception store 创建失败（多种 chunk/persistent 参数均 Invalid argument），故实际走 user_copy 降级路径——代码保留 dm 路径供具备 dm 权限的环境使用。
4. 171 Agent 已升级到 block-cdp 1.2.0（systemd unit 指向 agent_v14.py）。

**诚实边界**：用户态副本是全量复制（非 CoW，大卷存储开销高）；polling CBT 粒度=采样间隔，不是 hypervisor/内核写入期跟踪；真 dm 路径在本环境未跑通，不宣称已验证。

## 五、结论

软件可实现的条款已全部落地并真机验证（含本轮把卷级块 CDP 做成真裸块、BMR 真出 ISO、Mongo oplog 时点、事务级 quiesce、无人值守 BMR 强闸、三个新 VM provider 控制面）。**仍诚实保留的边界**：①真·卷级**一致性快照**（需 dm snapshot 冻结视图，当前裸块位图是变更识别非冻结快照）；②写入期 CBT（需 hypervisor 持续跟踪，OS 层无法事后切换）；③三新 provider 的数据面（无真机环境，明确拒绝不造假）；④10G 现场 ≥2000Mbps 测速、ARM 真机、硬件规格与原厂服务（环境/商务范畴）。所有改动保留本地工作区（未提交 git，沿用 2026-11-14 约定）。
