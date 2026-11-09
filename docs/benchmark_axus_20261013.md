# 对标分析：AXUS 硕讯管理软件专用备份方案（2026-10-13）

> 对标对象：《AXUS-管理软件专用备份方案报价.xlsx》（硕讯普惠型备份一体机，7000 元档，含 2 客户端授权）
> 结论先行：**本轮补齐其两项核心差异化能力（新建库自动纳管、文件镜像备份+紧急挂载）**；其余能力 AIDBM 均已覆盖或按「零安装」路线给出等效方案；AIDBM 在数据库广度、恢复校验、CDP/PITR、克隆、不可变备份等维度大幅超出。

## 1. 逐项能力对照

| AXUS 能力 | AIDBM 状态 | 说明 |
|---|---|---|
| WEB 统一管理 | ✅ 已有 | 25+ 功能页统一管理 |
| MSSQL / Oracle / MySQL 备份 | ✅ 已超越 | 另支持 MariaDB / PostgreSQL / 金仓 / 达梦 / openGauss / 磐维 / Redis / MongoDB / Neo4j / 达梦等国产库（共 12 引擎） |
| 文件备份（任意文件） | ✅ 已有 | 本地 + 远程 SSH 无 Agent |
| **文件镜像备份**（备份目录与源完全一致） | ✅ **本轮补齐** | `file_mirror` 模式：全量/增量产物均解包为与源一致的目录树（`mirror_full_<ts>/`、`mirror_inc_<ts>/`） |
| **快速恢复**（复制增量文件夹即恢复） | ✅ **本轮补齐** | 增量镜像目录相对结构与源一致，整目录覆盖回源即增量恢复；恢复 API 直接支持目录产物 |
| **紧急使用**（挂载备份数据直接用） | ✅ **本轮补齐** | 每任务维护 `mirror_latest/` 最新视图（增量自动合并 + 删除清单应用），可直接复制/挂载，避免长传输中断业务 |
| **备新建库**（管理软件新建库自动纳管） | ✅ **本轮补齐** | 任务开关 `auto_discover_new_dbs`：每次备份前枚举源实例，发现新库自动创建同配置备份任务并通知；全实例任务新库天然覆盖 |
| 重复数据删除（周期内局部重删、跨周期不重删） | ✅ 2026-11-10 补齐（可选策略） | 新增 `dedup_scope=cycle`：周期以最近一次成功全量备份为界，同周期内复用文件库、跨周期不复用旧字节；默认仍为 `global` 全局共享，存量任务零行为变化 |
| 全备 + 增量 | 🟡 部分对齐 | file / MySQL / Oracle / SQL Server 为真增量；PG / Redis / Neo4j 诚实回退全量，openGauss 物理通道如实失败，**Windows 文件通道仍为 v1 全量语义（增量待补）** |
| 一键备份 | ✅ 已有 | 立即备份（UI/API `POST /tasks/<id>/run`） |
| 最小间隔 60 秒 | ✅ 2026-11-10 补齐 | `_clamp_interval` 后端强制下限 1 分钟（60 秒），组合双调度同步钳制，任务表单新增分钟输入控件；不支持秒级（实时 CDP 另通道） |
| 自动删除 N 天前备份 | ✅ 已超越 | 按天数/份数/GFS + 定期清理 + 不可变锁定（防误删且防勒索清空备份） |
| 备份结束后邮件通知 | ✅ 2026-11-10 补齐 | 默认启用通知且成功/失败都发，异步发送不阻塞备份，未配置渠道时留审计提示；SMTP 仍需在「系统设置 → 通知配置」填写 |
| 异机备份 | ✅ 已有 | SSH 无 Agent 远程备份 |
| 异地容灾（P2P 隧道免公网 IP） | 🔀 路线差异 · 2026-11-10 等效链路补齐 | 三级存储（MinIO/S3）/ 异地备份机 / 单向网闸摆渡盘（ferry）之外，新增 `core/remote_pull.py`「云侧推对象存储 + 平台出站拉取」；平台无需公网 IP 暴露、无需被反向连入。**仍不提供 P2P 打洞/反向隧道**，前提是云侧与平台都能出站访问同一对象存储 |
| **Windows 客户端防勒索/防删除保护** | ✅ 2026-11-10 补齐（零安装，真机待验） | `core/win_protect.py`：经 Windows OpenSSH 下发 **Defender 受控文件夹访问**（防加密，业务程序可白名单）+ **NTFS 拒绝删除**（归档目录），支持多目录、核查状态、解除、WARNING 审计；被管机不装任何组件。**用户已提供在线主机 192.168.41.3（22/1433/445 可达），真机 E2E 待执行**；与备份侧不可变锁定叠加构成完整防线 |
| 存储平台基于 Linux | ✅ 已有 | 平台与备份存储均可 Linux 化部署 |
| 公有云备份（无需公网 IP / 云主机异地容灾） | ✅ 2026-11-10 等效链路补齐（路线差异如实） | 对象存储引擎 + S3/COS/OSS 出站 + 摆渡盘收件 + **`/remote-pull` 出站拉取登记**：云侧按摆渡包格式推桶，平台主动出站拉取、校验 sha256、登记异地副本记录；`push_record()` 提供云侧推送参考实现。**不是 P2P 反向隧道**，前提是双方都能出站访问同一对象存储 |
| 虚拟机备份 | ✅ 已超越 | PVE/KVM/ESXi/Hyper-V（AXUS 无） |
| 恢复校验 / 恢复演练 | ✅ 已超越 | 策略化自动恢复校验 + RTO/RPO 演练（AXUS 无） |
| PITR / CDP | ✅ 已超越 | MySQL binlog / PG WAL / 达梦 dmrman / Oracle LogMiner 时间点恢复（AXUS 无） |
| 克隆服务（VDB） | ✅ 已超越 | 秒级克隆测试库（AXUS 无） |
| AI 助手 | ✅ 已超越 | 自然语言运维（AXUS 无） |

