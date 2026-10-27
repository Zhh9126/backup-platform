# AIDBM 对标鼎甲迪备 DBackup V8 技术白皮书差距分析（2026-10-26）

对标输入：《鼎甲迪备 DBackup 技术白皮书 汇总版 for V8.0.32567》（84 页，已全文提取研读）。
对标重点：**备份与恢复能力为核心**（用户指定），兼顾通用能力、CDM/CDP、运维管理。
诚实边界声明：AIDBM 现状全部经代码核实并给出证据；未实现的显式标注，不得对客户宣称。

---

## 一、备份与恢复核心能力对照（用户重点关注）

### 1.1 备份类型矩阵

| 备份类型 | DBackup V8 | AIDBM 现状 | 证据 | 评级 |
|---|---|---|---|---|
| 完全备份 | 全资源支持 | 全库型支持（逻辑+物理+全实例） | core/engines/ 九大引擎 | 🟢 |
| 增量备份 | 全资源支持 | MySQL XtraBackup 增量链（6/6 实测含 prepare+apply）；文件增量（mtime diff+快照规则）；SQL Server DIFF | core/engines/mysql.py、file.py | 🟢 |
| 差异备份 | 全资源支持 | SQL Server DIFFERENTIAL 实测；文件差异 | core/engines/sqlserver.py | 🟢 |
| 事务日志备份 | Oracle/MySQL/SQLServer/Mongo/Informix/Caché | Oracle 归档日志备份（archivelog_backup）、MySQL binlog 段落封存、SQL Server LOG、MongoDB oplog（时间点恢复未做） | core/rt_backup/、oracle.py | 🟡 Mongo oplog 时间点恢复缺失 |
| **合成备份** | 一次全备+永久增量，块设备快照保多时间点，可挂载即时恢复 | **已实现系统内自动合成全量**：synthesize 周期任务（默认周日 03:00）把增量链合成全量，产物可直接恢复 | core/synthesize.py、core/synthetic.py、scheduler [synthesize] job | 🟡 合成有，**无 COW 快照多时间点与挂载恢复** |

### 1.2 数据库覆盖（备份+恢复）

| 库型 | DBackup | AIDBM | 备注 |
|---|---|---|---|
| Oracle（含 PDB） | RMAN 物理/逻辑/日志、SCN 精确恢复、表级/表空间异机恢复、BCT、多通道 1-255 | 11g/19c expdp/impdp、RMAN 物理（RESTORE VALIDATE/抽取数据文件）、表级（expdp scope）、跨主机恢复 | 🔴 **SCN/时间点 PITR 未真实验证**（rman_pitr 仅脚本生成，Memory 明确不得承诺）；BCT/多通道无 |
| MySQL/MariaDB/Percona | 自研物理引擎、集群架构、容器/RDS、binlog PITR、加速应用 binlog | 逻辑/物理/全实例/XtraBackup、binlog PITR 实测（GTID 回放）、容器/RDS 场景 | 🟢 基本对齐；DDL 重试、备份完删日志、binlog 加速应用无 |
| SQL Server | VDI/VSS、Always On、日志触发条件 | FULL/DIFF/LOG T-SQL 原生、还原 VERIFYONLY | 🟡 Always On 感知无 |
| PostgreSQL 系 | 原生接口、时间点、"恢复至备份状态/还原日志"、表级 | pg_dump/pg_basebackup/PITR（133 实测）、表级、跨主机 | 🟢；"仅还原日志文件（只下载不恢复）"选项无 |
| 达梦/金仓/openGauss/磐维 | 达梦 dmbackup+集群；金仓表级 | 达梦 dexp/dimp/disql 联机物理/DM_LOGMNR、金仓 sys_dump/sys_basebackup、openGauss 全矩阵、磐维 | 🟢 **信创细节领先**（AIDBM 真机矩阵更全） |
| MongoDB | mongodump+oplog 时间点 | mongodump --archive+恢复（实测） | 🟡 oplog 时间点恢复缺 |
| Redis | —（白皮书未列） | RDB 备份 | 🟢 鼎甲白皮书未提及 Redis，我方有 |
| DB2/Sybase/Informix/SAP HANA/Caché | 支持 | 无 | 🔴 结构性空白（按客户需求评估） |
| GBase/神通/瀚高/优炫/TBase/TDSQL | 支持 | 无（但瀚高/优炫/TBase 为 PG 内核系，可复用 PG 引擎低成本接入） | 🟡 **低成本可补** |
| Exchange/Domino/SharePoint | 支持（单邮件恢复） | 无 | ⚪ 不跟随 |
| 自定义脚本通道 | 前置/后置脚本 | backup_mode=custom（SSH 通道实测 13/13）+ env_vars | 🟢 对齐 |

### 1.3 恢复能力

