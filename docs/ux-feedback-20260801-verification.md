# UX 反馈增量 · T10 集成验收报告（2026-08-01）

> 文档类型：**验收报告（QA）**
> 上游：`docs/ux-feedback-20260801-prd.md` §6、`docs/ux-feedback-20260801-design.md` §7 T10
> 验证人：严过关（QA）｜验证环境：Windows / Python 3.14 / DEMO_MODE=on / SQLite 临时库
> 被测代码：T01–T09 全部交付物（`core/db.py`、`core/models.py`、`core/ai_alert.py`、`core/scheduler.py`、`api/link.py`、`scripts/backfill_checksum.py`、`templates/{alert,rt_timeline,restore,drlink}.html`、`static/js/app.js`）

---

## 0. 总体结论

> **状态更新（2026-08-01 第 2 轮复验后）**：B1 / G1 / E1 三项已全部闭环，最终结论为**完全通过**。
> 下表「第 1 轮」列保留首轮原始结论以备追溯，「最终」列为第 2 轮复验后的结论，详见 **§7**。

| 项 | 第 1 轮（初验） | 最终（第 2 轮复验后） |
|---|---|---|
| 自动化回归 | 329 passed / 3 skipped（新增 11 条 T04 单测全绿，既有 318 条零回归） | **340 passed / 3 skipped** ✅（工程师补 11 条 B1/G1 契约单测，既有 329 条**零回归**） |
| PRD §6 验收条目 | 共 14 条：通过 11 · 遗留 2 · 阻塞 1 | 共 14 条：**通过 14 · 遗留 0 · 阻塞 0** ✅ |
| 页面可渲染性 | `/alert`、`/rt-timeline`、`/restore`、`/dr-link` 四页全部 200（登录态）/ 302（未登录） | 同左，**保持全绿** ✅ |
| B1（P0 · 选源 UI 恒空） | ⛔ 阻塞 | **已闭环** ✅ 后端补扁平 `items`（含 `status` / `rpo_sec`），前端真实执行验证选源与回填均正常 |
| G1（P2 · 守护 stopped 兜底） | ❌ 遗留 | **已闭环** ✅ `#rtStoppedHint` 提示条 + `rtProbeDaemonRunning()` 探测 + warning 降级文案全部落地 |
| E1（环境 · 8080 未加载新码） | ❌ 404 | **已闭环** ✅ 重启后 `/api/disaster-links/sources` → **401**（不再 404），线上 `app.js` 与本地字节一致 |
| 结论 | 有条件通过 | **完全通过**：模块 A / B / C / D 四个模块**全部通过**，无遗留、无阻塞，具备交付条件 |

---

## 1. 自动化回归

### 1.1 全量回归

```
$ python -m pytest tests/ -q
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
...................................................s.................... [ 86%]
s.........s.................................                             [100%]
329 passed, 3 skipped in 65.17s (0:01:05)
```

- 改动前基线：`318 passed, 3 skipped in 57.31s` → 本次改动**未引入任何回归**。
- `tests/test_ai_alert.py`（既有 backup_fail / storage_full / link_degraded / drill_overdue analyzer 单测）**全绿**，A1 的「按任务分组」为旁路聚合、A2 的 verify_fail 为新增 metric，均未改动既有评分链路，与设计 §1 兼容性声明一致。

### 1.2 新增 T04 单测

新建 `tests/test_ai_alert_taskdetail.py`（11 条，2.47s 全绿）。隔离范式沿用 `tests/test_ai_alert.py`：`tempfile.mkdtemp` + `DEMO_MODE=on` + `META_DB_PATH` 真实 SQLite 临时库，**不 mock**；额外在模块内把 `config.META_DB_PATH` 切到独立库文件，规避「analyzer 全库扫描」与同进程其他测试模块共享 `meta.db` 的记录串扰。

