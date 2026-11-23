# 功能实现审计报告 - 2026-11-23

## 结论

当前平台已经形成数据库备份、文件备份、虚拟机、NAS、同步迁移、容灾编排、存储管理和 AI 助手的统一控制面。主要问题集中在部分能力是“编排层已实现、底层生产能力仍有边界”，以及真实环境缺少驱动时的降级路径。页面能够展示能力状态，但生产验收需要把降级状态纳入阻断条件。

## P0 需要优先处理

1. **运行时环境不稳定**。CentOS 7 镜像缺少完整 Python 3.11 SSL 扩展，当前服务通过临时构建运行时和系统依赖路径启动。应制作固定 Python 运行时或容器镜像，锁定解释器、依赖和 OpenSSL，避免重启或回滚后再次无法启动。
2. **真实 CDC 可能降级为仿真流**。`core/cdc/__init__.py` 明确包含仿真降级路径。MySQL binlog、Oracle LogMiner、Kingbase WAL、达梦日志等驱动或权限缺失时，任务可保持可用状态但数据不具备真实恢复意义。生产策略应禁止未显式允许的仿真降级，并让备份任务直接进入失败或阻断状态。
3. **FTP 明文传输仍可用**。Bandit 将 `core/netdev.py` 和 `core/storage_backends/ftp.py` 标为高风险。生产环境应默认关闭 FTP，保留 SFTP、FTPS 或对象存储，并在配置检查中阻止明文 FTP。
4. **默认管理员口令仍可能存在**。启动日志显示 `admin/admin123` 的安全告警。首次部署必须强制修改口令，或者在没有 `WEB_PASSWORD` 时阻止生产启动。

## P1 功能边界

1. **操作系统备份**。现有实现以文件级归档和清单增量为主，尚未达到设计文档中的块级 CBT、CoW 快照、BMR Recovery ISO、Windows VSS/WDF 驱动能力。整机恢复计划仍包含人工分区、fstab、initramfs 和 GRUB 步骤。
2. **群晖 NAS**。当前 `core/engines/synology.py` 通过 FileStation Web API 实现文件级全量、增量、断点续传和 SHA256 校验。设计中的 DSM 插件、fanotify/inotify、Btrfs send 对账、群晖 ACL、加密卷锁定监测、mTLS 反向连接尚未完整落地。
3. **虚拟机保护**。PVE/libvirt 等 Provider 已有快照和增量编排，但 VMware VDDK Direct SAN/HotAdd/NBDSSL、Hyper-V RCT、统一即时恢复 NFS/iSCSI 和跨平台 Live Mount 仍依赖具体 Provider 能力，不能作为所有环境的统一承诺。
4. **容灾切换**。`core/dr_*` 和 `api/dr_*` 已实现检查、演练、DNS/链路编排入口，但真实接管依赖外部 DNS、数据库、容器和网络条件。部分检查是只读预检或能力报告，不能替代真实隔离环境中的自动恢复验证。
5. **备份安全**。WORM、勒索检测、完整性检查和锁定记录已有多个模块，但 FTP、默认凭据、外部命令和旧运行时仍是生产攻击面。需要统一安全基线和部署前阻断检查。

## P1 架构问题

1. **并发控制仍是单进程内模型**。`core/scheduler.py` 使用线程池和进程内许可集合，能够支持同一进程多任务，但多实例之间没有统一队列或分布式租约下的任务执行锁。大规模任务需要持久队列、任务租约、幂等键和跨节点 worker。
2. **大数据任务缺少统一资源调度**。部分引擎有自己的线程池，和全局备份并发、带宽、源端 CPU 限流之间缺少统一预算。高并发下可能出现线程数叠加、内存放大和存储 I/O 争抢。
3. **依赖缺少生产锁定**。`requirements.txt` 主要使用最低版本约束，没有完整 lock 文件和哈希校验。当前回滚环境也无法完成可靠的 pip-audit，需要固定构建镜像后补做依赖漏洞审计。

## P2 质量和可观测性

1. 仿真、降级、真实执行状态需要在 API、任务记录、恢复点和首页指标中保持一致，任何仿真结果都应阻止“可恢复”结论。
2. 容灾就绪、恢复就绪、备份安全等指标需要绑定最近一次真实演练、真实校验和真实存储副本，而不是只读取配置字段。
3. AI 助手的自然语言动作已经有确认和候选任务机制，但高风险恢复、切换、删除、自动修复仍需统一确认策略、参数回显、幂等键和执行后回验。
4. 大量 SQL 字符串拼接、远程命令拼接和临时文件使用被 Bandit 标为中风险，需要按数据库标识符、参数值和远程命令白名单分批治理。

## 已完成的本轮安全修复

- 修复备份元数据采集、插件安装器和沙箱探测中的直接 `shell=True` 调用。
- 增加安全扫描报告和 FTP 风险说明。
- 服务已恢复并返回 HTTP 302。

## 建议实施顺序

1. 固化 Python/OpenSSL/依赖运行时和生产启动阻断规则。
2. 禁止真实任务静默降级为仿真，增加驱动、权限、位点和恢复可用性闸门。
3. 完成 FTP 下线或强制 FTPS/SFTP。
4. 建立跨进程任务队列和 worker 租约，统一并发、内存、带宽和 I/O 预算。
5. 按 OS Agent、群晖插件、VM Live Mount、CDC Direct Log、BMR 和自动恢复演练分别推进底层能力。
## P0 remediation applied

- Default administrator password rotated to a generated value and marked must_change_password.
- FTP backend registration and factory access now require AIDBM_ENABLE_INSECURE_FTP=true.
- Service restarted successfully after the hardening changes.

## P1 remediation applied

- Asynchronous backup submissions now acquire a persistent per-task lease in the metadata database, preventing duplicate execution across service instances.
- The lease is released in the worker finally path and expires after 24 hours for crash recovery.
