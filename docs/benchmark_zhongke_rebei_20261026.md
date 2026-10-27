# AIDBM 对标中科热备两地三中心能力差距分析（2026-10-26）

对标输入：中科热备（北京）云计算技术有限公司产品宣传口径——备份一体机（备份存储 + CDP 持续数据保护 + 自动化恢复演练 + 异地容灾复制）、云灾备（DRaaS）、数据副本管理（CDM）、数据库实时复制。

对标目的：把"两地三中心建而不用"问题转化为 AIDBM 产品化路线。核心命题引用自对标文章：
> 能备份不等于能恢复，能恢复不等于能接管，能接管不等于能快速接管。每一步，都得练。

诚实边界声明：本文所有 AIDBM 现状均给出代码证据；未实现的显式标注"未实现"，不得对客户宣称。

---

## 一、能力对照矩阵（9 项）

| # | 中科热备能力 | AIDBM 现状 | 证据 | 差距评级 |
|---|---|---|---|---|
| 1 | CDP 持续数据保护（秒级 RPO） | 日志级实时捕获双轨已实现：rt_backup 日志流捕获+封段+RecoveryJournal+PITR；CDC 引擎矩阵覆盖 MySQL/MariaDB binlog、PG WAL、Oracle LogMiner、达梦 DM_LOGMNR、金仓 WAL（信创三库真机验证）；文件 CDP（watchers：watchdog+polling 双实现）；rt_tasks 有 rpo_current_seconds 实时 RPO 指标 | core/rt_backup/、core/cdc/__init__.py:5-13、core/rt_backup/pitr.py、core/rt_backup/file_rt.py | 🟢 基本对齐（数据库级）；🟡 无一致性组 |
| 2 | 数据库实时复制（日志级、事务一致） | sync realtime 双引擎：polling（纯 Python 全插件库型，水位持久化+断点续传+回看窗口+周期对账）、binlog CDC（MySQL/MariaDB）；1.4.16 可靠性轮已落"不重不丢不错" | core/sync/engine.py、core/sync/realtime_runners.py | 🟢 对齐 |
| 3 | 自动化恢复演练（演练沙箱：隔离环境拉起+一键销毁） | **最大差距**。drill 已有评分体系（100 分制、阈值 60、report 含 rto_sec/rpo_sec/issues/recommendation、定时演练 weekly/monthly/quarterly），但演练是**元数据级评估**（检查备份文件存在性/大小/客户端就绪），**不真实恢复数据**；克隆服务（VDB 生命周期）是独立能力，与演练**完全未打通** | core/drill.py:36-129、api/drills.py、core/clone_service.py | 🔴 未对齐（P0） |
| 4 | 异地容灾复制 | 站点只是 disaster_links 上的两个文本字段（primary_site/dr_site），**无站点实体/站点间复制策略引擎**；异地实际靠存储层（L2=S3 异地副本、三级存储 TierReplicate、摆渡盘 ferry_inbox） | core/disaster_link.py、core/db.py:981-991 | 🔴 未对齐（P1） |
| 5 | 容灾切换（业务接管） | MySQL 主备切换引擎已真机验证：Fencing（read_only 冻结防双写）→提升→DNS 切换+解析回读→验证；failback GTID 分叉检测+追平+回切；实测 RTO 15-17s；多专线选路 select_route；切换历史落库 | core/dr_failover.py、core/dr_dns.py | 🟡 单库型（仅 MySQL），PG/金仓/达梦/Oracle 未覆盖 |
| 6 | 恢复编排（Runbook） | **未实现**（Vinchin 对标遗留 P3）：切换无步骤化剧本模板、无审批节点、失败无自动回退；多库按序恢复+逐步验证无编排 | dr_failover manual_checklist 仅返回文字清单 | 🔴 未对齐（P1） |
| 7 | RPO/RPO 目标驱动配置 | 策略体系已有 rpo_target_min/rto_target_min/backup_strategy/link_strategy（含 replication=sync/async、cross_site），演练基线对比已闭环；但 **link_strategy 是死 JSON——无代码按它驱动复制通道/备份频率/CDP 开关** | core/db.py:413-418、core/policy.py:29-95、core/models.py:236-244 | 🟡 半对齐（P0 收口成本低） |
| 8 | 数据副本管理 CDM | 部分对齐：克隆服务 VDB（申请→拉起→TTL 销毁）、多副本分层复制（TierReplicate）、对象存储版本化回放、不可变备份四层防护（保留锁/写保护/完整性复检/删除拦截） | core/clone_service.py、core/immutability.py | 🟡 部分对齐 |
| 9 | 云灾备 DRaaS | **不跟随**：AIDBM 交付形态是完全离线（air-gapped），无公有云依赖是产品定位而非缺陷；等效能力由离线摆渡（ferry_inbox 单向网闸）+ 异地备份机承担 | core/ferry_inbox.py | ⚪ 策略性放弃 |

