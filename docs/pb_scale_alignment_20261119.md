# AIDBM 对《企业级 PB 级备份与恢复系统》方案的吸收与边界

> 来源：用户提供的设计方案（Go 控制面 + Rust 数据面、PB 级一体机、NetApp/Isilon/Dorado 快照差量、QUIC/BBRv3 跨中心复制、NBD/iSCSI Live Mount 净室沙箱、SBT 2.0）。
> 日期：2026-11-19　版本：v2.4.3

## 一、前提差异（决定了哪些能吸收、哪些不能）

| 维度 | 方案假设 | AIDBM 现实约束 |
|---|---|---|
| 形态 | 软硬一体机，自有数据面 | 纯软件平台，部署在客户既有服务器/容器 |
| 语言栈 | Go + Rust（可写内核旁路、io_uring、QUIC） | Python 单体 + APScheduler，PyInstaller 离线打包 |
| 客户端 | 可部署 Agent / 内核模块 / SBT 库 | **零客户端安装**（客户库服务器不许装任何东西） |
| 网络 | 高 BDP 专线，QUIC+BBR+FEC | 内网 / 单向网闸 / 摆渡盘，SSH + SFTP 通道 |
| 保护对象 | PB 级文件与虚拟机 | 数据库（主）+ 文件 + 对象存储 + 虚拟机 |

结论：**架构形态类能力（Rust 数据面、SBT、Live Mount、块级 CDP）不跟随**；**算法与策略类能力（抖动调度、分块去重、熵/卡方检测、内核调优、DAG 编排、带宽仲裁）可以吸收**。

## 二、本轮已落地（v2.4.3，均带单测）

| 方案条款 | AIDBM 落地 | 代码 / 验证 |
|---|---|---|
| §5.1 确定性抖动调度（CRC32(UUID) % 300 摊平洪峰） | `_schedule_jitter_minutes()`：偏移 = CRC32(任务标识) % 窗口，**确定性**（重启后同槽位，运维可预期）；只对「分钟位是单一数字」的 cron 生效，通配/列表/范围/跨小时保持原样；默认关闭，`schedule_jitter_enabled` 开启，`schedule_jitter_seconds` 配窗口 | `core/scheduler.py`；`tests/test_schedule_jitter.py` 16 项 |
| §7.1 熵 + 卡方双判据识别密文 | 熵 > 7.95 时追加字节分布卡方检验，χ²∈[195,315]（自由度 255 的 ±2.6σ）标注「分布均匀，符合密文特征」，偏离则如实说明；作为量化证据而非独立阈值（高熵本身已告警） | `core/backup_anomaly.py` `sample_chi_square()` |
| §7.1 FastCDC 可变长分块（Gear hash + 归一化掩码） | `core/fastcdc.py`：纯 Python 实现（离线无新增原生依赖），min/avg/max 约束、内容定义边界；`estimate_reuse()` 对比「固定块 vs 可变长块」跨版复用率 | `tests/test_fastcdc.py` 8 项（实测：头部变长插入场景可变长块复用率 >70%，固定块 <30%） |
| §3.2 内核 / IO 调优 | `core/host_tuning.py`：只读检查 8 项 sysctl、逐盘 IO 调度器（NVMe→none / HDD→mq-deadline）、io_uring 可用性、挂载 noatime、磁盘水位；给出可直接复制的命令，**平台绝不自动改系统参数** | `GET /api/host-tuning`、`/api/host-tuning/recommend`；存储管理页「主机内核 / IO 调优诊断」卡片；`tests/test_host_tuning.py` 6 项 |
| §5.1 全局带宽 Max-Min 公平仲裁 | `core/bw_broker.py`：全局配额 `global_bandwidth_kbps` 按权重水填充分配，需求低于份额的任务先满足、剩余额度留在池中、单任务有 64 KB/s 保底、5s 重算、120s 无活跃自动剔除；未配置时完全沿用任务级限速（**零行为变化**） | `core/engines/base.py` 的 `bandwidth_limit` 统一接入（所有引擎继承）；`GET /api/bw-broker`；设置页「全局出口带宽配额」输入、存储页「并发带宽分配」卡片；`tests/test_bw_broker.py` 12 项 |
| §3.1 + §4 分块压缩内容寻址存储 | `core/chunk_store.py`：FastCDC 切块 → **逐块独立压缩**（zstd，回退 zlib）→ 按内容哈希寻址落盘 → 有序 manifest 可重建原文件；`diff_manifests()` 给出跨版本增量块；自适应分块参数（低熵大文件不会被切成"一整块"）；块缺失/被篡改时如实失败（逐块定位） | `GET /api/chunk-store/stats|manifests`、`POST /api/chunk-store/estimate|store|restore`；存储页「分块存储」统计；`tests/test_chunk_store.py` 11 项 |

### 为什么要 FastCDC（真实教训 + 实测修正）

平台此前的块级去重账本用**固定 4MB 分块**：产物被 gzip 重新打包后头部时间戳变化，其后所有字节整体移位，固定边界全部错位，跨轮命中率趋近 0（2026-10-26 实测）。

