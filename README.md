# 数据备份管理平台

跨平台的**数据库 + 文件**集中备份管理平台，支持 **Oracle、MySQL、PostgreSQL、Kingbase（人大金仓）、DM（达梦）、Redis、MongoDB** 等多种数据库，以及**文件/目录（本地与远程 SSH，无 Agent）**的集中备份、定时调度、保留策略、三级对象存储、数据同步、巡检与健康检查、通知告警与一键恢复。

基于 **Python + Flask** 构建，元数据使用 SQLite（零外部依赖、开箱即用）；备份核心通过调用各数据库官方客户端工具或 SSH/SFTP 实现，能以最小依赖在生产环境稳定运行。

---

## 功能特性

### 备份能力
- **多数据库支持**：Oracle / MySQL / PostgreSQL / Kingbase / DM / Redis / MongoDB
- **文件/目录备份**：本地与远程（SSH，无需安装 Agent）源，全量 + 增量（按 size+mtime 比对），`tar.gz` 归档；源主机与目标主机独立。增量基于**源快照**（同一路径的多任务共享基准），归档仅含变化文件，与全量一致直接保存到目标目录根下，不会删除目标目录里的其他文件。Windows 下采用**原子写入**（临时文件 + replace），避免防病毒/句柄锁导致空包
- **多种备份策略**：全量（full）、增量（incremental）、差异（differential）、快照（snapshot）

### 管理与可视化
- **Web 可视化管理**：仪表盘、数据库备份、文件备份、数据同步、存储管理、保护策略、备份/恢复记录、数据恢复管理、灾备管理、巡检、智能告警、系统设置等
- **定时调度**：基于 APScheduler，支持 `cron` 表达式与固定间隔两种调度方式
- **保留策略**：按“保留天数”和“保留份数”双重清理，避免备份无限膨胀
- **演示 / 兜底模式**：当数据库客户端工具未安装时，自动生成“标记仿真”的占位备份，平台照常运行与演示；客户端就绪后无缝切换为真实备份

### 三级存储体系（异地容灾）
- **L1 热数据 = MinIO**（S3 兼容对象存储，备份第一落点）
- **L2 冷数据 = S3**（AWS S3 或其兼容服务，备份完成后实时/异步推送）
- **L3 源端本地路径导出**（服务端本地文件系统导出，可离线转移）
- 备份成功后由 `tier_replication` 自动并行复制到各层级；`backup_records.storage_tier` 记录每条备份实际到达的层级（如 `minio+s3+local`）
- 可配置复制策略（`push_l1_minio` / `push_l2_s3` / `push_l3_local` / 时机 / 重试）

### 数据同步
- 将源端（可托管现有备份任务或手动填写连接）同步到目标端（手动填写连接）
- 同源同类型且客户端齐全时执行真实 `dump | load`（MySQL / PostgreSQL），否则仿真
- 支持连通性探测，失败触发通知

### 数据恢复与灾备
- **数据恢复管理**：一键将历史备份恢复到目标实例（数据库）或目标目录（文件）；文件增量恢复时**自动构建恢复链**（先回最近全量，再按时间顺序应用增量），数据库部署（将数据库部署到目标实例）
- **灾备管理**：迁移保护、容灾链路、克隆服务（创建可独立使用的克隆实例）

### 巡检与健康检查
- 对任务做连通性 + 调度 + 上次状态体检，判定 `pass` / `warn` / `fail`
- **任一任务 `fail` 立即通过通知模块告警**
- 巡检记录可查看明细并导出

### 通知告警
- 渠道：Webhook / 钉钉 / 企业微信 / 飞书 / 邮件
- 成功、失败可分别开关；**通知配置支持 Web UI（系统设置页）**，密码不回显（留空表示不改）

### 主机与连接纳管
- **SSH 主机纳管**（`ssh_hosts` 表）：用于文件备份的远程源/目标，密码 XOR + base64 加密，支持连接测试

### 一键恢复
- 选择历史备份记录恢复到目标实例（数据库）或目标目录（文件）

---

## 支持的数据库与所需客户端

