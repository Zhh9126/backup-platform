# 异构迁移兼容性顾问（compat_advisor）——信创迁移暗坑内置化

日期：2026-09-23 ｜ 关联：v1.4.13 后未提交改动

## 1. 背景与方向

依据《MySQL 迁移达梦 DM8：信创改造 7 个致命暗坑》及联网调研（达梦官方 FAQ：
Oracle→DM 大小写/COMPATIBLE_MODE/空串 FAQ、DM VARCHAR 与 MySQL 长度对比、
MySQL→PG 七坑等），把「迁移工具不报错、上线后随机炸」的共性暗坑内置到
迁移/同步引擎，分三层防御：

| 层 | 机制 | 位置 |
|---|---|---|
| 预检提示 | 目标库参数探测 + findings（warn/info/fail），修复建议直接给出 | `core/sync/compat_advisor.py::run_compat_advisory` → `precheck.run_precheck` |
| 建表防御 | 类型映射自动纠正（升位/放大/降级 CLOB/TEXT） | `dameng.py`/`oracle.py` 映射器 + `type_matrix._int_target` |
| 数据防御 | 零日期自动转 NULL（读端拷贝、实时轮询、预检试写三处口径一致） | `compat_advisor.sanitize_zero_dates` |

任何探测失败只降级为「少一条提示」，**绝不阻断迁移**（顾问原则）。

## 2. 七坑 → 引擎内置对照表

| # | 暗坑 | 引擎内置防御 |
|---|---|---|
| 1 | 大小写敏感（CASE_SENSITIVE 建库后不可改，默认 Y） | 预检探测 `SELECT CASE_SENSITIVE()`：敏感库 + MySQL/PG 类源 → warn「应用侧小写查询会报对象不存在，建议初始化 CASE_SENSITIVE=N 或双引号大写」。引擎自身建表/写入始终双引号大写标识符（数据落库不受影响） |
| 2 | VARCHAR 按字节计长（LENGTH_IN_CHAR=0，UTF-8 汉字 3 字节） | 探测 `SF_GET_PARA_VALUE(2,'LENGTH_IN_CHAR')` + `UNICODE()`；=0 时 `scale_char_length` 自动把 VARCHAR(N) 放大 ×3（UTF-8）/×2（GB18030），超 3900 降级 TEXT（达梦）/CLOB（Oracle）。预检 warn 给出根治建议（初始化设 1） |
| 3 | COMPATIBLE_MODE（4=MySQL） | 预检探测；非 4 且源为 MySQL → info 给出 `SP_SET_PARA_VALUE(2,'COMPATIBLE_MODE',4)` 建议（静态参数需重启） |
| 4 | 保留字当列名/表名 | 预检读 `V$RESERVED_WORDS WHERE RESERVED='Y'`（失败用内置兜底清单 SECTION/ORDER/LEVEL/KEY/USER...），表名命中 → warn。引擎 DDL 已全部双引号引用 |
| 5 | 字符集（UNICODE(): 0=GB18030, 1=UTF-8） | 探测后进入坑 2 的放大系数选择；GB18030 + MySQL 源 → warn 生僻字/emoji 风险 |
| 6 | 零日期 '0000-00-00' 目标库全非法 | `sanitize_zero_dates` 三处挂载：引擎主拷贝、实时轮询、预检数据试写——一律转 NULL（MySQL/MariaDB 目标保持原值）；PG 目标预检提示「已自动转换」 |
| 7 | 函数/分页/事务隔离等应用层差异 | 属应用 SQL 改造范畴，数据链路无法代劳；坑 1-6 的探测结果即为应用改造清单输入 |

## 3. 其他方向暗坑（同轮调研并内置）