## 二、差距根因：三个"断点"

中科热备的叙事把三件事"捏在一起"（数据保护、业务接管、演练验证），AIDBM 单项能力大多已有，**断的是三处连接**：

1. **演练 × 恢复断开**：drill 只看元数据，恢复引擎/克隆服务却不参与演练 → "能备份"未验证为"能恢复"。
2. **目标 × 配置断开**：策略里的 RPO/RTO/link_strategy 只是数字和 JSON，不翻译成备份频率、CDP 开关、复制通道 → 用户设了目标，系统行为不变。
3. **链路 × 站点断开**：disaster_links 是单实例级链路，站点是字符串不是实体 → "两地三中心"在数据模型上不存在。

## 三、吸收方案（P0→P2）

### P0-1 真实恢复演练闭环（对齐"演练沙箱"，最高优先级）

把 drill 从"元数据评估"升级为"真实恢复+验证+评分"：

- 演练时真实执行恢复：数据库产物 → 恢复到**临时目标**（优先复用克隆服务的 target 通道；无 target 时恢复到 BACKUP_ROOT 下演练目录的本机临时库），恢复后跑数据校验（复用恢复校验/数据对比的既有能力）→ 按校验结果 + RTO 实测 + RPO 实测计算评分（评分体系已有，只换输入源）。
- 演练环境销毁：演练完即删临时实例/库（克隆 TTL 销毁机制已有，复用钩子）。
- 隔离说明（诚实边界）：AIDBM 是数据库级平台，不做 VM/网络级沙箱；"隔离"= 独立临时实例 + 演练专用库前缀 + 不注册进生产调度。前端弹窗文案明示隔离边界。
- 打通定时演练：scheduled drill 默认切到真实恢复模式（元数据级检查保留为快速巡检模式）。

这一项直接回应文章核心痛点（"从来没人真正演练过""切换 40 分钟→12 分钟是练出来的"），且 AIDBM 的"真实执行不仿真"产品理念在此最加分。

### P0-2 RPO/RTO 目标驱动配置收口

把 link_strategy 死 JSON 变活：策略保存/任务创建时，按 rpo_target_min 翻译建议或直接下发——

- rpo ≤ 5 分钟 → 建议开启 rt_backup（CDP）或 realtime sync；未开启时策略页 warning。
- rpo ≤ 60 分钟 → 校验增量备份/调度频率是否满足（间隔 > rpo 目标即提示）。
- link_strategy.replication=cross_site=true → 校验 storage_targets 是否含异地目标（L2/S3/异地备份机），否则 warning。
- 演练基线对比已有（drill.get_baseline），补"目标未达标"的持续告警（复用 ai_alert 通道）。

成本低（策略→校验→提示，不改变执行引擎），价值高（目标体系从展示变成闭环）。

### P1-1 恢复编排 Runbook（对齐"业务接管"的编排层）

