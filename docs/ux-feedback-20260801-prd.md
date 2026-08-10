# UX 反馈增量 PRD（2026-08-01）

> 文档类型：**增量 PRD**（4 个已有功能的改进/修复，非重写）
> 来源：用户 2026-08-01 的 4 张截图反馈
> 平台栈：Python 3.14 + Flask + SQLite + Bootstrap 5 + jQuery（沿用，不引入新框架）
> 状态：**v2 已定稿** —— §7 的 10 个待确认问题已由架构师全部裁决，配套设计见 `docs/ux-feedback-20260801-design.md`（任务 T01–T10，约 9.5 人日）

---

## 1. 变更范围摘要

| # | 问题 | 模块 | 影响页面 / 代码 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 1 | AI 预测告警只给全局结论，无任务级失败明细；缺备份数据验证维度 | AI 告警 | `templates/alert.html`、`core/ai_alert.py`、`api/ai_alert.py` | **P0** | 大 |
| 2 | PITR 时间轴无任务可选、无创建入口，逻辑断链 | 实时备份 | `templates/rt_timeline.html`、`api/rt.py`、`core/models.py` | **P0** | 中 |
| 3 | 恢复页残留「（参考鼎甲迪备设计）」，选记录/反馈体验差 | 数据恢复 | `templates/restore.html`、`static/js/app.js` | **P1** | 小 |
| 4 | 容灾链路 HA 无数据源，与数据同步割裂 | 容灾 HA | `templates/drlink.html`、`core/db.py`、`api/link.py` | **P1** | 中 |

> 文件名说明（已与仓库核对）：AI 告警后端为 `api/ai_alert.py`；容灾后端为 `api/link.py`（**无** `drlink.py`，但页面确为 `templates/drlink.html`）；前端为单文件 `static/js/app.js`（4682 行，**无** `restore.js`），恢复页逻辑位于 `onRecordChange` / `submitRestore` / `renderRestoreRecords`。

**已定位的根因**

- 问题 1：`analyze_backup_failure_risk()` 调用 `models.list_records(limit=50)` **全局聚合**，从不按 `task_id` 分组；`basis` 是纯文本 `list[str]`，不含任何 ID。四个 analyzer 中**没有数据验证维度**——`backup_records.verified/verify_msg` 字段虽已存在，但只被 `scheduler._verify_backup()` 做了「文件头是否为 gzip/SQL」的弱校验，AI 引擎完全没消费。
- 问题 2：实时保护任务 = `backup_tasks.rt_enabled=1` + `rt_tasks` 扩展行（1:1），**不是独立实体**。页面只读 `/api/rt/tasks`，空列表时无任何出口。
- 问题 3：`restore.html:6` 硬编码该文案。
- 问题 4：`disaster_links` 表只有 `primary_site`/`dr_site`/`route_policy` 三个**文本字段**，无任何源连接信息；而 `sync_tasks` 已有完整 `src_*`/`tgt_*` 连接配置。两者零关联。

---

## 2. 用户故事

| # | 用户故事 |
|---|---|
| 1a | 作为 DBA，我希望在「备份失败」预测下展开看到**具体哪几个任务、失败几次、什么原因**，以便直接去修那个任务，而不是对着 55 分猜。 |
| 1b | 作为合规管理员，我希望平台定期验证备份文件的**可用性与完整性**（校验和比对 + 抽样恢复），以便证明备份是真能恢复的，而不只是"文件存在"。 |
| 2 | 作为运维，我希望进入 PITR 时间轴时能**选择或一键创建**实时保护任务，以便在没有任务时也知道下一步该做什么。 |
| 3 | 作为运维，我希望恢复页**去掉他厂商品名**并让当前选中的备份记录清晰可见、执行后有明确进度反馈。 |
| 4 | 作为容灾管理员，我希望新建 HA 链路时**直接选一个已有的数据同步任务**作为源，复用其连接信息，而不是重复填一遍站点和 JSON。 |

