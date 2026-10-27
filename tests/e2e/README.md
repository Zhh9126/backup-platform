# 多库型主机 E2E 测试矩阵（能力沉淀）

`scripts/e2e_matrix.py` 是平台各库型"备份→产物校验"最小闭环的**标准验收入口**，
独立可复用：新库型/新主机接入后，在 hosts 清单里加一个 target 即可回归。

## 复用方式

```bash
cd /root/CodeBuddy/20260826095855/backup-platform
.venv/bin/python scripts/e2e_matrix.py --hosts tests/e2e/matrix_hosts.json [--only <target-id>]
```

- 自动：纳管 SSH 主机 → 创建备份任务 → 立即备份（平台真实通道）→ verify_record 产物校验
- 报告：文本逐项 PASS/FAIL + `matrix_report.json`（含耗时/消息/record_id）

## 已验证用例

| target | 库型/通道 | 结果 | 日期 |
|---|---|---|---|
| 129-oracle-pitr | Oracle 19c RMAN PITR 全链路（全量→标记→回推备份片→RESTORE/RECOVER→OPEN RESETLOGS→硬核对） | 10/10 | 2026-10-26 |
| 139-pg14 | PostgreSQL 14.24 逻辑备份（SSH 远端 pg_dump）+ 产物校验 | 4/4 | 2026-10-27 |
| rsync-delta | MySQL 物理备份 rsync 增量拉取（裸 tar，实测实传 0.0%） | R1-R5b 过 | 2026-10-26 |

## 待登记（凭据就绪后即可回归）

| target | 库型 | 所需凭据 |
|---|---|---|
| 137-kingbase | 金仓 V9（54321，/opt/Kingbase） | system 密码 |
| 137-dameng | 达梦 8（5236，/dm/dbms） | SYSDBA 密码（默认 SYSDBA/SYSDBA 不对） |
| 129-mysql | MySQL（/usr/local/mysql，端口待探测） | root 密码 |
| 139-mysql8 | MySQL 8 | root 密码 |
| 137-redis | Redis | 无密码或密码 |

## 已知环境注意

- 137 磁盘 95%（2.1G 可用）：大产物测试前先清理。
- 129 是 CDB/PDB 架构 19c：PITR 走 CDB 整体恢复；`_ensure_pdbs_open` 已处理 PDB MOUNT 态。
- 备份前先 `df -h` 确认平台与目标主机磁盘（多次实测磁盘满引发误报）。