| 用例 | 覆盖 design §7 T04 / PRD §6 | 结果 |
|---|---|---|
| `test_task_details_has_two_tasks_with_full_key_set` | 2 个任务各有失败记录 → `task_details` ≥2；每项键集合恰为 9 键（8 固定字段 + `suggestion`） | ✅ |
| `test_task_details_sorted_and_suggestion_mapped_by_keyword` | 按 `task_risk_score` 倒序；`connection refused` → 网络建议、`access denied` → 权限建议 | ✅ |
| `test_missing_db_type_filled_with_null_not_omitted` | 缺值填 `null` **不省略键**（`db_type` 为空 → `None`，键仍在） | ✅ |
| `test_evidence_record_ids_point_to_real_failed_records` | `details.evidence.record_ids` 逐一可在 `backup_records` 查到且 `status='failed'` | ✅ |
| `test_evidence_lives_in_details_not_basis` | 机器可读 ID 只进 `details`，`basis` 恒为人类可读 `list[str]`（LLM 路径不覆盖 details） | ✅ |
| `test_run_all_checks_returns_five_metrics` | `run_all_checks()` 返回 5 个 metric，集合恰为 5 个且含 `verify_fail`；每项 `risk_level == _level_from_score(risk_score)` | ✅ |
| `test_verify_fail_config_subtable_present` | `DEFAULT_AI_CONFIG.verify_fail` 7 个键齐全 | ✅ |
| `test_tampered_file_raises_verify_risk_to_high` | 真实落盘文件 + 正确 checksum → 追加内容篡改 → `risk_score ≥ 65`、`risk_level ∈ {high, critical}`、`layers.l1.failed ≥ 1` | ✅ |
| `test_intact_file_not_flagged_as_l1_failure` | 反向：未篡改文件不得判 L1 失败 | ✅ |
| `test_all_checksum_empty_does_not_report_critical` | 25 条 checksum 全空 → **不判 critical**，`l1.failed==0`、`l1.skipped>0`、`no_checksum_count==sample_count`、basis 提示回填 | ✅ |
| `test_verified_records_without_checksum_still_no_critical` | 已 `verified=1` 但无 checksum → 仍不误报 critical | ✅ |

---

## 2. 页面可渲染性验证

### 2.1 HTTP 探测（运行中的 8080 服务，未登录）

```
$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080<path>
/alert                           302   ✅（302=重定向登录，属正常存活）
/rt-timeline                     302   ✅
/restore                         302   ✅
/dr-link                         302   ✅
/api/disaster-links              401   ✅（API 未登录返回 401）
/api/disaster-links/sources      404   ❌  见 §4 环境问题 E1
```

### 2.2 登录态渲染 + 关键元素断言（`app.test_client()`，进程内加载最新代码）

| 页面 | HTTP | 字节数 | 关键元素断言 |
|---|---|---|---|
| `/alert` | 200 | 24515 | `data-metric="verify_fail"` 第 6 张卡 **存在** ✅ |
| `/rt-timeline` | 200 | 31087 | `id="rtEmptyState"` ✅ / `id="rtCreateModal"` ✅ |
| `/restore` | 200 | 23119 | `r_record_card` 卡片 **存在** ✅ / 「鼎甲」「迪备」**已清零** ✅ |
| `/dr-link` | 200 | 20503 | `id="linkStep1"` + `id="linkStep2"` 两步弹窗 **存在** ✅ |

复现脚本：`scripts/_qa_probe_sources.py`（QA 临时探针，验收后可删）。

---

## 3. PRD §6 验收标准逐条结果

### 问题 1 —— AI 预测告警任务级明细 + 数据验证

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1.1 | ≥2 个有失败记录的任务时，`backup_fail.details.task_details` 长度 ≥2 且每项含全部 8 字段 | ✅ 通过 | `test_task_details_has_two_tasks_with_full_key_set`：键集合恰为 `{task_id, task_name, db_type, fail_7d, fail_30d, last_fail_at, last_error, task_risk_score, suggestion}`；缺值填 `null` 不省略键（`test_missing_db_type_filled_with_null_not_omitted`） |
| 1.2 | 前端可展开明细子表；`evidence.record_ids` 每个 ID 均可在 `backup_records` 查到 | ✅ 通过 | 后端：`test_evidence_record_ids_point_to_real_failed_records` 逐 ID 回查 `status='failed'`；前端：`app.js:4794 predDetailRowHtml`（`tr.pred-detail-row.d-none`）+ `:4810 togglePredDetail`（`classList.toggle('d-none')`）+ `:4937` 首列 `▶` 展开钮，子表 8 列与 evidence 行齐全 |
| 1.3 | `run_all_checks()` 返回 5 个 metric 含 `verify_fail`；篡改备份文件后该 metric `risk_level ≥ high` | ✅ 通过 | `test_run_all_checks_returns_five_metrics`（集合恰为 5）；`test_tampered_file_raises_verify_risk_to_high`（L1 sha256 失配 → 90 分 → critical，`≥ high` 门槛 65）。5 处接线核对齐全：`DEFAULT_AI_CONFIG:172`、`save_config:285`、`predict_with_ai` 三处 `fn_map:695/711/729`、`run_all_checks:1705`、`scheduler._verify_backup:487-499` 落 sha256 |
| 1.4 | 反向：checksum 全空的存量记录**不得**判 critical，只体现为未校验占比升高 | ✅ 通过 | `test_all_checksum_empty_does_not_report_critical`：25 条空 checksum → `risk_score=55`（medium），`l1.failed=0`、`l1.skipped=20`、`unverified_ratio=1.0`，basis 提示「执行 scripts/backfill_checksum.py 回填」。代码位置 `core/ai_alert.py:1229-1230`（空 checksum 走 skipped 分支） |