---

## 3. 需求池

### 问题 1 · AI 预测告警

| 级别 | 需求 |
|---|---|
| **P0** | `analyze_backup_failure_risk()` 改为**按 task_id 分组聚合**，输出 `details.task_details[]`（任务名、db_type、近 7/30 天失败次数、最近失败时间、失败原因摘要、建议动作、风险分） |
| **P0** | 机器可读证据**只放 `details.evidence = {task_ids:[], record_ids:[]}`**；`basis` 保持纯人类可读 `list[str]` 不变。前端「依据」列下钻读 `details.evidence`<br>⚠ 理由：`ai_alert.py:_parse_response()` 在启用大模型时会用 LLM 输出**整体覆盖 `basis`**，但保留 `details` 原样 —— evidence 若放 `basis`，一开大模型就丢 |
| **P0** | 新增第 5 个 analyzer `analyze_backup_verify_risk()`（**函数名**）→ 产出 metric 值 `verify_fail`，纳入 `run_all_checks()` 与 `DEFAULT_AI_CONFIG` |
| **P0** | `alert.html` 预测表行可展开，展示任务级明细子表 |
| **P1** | `_verify_backup()` 增强：校验和（sha256）落库并与上次比对、压缩包可解压性探测 |
| **P1** | 存量 `checksum` 一次性回填脚本（`scheduler.py:232` 的 `result.checksum` 在部分 driver / DEMO 模式下为空） |
| **P1** | 新增「数据验证」风险卡片 + metric 筛选项 |
| **P2** | L3 抽样恢复演练：本期**只留配置开关 + 空实现（默认关）**；将来落地走 `vdb_instances`，不用临时目录 |
| **P2** | 一键「按建议动作跳转到对应任务编辑页」 |

### 问题 2 · 实时备份 PITR

| 级别 | 需求 |
|---|---|
| **P0** | 任务下拉为空时，主区域显示**空状态引导卡**，含「创建实时保护任务」主按钮 |
| **P0** | 「创建实时保护任务」= 从已有 `backup_tasks` 中挑一个并置 `rt_enabled=1` + 建 `rt_tasks` 扩展行，不新建备份任务。入口复用 `PUT /api/rt/tasks/<id>/config`，候选列表复用 `GET /api/tasks` 前端过滤，**不新增端点** |
| **P0** | **T06 已重新定界（工作量下调）**：`models.update_rt_config()`（models.py:1497）确实不建 `rt_tasks` 行，**但整条链路已自动闭合**，无需新写 upsert —— 详见 §4.3 链路追踪。T06 收敛为「守护进程停止态的兜底 + 回归验证」 |
| **P1** | 守护进程处于 stopped 时，创建流程应提示「实时守护未启动，任务已配置但不会开始捕获」并提供启动按钮（这是链路唯一真实缺口） |
| **P0** | 下拉选项按 `db-log`（数据库·秒级日志 PITR）/ `file`（文件·分钟级变更捕获）**分组显示**，并标注实际 RPO |
| **P1** | 未选任务时，禁用「立即捕获」「对账」等任务级操作按钮（当前可点但无效） |
| **P1** | URL 支持 `?task_id=` 深链，从告警/任务页跳转直达 |
| **P2** | 空状态下展示「实时保护是什么」的 3 步说明图 |

### 问题 3 · 数据恢复

| 级别 | 需求 |
|---|---|
| **P0** | 删除 `restore.html:6` 的「（参考鼎甲迪备设计）」，改为「从备份记录恢复到源实例或跨主机恢复」 |
| **P0** | 全仓扫描并清除其余「鼎甲」「迪备」字样 |
| **P1** | 选中记录后以**卡片**展示（任务名 / 时间 / 大小 / 校验状态 / 存储层），替代当前 `alert-info` 单行长文本 |
| **P1** | 「执行恢复」改为提交后禁用按钮 + 显示进度条，完成后自动刷新恢复记录表并高亮新行 |
| **P2** | 恢复记录表增加状态徽章配色与「查看日志」链接 |

