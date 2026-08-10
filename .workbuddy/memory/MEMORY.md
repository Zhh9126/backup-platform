# 项目长期记忆：备份管理平台

## UI 设计规范（最高约束）
- **唯一标准源**：`D:/01【working】/000-【自动化运维工具】/UI_DESIGN_SPEC(1).md`（Slate + Teal 专业克制体系）。
- 技术栈：Python + Flask（Bootstrap 5 CDN + Bootstrap Icons + 自定义 `static/css/app.css`）。
- 调色板主色 `--primary: #0D9488`（teal-600），侧边栏 `#1E293B`，页面底 `#F8FAFC`，主文字 `#0F172A`。
- **严禁**：蓝紫渐变、Ant Design 蓝(#1677ff/#1890ff)、霓虹/荧光色、纯黑 #000000、任何非 Token 硬编码颜色。
- 所有颜色/间距/圆角/阴影/字号必须引用 `app.css` 中的 Design Tokens，禁止随意硬编码。
- 未实现明暗主题切换（规范要求固定调色板，刻意省略）。

## 数据文件备份功能（无 Agent / SSH 直连）
- 扩展点：`db_type="file"`，复用 `core/engines` 注册表与调度器，数据库与文件备份数据流分离。
- 引擎：`core/engines/file.py`（`FileBackupEngine`）— local/remote(SSH) 四种传输组合、全量+增量、tar.gz 归档；`extra_options` 存 source/target/排除规则；**源主机与目标主机独立**（`source_host`/`target_host` 指向 `ssh_hosts.host_key`）。
- 主机纳管：`ssh_hosts` 表 + `core/ssh_hosts.py` + `api/hosts.py`（CRUD + 连接测试）；密码 XOR+base64 加密。
- 页面：`templates/file_backup.html`（侧边栏"文件备份"导航），逻辑在 `static/js/app.js` 的 `initFileBackup()`。
- API：文件任务走现有 `/api/tasks`（加 `db_type_exclude=file` 让 DB 任务页排除 file）；主机走 `/api/hosts`。
- **运行环境坑**：必须用系统 Python 3.14.3（已装 Flask/paramiko/APScheduler）；managed 3.13.12 无依赖。
- file.py 历史坑已修：`_ssh_exec_pipe` 必须保持二进制 bytes（勿按文本编解码）；需 `import subprocess, shutil`。

## 平台更名与导航（2026-07-17）
- 平台名统一为 **数据备份管理平台**（base.html 品牌文字+title、login.html title/品牌）。
- 侧边栏分组：概览(仪表盘) / 备份管理(数据库备份、文件备份、数据同步、存储管理、保护策略) / 记录(备份记录、恢复记录) / 数据恢复管理(数据恢复、数据库部署) / 灾备管理(迁移保护、容灾链路、克隆服务) / 运维(巡检、智能告警、数据价值挖掘、系统设置、退出登录)。
- 克隆服务图标：Bootstrap Icons 不存在 `bi-clone`，使用 `bi-layers`。
- **数据库备份** = 原"任务管理"页（/tasks，file 任务已排除）；标题同步改为"数据库备份"。
- 新增 `.nav-group-label` 样式（app.css）。

## 仪表盘增强
- `/api/dashboard` 新增 `db_task_count`、`file_task_count`、`total_size_gb`（累计备份体积以 GB 计）。
- dashboard.html 统计卡改为：数据库备份任务 / 文件备份任务 / 累计备份体积(GB) / 成功失败。

## 数据同步功能（2026-07-17）
- 表：`sync_tasks`（源可托管现有备份任务或手动填连接；目标手动填）、`sync_records`。
- 引擎：`core/sync.py` `run_sync()` — 托管源解析 `models.get_task`；连通性探测 `core/probe.py`；同源同类型且客户端齐全时真实 dump|load（MySQL/PostgreSQL），否则仿真；失败触发 `notifier`。
- 调度：scheduler 注册 sync 任务（`_job_wrapper_sync`，job id 前缀 `sync_`）。
- API：`api/sync.py`（`/api/sync/tasks` CRUD + `/run` + `/api/sync/records`）；页面 `templates/sync.html` + `initSync()`。

## 恢复记录页面
- 新增 `/restore_records`（`templates/restore_records.html` + `initRestoreRecords()`），与"备份记录"在导航中紧邻。底层复用 `restore_records` 表与 `/api/restores`。

## 巡检功能 + 通知配置（2026-07-17）
- 表：`inspection_records`、`system_config`(key/value)。
- 引擎：`core/inspection.py` `run_inspection()` — 对任务做连通性(`probe`)+调度+上次状态体检，判定 pass/warn/fail；任一 fail 立即 `notifier.notify("failure",...)`。
- API：`api/inspection.py`（`/api/inspection/run` + `/api/inspection/records`）；页面 `templates/inspection.html` + `initInspection()`。
- 通知：`core/notifier.py` `Notifier` 现读取 `system_config.notify`（DB 配置优先于 `config.NOTIFY_DEFAULTS`），支持 webhook/钉钉/企微/飞书/邮件。
- 通知配置 UI：设置页新增"通知配置"卡片（`/api/notify-config` GET/POST）；密码不回显，留空表示不改。
- **坑**：`api/system.py` 的 notify 端点需 `from flask import request`（已修）。

## AI 预测告警 + 模型接入
- 每个分析器返回 `predicted_content`（人类可读预测结论）+ `basis`（list[str] 依据因子）；前端展示依据详情弹窗。
- AI 模型配置入口在系统设置页（折叠子卡片），支持 5 种 provider：OpenAI / Anthropic / Ollama / 本地(未实现) / 自定义。
- 密钥加密：`core/ai_secret.py`，XOR+base64，前缀 `aienc:`；`get_safe_config()` 掩码 api_key。
- URL 拼接规则（`_get_model_uri()`）：含 `/chat/completions` → 直接用；含 `/v1` → 追加 `/chat/completions`；否则追加 `/v1/chat/completions`。
- 请求体必须含 `"stream": false`；响应解析优先 `choices[0].message.content`，空时回退拼接 `choices[*].delta.content`。
- 降级策略：模型调用失败/超时/解析错误 → 回退规则引擎，永不丢失预测。
- API：`POST /api/alerts/model/test`（测试连接）、`GET /api/alerts/model/status`（模型状态）。

## 通用约定
- 新增蓝图须在 `api/__init__.py` 注册；新增页面须在 `app.py` 加路由、base.html 加导航、app.js 加 `initXxx()` 与启动分支。
- 所有列表/表格页用 `.page-card` 平铺；状态用 `statusBadge()`；DB 任务页用 `db_type_exclude=file` 排除文件任务。
- **API 蓝图模式**：本项目的 API 模块（tasks/records/storage 等）共用 `api/__init__.py` 中的 `api_bp`，通过 `from . import api_bp` + `@api_bp.route()` 注册路由。**不要**创建独立 Blueprint 对象（不会被自动注册）。
- **启动入口约定（重要坑）**：必须用 `python run.py` 启动——它调用 `scheduler.start_scheduler()` 拉起后台调度器（定时备份/巡检/清理）+ RT 守护。`app.py` 只定义 `create_app()`，**不启动调度器**；用 `python app.py` 起的服务 Web 能跑，但所有定时任务与 RT 守护全不工作（此前长期被此掩盖）。生产/gunicorn：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`。`RT_BACKUP_ENABLED` 默认 true（config.py:80），`start_scheduler()` 内 `_register_rt_backup` 会自动拉起 RtSupervisor（日志见 `[rt.supervisor] 已启动` + `[scheduler] 已注册周期任务 rt_health/rt_prune/rt_watchdog`）。

## 三级存储管理体系（2026-07-29 引入；层级定义随后按用户需求重定）
- **架构（当前生效，以代码为准）**：L1 = MinIO 热数据（第一落点）→ L2 = S3 冷数据（异地容灾）→ L3 = 源端本地路径导出（离线转移）。
  - ⚠️ 早期设计曾为 L1本地→L2 MinIO→L3 S3，后按用户需求重定为 MinIO=L1 / S3=L2 / 本地导出=L3。**权威来源是 `core/storage_backends/__init__.py` 的 `TYPE_META`（minio=1/s3=2/local=3），不要反向"修正"回旧模型。**
  - `tier_replication.replicate_to_tiers` 把本地备份文件**并行**复制到各层级目标（非严格 L1→L2→L3 级联），`storage_tier` 记为形如 "minio+s3+local" 的层级令牌。
- **核心模块**：
  - `core/storage_backends/` — 统一驱动抽象层（StorageBackend 基类 + Local/MinIO/S3 三个实现）
  - `core/tier_replication.py` — 三级复制引擎（`replicate_async` 异步不阻塞备份主流程；`replicate_to_tiers` 同步编排）
  - `api/storage.py` — 12 个 REST API 端点（CRUD + 测试连接 + 手动触发复制 + 复制策略）
  - `templates/storage.html` + `app.js initStorage()` — 前端管理页面
- **数据模型**：`storage_targets` 表（name/type/tier/endpoint/密钥/bucket 等；**tier 由后端按 TYPE_META 推导，前端不再硬编码**）；`backup_records.storage_tier` 记录每条备份实际到达的层级（如 "minio+s3+local"）
- **复制策略配置**：`system_config.replication_strategy`，字段 `push_l1_minio` / `push_l2_s3` / `push_l3_local` / `timing` / `max_retries` / `retry_interval`。**前端复制策略模态框与后端严格一致；旧字段名 `replicate_l1_to_l2` 等已废弃、被白名单忽略。**
- **依赖**：minio SDK v7.2.20（同时兼容 MinIO 和 AWS S3）
- **集成点**：`scheduler._execute_backup()` 备份成功后调用 `tier_replication.replicate_async()`
- **设计参考**：Databasus 项目的 StorageFileSaver 统一接口模式（Go→Python 适配）
- **生命周期模块（lifecycle）**：`core/lifecycle.py` 仅做 L1(MinIO)→L2(S3) 按龄/按容量下沉 + 全局到期清理；L3 本地导出为复制终态，不参与自动流转。`l2_to_l3_days` 配置项当前为保留字段（未使用），UI 已标注。

## VM 级 CDP 调研 → 收窄为 DB+文件级准 CDP（2026-07-31）
- 报告：`docs/cdp-vm-clone-research.md`（架构师高见远）。
- **结论**：VM 级"真 CDP"开源无法集成；可落地的是**准 CDP**。
- **用户决策**：不做 VM 级 CDP，只做**数据库+文件级准 CDP 实时备份**（DB 秒级日志 PITR + 文件分钟级近实时捕获），管理端跨 Windows/Linux。
- PRD：`docs/rt-backup-prd.md`；架构设计：`docs/rt-backup-design.md` + 2 张 Mermaid 图。
- **T01-T05 均已实现**（2026-08-01 收口）：在 T01 基建（`core/rt/` 日志仓库+PIT Journal + `core/db.py` 表 recovery_journal/rt_capture_state/rt_tasks/log_repository + `core/models.py` CRUD）之上：
  - T02 文件近实时捕获：`core/rt_backup/watchers/`（base/polling/watchdog_watcher）+ `file_rt.py`
  - T03 DB CDC 守护 + 总控：`core/cdc/`（mysql_binlog/pg_wal/kingbase_wal/dameng_logmnr/oracle_logminer/simulated）+ `supervisor.py` + `db_rt.py`
  - T04 PITR 恢复引擎 + API：`pitr.py` + `api/rt.py`（/rt/points|timeline|preview|recover|health|status 等全就绪）
  - T05 前端时间轴：`static/js/app.js` 的 initRtTimeline/rtLoadTasks/rtSubmitCreate/rtRecover + `app.py` /rt-timeline
- **调度生命周期集成（T03-S3，2026-08-01 完成）**：`core/scheduler.py` 接入 RtSupervisor —— `start_scheduler()` 调 `_register_rt_backup()` 拉起守护并注册 health/prune/watchdog 三周期 job（均 max_instances=1+coalesce=True）；`stop_scheduler()` 优雅停守护；`reload_scheduler()` 跳过 rt_supervisor_tick 不重启守护。`core/rt_backup/__init__.py` 的 `start(scheduler=None)` 透传共享调度器。新增 `tests/test_rt_scheduler.py`（5 用例）。
- RT 测试现状：`pytest tests/` RT 套件 **347 passed / 1 skipped**（含 test_rt_t01/rt_t02_t05/rt_t06_cdc/rt_journal/scheduler/qa_phase_3_4）。
- 坑（历史遗留，未动）：旧 `core/rt/log_repo.py` 与 `core/rt_backup/repo.py` 配额键名不统一（current_bytes/quota_bytes vs bytes/quota_gb）。

## AI 预测告警透明化（2026-07-31）
- `core/ai_alert.py`：每分析器新增 `predicted_content`（人类可读结论）与 `basis`（依据因子列表）。
- `_l1_usage()` 修正存储层级：原把 L3(local) 误当 L1，现 L1=MinIO（type='minio'），远程用量获取失败时优雅降级。
- `core/db.py`：`alert_predictions` 新增 `predicted_content TEXT` + `basis TEXT` 列（自动加列迁移）。
- 前端：alert.html 预测说明文字 + "预测内容"列 + 依据详情模态框（basisModal）；settings.html 新增「AI 预测告警配置」卡片。
- 配置后端复用 `/api/alerts/config`（两处共享）。

## AI 模型接入配置入口（2026-07-31）
- `core/ai_secret.py`：XOR+base64 加密模块（前缀 `aienc:`），密钥来源 env AI_SECRET_KEY > SECRET_KEY > config。
- `core/ai_alert.py DEFAULT_AI_CONFIG.ai_model`：10 字段（enabled/provider/endpoint/api_key/model_name/local_model_path/request_timeout_sec/max_input_chars/prompt_template）。
- `get_safe_config()`：GET 接口掩码 api_key → `***hidden***` + `api_key_set: bool`。
- `_call_model`：OpenAI 兼容 `POST /v1/chat/completions`（支持 openai/anthropic/ollama/custom）；失败降级规则引擎。
- `POST /api/alerts/model/test` + `GET /api/alerts/model/status`：测试连接 + 状态查询。
- 前端：settings.html 折叠子卡片「AI 模型接入配置」（9 字段 + 测试按钮 + 状态徽章）；alert.html 增加「模型来源」列（规则引擎 / OpenAI:gpt-4o-mini / 本地(未实现)）。
- 遗留：provider=local 仅校验路径存在，不实现真实本地推理。

## AI 智能问答 Agent 修复（2026-08-01）
- **崩溃 Bug（第1轮）**：`executor._call_get/_call_post` 返回裸 list（后端 `jsonify(list)`），`agent._react_loop` `exec_result.get(...)` 崩溃 → 修为包装 `{"ok":True,"data":...,"is_collection":bool}` + `isinstance` 防御。
- **截断泄漏 Bug（第2轮，5 处）**：
  - P2-3 `ai_alert._call_model` 硬编码 `max_tokens=1024` → 加 `_resolve_max_tokens`/`_resolve_timeout` 三级优先级（调用方 > cfg > 默认）；agent 侧 `AGENT_MAX_TOKENS=4096` / `AGENT_TIMEOUT_SEC=60` / `_invoke_model_with_retry`（只重试 timeout/network）。
  - P2-2 `tools.py` `list_recent_records` api_path `/api/backup-records`(404) → `/api/records`。
  - P1-1 `_extract_answer_from_llm` 非 answer 分支裸 `return text` → 降级链 `_recover_truncated_response` → `_strip_structural_noise` → `POST_ACTION_FALLBACK`。
  - P1-2 格式3 `re.search`（首匹配）→ `re.finditer` 遍历所有候选。
  - P2-1 空围栏体 `return None`（落纯文本泄漏 ```json）→ `return INCOMPLETE_ANSWER_FALLBACK`。
- **零回归铁律**：ai_alert 预测告警 max_tokens 仍 1024 / timeout 仍 30s；`_call_model(prompt, cfg, max_tokens=None, timeout=None)` 可选参数不传则用默认。
- 验证：`qa_verify_truncation.py` 33 场景 0 泄漏；`qa_verify_max_tokens.py` 33 断言全通过；全量 318 passed/3 skipped；线上 12/13 成功（92%），四项噪声全 0。

## UX 反馈增量闭环（2026-08-01）
- 4 项体验问题（用户截图反馈）：① AI 预测告警不科学（需任务级失败明细 + 数据验证）② 实时备份 PITR 无任务选择 ③ 恢复页优化 + 去掉「参考鼎甲迪备设计」文案 ④ 容灾 HA 无数据源，应与数据同步整合。
- 设计：`docs/ux-feedback-20260801-design.md`（T01-T10，含 §0 文件名纠正与 §9 裁决）；PRD：`docs/ux-feedback-20260801-prd.md`；验收：`docs/ux-feedback-20260801-verification.md`（2 轮，最终完全通过）。
- 后端：`analyze_backup_failure_risk` 按 task_id 分组写 `details.task_details/evidence`；新增 `analyze_backup_verify_risk`（metric=verify_fail，L1 sha256/L2 可用性三层校验，checksum 空不误报 critical）；`disaster_links` 加 `source_kind/source_id` 列 + `GET /api/disaster-links/sources`（扁平 `items` + `status` + `rpo_sec`）；`scripts/backfill_checksum.py` 回填脚本。
- 前端：alert.html 第6张验证卡 + 任务明细展开子表；rt_timeline.html 空状态 + 创建弹窗 + 守护停止提示条(#rtStoppedHint)；restore.html 去鼎甲文案 + 5 字段卡片；drlink.html 两步弹窗选源。
- QA 闭环缺陷：B1（P0，`/sources` 后端返回嵌套 `sources` 但前端读 `res.items` 扁平数组 → 后端补扁平 items，前端零改动）；G1（P2，实时创建未检测守护态 → 已加 `/api/rt/status` 探测 + warning 降级 + 启动入口）。
- 测试演进：基线 318 → 329（新增 `tests/test_ai_alert_taskdetail.py` 11条）→ 340（新增 `tests/test_link_sources_contract.py` 11条）。最终 **340 passed / 3 skipped，0 遗留**。
- 坑：改 `core/*`、`api/*` 模块后必须重启 8080 服务才生效（QA 报告 E1：旧进程对新端点返回 404，新进程才 200）。
