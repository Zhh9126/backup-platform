# 对标鼎甲 DBackup V8 能力补齐 & 离线环境部署 设计方案

> 版本：v1.0（2026-09-06）
> 对标材料：《DBackup 产品技术白皮书 汇总版 for V8.0》（84 页）
> 现状基线：本平台 2026-09-04 主干（v1.3.3，commit 9d3c4a8 之后）
> 目标：① 明确与行业标杆（鼎甲 DBackup、Veeam、Commvault、Rubrik 同类能力）的差距；
> ② 输出可实施的补齐设计方案；③ 支撑**完全离线（air-gapped）环境**下的多库型
> 备份恢复、数据同步与迁移交付。

---

## 一、对标总览：能力矩阵

图例：✅ 已具备（实测）｜🟡 部分具备（需增强）｜❌ 缺失

| 能力域 | DBackup V8 能力 | 本平台现状 | 差距 | 优先级 |
|---|---|---|---|---|
| **备份类型** | 完全/增量/差异/合成备份 | ✅ 全/增/差/组合(mixed) | 合成备份缺失 | **P0** |
| **重复数据删除** | 块级重删 + 分布式重删集群 | 🟡 dedup_index 全局重删索引（备份集级） | 变长分块/源端重删/重删比报表 | P1 |
| **数据加密** | 落盘加密 + 传输加密 | ✅ crypto_pool 加密落盘、TLS Web | 密钥托管（KMS）增强 | P2 |
| **数据压缩** | 多级压缩 | ✅ zstd/gzip 多级 | — | — |
| **保留策略** | 灵活 GFS 保留 | ✅ 生命周期（storage_tier/backup_sets） | GFS 模板化（周/月/年） | P1 |
| **数据库保护** | Oracle/MySQL/SQLServer/PG/DB2/Mongo/Informix/HANA/达梦/Caché | ✅ MySQL/MariaDB/PG/Oracle/SQLServer/达梦/金仓/Redis/MongoDB/文件 | DB2/Informix/HANA/Caché（低频，暂缓） | P2 |
| **连续日志保护** | Oracle CLRP / MySQL CLRP | 🟡 rt_cdc 准 CDP + MySQL binlog 同步 | Oracle 日志连续捕获；统一"日志备份计划" | **P0** |
| **恢复能力** | 即时恢复/细粒度恢复/跨平台恢复 | 🟡 库级/实例级恢复、恢复演练、克隆 VDB | 表级/对象级恢复标准化；即时恢复(BMR→VM) | **P0** |
| **数据库复制** | Oracle/MySQL/SQLServer 实时复制到备端 | 🟡 sync_tasks(MySQL binlog)、rt 链路 | Oracle/PG/达梦 复制；统一复制管理视图 | P1 |
| **CDM 副本管理** | 合成备份+快照克隆+挂载即时使用 | 🟡 VDB 克隆（MySQL/PG 逻辑克隆） | 块级快照克隆（真实 CDM） | P2 |
| **虚机/云平台** | VMware/FusionCompute/HCS/OpenStack/CAS/XenServer | ❌ | VMware(CBT) + KVM(libvirt) 优先 | P1 |
| **文件/NAS** | 文件备份 + NDMP | 🟡 file_backup（本机/SSH） | NDMP；海量小目录加速 | P2 |
| **对象存储保护** | 对象存储数据备份 | ❌（对象存储仅作为备份目标） | S3 兼容源数据保护 | P2 |
| **D2T 磁带** | 备份到磁带 | ❌ | 磁带/虚拟磁带库可插拔目标 | P2 |
| **D2C 上云** | 备份到对象存储/云 | ✅ L2-S3（MinIO/S3 兼容） | 断点续传、生命周期沉淀 | P1 |
| **远程复制** | 异地备份服务器间复制 | 🟡 storage_targets 多目标 + rt 链路 | 站点级复制策略、带宽窗口限速 | P1 |
| **CDP 持续保护** | 文件级/卷级 CDP | 🟡 rt_cdc 文件级 | 卷级 CDP（RPO≈0） | P2 |
| **迁移/同步** | —（DBackup 弱项，DTS 类产品能力） | ✅ db_migration_plans（DTS 一站式）、sync、hetero 异构转换、data_compare | 离线环境适配（见三章） | **P0** |
| **用户/多租户** | 用户管理体系 | 🟡 单管理员 + 角色 | 多租户/部门级权限/审计报表 | P1 |
| **监控告警** | 运行监控 | ✅ inspection + ai_alert + rt_timeline | SLA 达成率报表、备份窗口热力图 | P1 |
| **开放集成** | Webhooks/REST | 🟡 API 全量 + api_tokens | Webhooks 事件推送、SNMP/邮件模板化 | P1 |
| **部署** | 备份服务器+存储服务器+Agent 三件套 | 🟡 all-in-one + deploy 推送 | **离线交付包**、Agent 免安装化 | **P0** |