**附加验证（T01 回填脚本）**：`scripts/backfill_checksum.py --dry-run --limit 5` 预演不写库（回查 `checksum` 仍为 `None`）；去掉 `--dry-run` 后实际回填值与 `db.sha256_file()` 逐字节一致 ✅（脚本 `scripts/_qa_probe_backfill.py`）。

### 问题 2 —— 实时备份 PITR 任务选择

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 2.1 | 无 `rt_enabled=1` 任务时显示空状态卡 + 可点击创建按钮，不再只显示「请选择一个实时保护任务」 | ✅ 通过 | `rt_timeline.html:113-138` `#rtEmptyState`（标题 + 3 步说明 + `#rtEmptyCreateBtn` 主按钮 + 去备份任务页）；`app.js:2483-2494 rtToggleEmptyState()` 与主体区 `rtStatRow/rtTimelineCard/rtDetailRow` 互斥显隐，并联动 `rtSyncTaskButtons()` 禁用任务级按钮 |
| 2.2 | 创建后无需刷新即自动选中新任务并渲染时间轴 | ✅ 通过 | `app.js:2635-2679 rtSubmitCreate()`：`PUT /api/rt/tasks/<id>/config` → `rtLoadTasks()` → 回填 `RT.taskId` / `select.value` → `rtLoadHealth()` → `rtLoadTimeline()`，全程无 `location.reload()` |
| 2.3 | 下拉分组标签正确区分 db-log / file 两类，且每项显示实际 RPO | ✅ 通过 | `app.js:2458-2481`：`rtGroupOf()` 按 `db_type ∈ {mysql,mariadb,postgresql}` 且 `rt_mode ∈ {db_cdc,auto,""}` 分「数据库 · 秒级日志 PITR」/「文件 · 分钟级变更捕获」，`rtBuildOptGroups()` 输出 `<optgroup>` 且空组不渲染；`:2523` 选项文本拼接实际 RPO。`?task_id=` 深链见 `:2370 RT.deepLinkId` / `:2927` |
| 2.4 | **守护 stopped 时创建任务须提示「守护未启动」而非静默成功** | ❌ **遗留 G1** | `rtSubmitCreate()` 成功分支仅 `toast("已开启实时保护，正在加载时间轴…")`，未读取 `/api/rt/status` 的 `running` 字段；模板中无 design §7 T06 要求的「stopped 提示条 + 启动按钮」，全仓 `grep "未启动"` 命中 0 条。现状只有既有守护状态条 `#rtDaemonState` 显示「已停止」文本，属被动展示，仍是**静默成功** |

### 问题 3 —— 数据恢复页面优化

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 3.1 | `grep -ri "鼎甲\|迪备" templates/ static/ core/ api/` 返回 0 条 | ✅ 通过 | 全仓 `--include=*.html/*.js/*.py/*.css` 扫描仅命中 QA 探针脚本自身的断言字符串，业务代码 **0 命中** |
| 3.2 | 选中记录后卡片区可见 任务名 / 时间 / 大小 / 校验状态 / 存储层 5 项 | ✅ 通过 | `/restore` 渲染含 `r_record_card`；`app.js onRecordChange()` 渲染 5 项独立 `<span>`（含 `verified` → ✅已校验 / ⚠️未校验、`storage_tier`） |
| 3.3 | 提交恢复后按钮 3 秒内进入禁用+加载态，完成后恢复记录表首行为本次记录 | ✅ 通过 | `app.js:936-943`：提交即 `btn.disabled=true` + `spinner-border` + `#r_progress_wrap` 不确定态进度条；`:956 loadRestores(newId)` 回传新记录 ID，`:784-788` 对匹配行打 `class="restore-row-new"` 高亮 |