| 恢复能力 | DBackup | AIDBM | 评级 |
|---|---|---|---|
| 原机/异机恢复 | 全库型 | cross_host 跨主机恢复（多库型实测） | 🟢 |
| 时间点恢复 | Oracle SCN/MySQL binlog/PG/Mongo/Caché | MySQL binlog PITR 实测、PG PITR 实测（133 全链路）、达梦 PITR（待 137 复测） | 🟡 Oracle PITR 未验证；Mongo oplog 缺 |
| 表级/细粒度恢复 | Oracle 表级/表空间、单邮件 | Oracle/MySQL/PG 系表级；金仓表级 | 🟢 |
| 即时恢复（挂载） | 块设备虚拟卷挂载，RTO 分钟级 | 无块设备挂载（零安装约束）；数据库级克隆秒级拉起部分等效 | 🔴 结构性差异（见不跟随） |
| **定期异机恢复演练** | **所有库型标配**（自动恢复最新备份集+Oracle 可指定时间点） | drill 元数据级评估，**不真实恢复** | 🔴 **P0（最高优先）** |
| 恢复时自动创建实例 | Oracle 恢复作业自动建实例 | 无（目标库自动建库有） | 🟡 |
| 文件恢复增强 | 跳过已存在文件、HDFS/对象存储落地、Win/Linux 交叉恢复 | 镜像树恢复、指定路径恢复 | 🟡 HDFS/交叉恢复无 |
| Oracle 应急回迁（Live Recovery） | 应急实例接管后回迁生产（归档日志同步+切换） | 无（MySQL 侧 failback 已有，Oracle 侧无） | 🔴 P1 |

### 1.4 备份引擎通用能力