**结论**：平台在"库型覆盖、迁移/同步/异构、AI 巡检、恢复演练"上已达到或超过
DBackup 同类水位；核心差距集中在 ①合成备份 ②恢复细粒度/标准化 ③虚机/无代理
备份 ④**离线部署交付** 四条主线，以下按 P0 → P1 → P2 展开。

---

## 二、P0 详细设计（本期实施）

### 2.1 合成备份（Synthetic Full：一次全备，永久增量）

**对标**：DBackup 2.3「合成备份」——首次全备后仅做增备，由存储端合成出任意时点
全量点，兼具"备份窗口短 + 恢复速度快 + 存储省"。

**现状基础**：已有 `backup_sets`（合成全量/去重/链 chain_id/chain_status）与
`dedup_index` 数据模型，但缺合成执行器与调度策略。

**设计**：

```
┌────────┐  增备链    ┌──────────────┐   合成    ┌─────────────────┐
│ 源库    │ ────────▶ │ L1 暂存区     │ ────────▶ │ backup_sets 合成 │
│ (agent) │  每日 incr │ full+incr*N  │  (存储端)  │ 生成虚拟全量点    │
└────────┘           └──────────────┘           └─────────────────┘
```

1. **数据模型**（沿用/扩展 `backup_sets`）：
   - `chain_id`：一条合成链；`set_type`: full / incr / synthetic_full
   - `parent_set_id`：增量依赖的父点；`object_key`：物理位置
   - 新增列：`synth_from`（合成输入 incr 区间 [begin_rcid, end_rcid]）、
     `pin_until`（合成点保留期限）
2. **合成执行器** `core/synthetic.py`：
   - MySQL：基于 `xtrabackup --prepare`（增量页面合并）在 L1 暂存目录合成物理
     全量点；逻辑链（mysqldump binlog 位点）用 `mysqlbinlog --start-position`
     重放合成逻辑点（只合成元数据点 + 保留增量日志，恢复时统一回放）
   - PostgreSQL：`pg_receivewal`/`pg_basebackup`+WAL 归档合成（物理）；逻辑链用
     pg_restore 增量目录合并（自定义格式仅记录点）
   - 达梦/Oracle：dexp/dimp 链合成走逻辑点合并；Oracle RMAN 增量合并
     `RECOVER ... NOLOGS`（物理）
   - 合成过程**只读备份链、不碰源库**（存储端合成，这是与重跑全备的本质区别）
3. **调度策略**：任务表新增 `synthetic_policy`：
   `{"mode":"once_full_forever_incr","synth_cron":"0 2 * * 6","verify":true}`
   —— 周末低峰合成校验点，合成后自动 `verify`（校验和 + 试恢复到临时实例）
4. **恢复**：恢复入口无感知——选择任意合成点即等价全量点；链断裂（增量缺失）
   时恢复向导标红并给出最近可用点
5. **UI**：任务详情新增「备份链」可视化（full→incr*→synthetic 时间轴，链健康
   着色）；仪表盘新增「合成节省的备份窗口时长」指标

**验收**：MySQL 物理链 1 全备 + 7 增量 → 合成点恢复行数与源一致；合成期间源库
QPS 零影响（无源端连接）。

### 2.2 恢复能力标准化（对标 DBackup 2.10.3.2 + CDM 即时恢复）

**现状**：恢复已支持 实例级/库级/跨主机/克隆 VDB，但表级与对象级恢复散落在
restore_extras（PG 对象级已有），需要标准化为一个「恢复向导」。