### 问题 4 —— 容灾 HA 与数据同步整合

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 4.1 | 无 `sync_tasks` 且无实时任务时，保存按钮禁用并显示引导链接 | ⛔ **阻塞 B1** | `app.js:4329-4335` 逻辑本身正确（空源 → `#linkNoSource` 显形 + `linkNextBtn/saveLinkBtn` 禁用），但因 B1 契约缺陷，**有源时也会走进这个分支** → 该断言"意外恒成立"，无法作为通过依据 |
| 4.2 | 选择同步任务后，主站点 / 备站点 / 路由策略三项自动回填且与 `sync_tasks` 源值一致 | ⛔ **阻塞 B1** | 回填代码 `app.js:4379-4396 pickLinkSource()` 正确，但前端拿不到源列表 → 用户无从选择，路径不可达 |
| 4.3 | 新建链路 `source_kind`/`source_id` 非空；存量 `manual` 链路读取不报错 | ✅ 通过（后端） | `POST /api/disaster-links {source_kind:"sync_task", source_id:1}` → 201 `{ok:true, source_kind:"sync_task", source_id:1, source_name:"北京→上海 订单库同步"}`；`manual` 链路 201 且列表读取正常（`source_kind='manual'`, `source_id=null`, `source_missing=false`）。校验分支：非法 kind → 400「数据源类型非法」；引用型缺 `source_id` → 400「引用数据源时必须提供 source_id」。DDL：`core/db.py:418-419`（SCHEMA）+ `:731-744`（ALTER 兜底 + `UPDATE ... SET source_kind='manual'` 回填） |
| 4.4 | **快照语义**：链路创建后改源 `tgt_host`，已建链路 `dr_site` 保持不变 | ✅ 通过 | 改源前 `dr_site="10.20.0.1:3306/orders"` → 把 `sync_tasks.tgt_host` 改为 `10.99.99.99` 并改名后，链路 `dr_site` **仍为 `10.20.0.1:3306/orders`**（快照）；同时 `source_name` 跟随更新为「北京→广州 订单库同步(改名)」（引用）。与设计裁决②「引用 + 快照双轨」完全一致 |

**`GET /api/disaster-links/sources` 接口级验证（登录态 test_client）**：HTTP 200，返回 `sync_task` 1 条 + `rt_task` 1 条，字段含 `kind/id/name/primary_site/dr_site/db_type/enabled/last_status/last_run_at`（rt 源额外含 `rt_mode/rt_enabled`），**未下发任何密码字段** ✅。

---

## 4. 遗留问题 / 缺陷清单

> **本节为第 1 轮（初验）记录，三项缺陷均已于第 2 轮复验闭环 —— 结论见 §7。**

### B1（P0 · 阻塞模块 D 主流程 · 需工程师修复）前后端数据源契约不一致，选源 UI 恒为空 —— ✅ **已闭环（第 2 轮复验，采纳建议修法 A）**

| 项 | 内容 |
|---|---|
| 现象 | `/dr-link` 新增链路弹窗第 1 步恒显示「暂无可用数据源，请先创建数据同步任务 →」，`下一步` / `保存` 按钮恒灰（除非勾选手工模式），即使库中已有同步任务与实时任务 |
| 后端 | `api/link.py:203-209` 返回 `{"ok":true,"kinds":[...],"sources":{"sync_task":[…],"rt_task":[…]},"total":2}` |
| 前端 | `static/js/app.js:4262-4265` 读 `const items = (res && res.items) \|\| []` → **恒 `[]`** |
| 设计约定 | design §2 D 明确规定响应为 `{"ok": true, "items": [ … ]}`（扁平数组），后端实现偏离了该约定 |
| 放大因素 | 请求返回 200 不抛异常 → `loadLinkSources()` 的降级聚合分支（`app.js:4268-4301`）也不会执行，故障被完全静默 |
| 附带字段差异 | 前端渲染读 `s.status`（`app.js:4366 linkSrcStatusBadge`）与 `s.rpo_sec`（`:4355`），后端给的是 `last_status` 且 `_rt_source_item()` 不含 `rpo_sec` → 即使补齐 `items`，状态徽章仍显示「-」、RPO 不显示 |
| 复现 | `python scripts/_qa_probe_sources.py` → 观察 `sources_body` 无 `items` 键；或登录后访问 `/dr-link` 点「新增链路」 |
| 建议修法 A（推荐，前端零改动） | `api/link.py api_list_link_sources()` 响应体追加扁平 `items`：`items = sync_items + rt_items`，并在 `_sync_source_item` / `_rt_source_item` 内补 `"status": last_status`；`_rt_source_item` 再补 `"rpo_sec"`（取实时健康的 `rpo_actual_sec`，无值给 `None`）。保留现有 `sources` 分组键不动，前后端双向兼容 |
| 建议修法 B | 改 `app.js:4263` 为 `const g=(res&&res.sources)||{}; const items=[...(g.sync_task||[]),...(g.rt_task||[])].map(s=>({...s,status:s.last_status}))`；但 `rpo_sec` 仍缺，需后端配合 |