### 问题 4 · 容灾链路 HA

| 级别 | 需求 |
|---|---|
| **P0** | `disaster_links` 新增列：`source_kind`（`sync_task`\|`rt_task`\|`manual`）、`source_id`（INTEGER）；ALTER 兜底迁移 + `UPDATE ... SET source_kind='manual'` 兜底存量 |
| **P0** | 新增链路弹窗第一步改为**选择数据源**：下拉列出 `sync_tasks`（`enabled=1` 即可，**不强制 running**）+ `rt_enabled=1` 的任务；`last_status='failed'` 的源**标红但可选** |
| **P0** | **双轨复用**：`source_id` 做**引用**（展示源任务名/状态 + 告警联动）；主站点 / 备站点 / `route_policy` 做**创建时快照回填**（可手工改，执行以此为准）<br>⚠ 理由：现有卡片渲染与 `analyze_link_health()` 硬依赖这三个文本字段，不落库必回归；且源改址不应静默改变已生效的容灾配置 |
| **P0** | 无可用数据源时，空状态提示「请先创建数据同步任务」并给出跳转链接（**不允许**在无源情况下建链路） |
| **P1** | 链路卡片展示源任务名 + 源任务最近同步状态（`sync_tasks.last_status`） |
| **P1** | `analyze_link_health()` 纳入源同步任务 `last_status='failed'` 作为劣化因子，计 **+25 分**（明确低于既有 `switch_count_high`=70 与 `consistency_fail_score`=60，不压过原有因子） |
| **P1** | `manual` 模式**长期保留**（编辑存量链路时跳过选源第一步），非临时兼容 |

---

## 4. 关键改进点

### 4.1 AI 预测告警 — 任务级明细

`analyze_backup_failure_risk()` 输出结构扩展（向后兼容，只加字段）：

```json
{
  "metric": "backup_fail", "risk_score": 55.0, "risk_level": "medium",
  "details": {
    "fail_rate_30d": 0.28,
    "task_details": [
      {"task_id": 3, "task_name": "核心交易库", "db_type": "mysql",
       "fail_7d": 2, "fail_30d": 5, "last_fail_at": "2026-07-31T02:15:00",
       "last_error": "Connection refused (110)", "task_risk_score": 72.0,
       "suggestion": "检查源库 3306 可达性与账号权限"}
    ],
    "evidence": {"task_ids": [3, 7], "record_ids": [1201, 1188]}
  },
  "basis": ["任务「核心交易库」近30天失败 5 次（失败率 41.7%）", "..."]
}
```

**失败原因摘要**：取该任务最近一条 `status='failed'` 记录的 `message` 前 80 字符。**建议动作**：按错误关键词映射规则表（连接类 / 权限类 / 磁盘类 / 超时类 / 其他），不调 LLM。

### 4.2 AI 预测告警 — 数据验证维度（新 metric `verify_fail`）

三层校验，由浅入深，按配置开关分层启用：

| 层 | 手段 | 成本 | 默认 |
|---|---|---|---|
| L1 完整性 | 文件存在 + 大小 > 0 + sha256 与落库 `checksum` 比对 | 极低 | 开 |
| L2 可用性 | 压缩包可解压探测（gzip/tar 读尾）、DB dump 关键标记扫描 | 低 | 开 |
| L3 可恢复性 | 抽样恢复演练（走 `vdb_instances`） | 高 | **关**（P2，本期仅留开关+空实现） |

评分规则：L1 失败 → 90 分（critical，备份已损坏）；L2 失败 → 70；`verified=0` 的记录占比 ≥ 30% → 55；距上次成功验证 > 7 天 → 45。

**两条必须遵守的安全规则（否则会全线误报）**

