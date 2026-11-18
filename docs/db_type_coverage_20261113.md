# 数据库类型覆盖调研与落地（2026-11-13）

> 目标：让「数据库类型（可插拔适配器）」页面尽可能覆盖业界数据库，而不是只有内置那几种。
> 结论：内置引擎 21 种（代码级）+ 模板目录 53 种（可一键导入），合计覆盖
> DB-Engines 2026-11 Top 20 中的 16 项 + 国测/信创清单 15 个国产产品的绝大多数。

---

## 1. 为什么不能"全部做成内置引擎"

内置引擎意味着「平台侧有专用实现 + 真机验证过」，每条都要：
备份/恢复/校验/同步/克隆五类接口、工具解析、错误分类、恢复参数映射。
50+ 库型全按内置做，成本与验证工作量不可控，且大量长尾库现场只用逻辑导出。

因此采用**四层覆盖**：

| 层 | 通道 | 覆盖对象 | 状态 |
|---|---|---|---|
| L1 内置引擎 | `core/engines/*.py` | 主力 21 种（含 8 个国产） | 代码级，多数真机验证 |
| L2 协议兼容模板 | 模板目录 pg/mysql/oracle 三族 | 同类发行版、云原生、国产兼容品 | 与已验证引擎同族工具，参数一致 |
| L3 专有工具模板 | 模板目录 olap/ts/nosql/graph/other | 各厂商自带 dump / snapshot / API | 骨架按官方文档，**需现场核对** |
| L4 自定义脚本 | 页面「新增适配器」 | 完全私有系统 | 用户自写 |

L2/L3 都落在同一张 `db_adapters` 表里，走 `CustomDBEngine`（SSH 执行 bash 模板），
产物写 `PLATFORM_BACKUP_DIR`，平台负责回收、校验、落记录、可恢复。

---

## 2. 现状盘点

### 2.1 内置引擎（21 种，页面全部可见）

MySQL / MariaDB / PostgreSQL / Oracle / SQL Server / 达梦 / 金仓 KingbaseES /
openGauss / 磐维 PanWeiDB / 瀚高 HighGo / 优炫 UXDB / TiDB / OceanBase /
Redis / MongoDB / Neo4j / Qdrant / Chroma / 对象存储 / 群晖 NAS / 文件

> 修复：此前页面只显示 14 项——`BUILTIN_TYPES` 手写清单漏登记
> `synology/qdrant/chroma/highgo/uxdb/tidb/oceanbase`，`_list_combined()` 直接
> `continue` 掉，导致这些引擎**整行消失**。现改为遍历 `ENGINE_REGISTRY` +
> `builtin_types()` 动态合并，且能力标签取 `engine_meta_map()` 的真实值
> （此前 redis/file/mongodb 也被标成"物理 + 全实例 + 增量"）。

### 2.2 模板目录（53 种，勾选即导入）

| 分组 | 数量 | 清单 |
|---|---|---|
| PostgreSQL 协议族 | 10 | GaussDB、Vastbase（海量）、MogDB、IvorySQL、PolarDB-PG、Greenplum、YugabyteDB、CockroachDB、SeaboxSQL（海盒）、TimescaleDB |
| MySQL 协议族 | 6 | GoldenDB（中兴）、TDSQL（腾讯）、PolarDB-X、GreatDB（万里）、He3DB（中移）、GBase 8a（南大通用） |
| Oracle 兼容族 | 3 | 神通 OSCAR、崖山 YashanDB、PolarDB-O |
| 分析型 / MPP | 7 | ClickHouse、StarRocks、Doris、Hive、Impala、Vertica、Teradata |
| 时序 | 5 | InfluxDB 1.x、InfluxDB 2.x、TDengine、IoTDB、QuestDB |
| NoSQL / KV / 搜索 | 10 | Cassandra、ScyllaDB、HBase、CouchDB、Elasticsearch、OpenSearch、KeyDB、Dragonfly、etcd、Consul |
| 图 | 4 | NebulaGraph、ArangoDB、HugeGraph、JanusGraph |
| 其他 | 8 | SQLite、DuckDB、Firebird、IBM Db2、Informix、SAP ASE、SAP HANA、Access |