| 方向 | 暗坑 | 防御 |
|---|---|---|
| →Oracle | VARCHAR2 按 BYTE（NLS_LENGTH_SEMANTICS） | 预检探测 nls_database_parameters；=BYTE 且源按字符计长 → 建表自动放大 + warn 根治建议 |
| →Oracle | 空串 '' = NULL | 预检 info（Oracle 固有语义，须应用确认） |
| →Oracle | 12.1 以下标识符 30 字节上限 | 预检解析版本，表名超 30 字节 → **fail 阻断**（建表必失败 ORA-00972） |
| →PG/金仓 | server_encoding 非 UTF8 | 预检探测 SHOW server_encoding → warn |
| 全部 | 无符号整型缺失 | TINYINT U→SMALLINT、SMALLINT/MEDIUMINT U→INTEGER、INT U→BIGINT、BIGINT U→NUMBER(20,0)/DECIMAL(20,0)，按目标族选择合法类型名 |
| →MySQL | sql_mode 无 STRICT_TRANS_TABLES | 预检 warn（超长值静默截断） |

### 连带修复的存量缺陷

`type_matrix._int_target` 对非 MySQL 目标把 TINYINT/SMALLINT/MEDIUMINT
UNSIGNED 升位成了 **MEDIUMINT**——PG/达梦/Oracle 根本没有该类型，
`→PG 试写建表` 直接报 `type "mediumint" does not exist`（本轮 E2E 实测命中）。
另修正金仓 BIGINT UNSIGNED → NUMERIC(20,0)（PG 家族无 NUMBER 类型名）。

## 4. 真实验证记录

### 4.1 离线单测（`tests/test_sync_compat.py`，18 条）
零日期消毒（严格目标/MySQL 目标保留/部分前缀）、长度放大（UTF-8 ×3/GB18030 ×2/不截顶/Oracle 源不放大）、达梦+Oracle 无符号升位、达梦 VARCHAR 放大与 TEXT 降级、stub 连接参数探测（默认库全告警/优化库安静/探测失败不阻断/Oracle 30 字节 fail/PG 编码）。

### 4.2 真实 E2E：MySQL 5.7.44(3307) → PostgreSQL 14.12(5785)
构造含全部暗坑特征的表：`INT UNSIGNED` 自增主键、`TINYINT UNSIGNED`、
`BIGINT UNSIGNED`（存入 18446744073709551615 与 9223372036854775808）、
`VARCHAR(100)` 中文、emoji+生僻字龘、5000 字符 TEXT、**零日期行**。

结果：预检通过（含 pg_zero_date info 提示）；迁移 3 读 3 写 0 错误；
PG 侧逐行核对——BIGINT UNSIGNED 满值完整（numeric(20,0)）、零日期转 NULL、
emoji/龘/5000 字符/中文全部无损。类型矩阵 7×7 源×目标 ×10 整型共 490 组
冒烟违规 0。回归 124 passed（含 rt_journal/cleanup/dedup/api_contract/
meta_backend/ui_db_types）零回归。测试环境已清理。

### 4.3 待办：达梦真机演练（137 当前不在线）
DM8（137:5236）不可达，达梦目标链路的真实端到端（含 LENGTH_IN_CHAR=0/1
两种初始化、CASE_SENSITIVE=Y/N、COMPATIBLE_MODE=4）暂无法执行。
137 上线后必须补：MySQL→DM8 迁移（同款暗坑表）→ 逐行核对 → 预检告警
核验，并落盘报告。**在此之前不得对客户宣称「MySQL→达梦已真机验证」。**

## 5. 实现位置

- `core/sync/compat_advisor.py`（新增）：探测顾问 + 零日期消毒 + 长度放大
- `core/sync/precheck.py`：预检 1.5 步接入 findings（fail 级会阻断迁移）
- `core/sync/engine.py`：主拷贝/实时轮询两处挂载零日期消毒
- `core/sync/precheck_data.py`：数据试写采样消毒
- `core/sync/plugins/dameng.py`：LENGTH_IN_CHAR/UNICODE 探测、VARCHAR 放大+TEXT 降级、无符号升位、修饰词剥离
- `core/sync/plugins/oracle.py`：NLS_LENGTH_SEMANTICS 探测、VARCHAR2 放大+CLOB 降级、BIGINT UNSIGNED→NUMBER(20)
- `core/sync/plugins/base.py`：ColumnMeta 正规化 `unsigned` 字段
- `core/sync/plugins/mysql.py`：COLUMN_TYPE 解析回填 unsigned
- `core/sync/type_matrix.py`：UNSIGNED 升位按目标族选合法类型名（修 MEDIUMINT 缺陷）
