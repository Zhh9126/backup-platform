# 备份/恢复记录展示统一化与搜索 —— 增量 PRD

| 项 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 产品经理 | 许清楚 |
| 日期 | 2026-08-01 |
| 需求类型 | 增量功能（展示统一 + 搜索） |
| 项目名 | `record_display_unify` |
| 技术栈 | Python Flask + SQLite + Bootstrap 5 + 原生 JS（`static/js/app.js` 单文件） |
| 原始需求 | ①备份/恢复记录条目统一以「业务系统 + IP + 备份类型 + 备份方式 + 备份时间」输出；②备份记录页、恢复记录页增加按业务系统 / IP 的搜索框 |

---

## 1. 产品目标

1. **统一语义**：平台内所有"备份记录"的展示锚点统一为「业务系统 + IP + 备份类型 + 备份方式 + 备份时间」五要素，消除同一条记录在 4 个入口显示成 4 种样子的认知负担。
2. **可检索**：备份记录页与恢复记录页支持按业务系统、IP 快速定位记录，让运维在故障恢复时不再靠肉眼翻页。

---

## 2. 用户故事

1. **As a** 运维工程师，**I want** 在「数据价值挖掘」页的下拉里直接看到"OA @ 192.168.220.150 · 文件 · 全量 · 2026-07-20 11:31:10"，**so that** 我不用记住 `#59` 这种 ID 就能选对备份集。
2. **As a** 运维工程师，**I want** 在备份记录页输入 `192.168.220.150` 或 `OA`，**so that** 我能在几十上百条记录中秒级定位到目标业务系统的备份。
3. **As a** 值班主管，**I want** 恢复记录页也能看到"这次恢复的是哪个业务系统、源 IP 是什么、恢复的是哪种备份"，**so that** 我做事后追溯时不需要再拿 `record_id` 去备份记录页反查。

---

## 3. 字段映射表（**关键结论**）

> 结论均已通过读代码 + 查实际库（`instance/` 元数据库，14 任务 / 89 备份记录 / 12 恢复记录）双向验证。

| 用户视角术语 | 对应数据库字段 | 对应 API 字段 | 备注 |
|---|---|---|---|
| **业务系统** | `backup_tasks.name` | `task_name` | ✅ 已验证。`restore.html:64` 现有 label 就是「业务系统 / 任务名」，产品语义已在用；实测值 `OA`、`mysql-增量`、`phase2-demo-mysql`。**无独立"业务系统"字段**。⚠️ 见待确认 Q1（存在重名） |
| **IP** | `backup_tasks.host` | `source_host`（`/records/enriched` 已有）→ 建议统一新增归一化字段 `host_ip` | ⚠️ **脏数据**：实测 4 种形态 `root@192.168.220.150:22` / `192.168.220.150` / `127.0.0.1` / `本地`。**必须归一化**（见 §6 规则 R1）。与 `ssh_hosts` 表无需 JOIN；`restore_records.target_host` 是"恢复目标机"，**不是**本需求的 IP |
| **备份类型** | `backup_records.db_type` | `db_type` | ✅ 记录级真值，实测 `mysql` / `file`。与 `api/records.py:50` 导出表头「类型」列的现有约定一致。展示走 `config.DB_DISPLAY_NAMES` 映射 |
| **备份方式** | `backup_records.backup_type` | `backup_type` | ✅ 实测 `full` / `incremental`。**排除 `backup_tasks.backup_mode`**（logical/physical）：实测 14 条任务该列**全部为 NULL**，不可用；且 `api/records.py:50` 导出表头「备份方式」列已指向 `backup_type`。无 `operation` 字段 |
| **备份时间** | `backup_records.started_at` | `started_at` | ✅ **无歧义**：`backup_records` 表**根本没有 `created_at` 列**（见 `core/db.py:78-98`）。现有 restore 下拉也用 `started_at` |

**补充映射（恢复记录页专用）**

| 用户视角术语 | 对应数据库字段 | 备注 |
|---|---|---|
| 恢复目标主机 | `restore_records.target_host` | 保留原列，与「IP」并存不冲突 |
| 恢复时间 | `restore_records.started_at` | ⚠️ 与 `backup_records.started_at` **同名**，JOIN 时必须起别名 `backup_started_at` |

