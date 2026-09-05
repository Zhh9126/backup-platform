# 待完成验证清单（2026-09-06 会话中断续作指引）

> 背景：本轮完成了 M1-M5 实施与大部分实测，剩余几步因执行通道中断未跑完。
> 以下每项都可独立执行，按序完成即可闭环。

## 1. 达梦 PITR 最后一步（RESTORE 真机执行）

状态：编排代码完成（`core/engines/dameng.py::_restore_pitr`），物理备份已实测
成功，RESTORE 到正确 ini 路径（`/home/dmdba/pitr_clean/DAMENG/dm.ini`）的执行
被会话审批中断。

一键验收：
```bash
bash scripts/verify_dameng_pitr.sh
```
脚本内容：物理备份 → dminit 骨架 → RESTORE → RECOVER UNTIL TIME →
UPDATE DB_MAGIC → 副本启动(5336) → BKP_TEST 行数核对。
注意：脚本的 ini 路径逻辑已按「dminit 生成 `<dir>/DAMENG/dm.ini`」适配；
执行前确认 137 上 5336 端口无残留进程（`ps -ef | grep pitr_verify`）。

## 2. 137 PostgreSQL 14.12 部署后验证（部署已完成 ✓）

PG 14.12 已在 137 编译部署（/pg14，端口 5432，backup_test 库 +
kb_verify 表）。**剩余步骤**（会话中断时正在执行）：

```bash
# 2.1 允许平台(140)远程访问 + 设置密码（在 137 执行）
echo "host all all 192.168.220.140/32 md5" >> /pg14/data/pg_hba.conf
chown postgres /pg14/data/pg_hba.conf
su - postgres -c "/pg14/bin/psql -h 127.0.0.1 -U postgres -c \"ALTER USER postgres PASSWORD 'Pg@137_Verify';\""
su - postgres -c "/pg14/bin/pg_ctl -D /pg14/data reload"

# 2.2 平台机(140)部署新版 PG 客户端（旧 9.2 libpq 不支持 SCRAM）
# 从 133 拉取：ssh root@133 "cd /pgdb/pgsql && tar czf /tmp/pgc.tar.gz bin"
# scp root@133:/tmp/pgc.tar.gz /tmp/ && mkdir -p /pgdb/pgsql && tar xzf /tmp/pgc.tar.gz -C /pgdb/pgsql
```

然后平台页面建任务（137:5432，postgres/Pg@137_Verify，backup_test 库，
tool_path=/pgdb/pgsql/bin）→ 备份 → 表级恢复验证。

## 3. PG→MySQL 轮询实时同步跨机实测

代码已就绪（轮询 CDC 本期实现，MySQL 本机闭环已实测 PASS）。
137 PG 就绪后（见上），建同步任务：源 137:5432 backup_test（增量列 id，
save_mode=upsert）→ 目标 140 MySQL verify_sync，验证跨机 watermark 推进。

## 4. 金仓 KingBase

安装介质（KingbaseES V009R001C001B0030 ISO）未在任何机器找到，无法部署。
介质到位后：安装至 137 或新机（端口 55432）→ 平台任务 18 复测
（历史失败「未识别到已知备份格式标记」预计与产物格式校验有关，需带介质定位）。

## 已实测通过项（本轮）

| 项 | 结果 |
|---|---|
| Webhooks 事件推送（HMAC 签名） | ✅ 送达+收到 JSON |
| 合成备份（真实合成点 synthesized_real） | ✅ |
| GFS 保留（窗口语义+护栏 KEEP_MIN=3） | ✅ 4/6 标记，护栏保护最新 |
| 摆渡收件箱（sha256→解包→登记→三级复制→扫描） | ✅ |
| MySQL/PG 表级恢复（勾选表才恢复） | ✅ 双库型 |
| 达梦 dmp 对象清单解析（BKP_TEST/SYSDBA） | ✅（修正正则后） |
| 多库同步插件 7 库型注册 + 轮询实时同步（MySQL 闭环） | ✅ |
