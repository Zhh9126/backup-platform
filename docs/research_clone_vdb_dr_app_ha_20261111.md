# 调研：克隆服务（VDB 快照备份）与容灾链路 HA（应用级）

日期：2026-11-11　方法：只读全仓检索 `core/ api/ templates/ static/js/ tests/` 与 `/root/e2e/` 真机证据，逐项核到行号。本文未改动现网拓扑。

> **状态说明**：第 1～3 节是**改造前的调研快照**（描述「当时缺什么」）；当晚已实施的第一批能力见第 5 节，剩余缺口见第 6 节，请勿把第 1～3 节当作当前代码现状。

---

## 0. 结论先行

1. **克隆服务**已有「免审批直通 → 异步拉起 VDB → 预检/校验/接管检查 → 到期销毁」的单次生命周期，逻辑导入、PG `TEMPLATE` COW、MySQL LVM COW、Oracle `REMAP_SCHEMA` 四通道齐备；但**「VDB 快照」作为可管理对象完全不存在**：COW 快照名只是局部变量，无快照表、版本链、回滚点、再克隆、增量刷新，前端无入口。
2. **容灾链路**当前 = 数据级（实时同步/CDP，成熟）+ 数据库级单实例主从切换（仅 MySQL + dnsmasq A 记录）；**应用级容灾近似空白**。
3. **产品边界必须先改**：`docs/benchmark_zhongke_rebei_20261026.md:89` 把「应用层接管」列为**不跟随**、「平台边界=数据层；接管止于 DNS/连接串层」，与本次判断直接冲突。
4. 分工原则（用户口径）：**数据级容灾 = 实时同步，不重复造**；容灾链路 HA 上移到**应用级可用性**（应用探活、流量牵引、切换编排、切换后业务验证、站点/切换单元）。

---

## 1. 克隆服务 / VDB 现状

### 1.1 已有能力

| 能力 | 落点 |
|---|---|
| 8 态状态机、申请/审批/直通 | `core/clone_service.py:34-42,71-270`；`config.py:179-181` `CLONE_AUTO_APPROVE=true` |
| 异步拉起、失败原因写 note | `core/clone_service.py:162-193` |
| 到期销毁与调度 | `core/clone_service.py:273-293,412-426`；`core/scheduler.py:1324-1353,1967` |
| 只读预检 6 项 | `core/clone_preflight.py:23-103` |
| VDB 注册、密码加密持久化 | `core/clone_service.py:572-591`；`core/models.py:1244-1253` |
| 连接校验 / 应急接管检查 | `core/clone_service.py:295-410,608-629` |
| 四种克隆通道 | `core/clone_service.py:450-570`、`core/restore_extras_clone.py:20-135` |
| 前端 / API | `templates/clone.html:14-91`、`static/js/app.js:8494-8748`、`api/clone.py:23-155` |
| 真机证据 | `/root/e2e/e2e_feature_matrix_1111.py:470-555` + `feature_matrix_results.jsonl:69-73`（C2 五项全绿）、`e2e_a7_b3.py:150-208` |

### 1.2 缺口

**代码完全不存在**：VDB 快照/版本元数据表、快照对象、回滚点、再克隆、增量刷新、快照生命周期测试；从 VDB 再克隆/分支（`templates/vdb.html:8-27` 只有列表与删除）。

**部分实现 / 不一致（应一并优化）**
- `clone_mode` 靠 `note` 的 `[clone_mode=...]` 字符串编码并正则解析（`clone_service.py:126-130,437-448`）；失败原因无 `error_code/last_error/retry_count`。
- 密码双轨：`verify_clone` 只读 `clone_requests.target_password`（`:618-627`），接管检查优先 `vdb_instances.password`（`:329-342`），旧 API `api/restore_extras_api.py:145-150` 不写密码。
- **DDL 与代码不一致**：`clone_service.py:627,652` 读 `vdb.get("service")`，但 `core/db.py:774-790` 无该列 → Oracle 销毁回落默认 `ORCL`。
- 销毁即物理删行、无软删审计；异步拉起为纯内存 daemon 线程，重启后 `creating` 无恢复/幂等/退避。
- 前端未暴露后端已支持的 `clone_mode/live_source_db`（`api/clone.py:74-75` 收、`app.js:8699-8707` 不传）。
- 预检不含 LVM/VG 余量、PG 模板库连接占用、快照空间预算。
- 旧 QA 用例 `tests/qa_phase_0_1_2.py:435-497` 仍断言 `pending`/工单，与默认直通语义漂移。

### 1.3 可复用模型

`core/db.py:828-863` `recovery_journal`（点位/父子链/`rp_kind`）、`:813-826` `backup_sets`（`parent_set_id/chain_id/checksum/verified`）、`:891-899` `golden_backup_record_id`（黄金回退点）、`core/vm/base.py:123,175-179` 的 `SnapshotRef`/create/delete/clone_to_new_vm 抽象、`core/clone_preflight.py:17-20` 可扩展的 `_check` 结构、`core/db.py:1722-1730` 的 ALTER 迁移范式。