**JOIN 可行性已验证**：`restore_records → backup_tasks → backup_records` 三表 LEFT JOIN 实测 **孤儿记录数为 0**，可放心组装。

---

## 4. 需求池

### P0（必须做）

| 编号 | 需求 | 说明 |
|---|---|---|
| P0-1 | 后端 `/api/records` 返回记录时**补齐** `task_name` / `host_ip`（归一化后）/ `backup_mode_label` | 目前该接口只返回 `backup_records` 裸行，前端靠 `window._taskNames` 二次拼装，datamining 页拿不到任何任务信息 |
| P0-2 | 后端 `/api/restores` 返回时 **LEFT JOIN** 补齐 `task_name` / `host_ip` / `db_type` / `backup_type` / `backup_started_at` | 恢复记录页当前完全缺失五要素 |
| P0-3 | 前端新增**统一格式化函数** `fmtRecordLabel(rec)`（单行串）与 `fmtBizCell(rec)`（表格双行单元格） | 单一真源，杜绝再次分叉 |
| P0-4 | **数据价值挖掘页**下拉套用统一格式 | 当前 `#id · db_type · status · time`，最不达标 |
| P0-5 | **数据恢复页**下拉套用统一格式（补上缺失的「备份方式」） | 当前缺 full/incremental |
| P0-6 | **备份记录页**表格：`任务`列 → `业务系统`列（名称 + IP 双行）；新增 `备份类型`列；`类型`列表头改名 `备份方式`；`开始`列表头改名 `备份时间` | 字段本身不用换，主要是补列 + 正名 |
| P0-7 | **恢复记录页**表格：新增 `业务系统`（名称 + IP）/ `备份类型` / `备份方式` / `备份时间` 四列 | 与备份记录页对齐 |
| P0-8 | **备份记录页**增加搜索框（业务系统 / IP 两个输入框），后端 `?keyword=` LIKE 过滤 | 见 §5 搜索策略 |
| P0-9 | **恢复记录页**增加搜索框（同上） | 同上 |
| P0-10 | IP 归一化规则 R1 落地为后端工具函数 | 见 §6 |

### P1（可选 / 后续）

| 编号 | 需求 | 说明 |
|---|---|---|
| P1-1 | `config.DB_DISPLAY_NAMES` 补 `file: "文件"`、`full: "全量"` / `incremental: "增量"` 中文映射 | 当前 `file` 无映射会裸奔显示 `file`；建议中文化提升一致性 |
| P1-2 | 恢复页下拉的搜索改为复用后端 `?keyword=` | 当前是前端过滤（`app.js:987`），能用，不阻塞 |
| P1-3 | 搜索框加 300ms 防抖 | 数据量上来后体验优化 |
| P1-4 | CSV/Word/PDF 导出（`/api/records/export`）表头与五要素对齐 | 用户本次未提，建议顺带对齐避免二次返工 |
| P1-5 | `/api/records` 支持 `?limit=` 透传 | datamining 页传了 `?limit=200` 但后端**当前忽略**，硬编码 500 |

---

## 5. 影响页面 / 组件清单

| # | 页面路径 | 模板 | 前端函数（`static/js/app.js`） | 后端 API | 改动点 |
|---|---|---|---|---|---|
| 1 | `/datamining` | `templates/datamining.html:16`（`#dmSource`） | `initDataMining()` **L3319-3330** | `GET /api/records` | 下拉 option 文案换成 `fmtRecordLabel()`。当前拼装 `# id · db_type · status · time` |
| 2 | `/records` | `templates/records.html:23-27`（表头）、`#recordTable` | `loadRecords()` **L560-607**、`initRecords()` **L760-775** | `GET /api/records`（**扩展**：加 `task_name`/`host_ip`/`keyword`） | 表头改名 + 补列；`业务系统`单元格用 `fmtBizCell()`；工具栏加两个搜索框，`onchange`/`oninput` 触发 `loadRecords()` |
| 3 | `/restore` | `templates/restore.html:63-76`（`#r_search`/`#r_search_ip`/`#r_record`） | `renderRestoreRecords()` **L987-1018** | `GET /api/records/enriched` | option 文案补「备份方式」；IP 归一化改用 R1（**修 bug**：现有正则对 `本地` 会静默丢弃 `@IP` 段，导致格式不统一）。搜索框**已存在**，无需新增 |
| 4 | `/restore_records` | `templates/restore_records.html:12-16` | `initRestoreRecords()` **L1971-1979** | `GET /api/restores`（**扩展**：JOIN + `keyword`） | 补 4 列 + 加搜索框。**当前状态确认：完全没有五要素、也没有搜索框，改动量最大** |
| 5 | （连带）`/restore` 页内恢复记录表 | `templates/restore.html` | `loadRestores()` **L784-797** | `GET /api/restores` | 与 #4 同源，建议同步套用，避免同页两种风格 |
| 6 | （连带）`/clone` 页备份记录下拉 | — | `loadRecordsInto()` **L4181-4191** | `GET /api/records` | P1 顺带统一，当前格式 `#id db_type (task#N) status` |