## 2. 本轮新增实现与验证

### 2.1 新建库自动纳管（`core/auto_discover.py`）

- 任务开关：数据库备份任务高级选项「新建库自动纳管」；API：`POST /api/tasks/<id>/discover` 手动触发
- 调度集成：每次备份前自动扫描（失败安全，不影响本次备份）；首次建立基线快照不批量建任务
- 单库任务发现新库 → 自动克隆同配置任务（调度/保留/存储全继承）+ webhook 通知；全实例任务新库天然覆盖仅提示
- **E2E 5/5**：建 d1 → 基线 → 新建 d2 → 触发 → 自动创建任务 `e2e_ad_main_e2e_ad_d2` → 该任务可真实备份

### 2.2 文件镜像备份（`core/engines/file.py`）

- 任务开关：文件备份表单「镜像备份模式」（`extra_options.file_mirror`）
- 实现：复用既有打包+快照+增量链路，产物落盘后解包为镜像树并合并 `mirror_latest`（增量应用 `.deleted.txt` 删除清单）；兼容 zstd 二次压缩产物
- 恢复：目录产物直接整树复制回目标（`mirror.restore` 分支）
- **E2E 8/8**：全量镜像树与源一致 → 改/增/删 → 增量镜像仅含变化文件（3 文件）→ latest 反映最终状态（b 删、c 增、a 更新）→ 恢复到目标目录逐值一致

## 3. 路线差异的诚实说明

- **Windows 客户端防勒索**：AXUS 需在被管服务器安装客户端实现实时防加密；AIDBM 坚持零安装，防护点在备份侧（不可变锁定/写保护/Linux 平台）——能保证「备份不被清空、可恢复」，不能阻止源文件被加密本身
- **P2P 异地隧道**：AXUS 免公网 IP 直连云上；AIDBM 对等场景为摆渡盘/网闸（单向隔离更强）、对象存储，以及 2026-11-10 新增的「云侧推桶 + 平台出站拉取」。**不做公网穿透/反向隧道**；共享对象存储需双方均可出站访问（内网/专线/摆渡同步均可）
- **客户端授权计价**：AXUS 按客户端数量授权；AIDBM 零安装无客户端授权概念，但 2026-11-10 已补「按授权服务器数」的 `distinct host` 视图（同一服务器多任务只占 1 名额，平台自身不计入），可选开启强校验

## 4. 复现

- 镜像备份：`python /tmp/e2e_mirror.py`（8 用例）
- 新建库纳管：`python /tmp/e2e_autodisc.py`（5 用例，需本机 MySQL 3307）


---

## 5. 2026-11-10 逐项代码复核与补齐

> 复核原则：**只认代码与测试证据**，旧文档中「已实现/已验证」的措辞不作为实现证据。
> 复核使用 code-explorer 全仓只读扫描（108 次工具调用），逐项核到文件/符号/行号。

### 5.1 复核纠正的口径

| 项 | 旧文档口径 | 代码实况 |
|---|---|---|
| 镜像备份 | 「全量/增量目录都与源一致」 | `mirror_inc_<ts>/` 原为**稀疏变化集**，完整视图只在 `mirror_latest/` |
| 重删 | 「✅ 已超越」 | `.dedup_store` 全局跨周期共享，与「跨周期不重删」**口径相反**，且无周期概念 |
| 邮件通知 | 「✅ 已有」 | 默认 `enabled=false` / `on_success=false` / `channels=[]`，**默认一封不发** |
| 60 秒间隔 | 「✅ 已有」 | 无最小值校验，`interval_minutes=0/负` 可写入；前端无分钟控件 |
| Windows 防护 | 「🔀 路线差异，不跟随」 | 全仓无 `icacls`/CFA 实现，只有备份侧 WORM |
| 公有云 | 「✅ 等效」 | 无 P2P/反向隧道；对象存储引擎保护的是桶，不是云主机 |