| 数据库 | 备份客户端 | 恢复客户端 | 说明 |
|---|---|---|---|
| MySQL / MariaDB | `mysqldump`、`mysql` | `mysql` | 密码通过临时选项文件注入，不出现于命令行 |
| PostgreSQL | `pg_dump`、`psql` | `pg_restore` / `psql` | 通过 `PGPASSWORD` 环境变量传密码 |
| Oracle | `expdp` / `impdp`（服务端目录）或 `exp` / `imp`（传统增量） | 同左 | 数据泵导出到数据库服务端 `DIRECTORY` |
| Kingbase 人大金仓 | `sys_dump`、`ksql` | `sys_restore` / `ksql` | 兼容 PostgreSQL 协议，端口默认 54321 |
| DM 达梦 | `dexp` | `dimp` | 逻辑导出，端口默认 5236 |
| Redis | `redis-cli` | （复制 rdb + 重启） | 通过 `REDISCLI_AUTH` 传密码 |
| MongoDB | `mongodump` | `mongorestore` | 通过 `--password` 传密码 |

> 请将上述客户端工具安装到运行平台的 `PATH` 中。缺少客户端时平台仍以“演示模式”运行。

---

## 目录结构

```
备份管理平台/
├── run.py                 # 启动入口（Web + 调度）
├── init_db.py             # 初始化元数据数据库
├── app.py                 # Flask 应用与页面路由
├── config.py              # 全局配置（环境变量 / config.json 覆盖）
├── auth.py                # 登录鉴权
├── requirements.txt       # Python 依赖
├── core/
│   ├── db.py              # SQLite 封装、建表、加密、工具
│   ├── models.py          # 任务 / 记录 / 恢复 / 日志 数据访问
│   ├── storage.py         # 存储管理、SFTP 上传、保留策略
│   ├── storage_backends/  # 三级存储驱动抽象层
│   │   ├── base.py        # StorageBackend 抽象基类
│   │   ├── local.py       # 本地文件系统（L3）
│   │   ├── minio.py       # MinIO 热数据（L1，S3 兼容 SDK）
│   │   └── s3.py          # S3 冷数据（L2，S3 兼容 SDK）
│   ├── tier_replication.py  # 三级复制引擎（备份后并行复制到各层级）
│   ├── lifecycle.py       # 生命周期：L1→L2 按龄/按容量下沉 + 到期清理
│   ├── notifier.py        # 通知（webhook/钉钉/企微/飞书/邮件）
│   ├── ssh_hosts.py       # SSH 主机纳管（文件备份远程源/目标）
│   ├── sync.py            # 数据同步引擎
│   ├── inspection.py      # 巡检与健康检查引擎
│   ├── scheduler.py       # APScheduler 调度与单次执行入口
│   └── engines/           # 各数据库 + 文件备份引擎（统一接口）
│       ├── base.py        # 引擎抽象基类与结果对象
│       ├── mysql.py / postgresql.py / oracle.py / kingbase.py
│       ├── dameng.py / redis.py / mongodb.py
│       └── file.py        # 文件/目录备份（本地 + 远程 SSH 无 Agent）
├── api/                   # REST API 蓝图
│   ├── storage.py         # 存储目标 CRUD / 测试连接 / 复制 / 复制策略
│   ├── hosts.py           # SSH 主机 CRUD + 连接测试
│   ├── sync.py            # 同步任务 / 记录
│   ├── inspection.py      # 巡检执行 / 记录 / 排程
│   ├── system.py          # 系统设置 / 通知配置（UI）
│   └── tasks.py / records.py / restore.py / ...
├── templates/             # 前端页面（Bootstrap）
├── static/                # CSS / JS
├── backups/               # 备份文件落盘目录（运行时生成）
└── instance/              # SQLite 元数据库（运行时生成）
```

---

## 安装

```bash
pip install -r requirements.txt
```

- 必选：`Flask`、`APScheduler`
- 可选（按需）：
  - `paramiko`（SFTP 远程存储、文件备份远程 SSH 源/目标）
  - `minio`（三级存储 MinIO / S3 驱动，需 `boto3`）
  - `PyYAML`（config.yaml 支持）

> 启用三级对象存储时，请安装 `minio` 与 `boto3`：`pip install minio boto3`

---

## 配置

配置优先级：**代码默认值 < 环境变量 < `config.json`（项目根目录，可选）**。