**设计**：统一恢复向导（前端 4 步 + 后端策略引擎）

| 层级 | 范围 | 后端实现 | 说明 |
|---|---|---|---|
| L1 实例级 | 整实例 | 已有（full_instance） | BMR 场景 |
| L2 库级 | 单库/多库 | 已有（target_db + USE 剥离） | 本期已修复闭环 |
| L3 **表级** | 指定表 | MySQL：`mydumper/myloader` 或 dump 解析抽取表段；PG：pg_restore `--table`；Oracle：impdp `TABLES=`；达梦：dimp `TABLES=` | **新增**：备份产物自动解析表清单（入库 `backup_objects` 表）供前端勾选 |
| L4 **行级/时点** | WHERE 条件 / PITR | MySQL：binlog PITR（已有位点）；Oracle：RMAN until time / CLRP；PG：WAL 回放 | 复用恢复演练与 rt 模块 |
| L5 即时使用 | 挂载即用 | 已有：克隆 VDB（clone_requests） | 与 CDM 合流 |

新增表 `backup_objects(record_id, obj_type, obj_name, schema, size, rows_est)`：
备份完成后自动扫描产物注册对象清单（mysqldump --dry-run 级解析 / pg_restore -l /
impdp SQLFILE），恢复向导直接勾选，避免"恢复完才知道内容"。

### 2.3 离线部署体系（★ 本期重点，详见第三章）

---

## 三、离线环境部署专项设计（air-gapped）

**目标场景**：客户内网与互联网物理隔离（或仅单向通过），需在离线环境完成：
平台部署 → 多类数据库的备份/恢复 → 数据同步（binlog/WAL/日志）→ 数据迁移
（同构/异构）。全程不依赖公网 PyPI/DockerHub/NTP/AI API。

### 3.1 总体部署形态

```
离线内网
┌────────────────────────────┐    ┌──────────────────────┐
│ 管理区                      │    │ 数据库区（各网段）      │
│ 备份服务器(平台 all-in-one)  │◄───┤ MySQL/PG/Oracle/     │
│  - Web/API/调度/元数据       │SSH │ 达梦/SQLServer/金仓   │
│ 存储服务器(MinIO集群/大容量盘)│    └──────────────────────┘
│ 私有制品库(wheelhouse+镜像)  │         ▲ agentless SSH
│ 迁移Staging区(数据落地中转)   │         │ 或瘦Agent
└────────────────────────────┘    ┌──────────────────────┐
        ▲ 定期同步(单向摆渡)        │ 灾备区(可选)           │
┌─────────┐                      │ 异地存储/对象存储       │
│ 互联网区 │ 摆渡盘/单向网闸        └──────────────────────┘
│ 版本下载 │
└─────────┘
```

原则：**控制面集中（备份服务器）、数据面就近（SSH 到库端执行原生工具）、
制品离线前置（一个自包含离线包解决全部依赖）**。

### 3.2 离线交付包（One-Stack Offline Bundle）

单文件 `backup-platform-offline-<ver>.tar.gz`（目标 ≤ 2GB），结构：

```
offline-<ver>/
├── install.sh                     # 一键安装器（离线）
├── images/                        # Docker 镜像（可选容器化部署）
│   ├── backup-platform_<ver>.tar  # 平台主镜像（3.12-slim，依赖已烘焙）
│   ├── minio_<ver>.tar            # 对象存储
│   └── registry_<ver>.tar         # 私有 registry（可选）
├── wheelhouse/                    # 离线 PyPI（pip --no-index --find-links）
│   └── *.whl                      # 全依赖（含 pymysql/psycopg2/oracledb/
│                                  #   minio/paramiko/apscheduler/…）
├── clients/                       # 数据库客户端工具包（按 OS×ARCH 分目录）
│   ├── el7_x86_64/
│   │   ├── mysql_client.tar.gz    # mysqldump/mysql/xtrabackup24/8
│   │   ├── pg_client.tar.gz       # pg_dump/pg_restore/psql(14+)
│   │   ├── dm_client.tar.gz       # dexp/dimp/dmrman/disql
│   │   ├── oracle_instantclient/  # instantclient + imp/exp/expdp/impdp
│   │   ├── mssql_client/          # sqlcmd/bcp (msodbcsql)
│   │   └── kingbase_client/       # sys_dump/sys_restore/ksql
│   ├── el8_x86_64/ …  kylin_v10/  # 国产 OS 适配
├── app/                           # 平台源码包（非容器部署方式）
├── tools/
│   ├── sync_offline.sh            # 增量离线升级
│   └── healthcheck.sh             # 环境预检（端口/内核参数/时钟/磁盘）
└── manifests/version.yaml         # 版本清单 + sha256
```