1. **`checksum` 为空时只计入「未校验占比」，绝不判 L1 失败** —— 存量记录大量无校验和，误判会让 `verify_fail` 直接飙到 critical。
2. **触发时机跟随 `ai_alert_interval_hours`（默认 6h），不新增独立调度器**。限流：单次最多抽 **20 条**；单文件 **>512MB 跳过全量 sha256**，退化为「头 8KB + 尾 8KB + 文件大小」轻指纹。

### 4.3 实时备份 — 任务选择/创建流程

```
进入 /rt_timeline
   └─ GET /api/rt/tasks
        ├─ 有任务 → 下拉分组渲染 → 默认选中第 1 个 → 加载时间轴
        └─ 空     → 隐藏时间轴，渲染空状态卡：
                     「尚未开启任何实时保护任务」
                     [ 创建实时保护任务 ]  [ 了解实时保护 ]
                          └─ 弹窗：选择已有备份任务 → 选模式(db_cdc/file_polling)
                                   → 设捕获间隔 → PUT /api/rt/tasks/<id>/config
                                   → 成功后自动选中并加载时间轴
```

**后端链路已自动闭合（实测追踪，T06 据此瘦身）**

```
PUT /api/rt/tasks/<id>/config          api/rt.py:318
  └─ models.update_rt_config()          置 backup_tasks.rt_enabled=1
  └─ rt_backup.reconcile()              api/rt.py:336「立即对账使其生效」
       └─ supervisor.reconcile()        supervisor.py:334 按 rt_enabled 对账
            └─ _spawn_worker()          新开启的任务 → 建 worker
                 └─ worker.start()      db_rt.py:130 / file_rt.py
                      └─ _sync_rt_task_row()   db_rt.py:674 / file_rt.py:549
                           └─ get_rt_task() 无 → create_rt_task()  ✅ rt_tasks 行自动建立
```

两个关键结论：

1. **`rt_tasks` 行由守护进程自建**，upsert 逻辑已存在于 `db_rt.py:674` 与 `file_rt.py:549`（get→update / else create）。**不要在 API 层再写第三份**，否则三处逻辑各自演进必然漂移。
2. **时间轴渲染根本不依赖 `rt_tasks` 行**：`GET /api/rt/tasks`（api/rt.py:100）走 `list_rt_tasks()` = `SELECT * FROM backup_tasks WHERE rt_enabled=1`，全部字段取自 `backup_tasks`，健康态取自 monitor，**未 JOIN `rt_tasks`**。故置 `rt_enabled=1` 后任务立即出现在下拉中。

**唯一真实缺口**：守护进程 stopped 时 `reconcile()` 不会拉起 worker，`rt_tasks` 行不建、也不会开始捕获。按 P1 加提示与启动按钮即可，无需改数据层。

### 4.4 容灾链路 HA — 与数据同步整合

```
新增容灾链路（两步）
 步骤1 选择数据源 *
   ○ 数据同步任务  [下拉: 北京→上海 订单库同步 (running) ▾]
                   [下拉项示例: 报表库同步 (failed) ← 标红但可选]
   ○ 实时保护任务  [下拉: 核心交易库 (db_cdc, RPO 12s) ▾]
   ※ 无可选项 → 「暂无可用数据源，请先创建数据同步任务 →」（保存按钮禁用）
   ※ 编辑存量 manual 链路 → 跳过本步骤
 步骤2 链路参数（选中源后快照回填，可手工改）
   链路名称* [北京-上海 订单库容灾]   状态 [standby ▾]
   主站点 [10.10.0.5:3306 (来自源)]  备站点 [10.20.0.1:3306 (来自源)]
   多专线路由策略 [已按源目标地址预填 1 条，可增补 ▾]
```

保存时写入 `source_kind` + `source_id`（引用）；`primary_site`/`dr_site`/`route_policy` 按快照落库（保持现有渲染与 `analyze_link_health()` 零回归）。

