# 多数据库同步与实时同步 设计文档（离线环境优先）

> 版本 v1.0（2026-09-06）｜关联：dbackup-benchmark-offline-design-20260906.md
> 约束：**最终交付形态为完全离线环境**——任何同步/实时方案不得依赖公网组件
>（Flink 集群、Kafka、公网镜像均不可假设存在）。

## 一、能力矩阵与选型

| 源库 | 全量同步 | 定时增量 | 实时同步（离线） | 实时同步（在线可选） | 机制 |
|---|---|---|---|---|---|
| MySQL | ✅ 插件 | ✅ 增量列/binlog 位点 | ✅ **轮询 CDC**（本期新增） | ✅ Binlog CDC（mysql-replication）/Flink | binlog ROW + 位点 |
| PostgreSQL | ✅ 插件 | ✅ 增量列 | ✅ 轮询 CDC | ✅ WAL 逻辑解码（pgoutput） | 逻辑复制/WAL 归档拉取 |
| Oracle | ✅ 插件（本期新增） | ✅ 增量列 | ✅ 轮询 CDC | 🟡 LogMiner 挖掘（oracledb 直连可做，P1） | 归档日志 + LogMiner |
| 达梦 DM8 | ✅ 插件（本期新增） | ✅ 增量列 | ✅ 轮询 CDC | 🟡 归档日志拉取 + dbms_logmnr（P1） | dmarch.ini 本地归档 |
| SQL Server | ✅ 插件（本期新增） | ✅ 增量列/CDC 表 | ✅ 轮询 CDC | 🟡 CDC 表轮询（需开库级 CDC） | CDC/rowversion |
| 金仓 KingBase | ✅ 复用 PG 插件 | ✅ | ✅ 轮询 | ✅ 逻辑复制（PG 兼容） | PG 兼容协议 |

驱动（全部可离线交付，wheelhouse/ToolPack 随包）：
MySQL=pymysql，PG=psycopg2，Oracle=oracledb（瘦客户端纯 Python），
达梦=dmPython（随 DM 客户端 drivers/python 提供，离线包预置），
SQL Server=pymssql，金仓=psycopg2。

## 二、两种实时同步轨道

### 2.1 轮询 CDC（polling，离线默认，本期实现）
- 纯 Python：全量快照（upsert 幂等）→ 按**增量列**（updated_at 时间戳或
  自增 id）周期轮询 `> watermark` → upsert 目标端
- watermark 持久化在任务 message，重启续传；stop_event 优雅停止
- 前置条件：表有可比较的增量列；目标表有主键（upsert）
- 时延：poll_interval 秒级（默认 5s）；无外部组件、无源端触发器
- 引擎选择：`flink_config.engine` 缺省=polling；显式 `"flink"` 且 MySQL 源
  才走 Flink 轨道（在线环境）

### 2.2 日志 CDC（在线增强轨道）
- MySQL：binlog ROW + mysql-replication（已有）
- Oracle：LogMiner（oracledb 直连 dbms_logmnr，P1）
- PG：逻辑复制槽 pgoutput（P1）
- 达梦：归档拉取 + dbms_logmnr（P1）
- 共同点：变更捕获精确（含 DELETE），无增量列要求；但需要日志权限与
  归档配置——离线环境可用，作为 P1 增强

### 2.3 定时增量同步（不等同实时，批量窗口场景）
- 已支持 incremental_column + incremental_value 的批式拉取；
- 与摆渡收件箱组合：源侧平台拉增量 → 打包 → 摆渡 → 目标侧入库回放，
  实现**物理隔离网络**的数据同步（见 ferry_inbox）。

## 三、轮询 CDC 实现要点（core/sync/engine.py）

```
_run_realtime()
  ├─ engine=polling（默认）→ _run_realtime_polling()
  │    1) 全量快照（_run_full_migration，upsert 幂等）
  │    2) watermark = 当前增量列最大值
  │    3) 循环：SELECT ... WHERE 增量列 > watermark ORDER BY 增量列
  │       → 批量 upsert → 推进 watermark（MAX 查询）→ stop_event 停止
  └─ engine=flink 且 MySQL → _run_realtime_binlog()（原有）
```

- 插件化读写：新增 Oracle/达梦/SQLServer 插件（core/sync/plugins/），
  接口与 MySQL/PG 一致（list_tables/list_columns/read_batch/prepare_table/
  write_batch），类型映射统一走 type_mapper
- upsert：MySQL=ON DUPLICATE KEY UPDATE；PG/达梦/Oracle=MERGE INTO；
  SQL Server=MERGE ... USING VALUES

## 四、离线环境部署与运维要点

1. **驱动离线交付**：dmPython 从达梦服务器
   `<DM_HOME>/drivers/python/dmPython` 收集进离线包 wheelhouse/；
   pymssql/oracledb/psycopg2 均有预编译 wheel，make_bundle.sh 自动下载
2. **账号最小权限**：源端只读账号（SELECT + 增量列读权限）；
   目标端 DDL+DML（首表自动建表 prepare_table）
3. **断点与幂等**：全量快照 upsert 幂等，中断重跑不重不漏；
   watermark 落任务 message，服务重启后从断点续传
4. **监控**：同步行数/watermark 写任务 message（列表可见）；
   Webhooks 推送同步异常事件（复用事件中心）
5. **摆渡场景**：realtime 不可用时降级为「定时增量 + 摆渡收件箱」，
   RPO=摆渡周期

## 五、验证记录

- 插件注册：7 库型（mysql/mariadb/postgresql/oracle/dameng/sqlserver/kingbase）
- 达梦→MySQL 真机：137 达梦（dmPython）→ 140 MySQL 同步任务
  （见 readme 验证矩阵更新）
- 轮询 CDC：增量列 watermark 推进 + upsert 幂等（与表级恢复同源测试）