### 3.3 数据库客户端工具：推送式免安装（关键设计）

离线环境数据库服务器往往**不允许安装任何软件**。复用并强化现有
「xtrabackup 二进制推送」机制为通用 **ToolPack 推送**：

1. `clients/` 中每个客户端包做成**自解压绿色包**（tar.gz，仅解压到目标机
   `/tmp/bk_tools/<hash>/`，不写系统目录、不注册服务）
2. 任务执行时（`core/remote_dump.py` 扩展）：
   - SSH 探测目标机 PATH 是否有 `mysqldump/dexp/pg_dump...`
   - 无 → 按「目标 OS 版本 × 数据库版本 × 架构」从备份服务器 push 对应
     ToolPack（sftp + 解压 + 校验 sha256）→ 以绝对路径执行 → 任务结束可选清理
   - ToolPack 缓存标记文件避免重复推送
3. 与 `extra_options.tool_path` 兜底逻辑合并：**远端探测 → ToolPack 推送 →
   tool_path 指定 → 本机回退**，四级逐级降级
4. 现网验证：133 达梦（dexp/dimp 位于 /dm/dbms/bin）、140 MySQL
   （/opt/database/bin）已走通该模式的简化版

### 3.4 平台本体离线安装（install.sh 设计）

```bash
# 两种形态二选一，install.sh 自动探测 Docker 可用性
./install.sh --mode docker   # 形态A：docker load 镜像 → compose 启动（推荐）
./install.sh --mode native   # 形态B：python venv 离线装（wheelhouse）+ systemd
```

- 形态 A：`docker load -i images/*.tar` → `docker compose up -d`
  （compose 文件内置：平台 + MinIO + 健康检查 + 数据卷 /data）
- 形态 B：`python -m venv .venv && pip install --no-index --find-links
  wheelhouse -r requirements.txt` → `systemd` 单元 `backup-platform.service`
  （继承 start.sh 的环境，含 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 类环境隔离项）
- 元数据迁移：`app/upgrade.py --from <old>` 支持 sqlite meta 导入导出
- 预检（healthcheck.sh）：磁盘 ≥ 预留、内网端口（8080/9000/22）连通矩阵、
  Python/Docker 版本、时钟源（离线 NTP：指向内网 NTP 或宣告偏移容忍）

### 3.5 离线环境下的数据同步（对标 sync/CDP）

| 场景 | 离线适配设计 |
|---|---|
| MySQL 主→备数据同步 | 现有 sync_tasks（binlog 位点拉取）本身离线可用；补：目标端 SQL 重放账号最小权限脚本、断点续传（位点持久化已有） |
| PG 流复制/WAL 归档同步 | 新增 wal 归档拉取模式：平台定期 `rsync/scp` 拉取 `$PGDATA/pg_wal_archive`，按 `.backup`/.partial 顺序重放（离线摆渡也支持：文件校验后入库） |
| Oracle 日志同步（对标 CLRP） | 归档日志拉取：`rman backup archivelog` → scp 拉回 → 平台登记为增量恢复点；后续可扩展 GoldenGate 类实时（P2） |
| 达梦日志同步 | `dmarch.ini` 归档目录拉取 + dmrman 增量还原（复用任务 21 的 dmrman 通道） |
| **网络单向/摆渡** | 新增「摆渡收件箱」：平台监控一个导入目录，摆渡盘放入 `*.inc.tar`（含清单+校验）自动入库登记为备份记录，进入统一保留策略与三级复制 |

### 3.6 离线环境下的数据迁移

现有 `db_migration_plans`（结构/全量/校验三阶段）已支持异构（达梦→KingBase 等），
离线适配点：

1. **Staging 中转**：跨网段迁移（源网段 ↔ 目标网段单向可达）时，平台充当
   中转：`源库 → dump 落地备份服务器 → 目标库恢复`，复用备份产物，天然可追溯
