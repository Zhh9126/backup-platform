# 对标金仓 KDTS / 达梦 DTS：迁移能力对照与优化（2026-11-03）

> 输入：金仓 KDTS 迁移手册（15 源端支持矩阵 + 迁移六阶段方法论）、达梦 DTS 功能介绍。
> 结论：AIDBM 迁移与 KDTS 的最大差距在**迁移对象种类**（此前只迁表）——本轮补齐
> 非表对象迁移通道（视图/序列/注释直迁，函数/存储过程/触发器同构直通、异构导出），
> 并补「排除指定表」。多表并行迁移（KDTS 卖点之一）需引擎配置实例隔离重构，列 P1。

## 一、能力对照矩阵

| 能力 | KDTS | 达梦 DTS | AIDBM 现状 | 本轮 |
|---|---|---|---|---|
| 表结构+数据迁移（指定表/排除表） | ✅ | ✅ | ✅（tables 清单/断点续跑） | ✅ **新增 exclude_tables** |
| **视图迁移** | ✅ | ✅ | ❌ | ✅ **新增**（information_schema/pg_views/DBMS_METADATA 提取 → 方言适配 → CREATE OR REPLACE） |
| **序列迁移** | ✅ | ✅ | ❌ | ✅ **新增**（含当前值 START WITH 续位；MySQL 目标如实 skipped） |
| **表/列注释迁移** | ✅ | ✅ | ❌ | ✅ **新增**（COMMENT ON TABLE/COLUMN） |
| **函数/存储过程** | ✅（跨方言翻译内核） | ✅ | ❌ | ⚠️ **同构直通重建；异构导出源码待人工适配**（PL 方言翻译不做，不伪装） |
| **触发器** | ✅ | ✅ | ❌ | ⚠️ 同上 |
| 同义词/程序包/自定义类型 | ✅ | ✅ | ❌ | P1（依赖跨方言翻译内核） |
| 多线程并行迁移 | ✅（单机多线程） | ✅ | ❌（逐表串行） | **P1**（需引擎 cfg 实例隔离重构，本轮不做不装） |
| 容错重试/二次迁移 | ✅ | ✅ | ✅（批次重试+断点续跑+失败表清理） | — |
| 迁移报告（可视化） | ✅ | ✅ | ✅（manifest+逐表状态+verify 行数/抽样+详情弹窗） | ✅ 增对象结果展示 |
| 全流程方法论（评估→割接） | ✅ KDMS+KFS+Katalon | ✅ | ❌（工具定位差异） | 不跟随（评估工具属咨询交付物） |
| Web + SHELL 双接口 | ✅ | ✅ | Web ✅ / CLI ❌ | P2 |
| 自定义类型映射 | ✅ | ✅ | 有默认映射矩阵+兼容顾问 | P1（extra.type_mapping 覆盖） |
| 源端广度 | 15+（含 Gbase/Db2/神通/Sybase/Greenplum/HANA/Informix/OceanBase） | 达梦生态 | 7+ 库型 | **不跟随**（无订单库型不立项） |

## 二、本轮落地（代码）

1. **`core/db_migrate.py` 对象迁移阶段**（`_phase_objects`，表迁移成功后自动执行，失败不阻断主结论）：
   - **视图**：MySQL（information_schema.VIEWS）/ PG 系（pg_views）/ Oracle·达梦（DBMS_METADATA）提取 → 跨方言基础适配（反引号⇄双引号、剥 CREATE 头）→ 目标 `CREATE OR REPLACE VIEW`；逐对象记录成败；
   - **序列**：PG 系（pg_sequences 含 last_value）/ Oracle·达梦（all_sequences）→ 目标 CREATE SEQUENCE（START WITH 当前值+1 保证不重发）；MySQL 目标如实 skipped（无序列对象）；
   - **注释**：三方言族提取表/列注释 → 目标 `COMMENT ON TABLE/COLUMN`（MySQL 目标 skipped：列级注释需重建列定义，不伪装）；
   - **函数/存储过程/触发器**：同族直通重建（PG `pg_get_functiondef` / `pg_get_triggerdef` 完整定义、MySQL ROUTINES 拼装）；**异构导出源码进报告**，标注"需人工适配"——跨方言 PL 翻译不做（如实边界，KDTS 有专用翻译内核属其商业内核）。
   - 每类对象上限 500（防超大库拖垮窗口）；目标端逐对象独立连接执行、失败隔离。
2. **排除表**：`SyncConfig.exclude_tables` + 引擎全库循环过滤（与断点续跑的 skip_tables 语义分离，结果单列 `excluded_tables`）；迁移向导输入框。
3. **前端**：迁移向导新增「迁移对象」勾选组（视图/序列/注释/函数/存储过程/触发器）+「排除表清单」输入；详情弹窗新增 ②.5 对象迁移阶段展示（逐对象成败/导出清单）。
4. 计划 `extra_options` 列（db_migration_plans）承载以上配置。

## 三、验证

- 单测：tests/test_db_migrate_objects.py（成员归一/extra 解析/视图剥离与方言适配的纯逻辑）
- E2E：MySQL 3307（建视图/存储过程/表列注释/排除表）→ PG 5785：表迁移 + 视图直迁 + 注释 COMMENT ON + 例程导出标注 + 排除表不落地，逐项核对。

## 四、诚实边界

- 函数/存储过程/触发器的**异构翻译**不做（导出源码 + 报告标注），同构/同族场景全自动；
- 多表并行迁移 P1（需 SyncConfig 实例隔离重构）；
- 同义词/程序包/自定义类型 P1；
- MySQL 目标的序列/列级注释不支持（数据库本身无此对象/需重建列定义），如实 skipped。