**但 2026-11-19 的对照实验修正了这个结论的适用范围**：同一份数据两次 gzip 打包（只有头部 mtime 4 字节不同、deflate 流完全相同）时，固定块复用 96.3%、FastCDC 80% —— 固定块反而更好，因为它只丢第一块。

真正的结论是：**整包压缩的产物，任何跨版本复用都会失效**（源数据一变，压缩流大范围重编码）。要发挥内容定义分块的价值，必须**先分块、再逐块压缩**。这正是 `core/chunk_store.py` 的设计，实测数据：

| 场景（真实产物） | 结果 |
|---|---|
| 16 MB PG WAL 段 → 分块压缩存储 | 5 块，压缩比 0.209，耗时 4.6s |
| 同一文件二次存储 | **新增 0 块**（内容寻址全复用） |
| 中间改 64 KB 后再存储 | 只新增 **1 块**，复用率 80%，增量仅 906 KB / 总量 3.5 MB（省 71%） |
| 由 manifest 还原 | sha256 一致、逐字节相等 |
| 篡改/删除单个块 | 还原如实失败并定位到块（`corrupt_blocks` / `missing_blocks`） |
| 51 MB 真实备份产物：整包 vs 分块压缩 | 3.63 MB vs 3.83 MB，**额外开销 5.7%** |

即：用 5.7% 的体积代价，换到"跨版本只传变化块"的能力（对齐方案 §4 的密文块直通前提）。

### 备份产物自动块化入库（任务级开关）

任务高级选项勾选「备份产物块化入库」后（`extra_options.chunk_store_enabled`），
备份成功即在**后台**把产物分块、逐块压缩写入容器存储并回写
`backup_records.chunk_manifest_id`；产物被保留策略清理后恢复入口自动回填。
默认关闭；`chunk_store_max_mb`（默认 512，显式 0 = 不限制）超过阈值跳过，
避免拖垮大库备份；转存异常只写 WARNING，绝不影响备份结果本身。

真实闭环验证（任务 732，MySQL 逻辑备份）：
备份 success（857 B）→ 日志「记录 456 已转存（container）：1 块」→
`chunk_manifest_id=20261120095208_12b9cf40` → 块存储 blocks_indexed 8→9。

### 产物被清理后的自动回填（块级可重组）

`backup_records.chunk_manifest_id` 记录块清单：原产物被保留策略清理后，恢复入口
（`run_restore_now` 统一入口，覆盖 API/调度/演练）会自动把清单还原成本地缓存文件
再走正常恢复，引擎侧完全无感。块缺失/损坏时如实失败并保留"产物不存在"的原始
报错（不伪装成功）。

实测（真实记录 321，51.2 MB 达梦 dmp）：
- 分块压缩存储：8 块 / 3.74 MB（ratio 0.07）；
- 移走原产物后调用恢复入口 → 自动回填，**回填文件 sha256 与原产物一致**；
- 原产物恢复原位，块存储占用 3.6 MB。

### 调度器单实例租约（§1 Etcd 选主的等价物）

调度器是"进程内 APScheduler + 元数据库"。同一元数据库被两个实例接上
（容器滚动更新重叠、主备机挂同一个 PG 元库）时，两边都会注册同一批定时任务
→ 同一时刻重复备份。

`core/lease.py` 用元数据库实现带过期时间的租约，`start_scheduler()` 接入：
- 抢到租约者才是 leader（注册任务），其余进入 standby（不注册，每 30s 重试接管）；
- leader 每 20s 续租（TTL 60s），续不上则**暂停本实例全部定时任务**（防双活）；
- 进程崩溃 → TTL 到期 → 备实例自动接管；正常退出 `stop_scheduler()` 主动释放，
  让重启的新进程立即接手，不必等 TTL；
- 单实例零行为变化（总是抢到）；租约模块异常时"按原样启动"，不让运维因为
  租约故障彻底不调度。

**两层防护**：同机多进程由既有 `instance/app.lock` 拦截（实测第二进程直接退出并
记录 WARNING）；租约管的是**跨机 / 容器多副本**（SQLite 无法跨机共享，配 PG 元库时生效）。

### Append-Only 容器封箱（§3.1 的落地形态）

`chunk_store` 是"一块一个文件"的布局：块平均 512KB~8MB，1TB 备份会产生上百万
小文件，HDD 上全是随机写与 inode 压力。`core/container_store.py` 把块**追加**写进
固定上限（默认 64MB）的容器，写满即封箱（`.sealed`）开新箱；块位置记在索引
（`fp → cid/offset/size`），读取按偏移定位、连续块可一次预读。

实测（51.2MB 达梦 dmp，容器上限压到 1MB 以触发滚动）：
- 8 块 → **4 个容器文件**（blob 模式是 8 个小文件）；64MB 上限下为 1 个容器；
- 还原 sha256 一致、逐字节相等；
- 同内容二次存储 **0 新增块**，容器体积不增长；
- GC 只清索引条目、容器数据不搬移（Append-Only 的代价如实报告可回收箱数）。