2. **迁移用客户端**：迁移执行所需 impdp/dimp/sys_dump 等同 3.3 ToolPack 推送
3. **大文件校验**：迁移前后 data_compare（已有）+ sha256 清单，离线无差异
4. **断点续迁**：迁移计划按表分片（已有 phases_json），失败从失败表续迁

### 3.7 安全与合规（离线环境强化）

- 平台与 MinIO 间 TLS（内网自签 CA，install.sh 自动签发）
- SSH 凭据加密存储（已有 enc 体系）+ SSH host key 首次锁定
- 审计：所有恢复/销毁/下载操作写入 `system_logs`（已有），新增导出报表
- 国产化：麒麟 V10 / 统信 UOS 适配矩阵进 ToolPack 目录

### 3.8 升级与回滚

- 增量离线升级包：仅含变更 wheel + 代码 diff + `migrations/*.sql`
- 升级前自动备份 meta.db 与配置；一键回滚脚本（保留上版本镜像 tag）

---

## 四、P1 设计要点（下一期）

1. **VMware/KVM 无代理备份**：VMware via vSphere Web Services（CBT 增量），
   KVM via libvirt snapshot + qcow2 外部快照导出；恢复支持挂载单盘取文件
2. **站点级远程复制**：两套平台间增量同步（仅传未同步记录，带宽窗口限速，
   断点续传），主站点故障时灾备站点可接管恢复
3. **SLA 报表与备份窗口管理**：成功率/RPO 达成率/窗口热力图；任务错峰排队器
   （同一客户端串行、全局并发上限、限速）
4. **GFS 保留模板**：日 7/周 4/月 12/年 N，模板应用到任务组
5. **Webhooks 事件中心**：备份成功/失败/恢复/到期销毁事件推送到钉钉/企微/
   自定义 URL（重试 + 签名）
6. **多租户**：部门/业务系统维度隔离（数据行级权限），操作审计导出

## 五、P2 展望

- 真实 CDM：LVM/ZFS 快照 → 块设备克隆 → 挂载即时使用（iSCSI/NBD 导出到
  测试环境），对标 Oracle Live Recovery
- 卷级 CDP（RPO≈0）：基于 LVM thin + 常量日志重放
- 磁带（LTO/虚拟带库）作为冷层目标（D2T）
- DB2/Informix/SAP HANA 连接器

---

## 六、实施路线图

| 阶段 | 周期 | 内容 | 出口标准 |
|---|---|---|---|
| M1 离线交付打底 | 2 周 | 离线包（install.sh/wheelhouse/clients/ToolPack）、健康预检 | 离线机器 30 分钟完成部署；MySQL 备份恢复闭环 |
| M2 恢复标准化 | 2 周 | 恢复向导 4 级 + backup_objects 对象清单 + 表级恢复 | MySQL/PG/达梦表级恢复各 1 例通过 |
| M3 合成备份 | 3 周 | synthetic.py + 链可视化 + 合成校验 | MySQL 物理链合成点恢复一致；源端零连接 |
| M4 同步/迁移离线化 | 2 周 | WAL/归档拉取、摆渡收件箱、迁移 Staging | 单向网络下 PG 同步 + 达梦迁移演练通过 |
| M5 P1 增强 | 4 周 | VMware/KVM 备份、站点复制、SLA 报表、Webhooks | 按模块验收 |

---

## 七、风险与依赖

| 风险 | 缓解 |
|---|---|
| 离线环境 OS/ARCH 碎片化（麒麟/统信/CentOS 混布） | ToolPack 按矩阵预编译；healthcheck 预检；现场以预检报告定制包 |
| xtrabackup/物理工具与数据库小版本强耦合 | ToolPack 带 mini 版本探测（同现有 xtrabackup 选择逻辑） |
| 大备份产物跨区摆渡耗时 | 摆渡收件箱 + 分卷 + 断点清单；优先增量/合成链 |
| 合成备份对物理工具版本敏感 | 合成前 verify 演练门禁；失败自动回退"重跑全备"策略 |
| 单点（all-in-one） | meta.db 定期随三级复制外送；存储与控制面分离部署可选 |