常用配置项（环境变量）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | Web 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `SECRET_KEY` | 会话签名密钥（**生产务必修改**） | `dev-secret-...` |
| `WEB_USERNAME` / `WEB_PASSWORD` | 登录账号密码 | `admin` / `admin123` |
| `BACKUP_ROOT` | 备份文件根目录 | `./backups` |
| `DEMO_MODE` | `auto` / `on` / `off` | `auto` |
| `SCHEDULER_ENABLED` | 是否启用定时调度 | `true` |
| `DEFAULT_RETENTION_DAYS` / `DEFAULT_RETENTION_COUNT` | 默认保留天数 / 份数 | `30` / `50` |

`DEMO_MODE` 说明：
- `auto`（默认）：客户端工具缺失时自动生成仿真占位备份，平台照常运行 / 演示
- `on`：强制仿真（任何任务都只生成占位备份）
- `off`：强制真实（客户端缺失则任务失败）

---

## 快速开始

```bash
# 1. 初始化元数据数据库
python init_db.py

# 2. 启动平台（同时启动后台调度器）
python run.py
```

浏览器访问 `http://<服务器IP>:8080`，使用默认账号 `admin / admin123` 登录。

> 沙箱 / 演示环境中没有安装数据库客户端时，平台会自动以“演示模式”运行：新建任务并点击“备份”会生成标记 `simulated` 的占位备份文件，便于验证完整流程。

---

## 使用说明（导航结构）

平台侧边栏分组如下：

- **概览**：仪表盘（数据库备份任务数 / 文件备份任务数 / 累计备份体积 / 成功失败统计）
- **备份管理**
  - 数据库备份：原有「任务管理」页，管理各数据库备份任务（文件任务已在此排除）
  - 文件备份：文件/目录备份（本地与远程 SSH，无 Agent）
  - 数据同步：库到库的同步任务
  - 存储管理：三级存储目标（MinIO/S3/本地）配置、容量、复制策略
  - 保护策略：备份保护策略管理
- **记录**
  - 备份记录：数据库/文件的历史备份、校验值、下载、触发三级复制
  - 恢复记录：历次恢复操作的记录
- **数据恢复管理**
  - 数据恢复：选择备份记录恢复到目标实例/目录
  - 数据库部署：将数据库部署到目标实例
- **灾备管理**
  - 迁移保护：数据迁移保护
  - 容灾链路：容灾链路管理
  - 克隆服务：创建可独立使用的克隆实例
- **运维**
  - 巡检：手动/定时体检，判定 `pass` / `warn` / `fail`，`fail` 即告警
  - 智能告警：基于规则的智能告警
  - 数据价值挖掘：备份数据价值挖掘分析
  - 系统设置：调度器状态、通知配置（Web UI）、SSH 主机纳管、平台信息与日志

典型操作：

1. **数据库备份**：在「数据库备份」新建任务，填写连接、备份类型、调度、保留策略与存储目标。
2. **文件备份**：在「文件备份」先到「系统设置 → SSH 主机」纳管远程主机，再建文件任务（源/目标可分别选本地或远程）。全量备份生成 `*_full.tar.gz`；增量备份基于**源快照**（同一路径的多任务共享基准），仅打包变化文件，在目标目录根下生成 `*_inc.tar.gz`（与全量归档同级），不会覆盖或删除目标目录里的其他备份或文件。若增量任务找不到历史快照（如全量在修复前执行），会自动回退为全量。
3. **三级存储**：在「存储管理」分别新增 MinIO（L1）、S3（L2）、本地导出（L3）目标并“测试连接”；备份完成后自动复制到各层级。
4. **数据同步**：在「数据同步」新增同步任务（源可托管现有备份任务），点击“运行”。
5. **巡检**：在「巡检」点击“立即巡检”，查看各项 `pass/warn/fail` 明细；可配置定时巡检。
6. **数据恢复与灾备**：在「数据恢复」选择备份记录恢复到目标实例/目录；**文件增量恢复会自动先回全量、再按时间顺序应用增量**。在「数据库部署」部署数据库到目标实例；在「灾备管理」进行迁移保护、容灾链路、克隆服务操作。

---

## 三级存储体系说明

| 层级 | 类型 | 角色 | 说明 |
|---|---|---|---|
| L1 | MinIO | 热数据（第一落点） | 备份文件首先写入此处 |
| L2 | S3 | 冷数据归档 | 从 L1 实时/异步推送，用于异地容灾与长期归档 |
| L3 | 本地路径导出 | 离线转移 | 服务端本地文件系统导出，可作为离线介质 |