### 块级 GC（否则块存储只增不减）

`chunk_store.gc()` 先全量扫描 manifest 收集引用集，再比对 blobs 目录回收孤儿块；
**默认 dry_run 只报数**，运维确认后才实删；损坏的 manifest 跳过并计数（不会因
单个坏文件误删别人引用的块）。`POST /api/chunk-store/gc` 暴露该能力。

平台此前的块级去重账本用**固定 4MB 分块**：产物被 gzip 重新打包后头部时间戳变化，其后所有字节整体移位，固定边界全部错位，跨轮命中率趋近 0（2026-10-26 实测）。FastCDC 用内容定义边界，改动只影响局部块——单测里用「头部变长插入」精确复现了该场景。

## 三、已有等价能力（方案里写了，但 AIDBM 早已具备）

| 方案条款 | AIDBM 对应实现 |
|---|---|
| §5.2 DAG 工作流引擎 | `core/runbook.py`（8 类步骤：备份/恢复/行数校验/等待/通知/容灾切换/回切/测试克隆，含审批闸与 RTO 计时）；数据迁移 `core/db_migrate.py` 阶段编排（precheck→migrate→objects→verify→report） |
| §4 跨中心复制（部分） | `core/pool_replication.py` 池复制链（A→B→C，串行/并行、级联中断、副本取回）；`core/ferry_inbox.py` 摆渡盘收件箱（单向网闸场景） |
| §4 增量复制（部分） | 备份链路断点续传（`sftp_pull_resumable`）、rsync delta 拉取（MySQL 物理通道）、文件指纹增量与源端去重（实测节省 95.5%） |
| §6 恢复演练与安全检测（部分） | `core/drill.py` 真实恢复演练（评分/RTO/RPO）、`core/backup_anomaly.py` 体积突变+熵+结构三查、不可变备份四层防护 |
| §5.1 带宽控制（部分） | 任务级 `bandwidth_limit` + 限速时段 `rate_limit_window` |

## 四、明确不跟随（附理由）

| 方案条款 | 不跟随理由 |
|---|---|
| Go 控制面 + Rust 数据面重写 | 与 PyInstaller 离线单体交付形态冲突；当前瓶颈是数据库客户端/网络，不是 Python 本身 |
| §7.4 `libobk-sbt` Oracle SBT 2.0 动态库 | 需在 Oracle 服务器安装 `libobk.so` 并改 `allocate channel ... parms 'SBT_LIBRARY=...'`，**违背零客户端安装**；现有 RMAN 走 SSH 调用数据库自带工具，能力等价 |
| §6 Live Mount（NBD/iSCSI + RoW 零拷贝） | 需内核模块与网络块设备导出，客户环境不可控；现有「克隆服务 + 真实恢复演练」已覆盖分钟级拉起 |
| §6 netns + QEMU 净室沙箱 | 需 KVM 与 root 级网络命名空间，与「零 agent、可与数据库同机部署」冲突；现用临时库/异名 schema 隔离（MySQL/PG/达梦/金仓/Oracle 均已实测） |
| §4 QUIC + BBRv3 + Reed-Solomon FEC | 需 Rust `quinn` 与内核 BBR 模块；离线包无法保证目标内核支持。等效替代：并发分片拉取 + 断点续传 + 带宽限速 |
| §3.1 NVMe/HDD 分层与 64MB Sealed Container Append-Only | 属一体机硬件与存储引擎形态；AIDBM 通过 `storage_targets` 支持 local/MinIO/S3/磁带，由客户既有存储承担 |
| Cuckoo Filter 内存索引 | Python 侧现有 `dedup_index` + SQLite 已满足规模；引入需原生扩展，离线打包风险高 |
| §2 NetApp SnapDiff / Isilon Changelist / Dorado diff_session | 需要厂商阵列与专用 API 凭据，当前无真机无法验证——**不写「已支持」**。现有替代：SSH 只读指纹清单（`find+sha256sum+stat`）+ 文件级源端去重 |

## 五、后续路线（按投入产出排序）

1. **P1 NAS 快照差量适配层**：先做能力探测（阵列型号/API 可达性）与接口契约，拿到真机再实现，避免空承诺。
3. **P1 分块存储接入备份主链路（按任务可选）**：把 `chunk_store` 作为 `storage_backends` 的一种「块存储」后端，任务产物直接以块形态落盘，跨版本只写新块；需要先解决纯 Python 吞吐（16MB/4.6s ≈ 3.5MB/s，可用 C/Rust 扩展，但必须随离线包分发），并补齐块级 GC。
4. **P2 调度 DAG 可视化**：把 runbook 步骤依赖在页面上以图呈现（现为列表 + 顺序执行）。

---

**诚实声明**：本文列出的「已落地」项均有对应单测与页面入口；「不跟随」项不做接口伪装。方案中依赖专有硬件或厂商 API 的部分，AIDBM 在拿到真机前不会宣称支持。