### G1（P2 · 功能缺口 · 需工程师补齐）T06 守护 stopped 兜底提示未实现 —— ✅ **已闭环（第 2 轮复验，按建议修法实现）**

| 项 | 内容 |
|---|---|
| 缺口 | PRD §6 问题 2 验收 #5 与 design §7 T06 要求：守护进程 stopped 时创建实时保护任务，页面须提示「守护未启动」并给启动入口；当前 `app.js:2635-2679 rtSubmitCreate()` 无守护态检测，仅提示成功 |
| 影响 | 守护 stopped 时任务置 `rt_enabled=1` 但 worker 不拉起、`rt_tasks` 行不建、无恢复点产出，用户以为创建成功（design §2 B 明确称之为「静默失败」，正是 T06 要解决的唯一真实缺口） |
| 建议修法 | `rtSubmitCreate()` 成功后读 `GET /api/rt/status`：`running === false` 时把成功 toast 降级为 warning（「已开启实时保护，但守护进程未启动，暂不会产生恢复点」），并在 `rt_timeline.html` 加一条常驻提示条（复用既有 `#rtDaemonBar` 区域）+「启动守护」按钮（后端接口 `app.js:2978` 已有） |

### E1（环境问题 · 非代码缺陷 · 需重启服务）8080 运行实例未加载最新代码 —— ✅ **已闭环（第 2 轮复验，主理人已重启服务）**

- `curl http://127.0.0.1:8080/api/disaster-links/sources` → **404**，而同一份代码在进程内 `app.test_client()` 下 → **200**，`app.url_map` 中该规则存在。
- 说明 8080 上的进程仍是旧代码。**四个页面的 302 探测结果因此只能证明"服务存活"，页面新元素以 §2.2 的进程内断言为准**。
- 处置：重启 8080 服务后重新探测（复验时纳入）。

---

## 5. 复验清单（工程师修复后执行）

1. `python -m pytest tests/ -q` → 期望 `329 passed`（含 `tests/test_ai_alert_taskdetail.py` 11 条）。
2. `python scripts/_qa_probe_sources.py` → 期望 `sources_body` 含扁平 `items` 数组，每项含 `status`；rt 源含 `rpo_sec` 键。
3. 重启 8080 后 `curl /api/disaster-links/sources` → 期望 200/401（不再 404）。
4. 浏览器登录 `/dr-link` → 新增链路 → 第 1 步可见两组数据源、状态徽章正常、选中后主备站点与路由策略自动回填。
5. 停掉实时守护 → `/rt-timeline` 创建实时保护任务 → 期望出现「守护未启动」提示与启动入口。

---

## 6. 附：本次验收新增/使用的文件

| 文件 | 说明 | 去留 |
|---|---|---|
| `tests/test_ai_alert_taskdetail.py` | T04 单测（11 条，全绿） | **长期保留** |
| `docs/ux-feedback-20260801-verification.md` | 本报告 | 长期保留 |
| `scripts/_qa_probe_sources.py` | 数据源端点 / 四页面 / 快照语义探针 | 临时，复验后可删 |
| `scripts/_qa_probe_backfill.py` | checksum 回填脚本行为探针 | 临时，复验后可删 |
| `tests/test_link_sources_contract.py` | B1/G1 契约单测（11 条，工程师随修复提交） | **长期保留** |
| `scripts/_qa_probe_round2.py` | 第 2 轮复验探针（后端契约 + 模板 + 源码静态断言） | 临时，可删 |
| `scripts/_qa_probe_drlink_ui.mjs` | 第 2 轮复验前端行为谐调器（Node vm 真实执行 app.js） | 临时，可删 |
| `scripts/_qa_round2_fixture.json` | 上述谐调器的后端真实响应体夹具（由探针导出） | 临时，可删 |