**后端文件改动**
- `api/records.py` — `list_records()` 加 `keyword` 参数 + 字段补齐
- `api/restore.py` — `list_restores()` 加 JOIN + `keyword`（**注意：不存在 `api/restore_records.py`，恢复记录复用 `/api/restores`**）
- `core/models.py` — `list_records()` / `list_restores()` 加 `keyword` 形参与 SQL LIKE
- `config.py` — P1-1 显示名映射

---

## 6. 搜索策略与归一化规则

### 搜索策略结论：**后端 SQL LIKE**（推荐）

**理由（数据说话）**：
- 当前量级（89 备份记录 / 12 恢复记录）前后端过滤性能上**无差别**，本决策不是性能问题，而是**正确性问题**。
- `models.list_records()` 硬编码 `LIMIT 500`、`list_restores()` `LIMIT 200`。前端过滤只能在"最近 N 条"里搜，**记录增长后会静默搜不全**——用户搜不到旧记录却不知道为什么，这是比慢更糟的体验。
- 恢复记录页为拿到 `task_name` / `host` **本来就必须 JOIN `backup_tasks`**，加个 `WHERE ... LIKE` 几乎零边际成本。

**接口设计**
```
GET /api/records?keyword=OA&task_id=3
GET /api/restores?keyword=192.168.220.150
```
- 单个 `keyword` 参数，后端对 `backup_tasks.name` **OR** `backup_tasks.host` 同时做 `LIKE '%kw%'`（大小写不敏感）。
- **不建议**拆成 `?biz=&ip=` 两个参数：用户经常不确定输的是名字还是 IP，单框模糊匹配命中率更高、实现更简单。前端两个输入框可拼成一个 keyword，或直接改为**单个搜索框**（见 §7 UI 建议）。

### 规则 R1：IP 归一化（**P0，必须**）

输入 `backup_tasks.host` 存在 4 种实测形态，统一处理为：

| 原始值 | 归一化输出 | 规则 |
|---|---|---|
| `root@192.168.220.150:22` | `192.168.220.150` | 去 `user@` 前缀、去 `:port` 后缀 |
| `192.168.220.150` | `192.168.220.150` | 原样 |
| `127.0.0.1` | `127.0.0.1` | 原样 |
| `本地` | `本地` | **非 IP 时原样透出，不得置空** |
| `NULL` / `''` | `-` | 兜底占位 |

> ⚠️ 现有 `app.js:1003` 的 `/\d+\.\d+\.\d+\.\d+/` 正则**只保留 IPv4**，遇到 `本地` 会返回空串并把整个 `@IP` 段吞掉——这正是用户截图里"格式不统一"的直接成因。R1 必须**先剥离再兜底**，而非"匹配不到就丢弃"。

---

## 7. UI 文案 / 格式建议

### 推荐模板 A：下拉选项（单行串）—— 用于 `/datamining`、`/restore`、`/clone`

```
#{id} {task_name} @ {host_ip} · {db_type_label} · {backup_type_label} · {started_at}
```

**渲染示例**
```
#59 OA @ 192.168.220.150 · 文件 · 全量 · 2026-07-20 11:31:10
#33 mysql-增量 @ 192.168.220.150 · MySQL · 全量 · 2026-07-20 10:38:22
#106 123-增量 @ 本地 · 文件 · 增量 · 2026-07-31 11:10:41
```

