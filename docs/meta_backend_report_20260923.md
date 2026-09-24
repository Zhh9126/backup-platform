# 元数据库可插拔后端（SQLite → PostgreSQL/MySQL）验证报告 + P0 路线

日期：2026-09-23 ｜ 版本基线：v1.4.13（未提交改动）

---

## 1. 背景与目标

对标云祺/Veeam 替换方案的能力差距分析（2026-09-23）确认 §9 P0 缺口。本文档覆盖两件事：

1. **元数据库可插拔后端**（本轮已开发并真实验证）——平台 HA 路线的第一步：元数据库从「仅 SQLite + 单机」解锁为「可选 PostgreSQL/MySQL」，输入账号密码即可无缝切换。
2. **P0 四件套路线**（规划）——不可变备份 + 平台 HA 的落地顺序。

## 2. 功能说明（元数据库切换）

- 入口：系统设置 →「元数据库」面板。
- 后端：`sqlite`（默认，零依赖离线交付）/ `postgresql` / `mysql`（含 MariaDB）。
- 语义：填账号密码 → 测试连接 → 预检（目标库幂等建表 + 逐表行数预览）→ 执行切换（持全局写锁：目标建表 → 全量搬移 → 逐表行数校验 → 热切换连接工厂 → 持久化）。**切换即热生效，无需重启**；重启后由 `instance/meta_backend.json` 自动恢复（密码 `enc:` 加密存储）。
- 回切：先把当前后端数据全量搬回 SQLite 并校验，再热切回；SQLite 文件全程保留。
- 目标库始终视为**影子副本**（重复切换前自动清空重建），源库为权威。

## 3. 实现位置

| 模块 | 内容 |
|---|---|
| `config.py` | `META_BACKEND_FILE` / `load_meta_backend()`（环境变量 > json 文件 > sqlite） |
| `core/db.py` | `_ACTIVE` 运行时后端状态；`open_backend_conn()`（psycopg2/pymysql）；`_MetaConn/_MetaCursor/_Row` 适配器（qmark→pyformat 翻译、DDL 方言翻译、PG INSERT 自动 RETURNING 主键、DDL 立即提交、失败语句自动回滚）；`_translate_schema_ddl()`（AUTOINCREMENT→SERIAL/AUTO_INCREMENT、MySQL 保留字反引号、CREATE INDEX IF NOT EXISTS 降级）；`upsert_sql()`；`switch_backend()/backend_status()/qcol()`；`init_schema(conn, backend)` 支持外部连接；`_write_lock` 改 RLock（切换流程同线程重入） |
| `core/meta_migrate.py` | 迁移引擎：表清单（sqlite_master/information_schema）、外键拓扑排序、列同步（补齐历史 ALTER 列）、批量拷贝、逐表行数校验、PG setval 序列归位、目标清空、`switch_flow()/rollback_flow()` |
| `api/meta_db.py` | `GET /api/meta-db/status`、`POST /api/meta-db/test|plan|migrate|rollback`（PG 库不存在可自动建库） |
| `templates/settings.html` | 元数据库面板（连接表单/测试/预检/切换/回切，二次确认） |
| `tests/test_meta_backend.py` | 10 条离线回归 |

业务层 300+ 处 `db.execute/query/query_one` 调用点**零改动**。方言修补点：`upsert_plugin_host_state`、`set_system_config`、`models.upsert_rt_state`（upsert 构造器）；`api/storage.py` 两处 `INSERT OR REPLACE` 改 `set_system_config`；`backup_cleanup.py` `||` 拼接按后端分支；6 处 `system_config.key` 引用走 `qcol()`（MySQL 保留字）。

## 4. 真实验证记录（本机 PostgreSQL 14.12 @ 5785）