---

## 5. 页面草图

**5.1 告警页 · 可展开明细**

```
时间        指标      预测内容              等级    评分  依据    模型来源
▼ 08-01 10:00 备份失败 备份失败概率上升      medium  55.0  3 项 ⓘ  规则引擎
  ├ 任务         类型   近7天  近30天  最近失败      原因摘要            建议
  ├ 核心交易库   mysql   2      5     07-31 02:15  Connection refused  检查 3306 可达性 →
  └ 报表归档库   pgsql   1      2     07-29 23:40  磁盘空间不足        清理 L1 暂存目录 →
▶ 08-01 10:00 数据验证 3 个任务备份未通过校验 high    70.0  2 项 ⓘ  规则引擎
```

**5.2 恢复页 · 选中记录卡片**

```
数据恢复
从备份记录恢复到源实例或跨主机恢复          [刷新]
② 选择备份记录
┌──────────────────────────────────────────────┐
│ ✓ 核心交易库 · 全量 · mysql                    │
│ 2026-07-31 02:00  |  1.2 GB  |  ✅ 已校验      │
│ 存储层 L2(MinIO)  |  记录 #1201                │
└──────────────────────────────────────────────┘
```

---

## 6. 验收标准

**问题 1**
1. 存在 ≥2 个有失败记录的任务时，`backup_fail` 预测的 `details.task_details` 长度 ≥2，且每项含全部 8 个字段。
2. 前端点击预测行可展开明细子表，`details.evidence.record_ids` 中每个 ID 均可在 `backup_records` 查到。
3. `run_all_checks()` 返回 5 个 metric，含 `verify_fail`；篡改任一备份文件后重跑，该 metric `risk_level ≥ high`。
4. **反向用例**：一批 `checksum` 为空的存量记录参与验证时，`verify_fail` **不得**因此判 critical，只体现为「未校验占比」升高。

**问题 2**
1. 无 `rt_enabled=1` 任务时，页面不再出现「请选择一个实时保护任务」，而是空状态卡 + 可点击的创建按钮。
2. 通过该按钮完成创建后，无需刷新即自动选中新任务并渲染时间轴。<br>**无阻塞依赖**（原「依赖 T06 upsert」判断已撤销）：`GET /api/rt/tasks` 不 JOIN `rt_tasks`，置 `rt_enabled=1` 后任务即刻可见；`rt_tasks` 行由 `reconcile()→worker.start()→_sync_rt_task_row()` 自动创建。
5. **守护进程 stopped 时**创建任务，页面须提示「守护未启动」而非静默成功；启动守护后该任务自动开始捕获且 `rt_tasks` 行出现。
3. 下拉分组标签正确区分 db-log / file 两类，且每项显示实际 RPO。

**问题 3**
1. `grep -ri "鼎甲\|迪备" templates/ static/ core/ api/` 返回 0 条（当前仅 `restore.html:6` 一处命中；PRD/设计文档自身的引用不在此范围内）。
2. 选中记录后卡片区可见任务名/时间/大小/校验状态/存储层 5 项。
3. 提交恢复后按钮 3 秒内进入禁用+加载态，完成后恢复记录表首行为本次记录。

**问题 4**
1. 无 `sync_tasks` 且无实时任务时，「新增容灾链路」保存按钮为禁用态并显示引导链接。
2. 选择同步任务后，主站点/备站点/路由策略三项自动回填且与 `sync_tasks` 源值一致。
3. 新建链路在 DB 中 `source_kind`/`source_id` 非空；存量链路读取不报错（`source_kind='manual'`）。
4. **快照语义验证**：链路创建后修改源 `sync_tasks` 的 `tgt_host`，已建链路的 `dr_site` **保持不变**（快照而非引用）。

---

## 7. 决策记录（12 项，全部已裁决 · 无待确认项）