| 能力 | DBackup | AIDBM | 评级 |
|---|---|---|---|
| 重删 | 源端变长块+固定块、分布式重删集群 2-64 节点 | 存储节省/重删统计（存储管理页）、对象存储版本化 | 🟡 无源端重删与集群 |
| 数据加密 | AES/Blowfish/**SM4 国密**、传输+存储 | 任务级数据加密（有"加密任务"统计）；凭据为轻量混淆（db.py:1972 自认"非高强度加密"）；**SM4 无** | 🔴 国密缺失（政企/金融刚需） |
| 压缩 | 29 级可调 | gzip/zstd + compress_level | 🟢 够用 |
| 保留策略 | 配额/天数/全备最小个数 | retention_days/count/GFS/**keep_min 兜底+不可变锁**（鼎甲无不可变） | 🟢 领先 |
| 流量控制 | 多时间段限速 MiB/s | 任务级 bandwidth_limit（pv 管道，仅压缩路径，无分时段） | 🟡 |
| **多通道并行** | 多通道 1-255、库级并行 | **单任务多线程未实现**（Vinchin 对标遗留同款） | 🔴 P1 |
| 断网续传 | 断点续传（复制/D2C） | SSH 握手重试有，真正断点续传无 | 🟡 |
| 归档日志触发条件 | 未备份日志数量/剩余空间/比率驱动 | 无（固定 cron） | 🟡 |
| 多存储介质 | 磁盘/磁带/对象存储 17+ 云 | 磁盘/S3/磁带库 D2T（api/tape 实测）/摆渡盘 | 🟢 |
| D2C 云归档 | 云网关断点续传 | 对象存储备份（S3 桶）有 | 🟡 |

### 1.5 高阶能力（CDM/CDP/复制/两地三中心）

| 能力 | DBackup | AIDBM | 评级 |
|---|---|---|---|
| 数据库 CDP（连续日志） | Oracle/MySQL CLRP，RPO 0.2s | 日志级捕获五库型（binlog/WAL/LogMiner/DM_LOGMNR/金仓 WAL）+PITR+文件 CDP | 🟢 **信创库覆盖领先** |
| 卷级 CDP | 系统层 I/O 日志、1 秒粒度、虚拟卷挂载 | 无 | 🔴 结构性放弃（需内核驱动，违反零 agent） |
| CDM 黄金副本/克隆 | 合成+快照+挂载，多副本 | 黄金副本（迁移保护）、克隆 VDB 秒级、合成全量 | 🟡 无块设备快照挂载 |
| 数据库复制（主备切换） | Oracle/SQL Server 物理复制+切换 | MySQL 全流程（Fencing/GTID/DNS，RTO 15s 实测） | 🟡 库型互补：鼎甲 Oracle/SQLServer，我方 MySQL |
| 池复制两地三中心 | A→B→C 链式、断点续传、元数据齐套、任一池恢复、取回/源池修复 | storage_targets 多目标+TierReplicate+异地备份机+摆渡 | 🔴 无链式编排/取回语义（P1-2 站点实体化） |
| 恢复演练 | 全库型定期异机真实恢复 | 元数据级 | 🔴 P0 |
| Webhooks | 事件中心（用户/主机/存储/作业/审批等 9 类） | core/webhooks.py 事件中心（备份/恢复/克隆等） | 🟡 事件类目可扩（审批/资源） |
| RBAC | 7 角色+多级租户 | RBAC 模块（admin/操作员），无多级租户 | 🟡 |
| 日志审计/报表/标签 | 有 | 审计日志+报表（CSV/docx/pdf）+biz_system 标签 | 🟢 |
| 虚拟机备份 | 20+ 平台无代理 CBT | 4 平台 SSH 通道（ESXi/Hyper-V/libvirt/PVE） | 🟡 数量差距大，核心平台有 |

## 二、差距根因与优先级判断

备份/恢复核心层面，AIDBM 的差距集中在**四个可收敛点**（按用户"备份恢复是核心"的判断排序）：

1. **恢复演练**：鼎甲把它做成全库型标配（定期异机自动恢复最新备份集），AIDBM drill 不落地。这是"备份可信度"的最后一公里，且 AIDBM 恢复/克隆/校验/评分积木齐全。
2. **Oracle PITR 空白**：白皮书明确"查找 SCN 精确时间点恢复"，AIDBM 代码有 rman_pitr 但从未真机验证——**要么补验证，要么从产品口径删除**，不能悬着。
3. **单任务多通道并行**：大数据量场景备份窗口的直接瓶颈（Vinchin 对标遗留）。
4. **国密 SM4**：政企/金融招标硬指标，缺失即出局项。

## 三、开发迭代路线

### P0（备份恢复核心，1-2 个迭代）

| 项 | 内容 | 复用 |
|---|---|---|
| P0-1 真实恢复演练闭环 | 定期异机演练：真实恢复最新备份集到临时目标→数据校验→评分→销毁；Oracle 支持指定时间点；与中科热备对标的 P0-1 同一项，两家竞品均以此为标配 | drill 评分体系、恢复引擎、克隆通道、restore_verify |
| P0-2 Oracle PITR 真实验证 | 用 129（19c ARCHIVELOG 已开）真机跑通 rman_pitr 全链路（restore→recover→resetlogs），产出报告；顺带补 SCN 恢复入口 | 现有 rman_pitr 脚本生成、归档日志备份 |
| P0-3 国密 SM4 | ①备份数据存储加密加 SM4 算法选项（离线环境自带实现，无外部依赖）；②凭据存储从轻量混淆升级为 SM4/AES-GCM 高强度加密（迁移兼容旧 enc: 前缀） | core/db.py encrypt_secret、任务加密路径 |
| P0-4 日志备份触发条件 | Oracle：归档日志备份支持"未备份日志>N / 剩余空间<阈值"触发；MySQL：备份完删除已备份 binlog（含保留 N 天策略） | scheduler job、rt_capture_state、MySQL binlog 清理 |

### P1（1-2 个迭代）

| 项 | 内容 |
|---|---|
| P1-1 单任务多通道并行 | MySQL 单库逻辑备份并行（按表分片多连接）、文件多通道分片传输；恢复并行应用 |
| P1-2 池复制两地三中心 | storage_targets 链式编排 A→B→C、复制进度/断点续传、元数据齐套校验、任一池恢复、取回/源池修复语义（配合站点实体化） |
| P1-3 PG 内核系国产库接入 | 瀚高/优炫/TBase 复用 PG 引擎（探测+方言差异表），低成本补齐国产库清单 |
| P1-4 Oracle/SQL Server 数据库复制 | 对标"数据库复制"章节：Oracle 用 CLRP 等效（LogMiner 归档应用）+ SQL Server 日志传送式同步，备库可读+切换 |
| P1-5 恢复选项补齐 | "仅还原日志文件"（只下载不恢复）、恢复跳过已存在对象、Mongo oplog 时间点恢复 |

### P2 / 结构性取舍

- **不跟随（诚实标注）**：卷级 CDP（需内核驱动，违反零客户端安装铁律）、裸机/P2P/P2V/V2V、Exchange/Domino/SharePoint、NAS NDMP、HDFS、20+ 虚拟化平台全量清单（已有 4 核心平台按需扩）。
- **按需评估**：DB2/Informix/SAP HANA/Caché/GBase/神通（有客户订单再立项，引擎框架已插件化）。
- **P2 增强**：分布式重删（当前单机重删即可满足目标场景）、多级租户 RBAC、CDM 块设备快照多时间点（依赖设备快照能力，先以合成全量+GFS 保留多时间点替代）。

## 四、我方领先点（备份恢复维度）

1. **信创库真实矩阵**：达梦/金仓/openGauss/磐维全类型真机通过（白皮书多为清单式宣称，AIDBM 有逐库 E2E 报告）。
2. **不可变备份**：保留锁/写保护/完整性复检/删除四路拦截——白皮书无对应能力。
3. **恢复点安全检测**：熵值/体积突变/结构嗅探三查（防勒索覆写备份）。
4. **零客户端安装**：白皮书架构强依赖 Agent（"代理端安装在生产服务器上"），AIDBM 全程 SSH+SFTP 零安装——对客户是硬差异。
5. **Redis 备份**、**自定义脚本通道双模**（本机/SSH）、AI 原生告警。