### 1.4 「参考 VDB 快照备份」的落地口径

业界 CDM/虚拟副本 = 一次全量 + 增量快照链 + 时间点分支 + 挂载/回滚 + 再克隆 + COW 空间共享。结合本平台硬约束（**零客户端安装、完全离线、工具只在平台侧**）：

- **P0 逻辑快照 + 版本链**：平台侧导出就绪 VDB（`mysqldump` / `pg_dump -Fc`，工具经 `_resolve_local_tool` 解析、不写死路径），落 `BACKUP_ROOT/vdb_snapshots/<vdb_id>/<ts>/`，记 `sha256/size/表数`，支持**回滚**（同一 VDB 回到快照点）、**再克隆**（以快照产物拉起新 VDB）、**版本链**（`parent_snapshot_id`）。不依赖 LVM/VG，离线可用。
- **P1 前端与预检**：快照列表/创建/回滚/再克隆 + 预检联动。新增模板中文必须同步 `static/js/i18n.js` 并升缓存戳（`tests/test_i18n_coverage.py` 门禁）。
- **P2 物理 COW 可管理化**：把 `restore_extras_clone.py:98,134` 的一次性 LVM 快照升级为登记对象（需远端 VG 余量/回滚语义，真机条件受限，不先承诺）。

---

## 2. 容灾链路 HA 现状分层

### 2.1 数据级（实时同步承担，不重复造）

`core/rt_backup/`（捕获/封段/RecoveryJournal/PITR）+ `core/cdc/`（MySQL binlog / PG WAL / Oracle LogMiner / 达梦 DM_LOGMNR / 金仓 WAL）+ `core/sync/engine.py:464-777`（realtime 双引擎、水位持久化、先写目标再推位点、周期对账、post-verify）+ `api/rt.py` 13 端点。`core/disaster_link.py:220-257` 的 `fill-gap` 只做只读观测、`api/link.py:68-94` 读取 `rpo_actual_sec` —— 定位正确，保留。

### 2.2 数据库级（当前 DR 主体）

- 实体 `core/db.py:1044-1059`：`primary_site/dr_site` 仅 TEXT，`failover_config/failover_history` 由 ALTER 追加（`:1519-1524`）；**无 `link_strategy/rpo/rto` 列**（目标在 `protection_policies`，只做展示与 `core/goals.py` 告警）。
- `core/dr_failover.py:261-454`：failover 五步（探测 → Fencing `super_read_only` → 提升 → DNS 切并解析回读 → 新主验证）、failback 六步（GTID 分叉检测 → rejoin 追平且必须 IO/SQL=Yes & behind=0 → 提升 → 现主转从 → DNS 切回 → 验证），逐步 RTO 计时 + 文字 `manual_checklist`。
- `core/dr_monitor.py:84-231`：连续失败计数、告警去重、指数退避、`auto_running` 护栏、自动切换复用 `run_failover(force=False)`；`core/scheduler.py:1531-1548` 每 30s。
- **仅 MySQL**；DNS 仅 dnsmasq 且 `dr_failover.py:154-191` 与 `core/dr_dns.py:99-146` 两套实现并存；手动 failover/failback **无链路级并发锁**。
- 真机：`/root/e2e/e2e_dr_170171.py`、`e2e_dr_auto_170171.py`、`e2e_failback_final.py`、`dr170_report.txt`（**中间态报告含 2 项 FAIL，不可当全绿**）；`README.md:832` 的 RTO 15.2/15.6s 属文档口径，须与报告并列呈现。全部证据仅 2 台 MySQL + 1 个 dnsmasq 容器，**没有应用服务器 / HTTP 业务 / VIP**，应用级真机验证为 0。

### 2.3 应用级逐项核查（几乎全无）

| 要素 | 结论 |
|---|---|
| 站点 / 应用(服务) / 切换单元实体 | 无（`pool_chains.sites` 是存储副本点位，属数据级） |
| VIP / HAProxy / keepalived | 无（仅切换清单文案提及 Nginx upstream） |
| HTTP / 业务 URL 探活 | 无（`health_snapshot:542-591` 只有 primary TCP / standby 复制 / dns） |
| Runbook 编排（依赖/暂停/重跑/回退） | 无（仅文字清单；`core/drill.py` 无编排） |
| 审批 / 切换窗口 / 双人复核 | 无（`core/itsm.py` 已有能力未接 DR，前端只有 `window.confirm`） |
| 多节点、服务发现、健康剔除 | 无（单主单备，拓扑 SVG 固定两节点） |
| 应用连接串自动切换 | 无（人工项） |
| 切换后业务验证产品化 | 无（仅 E2E 脚本 marker 直写硬核对） |
| drill × failover 打通 | 无（`core/drill.py` 对 failover 0 匹配） |
| 应用级 RPO/RTO/SLA 视图 | 无（目标在策略表、实测在 history，二者无关联） |