- 复制由 `tier_replication.replicate_to_tiers()` 在备份成功后**并行**执行（L3 为复制终态，不参与自动流转）。
- 复制策略字段：`push_l1_minio` / `push_l2_s3` / `push_l3_local`（默认均启用）、`timing`、`max_retries`、`retry_interval`。
- 生命周期（`lifecycle.py`）：目前实现 L1(MinIO)→L2(S3) 按龄/按容量下沉与全局到期清理；L3 为终态。

---

## 通知配置

通知渠道支持 Webhook / 钉钉 / 企业微信 / 飞书 / 邮件，成功与失败可分别开关。**推荐在「系统设置 → 通知配置」中通过 Web UI 配置**（密码不回显，留空表示不改）。

也可在 `config.json` 中配置 `NOTIFY_DEFAULTS`（DB 配置优先于此处默认值）：

```json
{
  "NOTIFY_DEFAULTS": {
    "enabled": true,
    "on_success": false,
    "on_failure": true,
    "channels": [
      {"type": "webhook", "url": "https://example.com/hook"},
      {"type": "dingtalk", "url": "https://oapi.dingtalk.com/robot/send?access_token=xxx"},
      {"type": "wechat", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"},
      {"type": "feishu", "url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
      {"type": "email", "smtp_host": "smtp.example.com", "smtp_port": 465,
       "use_tls": true, "smtp_user": "alert@example.com", "smtp_password": "xxx",
       "from_addr": "alert@example.com", "to": ["ops@example.com"]}
    ]
  }
}
```

---

## 生产部署建议

- 使用 `gunicorn` 运行：`gunicorn -w 2 -b 0.0.0.0:8080 run:app`
- 通过 Nginx 反代并启用 HTTPS
- 修改 `SECRET_KEY` 与登录密码，必要时接入企业统一认证
- 将 `BACKUP_ROOT` 指向大容量、有冗余的存储；启用三级对象存储实现异地容灾
- 配置系统服务（systemd）实现开机自启与进程守护

---

## 安全说明

- 数据库连接 / SSH 主机密码以混淆方式存储于 SQLite，Web 接口默认不回显明文；生产环境建议结合密钥文件 / 环境变量管理
- MySQL 等使用临时选项文件（权限 `600`）承载密码，避免明文出现在进程参数中
- Redis 通过 `REDISCLI_AUTH` 环境变量传密码
- 请在生产环境务必修改默认登录账号与 `SECRET_KEY`

---

## 常见问题

**Q：运行环境没有安装数据库客户端工具，能否使用？**
可以。默认 `DEMO_MODE=auto`，客户端缺失时平台生成“标记仿真”的占位备份并继续运行，便于演示与流程验证；安装客户端后即自动切换为真实备份。

**Q：逻辑增量备份是否完全可用？**
MySQL 增量依赖 binlog；PostgreSQL / Kingbase / MongoDB 的逻辑增量能力有限，建议配合 WAL 归档 / oplog / 时间点恢复或物理备份；达梦增量建议使用 `dmrman` 物理备份。本平台逻辑引擎对不支持真正增量的库会回退为全量并在备注中说明。

**Q：如何实现异地备份？**
两种方式：(1) 任务“存储后端”设为 `SFTP`，填写远程主机/路径（需 `paramiko`）；(2) 在「存储管理」配置 MinIO(L1) + S3(L2)，备份完成后自动复制到对象存储实现异地容灾。

**Q：文件备份需要被备份机器装 Agent 吗？**
不需要。文件备份通过 `paramiko` SSH 在远程主机上执行 `find`/`tar`，无需在被备份机器安装任何 Agent；源与目标可分别选择本地或远程。

**Q：三级存储的层级是如何定义的？**
L1 = MinIO（热数据，第一落点）、L2 = S3（冷数据归档）、L3 = 源端本地路径导出（离线转移）。备份先写 L1，再由 `tier_replication` 并行复制到 L2/L3，`backup_records.storage_tier` 记录实际到达层级（如 `minio+s3+local`）。

**Q：巡检判定规则是什么？**
对任务做连通性 + 调度 + 上次状态体检：连通性失败或最近一次备份失败 ⇒ `fail`；无法判定连通性 / 从未运行 / 未配置调度 ⇒ `warn`；均正常 ⇒ `pass`。任一任务 `fail` 会立即告警。

---

## 许可证

本项目仅供学习与内部交付使用。
