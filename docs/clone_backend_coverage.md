# 克隆后端开发与验证记录

所有实现位于 Linux 仓库 /root/backup-platform-private，运行通道沿用平台已有的数据库协议、SSH 和厂商 HTTP API。

## 本轮交付

新增 clone_backends 统一后端，接入 TiDB、OceanBase、GreatDB、GoldenDB、openGauss、盘维、瀚高、优炫、GaussDB、Vastbase、SQL Server、MongoDB、Qdrant 的命名空间恢复、恢复后校验和删除。新增通道当前只接受源实例上的异名克隆，目标账号继承源任务。目标不存在检查通过后分配资源台账，恢复失败保留台账，便于用户清理。关系型兼容数据库使用单库逻辑备份。SQL Server 复用独立数据库恢复通道，MongoDB 使用命名空间重映射，Qdrant 使用独立 collection。

原有 MySQL、MariaDB、PostgreSQL、Kingbase、Oracle、达梦通道继续保留。销毁失败现在向调用者报告并保留台账，创建中请求禁止并发销毁。页面增加数据库能力清单，克隆选择器只列出已接入库型的成功备份。

## 附件方案的应用

- 对应方案第五节，将恢复、校验、资源回收的成功条件明确化，恢复后校验失败不能变为 ready。
- 对应第六节，页面披露命名空间隔离级别及 Live Mount、网络沙箱能力。执行隔离级别按数据库、collection 或专用容器在能力接口中披露。
- NBD/iSCSI、RoW、network namespace 与 QEMU 沙箱尚未在克隆执行器实现。
- 全部执行发生在平台或数据库自带接口和工具中，被保护主机无需安装 Agent。

## 待完成与验证限制

Redis、Neo4j、Chroma 已接入管理服务器上的专用 Docker 容器运行时。通用 JDBC 已接入独立目标管理任务、厂商命名空间生命周期 SQL 与逐表行数校验。所有 23 类内置数据库均已提供克隆入口，实际可执行性依赖预检、兼容的产物格式、运行时镜像或 JDBC 驱动。

Redis 支持 RDB 与 gzip RDB。Neo4j 支持 neo4j-admin .dump，源和目标版本需兼容。Chroma 支持含 chroma.sqlite3 的目录 tar，解包前拒绝越界路径和链接，并检查 SQLite 完整性。JDBC 支持 JSONL 表与行数据，在新数据库内执行单表 CREATE TABLE 和参数化 INSERT。复杂 DDL、跨 schema 同名表、跨 schema 引用需专门适配。

当前测试部署没有登记业务数据库，新增库型尚未完成真库端到端验证。契约回归验证资源所有权、恢复失败留账、销毁失败可重试及能力覆盖。系统已有的文件扫描分类和全站权限映射测试存在与本次改动无关的失败，本轮未修改这些模块。


## 隔离运行时部署配置

在管理服务器安装 Docker 并预先导入镜像，页面克隆预检会报告 Docker 或镜像缺失原因。默认镜像分别为 redis:7.4、neo4j:5.26、chromadb/chroma:0.6.3。可在源任务的 extra_options 设置 clone_image，指定与源备份兼容的镜像。镜像必须能使用运行时内置的路径和命令。

容器分配本机随机端口，仅绑定 127.0.0.1。Redis 数据目录为 /data，Chroma 为 /chroma/chroma。每个克隆使用独立容器及数据卷，销毁前验证 aidbm.clone.owner 标签。容器内存上限为 2 GiB，CPU 上限为 2 核。就绪等待当前为 30 次，失败保留台账以便清理。容器网络沿用 Docker 默认 bridge，页面的 network_sandbox 仍为 false。

通用 JDBC 在源任务 extra_options 中配置以下对象。示例适用于数据库级隔离且支持该 SQL 方言的驱动，按厂商调整 SQL。

```json
{
  "jdbc_db_type": "postgresql",
  "clone_jdbc": {
    "target_task_id": 42,
    "create_sql": "CREATE DATABASE \"{name}\"",
    "drop_sql": "DROP DATABASE \"{name}\"",
    "exists_sql": "SELECT datname FROM pg_database WHERE datname='{name}'"
  }
}
```

目标管理任务使用 jdbc_generic 引擎、相同 jdbc_db_type、目标管理员连接及平台侧驱动。每项生命周期 SQL 须包含 {name}，执行时替换为本次系统分配的 clone_N。exists_sql 在不存在时返回零行，存在时返回一行或多行。恢复连接必须通过 JDBC getCatalog 或 getSchema 确认已进入 clone_N。目标任务凭据以平台密钥加密保存在克隆运行时台账中。执行器关闭全部连接后才执行销毁并复核命名空间已经不存在。