---

## 3. 差距归类与路线

- **A 类｜应用级应有但没有**：站点/应用/切换单元、应用探活、Runbook、审批与窗口、业务验证、连接串切换、drill×failover、SLA 视图。
- **B 类｜偏数据库级需上移**：单实例链路 DDL、引擎硬编码 MySQL、健康探测=DB TCP、RTO 不含应用重连/业务验证、`manual_checklist` 外置、DNS 双实现、手动切换无锁、`failover_config` 与 `source_kind=rt_task/sync_task` 互不联动、`link_strategy` 死 JSON。
- **C 类｜数据级已由实时同步承担，不应重复**：CDC 捕获/封段、水位续传、周期对账、一致性校验、日志补传、PITR/恢复点、秒级 RPO。**应用级改造应新增编排/实体/探活层，不重写复制层。**

| 优先级 | 项 | 新增表 | 动现网拓扑 | 真机可行性 |
|---|---|---|---|---|
| P0 | 改产品边界 + `sites`/`app_services`/`app_instances`/`dr_switch_units` 实体，`disaster_links` 加 `site_id/switch_unit_id` | 是 | 逻辑模型（170/171 可不动） | 可，影子映射 |
| P0 | 应用 HTTP 探活纳入 `health_snapshot`，「DB 不可达 ∨ 业务探活失败」复合判定 | 否（落 `failover_config` JSON） | 否 | 可，平台侧起测试 `/healthz` |
| P0 | 切换后业务验证产品化（SQL/HTTP 断言），失败即判切换不成功并提示回切，结果进 `failover_history` | 否 | 否 | 可，复用 E2E marker |
| P0 | 审批 / 维护窗口 / 手动切换链路级锁 + running 标记，防向导与自动监控双触发 | 可选（复用 `itsm_tickets`） | 否 | 可，并发调用两次验证 |
| P0 | Runbook 编排：把 `_step` 抽象为步骤模板（fence/promote/dns/app_reconnect/verify），失败暂停/重跑/回退、整体 RTO 落库 | 是（`runbooks`/`runbook_runs`） | 否 | 可，注入单步失败 |
| P1 | `DnsProvider` → `TrafficEndpointProvider`（dnsmasq / VIP / HAProxy / LB） | 否 | 是（需代理节点） | 需增节点 |
| P1 | 应用连接串自动切换（平台侧下发，不装 agent） | 是 | 否 | 受零 Agent 定位约束，先测试应用验证 |
| P1 | 切换引擎多库型扩展（PG/金仓 `pg_promote`；Oracle/达梦半自动） | 否 | 否 | 可 |
| P1 | `drill_type=switch_drill`，隔离域名真实切换演练 + 演练 RTO 趋势 | 是（drills 加列） | 否 | 可 |
| P1 | `rpo_target_sec/rto_target_sec` 落链路并与实测闭环告警 | 是（ALTER 2 列） | 否 | 可，历史回放 |
| P2 | 一致性组 / 两地三中心视图 / 服务发现集成 | 是 | 是 | 需新增真机资源，不承诺 |

**实施顺序**：P0 的 #2/#3/#4 成本最低且可直接用 170/171 + 平台侧测试应用验证；#1 是其余项地基；#6/#7 涉及 VIP/代理节点与零 Agent 定位，须先确认边界（建议采用「平台侧出站探活 + 流量端点抽象」，不要求应用安装任何东西）。

---

## 4. 验收口径（后续实施必须遵守）

- **真实执行、如实失败**：不看 HTTP 200，验产物 sha256/size、回滚后行数与中文硬核对、再克隆后数据一致；失败写结构化原因，不降级仿真。
- **零客户端安装 / 离线自足**：快照导出与业务探活只在平台侧；不要求应用或被管库装 agent/依赖。
- **不扰动现网 DR 拓扑**：170/171 已是生产式主备，切换类真机验证用隔离域名/隔离 `meta.db`，不直接改业务 DNS 记录。
- **前端新增中文**同步 `i18n.js` 词典 + 升缓存戳，跑 `tests/test_i18n_coverage.py`、`test_static_cache_buster.py`。
- **回归**：新增单测 + `route_smoke`（0 UNAUTH / 0 BAD）+ `full_smoke`（21/21）+ 相关组合 pytest；全仓 pytest 既有基线为 `910 passed / 249 failed / 36 errors`，零回归以选定组合判定。

---

## 5. 当晚已实施（2026-11-11）

### 5.1 克隆服务：VDB 逻辑快照闭环