| # | 问题 | 裁决 |
|---|---|---|
| 1 | 验证深度 / L3 是否延后 | L1+L2 本期做；**L3 延后 P2**，仅留配置开关 + 空实现（默认关）。将来落地走 `vdb_instances`，不用临时目录 |
| 2 | checksum 存量 | 现网 `instance/meta.db` 为空库，但 `scheduler.py:232` 的 `result.checksum` 在部分 driver / DEMO 下为空 → **需一次性回填脚本**（T01）。硬规则：checksum 为空**只计未校验占比，绝不判 L1 失败** |
| 3 | 验证触发时机 / IO 开销 | **跟随 `ai_alert_interval_hours`（6h）**，不新增调度器。单次最多抽 20 条；>512MB 文件跳过全量 sha256，退化为「头 8KB + 尾 8KB + 大小」轻指纹 |
| 4 | 实时任务创建入口 | **复用 `PUT /api/rt/tasks/<id>/config`** + 补 `rt_tasks` upsert（T06）。候选列表复用 `GET /api/tasks` 前端过滤，不新增端点 |
| 5 | file_polling 是否同一时间轴 | **是**，复用同组件。`recovery_journal` 已统一承载 `file-inc` 与 `db-log` 两类 `rp_kind`；60/120/240 格分桶对分钟级捕获足够 |
| 6 | HA 数据源建模 | **加列**（`source_kind`+`source_id`），不建 `dr_sources`。现表是单站点对语义，无 1:N 消费者，建关联表属过度设计 |
| 7 | 源任务状态要求 | **`enabled=1` 即可**，不强制 running。`last_status='failed'` 的源在下拉标红但可选 |
| 8 | 连接信息引用 vs 快照 | **双轨**：ID 做引用（展示 + 告警联动），站点与路由策略做创建时快照回填（执行以此为准） |
| 9 | 存量链路兼容 | 现网 `disaster_links` 为空，仍按 ALTER + `UPDATE ... SET source_kind='manual'` 兜底。`manual` 模式**长期保留** |
| 10 | 告警权重平衡 | 源同步任务 `last_status='failed'` 计 **+25 分**，低于既有 `switch_count_high`(70) 与 `consistency_fail_score`(60) |

| 11 | 两套并行 rt_* 字段以谁为准 | **`backup_tasks.rt_*` 为配置唯一真相源**；`rt_tasks` 的 `capture_interval` 等为守护进程回写的运行时镜像。**配置漂移风险不成立**（原顾虑已撤销） |
| 12 | T06「补 rt_tasks upsert」是否必要 | **否，T06 瘦身**：upsert 已存在于 `db_rt.py:674` / `file_rt.py:549` 且已被 `PUT /config → reconcile()` 自动触发。仅保留「守护 stopped 兜底提示」+ 回归验证 |

### 命名约定（非问题，避免实施期误解）

- `analyze_backup_verify_risk()` 为**函数名**，产出的 metric 值为 `verify_fail` —— 命名不一致属有意为之，前端筛选项与 `DEFAULT_AI_CONFIG` 键名统一用 `verify_fail`。

### 决策 11 / 12 的代码证据

| 结论 | 证据 |
|---|---|
| 守护进程按 `backup_tasks.rt_*` 构建运行配置 | `types.py:180` `interval_sec=...task.get("rt_interval_sec")`；`:184` 同理取 `rt_log_retention_days` |
| `rt_tasks.capture_interval` 是**只写镜像** | 全仓引用中，仅 `db_rt.py:678` / `file_rt.py:553` 以 `self.rt.interval_sec` **写入**；无任何一处读取它驱动行为（其余引用均为 schema / model 存取层 / 测试） |
| API 与前端亦读 `backup_tasks.rt_*` | `api/rt.py:118,120` |
| 天然职责分离 | 配置目标 `rt_rpo_target_sec` 只在 `backup_tasks`；实测值 `rpo_current_seconds` 只在 `rt_tasks` |