---

## 7. 第 2 轮复验（2026-08-01）

> 触发：工程师寇豆码依据本报告 §4 的 B1 / G1 缺陷单完成修复，主理人重启 8080 服务加载最新代码。
> 复验人：严过关（QA）｜复验依据：本报告 §5 复验清单 5 项｜复验方式：全量 pytest + 活服务 curl + 进程内 `test_client` + **Node vm 真实执行 `app.js`**
> 复验结论：**5 / 5 项全部通过，B1 / G1 / E1 三项缺陷全部闭环，无新增回归，最终结论为「完全通过」。**

### 7.1 复验结果总览

| # | 复验项 | 方法 | 结论 |
|---|---|---|---|
| 1 | 全量回归 | `python -m pytest tests/ -q` | ✅ **通过** — 340 passed / 3 skipped |
| 2 | B1 后端契约（扁平 `items` / `status` / `rpo_sec`） | 进程内 `test_client`（`scripts/_qa_probe_round2.py`，5 条断言） | ✅ **通过** — 5/5 |
| 3 | B1 环境项 E1（`/sources` 不再 404） | 活服务 `curl` | ✅ **通过** — HTTP **401**（未登录，属正常） |
| 4 | B1 前端可用（选源渲染 + 回填） | `test_client` 渲染 + Node vm 真实执行 `app.js`（8 条断言） | ✅ **通过** — 8/8 |
| 5 | G1 守护态兜底（提示条 + warning 降级） | `test_client` 模板断言 + 源码静态断言 + Node vm 行为断言（7 条） | ✅ **通过** — 7/7 |

**断言总计：20 项自动化断言（Python 侧 11 + JS 行为侧 9）全部通过 + 340 条 pytest 用例全绿。**

### 7.2 逐项证据

#### 第 1 项 · 全量回归 ✅

```
$ python -m pytest tests/ -q
........................................................................ [ 20%]
........................................................................ [ 41%]
........................................................................ [ 62%]
..............................................................s......... [ 83%]
...........s.........s.................................                  [100%]
340 passed, 3 skipped in 64.42s (0:01:04)
```

- 上一轮基线 `329 passed / 3 skipped` → 本轮 `340 passed / 3 skipped`，**净增 11 条**，与工程师新增的 `tests/test_link_sources_contract.py`（B1 5 条 + rt `rpo_sec` 4 条 + G1 2 条）**数量完全吻合**。
- **既有 329 条零回归**，失败数 0、错误数 0；skipped 仍为 3 条（与基线一致，非本次引入）。

#### 第 2 项 · B1 后端契约 ✅（5/5）

复现：`python scripts/_qa_probe_round2.py`（造 2 个同步任务 + 2 个实时任务，其中 1 个实时任务写入 `rpo_actual_sec=42` 运行态，另 1 个不写 → 覆盖 `rpo_sec` 有值/为空两个分支）。

| 断言 | 结论 | 证据 |
|---|---|---|
| 2.1 顶层含扁平 `items` 数组，长度 = 源数 | ✅ | HTTP 200；`type(items)=list`；`len(items)=4 = sync 2 + rt 2`；且 `items == sources.sync_task + sources.rt_task` 顺序拼接 **True** |
| 2.2 每项含非空 `status`（sync 项亦然） | ✅ | sync 项 `status=['never','never']`、rt 项 `status=['never','never']`，空 `status` 项 **0 个** —— 状态徽章不再恒显「-」 |
| 2.3 rt 项必含 `rpo_sec` 键 | ✅ | 缺键项 **0 个**；`rpo_sec={rt#1: 42, rt#2: None}` —— 有运行态取实际值 42，无运行态为 `None` **但键存在**（未省略键），符合契约 |
| 2.4 向后兼容 | ✅ | 顶层键 `['items','kinds','ok','sources','total']`，旧键 `kinds`/`sources`/`total` 均保留，`total=4 == len(items)` |
| 2.5 登录态保护未被破坏 | ✅ | 匿名 `GET /api/disaster-links/sources` → **HTTP 401** |