### 5.2 本轮补齐

1. **Windows 客户端防护**（`core/win_protect.py`、`api/win_protect.py`、`templates/file_backup.html`、`static/js/win_protect.js`）：CFA / ACL 拒绝删除 / 双重三种模式，多目录 + 授权程序白名单 + 状态核查 + 解除 + 审计；PowerShell `-EncodedCommand` 传参避免中文路径与引号注入，路径白名单校验。**23 项契约测试通过；真机未测**。
2. **重删周期**（`core/dedup_source.py`、`core/engines/file.py`）：`dedup_scope=cycle` 时按最近一次成功全量划分周期，文件库隔离到 `.dedup_store/cycles/<cycle_id>/files`，跨周期不复用；默认 `global` 不变。
3. **备份结束通知**（`config.py`、`core/notifier.py`、`core/scheduler.py`）：默认启用且成功/失败都发，`notify_async` 后台线程发送，SMTP 抖动不影响备份结果。
4. **60 秒下限**（`core/models.py`、`templates/tasks.html`、`static/js/app.js`）：创建/更新任务统一钳制，组合双调度同步，前端分钟输入。
5. **增量镜像完整树**（`core/engines/file.py`）：`mirror_full_tree` 开启时 `mirror_inc_<ts>/` 为与源一致的完整树，稀疏变化集另存 `mirror_inc_changes_<ts>/`。
6. **文档诚实性**：修正 `docs/benchmark_file_backup_20261026.md` 中「Windows 走 SMB 挂载」的虚报（实际为 OpenSSH + `tar.exe` + SFTP）。
7. **Windows 文件增量（P1）**：新增 `core/engines/win_file.py`——PowerShell `Get-ChildItem` / `LastWriteTimeUtc` / 可选 `Get-FileHash` SHA256 指纹清单，UTF-8→Base64 绕开控制台代码页；`file.py` 全量建立真实基线、增量按 size+mtime（可叠加内容指纹）差分，SFTP 拉变化文件由平台 `tarfile` 打包，删除写 `.deleted.txt` 并用 PowerShell 回放。清单失败/超限**回退全量且不保存空快照**（防误判全删）。
8. **免公网 IP 云主机容灾（P2）**：新增 `core/remote_pull.py` + `api/remote_pull.py` + `/remote-pull` 页；来源配置（SM4 加密凭据）、出站探活、预览、`pull_source`（sha256 校验 → 摆渡入库 → 记录登记 → 幂等）、`push_record`（云侧推送参考实现）、周期调度 start/reload 双注册。包格式与摆渡盘一致。**明确不是 P2P 反向隧道**。
9. **按授权服务器数计量（P2）**：`core/license.py` 新增 `server_view()`（`backup_tasks` / `ssh_hosts` / `synology_devices` / `vm_hypervisors` / `netdev_devices` 归一化去重）、`/api/system/license/servers`、设置页服务器视图；`max_servers` 进授权信息；`check_server_quota()` 由 `system_config.license_server_metering=1` 开启，默认关（零破坏）。
10. **顺带修复的真缺陷**：`models.create_record` 白名单静默丢弃 `ferry_inbox` 传入的 `note`，导致摆渡/异地副本导入记录没有来源标记 → 映射到 `message` 保留审计轨迹。

### 5.3 仍未对齐（不虚报，排期）

| 项 | 缺口 | 计划 |
|---|---|---|
| Windows 增量真机 | 代码与本地协议级仿真已通过，尚无 Windows 真机 E2E | 用已提供的 192.168.41.3（22/1433/445 可达）跑 `check → apply → check → remove` 与文件全量/增量/恢复闭环 |
| 免公网 IP 真机链路 | 推→拉闭环已用内存对象存储验证；未跑真实 MinIO/S3/云侧双站点 | 用内网 MinIO（或 moto 真实 HTTP 协议）跑一次跨实例推拉；不承诺 P2P 隧道 |
| 快照 / 块级增量 | 群晖/Windows 无统一快照编排 | 见群晖适配路线，另行立项 |
| 快照 / 块级增量 | 群晖/Windows 无统一快照编排 | 见群晖适配路线，另行立项 |

### 5.4 复现

```
CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/bin/python -m pytest \
  tests/test_axus_alignment.py tests/test_win_protect.py \
  tests/test_win_file_incremental.py tests/test_remote_pull.py \
  tests/test_license_servers.py -q
```
