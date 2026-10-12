# 对标调研：Veeam / Commvault / 云祺（Vinchin）与 AIDBM 备份恢复能力（2026-10-12）

> 背景：外资备份厂商在华直营模式落幕（Veeam 收缩为总分销、Commvault/Veritas 相继调整），
> 信创替代 + 勒索攻击升级双重驱动下，用户关注自主可控灾备底座。本文延续
> `benchmark_acronis_20260916.md` / `benchmark_yakops_migration_sync_20260916.md` 的方法：
> 只记录真实差距、只吸收可落地的能力，未实现显式备注。

## 1. 三家厂商能力地图（与本产品相关部分）

| 能力域 | Veeam | Commvault | 云祺 Vinchin | AIDBM 现状 |
|---|---|---|---|---|
| 无代理备份 | 仅 VMware/Hyper-V；国产虚拟化需装代理 | 多数需代理 | 全平台无代理 | ✅ 全程零客户端/零 agent（数据库级，更彻底） |
| 不可变备份/防勒索 | Hardened Repository（Linux 硬化仓库）+ Object Lock | Immutable Storage | 不可变备份+防勒索 | ❌→✅ **本轮补齐**（保留锁定+写保护+完整性复检+删除拦截，见 §3） |
| 异机/即时恢复 | Instant Recovery（VM 级） | Live Recovery | 恢复到任意平台 | ✅ 数据库级恢复 + 恢复校验 + 克隆（VDB） |
| 跨平台迁移（X2X） | 受限 | V2X 部分支持 | X2X 虚拟机迁移 | ✅ 数据库级：迁移计划/同步/克隆/cross_host 跨主机恢复 |
| 永久增量/合成全量 | ✓（Synthetic Full） | ✓ | ✓ | ⚠️ 有增量备份与 GFS 合成链；无合成全量（rt_backup 合成有雏形） |
| 数据缩减 | 重删+压缩 | 全局/可变重删 | 重删+压缩+自定义块大小 | ⚠️ gzip 压缩 + 对象级去重（global_dedup/backup_sets）；无可变块重删 |
| 多线程/限速 | ✓ | ✓ | ✓（线程数+带宽限速） | ⚠️ 令牌桶限速/避峰（_BANDWIDTH）+多任务并发；单任务内多线程无 |
| 历史备份纳管 | — | — | 纳管 Veeam 历史备份 | ✅ 本身即统一纳管平台（含摆渡 ferry 隔离导入） |
| 恢复点安全检测 | 恶意软件扫描 | ✓ | ✓ | ❌ 离线无病毒库，仅可启发式（Acronis 对标 P1，未实现） |
| 恢复编排（Runbook） | OR 成熟 | ✓ | ✓ | ❌ 无多库顺序恢复编排（P3，未实现） |
| 授权/版本分层 | 按工作负载订阅 | 订阅+功能单独付费 | 按订阅 | ❌ 无（G10，商业化的前置） |

## 2. 判断：该学什么、不学什么

**学（P0 已落地）**：不可变备份与保留锁定——三家共同主打、勒索场景刚需、
且是设计说明书 G13 的结构性缺口。见 §3。

**学（后续路线，未实现）**：
- P1 恢复点安全检测：离线环境只能做启发式异常检测（备份体积/熵值/结构
  异常），必须诚实标注"非病毒查杀"（Acronis 对标已有方案）；
- P3 恢复编排 Runbook：多库按序恢复+逐步验证，复用 drill/disaster_link 地基；
- 合成全量：从增量链定期合成全量，缩短全量窗口（rt_backup 合成可扩展）。

**不学**：虚拟机级无代理/裸机恢复（AIDBM 是数据库级灾备，定位不同且已是
零安装）；SaaS 云存储（违背离线自足原则）；按容量订阅计费模型。

## 3. 本轮落地：不可变备份四层防护（G13 闭环）

实现 `core/immutability.py`（对标 Veeam Hardened Repository 语义，应用层 WORM）：

| # | 能力 | 实现 |
|---|---|---|
| 1 | **保留锁定** | `lock_record(days[, 0=永久])`：锁定期内删除/覆盖一律拒绝（定期清理、存储保留策略、GFS 策略、任务级联删除四条路径全部拦截）；解锁必须带操作者+原因，强制审计（`db.add_log WARNING`） |
| 2 | **产物写保护** | 锁定时产物 chmod 去写位（0444）；不支持的环境如实置 `write_protected=0` 并告警，不静默假装 |
| 3 | **完整性基线+周期复检** | 以记录 sha256 为基线，周期任务 `backup_integrity_check`（cron 默认 `40 4 * * *`，system_config `integrity_check_cron` 可覆盖，start/reload 双路径注册）重算比对，漂移即 ERROR 日志供告警 |
| 4 | **删除路径拦截** | `core/backup_cleanup.py`（扫描排除 + 执行时二次硬护栏，防"计划与执行之间新加的锁"）、`core/storage.py apply_retention`、`core/retention_gfs.py`（锁定记录不标记不删）、`models.delete_task`（有锁任务禁删） |

**入口**：任务 `extra_options.retention_lock_days` 备份成功即自动上锁
（"备份即上锁"防勒索默认姿势）；单条记录 API：
- `POST /api/records/<id>/lock` `{days, reason}`
- `POST /api/records/<id>/unlock` `{reason}`（必填，审计）
- `POST /api/records/<id>/verify-integrity`
- `GET /api/records/locked`

**数据模型**：`backup_records` 增列 `locked/locked_at/retention_until/
write_protected/checksum_verified_at/checksum_ok/lock_note`（CREATE+ALTER 均含）。

**诚实边界**：应用层锁防平台自身与凭据泄露场景；chmod 只读防普通误删
（root 仍可强制删）；真物理不可变需文件系统 immutability 或 S3 Object Lock，
本环境不支持时显式报告（设计原则 6：不静默降级）。隔离副本（air-gap）
由磁带 D2T + 摆渡 `ferry_inbox` 承接物理隔离。

## 4. 验证

- `tests/test_immutability.py` 14 条：锁定/解锁审计闸门、只许延长不许缩短、
  永久锁、幽灵记录/失败备份不可锁、任务级联删除拦截、定期清理跳过
  （扫描+执行双护栏）、GFS 不标记不删、完整性一致/漂移/基线新建、
  自动锁钩子、API 路由契约、表结构契约；
- 回归：与 test_backup_cleanup 等九文件组合 115 passed（唯一失败为已知的
  跨模块共享临时库污染，单跑通过，与本轮改动无关）。
