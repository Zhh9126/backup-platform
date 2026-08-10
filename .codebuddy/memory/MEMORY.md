# 备份管理平台 - 长期记忆

## 项目概览
- **路径**: `e:\备份管理平台\backup_platform`
- **技术栈**: Flask + SQLite + Jinja2 + APScheduler
- **端口**: 8080
- **登录**: admin / admin123
- **启动**: `cd e:\备份管理平台\backup_platform; Start-Process python -ArgumentList "run.py" -WindowStyle Hidden`

## 架构
- 9 个数据库引擎 (`core/engines/`): mysql, mariadb, postgresql, oracle, kingbase, dameng, redis, mongodb, file
- 21 个 API 蓝图 (`api/`): 每个功能模块独立 blueprint
- 23 个页面模板 (`templates/`): Jinja2 渲染
- JS 模块化: `bkp-core.js`(核心工具) + `app.js`(业务逻辑) + 各模块 JS
- 插件系统 (`core/plugins/`): manifests 驱动，**离线下载优先**，Linux only

## 9 个备份 Skills
位置: `e:\备份管理平台\backup_platform\skills\`（项目根，不是 `.codebuddy/skills/`）
- `mysql-backup`: mysqldump + xtrabackup + binlog PITR
- `mariadb-backup`: 继承 MySQL，mariabackup
- `postgresql-backup`: pg_dump + pg_basebackup + WAL
- `oracle-backup`: expdp/exp + RMAN + archivelog PITR
- `kingbase-backup`: sys_dump + sys_basebackup
- `dameng-backup`: dexp + dmrman
- `redis-backup`: RDB 快照
- `mongodb-backup`: mongodump
- `file-backup`: tar.gz 全量 + 快照增量 + 准CDP + 恢复链

## 关键文件位置
- 引擎基类: `core/engines/base.py`
- 插件目录: `core/plugin_catalog.py`, `core/plugin_installer.py`
- 插件清单: `core/plugins/manifests/*.json` (7 个，仅 Linux，离线下载优先)
- JS 核心: `static/js/bkp-core.js`
- 启动入口: `run.py` → `app.py`

## 关键 bug 修复记录
- `app.js` 加载 `/api/meta` 后必须同步到 `BKP.META`：`BKP.META = Object.assign(BKP.META, meta)`。否则 `fillDbTypeSelect()` 读不到 db_types。
- 新写 JS 时务必用 `api(method, url, body)` 签名（bkp-core.js 定义），不能简写。直接用 `fetch()` 时务必在 401 时跳转 `/login`。
- `templates/base.html` 末尾必须有 `{% block scripts %}{% endblock %}` 才能让子模板注入 script。
- 数据库路径是 `instance/meta.db`（不是 `instance/backup_platform.db`，那个是空的）。
- **独立 IIFE 文件不要直接用 `$("xxx")`**——只有 `app.js` IIFE 顶部声明了 `var $ = BKP.$`。新写 `plugins.js` 这类独立 IIFE 时必须在内部声明 `const $ = (id) => document.getElementById(id);`，或 `const { api, esc, toast, $ } = BKP;`，否则会报 `ReferenceError: $ is not defined`。
- 新写独立 JS IIFE 时不要假设 `BKP.META.display_names` 已被 app.js 填充——在 init 时主动拉 `/api/meta` 同步一次。

## 备份质量监控
- `/api/records/overrun-stats` 返回：超长备份（按 long_rule 规则判定）和超频备份（默认 5min 内同任务 ≥3 次）。
- 阈值存储在 `system_config` 表 `backup_quality_thresholds` key（JSON）。
- 仪表盘 `templates/dashboard.html` 已增加 3 个 KPI 卡片：超长/超频/单次最大 + 2 张明细表。
- `core/scheduler.py` 用 `time.monotonic()` 跟踪备份时长（亚秒级精度），避免 datetime 字符串秒级舍入导致 0.0。
- API: `GET/POST /api/settings/backup-quality-thresholds` 读写阈值。
- 超长判定规则 (`long_rule`):
  - `fixed`: 仅按 `long_minutes`（固定分钟数）。
  - `speed`: 按"实际耗时 > 数据量/预期速度 × (1+tolerance)"（推荐，例 500GB/h 1h 备份 500GB 视为达标）。
  - `both`: 两者任一即视为超长。
- 期望速度 = `expected_speed_gb_per_hour`（默认 500 GB/h ≈ 138 MB/s）。
- 浮动容忍度 = `speed_tolerance_pct`（默认 20%）。

## 插件绑定数据库
- 插件 manifest 用 `supports` 字段（list）声明关联的 db_type（mysql / postgresql / redis / ...）。
- `core/plugin_catalog.py` 暴露 `db_types` 字段（== supports），并新增 `recommended` 标志（未装+OS适配+具备安装策略）。
- 调度前 `engine.preflight()` 检查：物理备份缺客户端则硬失败并提示"前往【备份插件】页安装"；逻辑备份允许仿真兜底。
- `/api/plugins/recommend?db_types=mysql,redis` 按 db_types 过滤；`/api/plugins/batch-install` 一键安装。
- `templates/plugins.html` 已无"参照 dbcheck 插件市场设计"文案，改为"插件在服务端安装，安装后即可在备份任务中调用对应能力"。

## 页面风格统一（toolbar / card-stat / page-card）
- 所有页面用 `.toolbar` 容器（`page-title` + `page-sub` + 右侧操作按钮组）。
- KPI 卡片统一 `.card-stat` 类（`stat-icon` + `stat-num` + `stat-label`）。
- 表格容器统一 `.page-card` 类。
- 颜色变量：见 `static/css/app.css`（如 `--bs-primary-border-subtle`）。
- 不要自己造风格，沿用 dashboard/records 已有结构。

## 插件页面（dbcheck 卡片网格）
- `templates/plugins.html` + `static/js/plugins.js` + `static/css/plugins.css` 共同实现卡片网格风格。
- 顶部 toolbar 含搜索框 + 刷新 + "一键安装本机所需"（调用 `POST /api/plugins/batch-install`）。
- 两个分区 `.page-card`：「已安装」「插件市场」，每张卡片含左侧边色（绿/蓝/灰）、图标 + 名称、状态徽章、描述、db tag、依赖客户端 pill + 包管理器 pill、底部按钮。
- 安装日志 modal：状态 + 进度条 + 实时日志（auto scroll），modal 关闭时停止 polling。
- 卸载仅清理本平台下载的离线安装目录 + 元数据；系统包管理器装的 binary 仍需手动卸载——前端在 confirm 中明确告知。

## 插件系统重要变更（2026-08-01）
- **Linux Only**：最终部署目标为 Linux 服务器，所有 manifest 只保留 Linux 安装策略，移除 Windows。
- **离线下载优先**：安装策略优先级改为 fallback_download > package_manager > manual_only。
- **数据库自带工具不装**：Oracle RMAN、达梦 dmrman、金仓 sys_dump/sys_basebackup 随数据库自带，已删除对应 manifest。
- **保留的 7 个插件**：percona-xtrabackup-80, percona-xtrabackup-24, mariabackup, pgbackrest, mongodb-database-tools, redis-tools, percona-toolkit。
- **.download 后缀 bug 已修复**：`_download_and_extract()` 现在从 URL 路径中提取真实扩展名（.tar.gz/.zip 等），不再固定 .download。
- **离线目录检测**：`check_installed()` 除了系统 PATH，还会扫描安装状态文件中的 extract_dir/bin/ 路径。