| # | 步骤 | 结果 |
|---|---|---|
| 1 | 源库基线：46 表 / 22307 行 | ✅ |
| 2 | `switch_flow`：目标建表 → 全量搬移 → 逐表计数校验（含外键拓扑序、历史列同步） | ✅ 全部一致 |
| 3 | PG 下 CRUD：INSERT 返回自增 id（RETURNING）、query、set/get_system_config | ✅ |
| 4 | upsert 三处真实验证：plugin_host_state、rt_capture_state（ON CONFLICT）、system_config | ✅ |
| 5 | PG SERIAL 序列 setval 归位后继续插入不冲突 | ✅ |
| 6 | `meta_backend.json` 持久化（密码 enc: 加密）；模拟重启（新进程）后自动恢复 PG 后端并正常查询 | ✅ |
| 7 | 回切 `rollback_flow`：PG→SQLite 全量搬回 + 校验；切换期间写入的行被带回 | ✅ |
| 8 | 回归：`test_meta_backend.py` 10 passed；`test_rt_journal + test_backup_cleanup + test_global_dedup + test_api_contract` 77 passed（基线同批全绿）；`create_app` + Flask test client 冒烟、/settings 面板渲染 | ✅ |
| 9 | 环境清理：测试库 `aidbm_meta_test` 已删除、平台恢复 SQLite 后端 | ✅ |

### 验证中发现并修复的关键问题（对齐 SQLite 语义）

1. **事务污染**：PG 中一条失败语句会中止事务；适配器在 execute 失败时自动 rollback（对应 SQLite 逐句容错语义）。
2. **DDL 回滚丢表**：init_schema 的「列已存在」ALTER 失败会回滚掉同事务已成功的建表 → DDL 成功即提交。
3. **空表 description=None**：列清单改用 PRAGMA/information_schema 而非 cursor.description。
4. **外键顺序**：PG 强制 FK，拷贝按外键拓扑排序（父表先拷）。
5. **历史列**：源库上历史 ALTER 出的列（如 data_compare_tasks.force_pk_compare）通过补列同步带到目标库。

### 已知边界（不虚报）

- **MySQL 后端未做真机 E2E**（本机无可用 MySQL 实例承载切换验证）；方言翻译/单测已覆盖，上线前需在真实 MySQL 补一次「切换+回归」演练。
- 切换/回切为同步接口，元库很大时 HTTP 请求耗时较长（期间写操作被写锁阻塞，读不受影响）。
- 加密字段为 `enc:` 前缀混淆串，跨后端原样搬运（与现有机制一致）。
- 平台自身仍是单进程（APScheduler 进程内），元数据库换 PG 只是**为多进程/HA 铺路**，不等于已具备 HA。

## 5. P0 四件套路线（不可变备份 + 平台 HA）

对标 Acronis（G13）与云祺/防勒索叙事，按依赖顺序：

| 阶段 | 项 | 内容 | 依赖 |
|---|---|---|---|
| **P0-1（已完成）** | 元数据库可插拔 | SQLite→PG/MySQL 无缝切换（本轮交付） | 无 |
| **P0-2** | 产物保留锁定（WORM） | `backup_records` 增加 `retention_until`/`immutable` 字段；清理/删除/生命周期/定期清理（backup_cleanup、lifecycle、GFS）一律检查保留锁；到期前物理删除一律拒绝并审计 | 无 |
| **P0-3** | 产物写保护 + 周期完整性复检 | 备份产物落盘后 chattr +i（ext4/xfs）或 chmod 000 + 属主隔离（隔离副本包装：manifest+产物打包为只读目录）；`integrity_check` 周期任务：sha256 复检 + 结果落 `integrity_reports` | P0-2 的字段 |
| **P0-4** | 平台 HA（多进程 + 元库外置） | 元库外置（P0-1）→ gunicorn 多 worker + APScheduler 换 DB 作业存储或单调度器选举（leader election，PG advisory lock 优先）→ 平台进程双实例 + keepalived/VIP | P0-1 |

验收口径（延续「不仿真」原则）：每阶段在真实环境落盘 E2E 报告——P0-2/3 需验证「到期前删除被拒、到期后可删、篡改产物被复检发现」；P0-4 需验证「kill 主进程后另一实例接管调度」。