| 落点 | 内容 |
|---|---|
| `core/db.py` | 新增 `vdb_snapshots` 表（版本链 `parent_snapshot_id`、`checksum`、表/行数摘要、软删 `deleted_at`）；补 `vdb_instances.service` 列与迁移 |
| `core/vdb_snapshot.py`（新） | 平台侧原生驱动导出/回滚：gzip JSON 自描述产物、sha256、标识符白名单、只允许平台侧工具、表数/行数上限保护、事务提交与失败 rollback |
| `core/clone_service.py` | `list/create/rollback/reclone/delete_snapshot`；回滚前 sha256 复核（篡改即拒）；再克隆用源备份产物拉起新 VDB 后回放快照；统一 `_vdb_password()` 三处口径；Oracle 服务名持久化 |
| `api/clone.py` | 5 个快照端点；回滚接 `verify_code_gate` 高危验证码闸 |
| `tests/test_vdb_snapshot.py` | 8 条（表契约 / 值编码 / 注入拦截 / 版本校验 / 校验和拦截 / 生命周期 / API 契约） |
| 真机 E2E | `/root/e2e/e2e_vdb_snapshot.py` **17/17**：真实备份 → 免审批克隆 → 快照(2 表 5 行) → 篡改中文/插行/建表 → 回滚（中文 3 行硬核对 + t3 清理）→ 再克隆数据一致 → 软删 |

**诚实边界**：数据级快照（非 DDL 快照），表结构变化时回滚如实失败并提示重新克隆；全量快照，`parent_snapshot_id` 只记版本链不做增量；再克隆依赖源备份产物仍存在；仅 mysql/mariadb/postgresql/kingbase；大库受表数/行数上限保护。

### 5.2 容灾链路：应用级 HA 的第一批能力

| 落点 | 内容 |
|---|---|
| `core/dr_app_ha.py`（新） | 平台侧出站应用 HTTP 探活（接 `core/net_guard` SSRF 防护、零 Agent）；切换后业务验证（SQL 断言仅 SELECT + HTTP 断言，凭据复用加密的 `failover_config`，字段白名单防塞明文口令）；默认关闭零破坏 |
| `core/dr_failover.py` | `health_snapshot` 增 `app` 段；`run_failover/run_failback` 加**链路级非阻塞并发锁**（防向导与自动监控双触发）+ 切换后自动业务验证，失败标 `business_ok=false` 并落 ERROR 审计 |
| `core/dr_monitor.py` | 应用探活失败单独记录/告警，**不伪造成数据库故障**（切库修不好应用，不误触发 DB failover） |
| `api/link.py` | `failover-config` 收发 `app_probe` / `post_switch_verification`（URL 安全校验 + 键白名单）；新增只读 `POST /disaster-links/<id>/verify-business` |
| `tests/test_dr_app_ha.py` | 9 条（探活 200/404、SSRF、SQL 只读拦截、业务验证通过/失败、密钥过滤、并发切换拒绝、健康快照契约） |
| 影子真机 E2E | `/root/e2e/e2e_dr_app_ha.py` **9/9**：影子链路 + 平台侧测试应用 + 本机 3307；健康快照含 app 段、业务验证 SQL+HTTP 通过、插行被真实检出、应用停服如实失败、主库仍可达不误切。**未触发任何真实 failover/failback，未动 170/171 拓扑** |

**产品边界已改**：本轮把「应用可用性」纳入容灾链路判定与切换结果；数据级复制继续由实时同步/CDP 承担，未重写复制层。

---

## 6. 仍未完成（按优先级）

- **P0 剩余**：`sites` / `app_services` / `app_instances` / `dr_switch_units` 实体（应用级编排的地基）、Runbook 步骤模板与失败回退、审批/维护窗口、切换引擎多库型扩展（现仍仅 MySQL）、drill × failover 打通、RPO/RTO 目标落链路闭环。
- **P1**：`DnsProvider` → `TrafficEndpointProvider`（VIP / HAProxy / keepalived）需要新增代理节点；应用连接串自动切换需与「零 Agent」定位一并确认；`link_strategy` 仍是死 JSON。
- **前端**：VDB 快照与容灾应用探活/业务验证目前**只有 API，没有页面入口**。补 UI 时需同步 `static/js/i18n.js` 英文词条并升缓存戳（`test_i18n_coverage.py` / `test_static_cache_buster.py` 门禁）。
- **克隆服务遗留**：`clone_mode` 仍靠 note 字符串编码、失败原因非结构化、异步拉起重启不可恢复、销毁无软删审计、预检未覆盖快照空间预算、前端未暴露 `clone_mode/live_source_db`。
- **真机边界**：应用级容灾尚无「真实应用 + VIP/代理 + 多站点」验证；当前证据是平台侧测试应用 + 影子链路，不能外推为两地三中心已验证。