模板字段：`db_type / 显示名 / 分类 / 默认端口 / 能力 / 客户端工具 /
backup_full / restore / test_conn / list_dbs 脚本 / params 提示`。
UI 上每条标注验证级别：

- **协议兼容**（16 条）：与已验证引擎同族工具（pg_dump / mysqldump / exp-imp）。
- **需现场核对**（37 条）：厂商专有工具或 API，命令骨架取自官方文档。

---

## 3. 与业界清单的对照

### 3.1 DB-Engines 2026-11 排名前 20

| 排名 | 产品 | 覆盖方式 |
|---|---|---|
| 1 | Oracle | 内置（expdp/RMAN/LogMiner，11g/19c 真机验证） |
| 2 | MySQL | 内置（逻辑+物理+增量+同步） |
| 3 | SQL Server | 内置（物理 SSH + 逻辑 JDBC 免 SSH） |
| 4 | PostgreSQL | 内置（逻辑+物理+PITR+同步） |
| 5 | MongoDB | 内置（mongodump/mongorestore） |
| 6 | Snowflake | 不覆盖：纯 SaaS，离线环境无本地数据面 |
| 7 | Databricks | 不覆盖：同上（云数仓） |
| 8 | Redis | 内置（RDB） |
| 9 | IBM Db2 | 模板（db2 backup db） |
| 10 | Cassandra | 模板（nodetool snapshot） |
| 11 | SQLite | 模板（.backup） |
| 12 | Elasticsearch | 模板（_snapshot API） |
| 13 | MariaDB | 内置 |
| 14 | Splunk | 不覆盖：日志分析平台，非数据库备份对象 |
| 15 | Azure SQL | 不覆盖：云托管，同 Snowflake |
| 16 | Hive | 模板（EXPORT TABLE） |
| 17 | Access | 模板（mdb-export） |
| 18 | DynamoDB | 不覆盖：云 NoSQL API |
| 19 | Neo4j | 内置（dump / 在线 backup / APOC） |
| 20 | SAP HANA | 模板（hdbsql BACKUP DATA） |

### 3.2 国测 / 信创清单（安全可靠测评）

| 产品 | 覆盖方式 |
|---|---|
| 达梦 DM8 | 内置（dexp/dimp + dmrman + 实时） |
| KingbaseES V8 | 内置（sys_dump / sys_basebackup） |
| GBase 8s | 模板（Informix 系 dbexport/ontape） |
| GBase 8a | 模板（MySQL 协议，gcdump） |
| 神通 OSCAR | 模板（Oracle 兼容 exp/imp） |
| 瀚高 HighGo | 内置（PG 协议兼容层） |
| 优炫 UXDB | 内置（PG 协议兼容层） |
| PolarDB（PG/O/MySQL 版） | 模板 ×3（三族各一条） |
| TDSQL | 模板（MySQL 协议） |
| GoldenDB | 模板（MySQL 协议） |
| OceanBase | 内置（MySQL 租户协议兼容层） |
| GaussDB | 模板（gs_dump） |
| 海量 Vastbase | 模板（openGauss 系） |
| 虚谷 Xugu | 待补（自有协议，工具名随版本差异大） |
| SeaboxSQL（海盒） | 模板（PG 系） |
| 万里 GreatDB | 模板（MySQL 系） |
| 崖山 YashanDB | 模板（Oracle 兼容） |
| TiDB | 内置（MySQL 协议兼容层） |
| openGauss / 磐维 | 内置 |
| 巨杉 SequoiaDB | 待补（分布式，快照通道需现场确认） |

---

## 4. 本次改动

### 代码

1. 新增 `core/db_adapter_templates.py`（53 条目录 + 分组 + 统计）；
2. `core/db_adapters.py`
   - `builtin_types()` 动态合并（修页面隐藏 7 个引擎）；
   - `CustomDBEngine._params()` 支持**任务级覆盖** `extra_options.adapter_params`
     （db_file / data_dir / repo_dir / snapshot 这类参数每台机、每个任务都不同，
     只放适配器级别会导致同一库型只能备份同一路径）；