修复位置核对：`api/link.py:256-262`（`items = sources.sync_task + sources.rt_task`，`total=len(items)`）、`:62`（`_sync_source_item` 补 `status`）、`:125-127`（`_rt_source_item` 补 `status` + `rpo_sec`）、`:68` 新增 `_rt_rpo_sec()` 容错取值。工程师采纳的是本报告 §4 推荐的**修法 A（前端零改动、双向兼容）**。

#### 第 3 项 · B1 环境项 E1 ✅（上一轮 404 → 本轮 401）

```
$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080<path>
/alert                           302   ✅（未登录重定向，属正常）
/rt-timeline                     302   ✅
/restore                         302   ✅
/dr-link                         302   ✅
/api/disaster-links              401   ✅
/api/disaster-links/sources      401   ✅  ← 上一轮为 404，E1 已闭环
```

补充佐证：`curl http://127.0.0.1:8080/static/js/app.js` 取回的线上资源为 **250807 字节，与本地 `static/js/app.js` 字节数完全一致**，且线上副本中 `rtStoppedHint`（2 次）、`rtProbeDaemonRunning`（2 次）、`rtSyncStoppedHint`（4 次）均可命中 —— 证明 8080 进程**确已加载最新代码**，而非仅路由存活。

#### 第 4 项 · B1 前端可用 ✅（8/8）

分两层取证。**服务端渲染层**（进程内 `test_client`）：

| 断言 | 结论 | 证据 |
|---|---|---|
| 4.1 `/dr-link` 登录态 200 且选源/回填 DOM 锚点齐全 | ✅ | HTTP 200，20503 字节；`linkStep1`/`linkStep2`/`linkSourceList`/`linkNoSource`/`linkNextBtn`/`saveLinkBtn`/`l_source_kind`/`l_source_id`/`l_primary_site`/`l_dr_site`/`l_route_policy`/`l_manual_mode` **12 个锚点 0 缺失** |
| 4.2 「暂无可用数据源」提示默认隐藏 | ✅ | `<div class="alert alert-warning py-2 d-none" ... id="linkNoSource">` —— 默认带 `d-none`，显隐交由 JS 按实际源数决定 |

**前端行为层**（`scripts/_qa_probe_drlink_ui.mjs`：Node vm 装载**真实 `app.js`**，DOM/fetch 打桩，喂入上一步导出的**真实后端响应体**，走 `DOMContentLoaded → initDrLink() → openLinkModal()` 真实代码路径）：

| 断言 | 结论 | 证据 |
|---|---|---|
| 4.3 第 1 步渲染出数据源分组，**不再恒显「暂无可用数据源」** | ✅ | `#linkNoSource` 保持 `d-none`（隐藏）；`#linkSourceList.innerHTML` 2317 字符，含分组标题「数据同步任务（2）」「实时保护任务（2）」 |
| 4.4 四个源全部渲染、状态徽章有值、rt 源显示实际 RPO | ✅ | 源名命中 **4/4**；含 `badge` 状态徽章；含 `RPO` 文本（`rpo_sec=42` 已渲染） |
| 4.5 有源时「下一步」「保存」解禁 | ✅ | `linkNextBtn.disabled=false`、`saveLinkBtn.disabled=false` —— 上一轮「按钮恒灰」现象消失 |
| 4.6 `pickLinkSource()` 回填主/备站点且与源值一致 | ✅ | `l_source_kind=sync_task`、`l_source_id=2`；`l_primary_site="10.30.0.7:5432/users"`、`l_dr_site="10.40.0.2:5432/users"`，与源值**逐字符一致** |
| 4.7 `pickLinkSource()` 回填路由策略 | ✅ | `l_route_policy=[{"provider":"默认专线","endpoint":"10.40.0.2:5432/users","priority":1,"enabled":true}]` —— 端点取源备站点 |
| 4.8 选源后自动取消手工模式 + 链路名默认回填 | ✅ | `l_manual_mode.checked=false`；`l_name="上海→广州 用户库同步 容灾链路"` |

fetch 轨迹（证明确实打了后端而非走降级分支）：`GET /api/meta → GET /api/disaster-links/sources → GET /api/disaster-links → GET /api/disaster-links/sources`。