- 步骤模板：恢复步骤（按依赖排序多库恢复）+ 验证步骤（行数/抽样对比）+ 切换步骤（复用 dr_failover failover/failback 原子步骤）。
- 审批闸门：执行前需确认（沿用 needs_confirm 模式）；关键步骤失败可停、可重跑、可回退（failback 已有）。
- RTO 计时：按 Runbook 整体计时落库，与 rto_target_min 对比。
- 对标文章里的"切换剧本"对应此能力；Vinchin 对标遗留 P3 一并闭环。

### P1-2 站点实体化（对齐"异地容灾复制"）

- 新增 sites 表（站点名/类型 primary|local_dr|remote_dr/地址/负责人），disaster_links、storage_targets、ssh_hosts 关联 site_id。
- 两地三中心视图：站点拓扑页（生产/同城灾备/异地灾备三节点，各节点展示受保护任务数/最近恢复点/演练状态）。
- 站点间复制策略：异地灾备站点的数据通道复用既有能力（L2 异地 S3 / 异地备份机 / ferry 摆渡），把"策略"挂到站点上而非散落在存储配置里。
- 不做：站点级双向复制引擎、存储级双活（超平台定位）。

### P1-3 切换引擎多库型扩展

- PG/金仓/openGauss：流复制 promote（pg_promote / sys_ctl promote / gs_ctl promote）+ 只读检查 + DNS 切换复用 dr_dns；语法同构，工程量可控，优先做。
- Oracle/达梦：如实评估——Oracle Data Guard 需库端配置（DB 自带组件，不违反零安装约束，但离线环境下配置复杂度高）；达梦 DW/PITR 路径类似。先出"检查+脚本生成+人工执行"半自动模式，不承诺全自动。

### P2（视排期）

- 一致性组 PITR：多库同事务点位联合恢复点（MySQL GTID 联合位点），对齐"数据半截子都不行"的一致性叙事。
- CDM 强化：黄金副本管理页（克隆/摆渡/异地副本统一视图）。
- 演练 RTO 趋势对标（文章"40 分钟→12 分钟"叙事：同一 Runbook 多次演练的 RTO 折线，drills 表已有 rto_actual_sec 基础）。

## 四、明确不跟随

| 项 | 理由 |
|---|---|
| 云灾备 DRaaS（公有云） | AIDBM 完全离线交付形态是产品定位；等效=离线摆渡+异地备份机 |
| VM 级双活/存储级双活 | 超出数据库灾备平台定位 |
| 应用层接管（中间件拉起） | 平台边界=数据层；接管止于 DNS/连接串层 |
| 34 项专利/66 项软著类资质叙事 | 非能力项 |

## 五、我方领先点（宣传口径可用）

1. **真实执行不仿真**：演练/校验/切换全部真机验证过（RTO 15s 实测、五库型 E2E 硬核对），对标文章里"演练沙箱"在 AIDBM 落地就是真恢复真校验。
2. **国产化一等公民**：达梦 DM_LOGMNR、金仓 WAL、openGauss 真机全矩阵通过——中科热备叙事里没有信创库细节。
3. **零客户端安装 + 完全离线**：数据库服务器不装任何 agent/备份工具；air-gapped 部署开箱即用。
4. **AI 原生**（收费模块）：智能告警、自然语言驱动，区别于传统一体机的规则告警。
5. **不可变备份四层防护**：保留锁/写保护/完整性复检/删除路径四路拦截——防勒索删备份。

## 六、落地节奏建议

| 阶段 | 内容 | 依赖 |
|---|---|---|
| 第一步 | P0-1 真实恢复演练闭环 + P0-2 目标驱动收口 | 全部复用既有恢复/克隆/校验/策略能力，新代码主要是编排粘合 |
| 第二步 | P1-1 Runbook + P1-3 PG 系切换扩展 | dr_failover 步骤抽象化 |
| 第三步 | P1-2 站点实体化 + 两地三中心视图 | 需用户确认站点模型后动表 |
| 第四步 | P2 一致性组/CDM 视图 | — |
