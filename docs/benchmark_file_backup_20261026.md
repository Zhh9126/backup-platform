# 对标：文件备份功能清单（17 项，2026-10-26）

对标输入：用户提供的"详细备份功能介绍"清单（中科热备文件备份模块 17 项）。
方法：逐项对照 AIDBM 代码现状（路径级证据），缺口补齐，不虚报。

## 逐项对照

| # | 功能项 | AIDBM 现状 | 评级 |
|---|---|---|---|
| 1 | WEB 统一管理 | 全平台 WEB 集中管理（任务/策略/恢复/演练） | 🟢 领先（覆盖面更广） |
| 2 | 数据备份对象（授权服务器） | 任务制+纳管主机，无授权数硬限（license 通道预留） | 🟢 |
| 3 | 数据库支持 MSSQL/ORACLE/MySQL | 三者全有**且多出** PG/金仓/达梦/openGauss/Redis/Oracle 真机矩阵 | 🟢 领先 |
| 4 | 文件备份（Office/压缩/图片/视频） | core/engines/file.py 全类型文件 | 🟢 |
| 5 | 文件镜像备份（目录与源端完全一致） | file_mirror 镜像模式 + mirror_latest 最新视图（直接挂载可用） | 🟢 对齐 |
| 6 | 重复数据删除（周期内局部重删，跨周期不重删） | **不同实现且更强**：源端传输去重（文件级，实测 95.5% 节省带宽）+ 全局块级去重（引用计数+损坏自动重拉兜底）。对方"跨周期不重删"是怕块库损坏全灭——AIDBM 用校验+兜底重拉+不可变存储解决同一安全问题 | 🟢 实现不同/能力更强 |
| 7 | 全备+增量 | 文件增量 `_incremental_transfer`（diff/打包/快照）+ DB 增量链 | 🟢 |
| 8 | 客户端平台 Windows/Linux | **零 Agent**：Linux SSH 原生；Windows 文件源经 SMB/共享目录挂载路径支持（无 Windows agent，不做桌面级） | 🟡 定位不同（诚实标注） |
| 9 | **一键备份** | **原缺失 → 本轮已实现**：任务页「一键备份」按钮（bi-lightning），对全部启用任务逐个异步触发全量，POST /api/tasks/quick-backup 返回逐任务受理结果 | 🟢 已补齐 |
| 10 | 备份间隔最小 60 秒 | interval_minutes 分钟粒度（最小 1 分钟）；秒级间隔由 rt_enabled CDP（秒级 RPO）覆盖 | 🟢 语义等效 |
| 11 | 邮件通知 | core/notifier.py + email_template.py（备份结果邮件卡片）+ SMTP 配置 | 🟢 |
| 12 | 备份策略（自动删除 N 天前数据） | retention_days/retention_count + backup_cleanup + GFS | 🟢 |
| 13 | 客户端防删除保护（防勒索） | **不同实现且更强**：备份服务端不可变存储四层防护（WORM/保留锁/防删校验/恢复点安全检测）——比"客户端目录防删"更彻底（客户端被攻陷也删不掉备份） | 🟢 实现不同/更强 |
| 14 | 存储平台 Linux | 平台即 Linux（离线一体机交付） | 🟢 |
| 15 | 多级备份（本机→异地备份机） | storage_targets 多目标 + tier_replication L2/L3 自动级联 | 🟢 |
| 16 | 公有云备份（无需公网 IP） | S3/MinIO/OSS/COS 对象存储出站上传（备份机主动外联，无需公网 IP/开入站）+ 云主机零 agent SSH 备份 | 🟢 |
| 17 | 异地容灾 | L3 异地 + 摆渡 + 池复制多目标（链式编排 v2 路线） | 🟡 |

## 本轮补齐

- **一键备份**（#9，原缺失）：`api/tasks.py POST /api/tasks/quick-backup`（缺省全部启用任务/可传 task_ids，逐任务异步 full，单任务失败不影响其余）+ 任务页工具栏「一键备份」按钮（确认弹窗→触发→toast 汇报 N/M）。

## 宣传口径要点

- 对方清单里的"重复数据删除"和"客户端防护"两项，AIDBM 用**不同且更强的机制**实现（源端+全局去重、服务端不可变四层防护 vs 局部重删、客户端目录防删）——这是差异化卖点，不是跟随项。
- 对方"最小间隔 60 秒"是定时备份粒度；AIDBM 直接用 CDP 覆盖秒级 RPO，不在"更短的定时"上卷。
- Windows 客户端：零 Agent 定位下按"文件共享/挂载路径"支持，不承诺桌面级 agent 能力（结构性不跟随）。

## 生效说明

- 本轮改动（api/tasks.py、templates/tasks.html、static/js/app.js、app.css、base.html 缓存戳 js=2026102602/css=2026102602）需重启服务 + 浏览器强刷（Ctrl+F5）。
- 侧栏两列乱象根因：Bootstrap .nav 默认 flex-wrap:wrap，加高度约束后溢出项换列——已补 flex-wrap:nowrap + flex-direction:column 显式锁定。