> 说明：上一轮 §3 中 4.1 / 4.2 两条因 B1 阻塞而「断言意外恒成立、路径不可达」，本轮已通过真实执行走通完整路径，**两条验收标准现可判定为真实通过**。

#### 第 5 项 · G1 守护态兜底 ✅（7/7）

**模板层 + 源码层**（`test_client` + 静态断言）：

| 断言 | 结论 | 证据 |
|---|---|---|
| 5.1 `/rt-timeline` 含 `#rtStoppedHint` 且默认 `d-none` | ✅ | HTTP 200；`<div class="alert alert-warning d-none d-flex align-items-center gap-2 py-2 mb-3" role="alert" id="rtStoppedHint" ...>`（`templates/rt_timeline.html:155-163`） |
| 5.2 含 `#rtStoppedHintStartBtn` 与「守护未启动」文案 | ✅ | `id="rtStoppedHintStartBtn"` 存在；文案「守护进程未启动，实时保护暂不产生恢复点」存在；按钮文案「启动守护」存在 |
| 5.3 `rtSubmitCreate()` 成功分支含守护态探测 + warning 降级 | ✅ | 函数体 1910 字符，6 项要素 **0 缺失**：`rtProbeDaemonRunning()` / `daemonRunning === false` / `"warning"` / 降级文案「但守护进程未启动，暂不会产生恢复点」/ running 分支保留原成功文案 / `rtSyncStoppedHint()` 联动（`app.js:2688-2696`） |
| 5.4 探测 / 启动 / 显隐三件套接线完整 | ✅ | `api("GET", "/api/rt/status")`（`:2421`）、`async function rtStartDaemon()`（`:2427`）、`rtStoppedHintStartBtn` 点击绑定（`:3012-3013`）、`function rtSyncStoppedHint(running)`（`:2413`）**均存在** |

**行为层**（Node vm 真实执行 `app.js`，走 `DOMContentLoaded → initRtTimeline() → rtLoadDaemon()`）：

| 断言 | 结论 | 证据 |
|---|---|---|
| 5.5 守护 `running=false` 时提示条**由隐藏变为显形** | ✅ | 初始 class 含 `d-none`；读到 `/api/rt/status {running:false}` 后 `d-none=false`，当前 `class="alert alert-warning"`（warning 级别）；守护状态文案「已停止」 |
| 5.6 确已实际请求 `/api/rt/status`（非静态兜底） | ✅ | fetch 轨迹：`GET /api/meta → GET /api/rt/status → GET /api/rt/tasks → GET /api/rt/health` |
| 5.7 **反向用例**：`running=true` 时提示条保持隐藏（无误报） | ✅ | `d-none=true`；守护状态文案「运行中」 |

> 结论：上一轮「静默成功」缺陷已消除 —— 守护 stopped 时不仅**创建动作本身**会把 toast 降级为 warning 并给出明确文案，页面还会常驻 warning 提示条并提供「启动守护」一键入口；反向用例证明守护正常时不会误报。

### 7.3 智能路由判定

| 判定 | 结果 |
|---|---|
| 源码 Bug（需回报工程师） | **0 项** —— B1 / G1 修复均符合契约，全量回归零退化，未发现任何新增源码缺陷 |
| 测试脚本 Bug（QA 自行修复） | **2 项，均已自修**：① `scripts/_qa_probe_round2.py` 列表推导式闭合括号笔误（`}` → `]`）；② `scripts/_qa_probe_drlink_ui.mjs` vm 上下文缺 `localStorage` 等裸全局导致 `bindSidebar()` 抛错。二者均为**复验脚本自身缺陷，与被测代码无关**，修复后全部通过 |
| 路由结论 | **Send To: NoOne** —— 无需回工程师，验收关闭 |

### 7.4 遗留问题

**无。** B1（P0）、G1（P2）、E1（环境）三项全部闭环；PRD §6 十四条验收标准**全部通过**；无「Known Issues」。

### 7.5 最终结论

> **完全通过（Pass）**。340 条自动化用例全绿（3 skipped 为基线固有），20 项复验断言全通过，四个模块（A 告警+数据验证 / B 实时 PITR / C 恢复页 / D 容灾链路整合）**无遗留、无阻塞**，具备交付条件。
