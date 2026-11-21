# SQL Server 2005 兼容性修复（2026-11-21）

## 现场现象

用户现场 SQL Server 2005 实例逻辑备份失败：

```
阶段结束: 执行备份（成功，耗时 0.031s）
引擎返回: success=False, status=BackupStatus.FAILED, 大小=0 B, 产物=None
错误消息: com.microsoft.sqlserver.jdbc.SQLServerException: 找不到存储过程 'sp_columns 100'。
```

2008 及以上实例全部正常。

## 根因

平台代码里并没有调用 `sp_columns`。真正的原因是 **微软 JDBC 驱动在实现
`DatabaseMetaData.getColumns()` 时，内部会调用 `sp_columns`**（SQL Server
2008 才引入的 ODBC 目录兼容存储过程）。因此：

- 2008+：驱动能拿到元数据 → 正常
- 2005：驱动一调 getColumns 就报「找不到存储过程 'sp_columns 100'」→ 备份 0 字节失败

第二处隐患：物理备份命令**无条件**带 `COMPRESSION`，而 `COMPRESSION` 同样是
SQL Server 2008 才引入的 BACKUP 选项，2005 上会直接语法报错。

## 修复

1. **元数据不再走 JDBC DatabaseMetaData**（`core/sqlserver_jdbc.py`）：
   - 列定义：`INFORMATION_SCHEMA.COLUMNS`（2000 起支持）
   - 主键：`INFORMATION_SCHEMA.KEY_COLUMN_USAGE` + `TABLE_CONSTRAINTS`（2000 起支持），`sys.key_constraints` 作为回退
   - 自增列：`sys.identity_columns`（2005 引入的目录视图）
   - 于是 **2005 与 2008+ 走完全相同的代码路径**，不再有版本分支。
   - 类型映射同步修正：`varchar(max)`（CHARACTER_MAXIMUM_LENGTH=-1）、
     `decimal(p,s)`、`nvarchar(n)`、`datetime2/time` 精度，均按标准 SQL 输出。

2. **版本探测**（`core/sqlserver_jdbc.py::_server_version`、
   `core/engines/sqlserver.py::_server_major`）：
   `SERVERPROPERTY('ProductVersion')` 前缀解析 major（9=2005、10=2008、11=2012、
   15=2019、16=2022）。2005 没有 `ProductMajorVersion`（2008 才有），解析失败时
   自动回退到前缀解析。

3. **物理备份按版本自适应**：`COMPRESSION` 仅在 major>=10 时启用；2005 自动关闭，
   并在结果消息里如实说明「检测到 SQL Server 2005，已按版本关闭 COMPRESSION」。
   `CHECKSUM / STATS / INIT / DIFFERENTIAL / RESTORE VERIFYONLY / WITH MOVE`
   均为 2005 已支持的语法，无需分支。

4. **恢复通道**：产物只含 2005 兼容语法（`CREATE DATABASE`、`DROP TABLE`、
   `SET IDENTITY_INSERT`），2005 源库备份 → 2008+ 目标恢复可直接回放。

## 实测证据

| 验证项 | 结果 |
|---|---|
| 2005 兼容性单测（新增 `tests/test_sqlserver_2005_compat.py`） | 4 passed：元数据只走 INFORMATION_SCHEMA（一旦回退 DatabaseMetaData 即失败）、2005 版本解析、COMPRESSION 自适应 |
| 真机回归（SQL Server 2022，192.168.1.24:1433，sa） | 10/10：版本探测 → 种子 3 行中文 → 逻辑备份 → 产物含结构（PRIMARY KEY / IDENTITY(1,1) / NVARCHAR(50) / DECIMAL(12,2) / VARCHAR(MAX)）与数据 → 恢复到异名库 → 数据与结构逐项核对一致 |

## 诚实边界

- **未在真实 SQL Server 2005 实例上验证**（当前环境无 2005 实例）：修复路径不依赖
  2008 才有的对象/语法（INFORMATION_SCHEMA 与 sys.identity_columns 在 2005 均存在），
  且 2008+ 真机回归无劣化；拿到 2005 实例后应补一次真机验收。
- 2005 备份 → 2005 恢复的物理通道（BACKUP/RESTORE）依赖远端自带 sqlcmd；
  平台侧 sqlcmd 为 17 系列（ODBC 17 默认不强制加密），对 2005 可用。