**推荐理由**
1. **严格遵循用户口述顺序**「业务系统 + IP + 备份类型 + 备份方式 + 备份时间」，避免验收时来回改。
2. **`@` 标记 IP**：沿用现有 restore 下拉已有的视觉习惯（`@127.0.0.1`），用户已建立认知，改动最小。
3. **`·` 分隔而非 `[]` 混排**：现有 `phase2-demo-mysql [mysql] @127.0.0.1 - 2026-07-30 10:29:36` 混用了 `[]`、`@`、`-` 三种分隔符，视觉噪音大。统一为中点分隔更易扫读。
4. **保留 `#{id}` 前缀（重要）**：实测库中 `phase2-demo-mysql` 一名对应 **8 个任务**、`123` 对应 2 个，且 host 相同。若去掉 ID，下拉里会出现多条**肉眼完全无法区分**的选项，用户选错就是恢复错数据——这是安全底线，**建议不要为了"干净"而砍掉 ID**。

### 推荐模板 B：表格单元格（双行）—— 用于 `/records`、`/restore_records` 的「业务系统」列

```html
<div class="fw-bold">OA</div>
<div class="small text-muted">192.168.220.150</div>
```

**推荐理由**：表格已有独立的「备份类型」「备份方式」「备份时间」列，**不应**把五要素挤进一个单元格——那会牺牲列排序能力和横向扫读效率。业务系统与 IP 是强绑定的一对，双行合并即可；其余三要素各占一列，语义更清晰。

### 表头文案对齐

| 页面 | 原表头 | 新表头 |
|---|---|---|
| `/records` | 任务 | **业务系统** |
| `/records` | （无） | **备份类型**（新增列，`db_type`） |
| `/records` | 类型 | **备份方式**（同字段 `backup_type`，仅正名） |
| `/records` | 开始 | **备份时间** |
| `/restore_records` | （无） | **业务系统** / **备份类型** / **备份方式** / **备份时间**（新增 4 列） |
| `/restore_records` | 开始 / 结束 | **恢复开始** / **恢复结束**（与「备份时间」区分，避免歧义） |

### 搜索框文案

建议**单框**（理由见 §6）：
```
placeholder: "搜索业务系统 / IP…"
```
若坚持双框，则沿用 `/restore` 页现有文案：`业务系统 / 任务名` + `IP / 主机`，保持全站一致。

---

## 8. 待确认问题

| # | 问题 | 现状 / 我的建议 |
|---|---|---|
| **Q1** | **「业务系统」是否就等于任务名？** 实测 `phase2-demo-mysql` 有 8 个同名任务、`123` 有 2 个，任务名重复严重且更像"任务"而非"业务系统"（只有 `OA` 像真实业务系统名）。 | 本期**建议先复用 `tasks.name`**（零成本、语义已在 `restore.html` 中使用），并靠 `#{id}` 前缀消歧。**若用户期望的是独立的业务系统台账**（一个业务系统下挂多个备份任务），则需要新增 `backup_tasks.biz_system` 字段 + 任务表单录入 + 数据回填，属于独立需求，**建议单独立项**。请主理人向用户确认。 |
| **Q2** | **恢复记录里的「IP」指源业务系统 IP，还是恢复目标机 IP？** 两者都存在且都有意义（`tasks.host` vs `restore_records.target_host`）。 | **建议：「IP」= 源业务系统 IP（`tasks.host`）**，以保证与备份记录页语义一致（用户原话是"与备份记录一致"）；`目标主机`作为独立列保留。请确认。 |
| **Q3** | **「备份类型 / 备份方式」的中文取值是否需要汉化？** | 建议 P1 汉化（`mysql→MySQL`、`file→文件`、`full→全量`、`incremental→增量`）。若用户/运维习惯看英文原值，则保持原样即可，改动为零。 |

---

## 9. 验收标准

1. 在 `/datamining`、`/restore`、`/records`、`/restore_records` 四个入口，同一条备份记录（如 `#59`）展示的五要素**取值完全一致**。
2. `本地`、`root@x.x.x.x:22` 两种脏 host 在所有入口均能正确显示 IP 段，**无空白、无丢字段**。
3. `/records`、`/restore_records` 输入 `OA` 或 `192.168` 能正确过滤，清空搜索框恢复全量列表。
4. 恢复记录页可直接读出"哪个业务系统、源 IP、备份类型、备份方式、备份时间"，无需跳转反查。