3. `core/engines/base.py`：`params` 真正注入 `PLATFORM_PARAM_<KEY>`
   （此前文档承诺、代码从未实现，模板里引用恒为空）；
4. `core/engines/__init__.py`：补齐 17 个引擎的**真实能力**元信息；
5. `api/db_adapters.py`
   - `GET /api/db-adapters/templates` 增加 `groups` / `stats`；
   - 新增 `POST /api/db-adapters/import`（批量导入，逐条返回 created/skipped/failed）；
   - 列表补 `using_tasks` 聚合（一次 SQL，避免 N 次查询）；
6. `templates/_db_adapters_panel.html`：模板弹窗改为分组 + 搜索 + 勾选批量导入 +
   单条「套用」；列表显示真实能力标签。

### 顺手修掉的 3 个真实缺陷

| 缺陷 | 影响 |
|---|---|
| `BUILTIN_TYPES` 漏登记 7 个引擎 | 页面整行不显示（用户看到的 14 项就是这么来的） |
| `PLATFORM_PARAM_*` 从未注入 | 所有带参数的模板脚本静默取空值（工具路径、仓库目录全失效） |
| 内置能力标签硬编码全能力 | redis/file/neo4j 等被误标"物理+全实例+增量" |

---

## 5. 验证

- 单测 `tests/test_db_adapter_catalog.py`：19 passed
  （目录完整性 / 脚本 bash -n / 产物落 PLATFORM_BACKUP_DIR / 参数注入 /
  内置可见性回归 / payload 通过 validate）；
- i18n 门禁 `tests/test_i18n_coverage.py`：4 passed（新增中文文案已补英文 + 升缓存戳）；
- 真机 E2E `/root/e2e/e2e_adapter_sqlite.py`：
  导入模板 → 建任务（任务级 `adapter_params.db_file`）→ 备份 8.0 KB →
  恢复到 `restored.db` → 核对 `(3, '模板-甲,模板-乙,模板-丙')` **PASS**；
- 服务端实测：`GET /api/db-adapters` 返回 21 项（全部内置引擎可见），
  `GET /api/db-adapters/templates` 返回 53 条 / 8 组。

E2E 过程中还暴露并修掉一个模板级坑：`sqlite3` 对不存在的路径会**新建空库**，
在参数没生效时会静默产出空备份——sqlite 模板已加
`[ -f "$DBF" ] || exit 3` 与空产物检查，避免"备份成功但备份是空库"。

---

## 6. 诚实边界

1. `verification = "template"` 的 37 条**未经真机验证**，只保证脚本语法正确、
   变量注入完整；厂商工具名/参数随版本变化，首次使用请点「测试」核对；
2. 云托管服务（Snowflake / Databricks / Azure SQL / DynamoDB）与 Splunk
   不纳入——离线环境没有本地数据面，JDBC 导出也不构成可恢复的全量备份；
3. StarRocks / Doris / Hive / Impala / Elasticsearch / HBase / Nebula 等依赖
   「仓库目录或后端存储快照」的库型，恢复前提是现场已有对应仓库/后端路径；
4. 虚谷 Xugu、巨杉 SequoiaDB 仍在待补清单（自有协议/工具差异大，需现场确认）。

---

## 7. 后续路线

| 优先级 | 事项 | 说明 |
|---|---|---|
| P1 | 通用 JDBC 逻辑备份引擎 | 复用 `core/sqlserver_jdbc.py` 的元数据+数据导出，一次覆盖所有关系型长尾（Db2/HANA/Vertica/Teradata/ClickHouse 等无 CLI 场景） |
| P1 | 模板真机验证排期 | 每个族挑 1-2 个代表（ClickHouse、TDengine、Cassandra、etcd、ArangoDB、Db2）跑「备份→恢复→核对」 |
| P2 | 新增 PG/MySQL 协议兼容引擎 | 把 GaussDB / Vastbase / GreatDB / GoldenDB 提升为内置引擎（复用现有工具族） |
| P2 | 模板参数 UI | 任务表单里直接填 `adapter_params`（目前走 API / extra_options） |
| P3 | 补虚谷、巨杉、OpenSearch 插件生态 | 需现场工具确认 |
