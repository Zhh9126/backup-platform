# -*- coding: utf-8 -*-
"""
克隆服务（CloneService）：把零散的 `mysql_clone_to_test` / `pg_clone_to_test` + VDB
实例标准化为「申请 → 审批 → 拉起 VDB → 生命周期 → 自动销毁」的标准服务。

职责
----
- request_clone：开发自助申请 → 建 clone_request(pending) + itsm_ticket(internal) + 发起审批。
- approve_clone / reject_clone：平台内审批；approve 后按源库类型调用底层克隆函数，
  写入 vdb_instance_id，置 expires_at（默认 7 天，可被策略/配置覆盖）。
- expire_clone / destroy_clone：到期自动销毁（scheduler 周期 job 调用）+ 手动销毁，
  调用 restore_extras.drop_clone 释放 VDB 实例（无真实环境时 DEMO 下置状态）。
- list_clones / get_clone。

所有外部依赖（底层克隆函数）均支持 DEMO_MODE 仿真兜底：DEMO 下直接仿真成功并登记
VDB 实例，保证无真实数据库客户端也能跑通自测闭环。
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import config
import core.models as models
import core.db as db
from core import restore_extras
from core import restore_extras_clone
from core.itsm import get_itsm_adapter


_logger = db.get_logger("clone")

# 克隆状态机
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CREATING = "creating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"

# 触发底层克隆引擎的 db_type 映射；其余类型明确不支持（不仿真）
_CLONE_ENGINE = {
    "mysql": "mysql_clone_to_test",
    "mariadb": "mysql_clone_to_test",
    "postgresql": "pg_clone_to_test",
    # 2026-11-13：金仓补上真实克隆（此前直接拒："暂不支持真实克隆"）。
    # 走金仓引擎自身的恢复通道（远端 SSH + ksql 建库导入），该通道已在
    # 170 真机验证：异名库 293 张业务表完整落地。
    "kingbase": "kb_clone_to_test",
    # 2026-11-15：达梦真实克隆（逻辑 .dmp 产物）：走达梦引擎自身的恢复
    # 通道（dimp REMAP_SCHEMA=源:clone_N + dmPython 自动建模式，171:5238
    # 真机验证），克隆实例=同实例上的独立 schema（DM8 无库级隔离语义，
    # schema 即达梦的"库"对等物，与 Oracle schema 克隆同构）。
    "dameng": "dameng_clone_to_test",
    # 2026-11-17：Oracle 逻辑备份产物（.dmp）克隆补齐。此前逻辑分支没有
    # oracle 映射，用户从 Oracle 备份记录发起克隆必然报「暂不支持真实克隆」。
    # 走 Oracle 引擎恢复通道（impdp REMAP_SCHEMA=源:CLONE_N，与达梦 schema
    # 克隆同构）：克隆实例=源实例上的独立 schema，verify/drop 均已支持。
    "oracle": "oracle_clone_to_test",
}


def _snapshot_keep() -> int:
    """每个克隆请求保留的快照数量上限（环境变量可调，默认 10）。"""
    try:
        v = int(os.environ.get("AIDBM_VDB_SNAPSHOT_KEEP") or 10)
    except (TypeError, ValueError):
        v = 10
    return v if v > 0 else 10


def _default_ttl_days() -> int:
    try:
        return int(db.get_system_config("clone_default_ttl_days") or 7)
    except Exception:
        return 7


def _compute_expires(ttl_days: int) -> str:
    """计算到期时间，统一用 UTC 字符串（与 SQLite datetime('now') 格式对齐）。"""
    return (datetime.utcnow() + timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")


class CloneService:
    """克隆服务：标准化克隆全生命周期管理。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 申请 -------------------------
    def request_clone(self, source_record_id: int, target_env: str,
                      requested_by: str, note: str = "",
                      itsm_system: str = None,
                      target_host: str = "127.0.0.1",
                      target_password: str = "",
                      clone_mode: str = "logical",
                      live_source_db: str = "",
                      purpose: str = "test",
                      databases: list = None) -> dict:
        """提交克隆申请。

        clone_mode：
        - logical（默认）：从备份产物逻辑导入克隆（mysqldump/pg_restore 流式导入）
        - template：PostgreSQL 模板库 COW 秒级克隆（live_source_db=同实例源库名）
        - snapshot：MySQL LVM 快照秒级克隆（源 datadir 须在 LVM 卷上）
        - schema：Oracle expdp/impdp REMAP schema 克隆（live_source_db=源 schema）

        purpose：用途标记（test/dev/staging/emergency）。emergency=应急接管
        （B3 对标鼎甲「自动灾难恢复一键激活」）：生产故障时从最新备份拉起的
        克隆库可临时承载业务，配合「接管检查」验证就绪状态。

        免审批直通（默认，业界 VDB 标准打法，借鉴 Delphix/Neon）：
        1) 建 clone_request(pending)
        2) 立即后台异步拉起克隆库（creating → ready/failed），不阻塞 API
        3) ITSM 工单仅在显式指定 itsm_system 时创建（可选审计留痕）

        target_host：克隆库拉起的目标主机（默认本机 127.0.0.1，可指定其他机器 IP，
        要求该机器上的 MySQL/PG 实例允许从本平台远程连接）。
        target_password：目标实例管理员密码（可空=沿用源任务密码）。

        itsm 模式（CLONE_AUTO_APPROVE=false）：走原审批流。
        """
        rec = models.get_record(source_record_id)
        if not rec:
            raise ValueError(f"源备份记录不存在: {source_record_id}")
        target_env = (target_env or "").strip()
        if not target_env:
            raise ValueError("目标环境 target_env 必填")
        requested_by = (requested_by or "anonymous").strip() or "anonymous"
        target_host = (target_host or "127.0.0.1").strip() or "127.0.0.1"
        clone_mode = (clone_mode or "logical").strip() or "logical"
        live_source_db = (live_source_db or "").strip()
        if clone_mode in ("template", "schema") and not live_source_db:
            raise ValueError(f"克隆模式 {clone_mode} 需要提供源库名/schema（live_source_db）")

        # VDB 克隆预检（只读，对标 zBackup clonePreflight）：在创建申请之前
        # 拦住产物缺失 / 目标端口不可达 / 安全隔离态，避免拉起半成品 VDB。
        from core.clone_preflight import preflight as _clone_preflight
        pf = _clone_preflight(rec, target_host=target_host,
                              target_password=target_password,
                              note=note, clone_mode=clone_mode)
        if not pf["ok"]:
            raise ValueError("克隆预检未通过: " + "；".join(pf["errors"]))
        if pf["warnings"]:
            self.logger.info("[clone] 预检提示: %s", "；".join(pf["warnings"]))

        # 克隆模式编码进 note（免改表结构，_launch_vdb 解析）
        mode_tag = f"[clone_mode={clone_mode}]"
        if live_source_db:
            mode_tag += f"[live_src={live_source_db}]"
        note = (mode_tag + "\n" + note) if note else mode_tag

        req_id = models.create_clone_request({
            "source_record_id": source_record_id,
            "target_env": target_env,
            "target_host": target_host,
            "target_password": target_password or "",
            "status": STATUS_PENDING,
            "purpose": (purpose or "test").strip() or "test",
            "requested_by": requested_by,
            "note": note,
            "databases": (json.dumps([str(x).strip() for x in databases
                                      if str(x).strip()], ensure_ascii=False)
                          if databases else ""),
        })
        # ITSM 工单：仅显式指定适配器时创建（可选审计留痕，不阻塞克隆）
        if itsm_system:
            adapter = get_itsm_adapter(itsm_system)
            ticket = adapter.create_ticket("clone", req_id, {
                "source_record_id": source_record_id,
                "target_env": target_env,
                "requested_by": requested_by,
                "note": note,
            })
            models.update_clone_request(req_id, {"itsm_ticket_id": ticket["id"]})

        if config.CLONE_AUTO_APPROVE:
            self.logger.info("[clone] 申请 %s 已提交，免审批直通拉起（env=%s, by=%s）",
                             req_id, target_env, requested_by)
            self._provision_async(req_id)
        else:
            self.logger.info("[clone] 申请 %s 已提交，等待审批（env=%s, by=%s）",
                             req_id, target_env, requested_by)
        return self.get_clone(req_id)

    def _provision_async(self, request_id: int) -> None:
        """后台线程拉起 VDB：creating → ready / failed（失败原因写入 note）。"""
        import threading

        def _run() -> None:
            try:
                req = self.get_clone(request_id)
                if not req:
                    return
                models.update_clone_request(request_id, {"status": STATUS_CREATING})
                self.logger.info("[clone] 请求 %s 开始拉起 VDB", request_id)
                vdb_id = self._launch_vdb(req)
                ttl = _default_ttl_days()
                expires = _compute_expires(ttl)
                models.update_clone_request(request_id, {
                    "status": STATUS_READY,
                    "vdb_instance_id": vdb_id,
                    "expires_at": expires,
                })
                self.logger.info("[clone] 请求 %s VDB %s 已就绪，到期 %s",
                                 request_id, vdb_id, expires)
            except Exception as exc:
                self.logger.error("[clone] 请求 %s 拉起失败: %s", request_id, exc)
                cur = self.get_clone(request_id) or {}
                note = (cur.get("note") or "").strip()
                models.update_clone_request(request_id, {
                    "status": STATUS_FAILED,
                    "note": (note + "\n" if note else "") + f"拉起失败: {exc}"[:480],
                })

        threading.Thread(target=_run, daemon=True,
                         name=f"clone-provision-{request_id}").start()

    # ------------------------- 审批 -------------------------
    def approve_clone(self, request_id: int, approved_by: str = "admin") -> dict:
        """审批通过：拉起 VDB 实例并登记，置 ready。"""
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        if req["status"] in (STATUS_READY, STATUS_CREATING, STATUS_EXPIRED,
                             STATUS_DELETED):
            # 幂等：已拉起/已销毁则不重复执行
            return req

        # 1) 同步 ITSM 工单状态（内部/外部适配器，幂等）
        if req.get("itsm_ticket_id"):
            try:
                get_itsm_adapter().approve_ticket(req["itsm_ticket_id"], approved_by)
            except Exception as e:
                self.logger.warning("[clone] 同步 ITSM 工单失败（忽略）: %s", e)

        # 2) 审批路径同样做预检（关闭免审批直通时不能绕过）
        import re as _re
        from core import clone_preflight as _cpf
        _m = _re.search(r"\[clone_mode=([^\]]+)\]", str(req.get("note") or ""))
        _mode = _m.group(1) if _m else "logical"
        _src = models.get_record(req.get("source_record_id"))
        _pf = _cpf.preflight(
            _src, target_host=req.get("target_host") or "127.0.0.1",
            target_password=req.get("target_password") or "",
            note=req.get("note") or "", clone_mode=_mode)
        if not _pf["ok"]:
            raise ValueError("克隆预检未通过: " + "；".join(_pf["errors"]))

        # 3) 拉起 VDB 实例（异常必须落 failed，避免卡死 creating 无法重试）
        req = models.get_clone_request(request_id, include_secret=True)
        req["status"] = STATUS_CREATING
        models.update_clone_request(request_id, {"status": STATUS_CREATING,
                                                  "approved_by": approved_by})
        self.logger.info("[clone] 请求 %s 审批通过，开始拉起 VDB", request_id)
        try:
            vdb_id = self._launch_vdb(req)
        except Exception as exc:
            self.logger.error("[clone] 请求 %s 拉起失败: %s", request_id, exc)
            cur = self.get_clone(request_id) or {}
            note = (cur.get("note") or "").strip()
            models.update_clone_request(request_id, {
                "status": STATUS_FAILED,
                "note": (note + "\n" if note else "") + f"拉起失败: {exc}"[:480],
            })
            raise
        ttl = _default_ttl_days()
        expires = _compute_expires(ttl)
        models.update_clone_request(request_id, {
            "status": STATUS_READY,
            "vdb_instance_id": vdb_id,
            "expires_at": expires,
        })
        self.logger.info("[clone] 请求 %s VDB %s 已就绪，到期 %s",
                         request_id, vdb_id, expires)
        return self.get_clone(request_id)

    def reject_clone(self, request_id: int, by: str = "admin") -> dict:
        """驳回克隆申请。"""
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        if req["status"] in (STATUS_READY, STATUS_CREATING, STATUS_EXPIRED,
                             STATUS_DELETED):
            return req
        if req.get("itsm_ticket_id"):
            try:
                get_itsm_adapter().reject_ticket(req["itsm_ticket_id"], by)
            except Exception as e:
                self.logger.warning("[clone] 同步 ITSM 工单失败（忽略）: %s", e)
        models.update_clone_request(request_id, {
            "status": STATUS_REJECTED, "approved_by": by})
        self.logger.info("[clone] 请求 %s 已驳回（by=%s）", request_id, by)
        return self.get_clone(request_id)

    # ------------------------- 销毁 -------------------------
    def destroy_clone(self, request_id: int) -> dict:
        """手动销毁：释放 VDB 实例并置 deleted。"""
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        self._release_vdb(req)
        models.update_clone_request(request_id, {"status": STATUS_DELETED})
        self.logger.info("[clone] 请求 %s 已手动销毁", request_id)
        return self.get_clone(request_id)

    def expire_clone(self, request_id: int) -> dict:
        """到期自动销毁：释放 VDB 实例并置 expired。"""
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        if req["status"] not in (STATUS_READY, STATUS_CREATING):
            return req
        self._release_vdb(req)
        models.update_clone_request(request_id, {"status": STATUS_EXPIRED})
        self.logger.info("[clone] 请求 %s 已到期自动销毁", request_id)
        return self.get_clone(request_id)

    def takeover_check(self, request_id: int, operator: str = "") -> dict:
        """应急接管就绪检查（B3，对标鼎甲「自动灾难恢复一键激活」）。

        对 ready 的克隆库做业务承载就绪验证：VDB 状态 / 原生直连 / 表数量 /
        行数抽样（首表前 5 行计数）。全部通过=克隆库可临时承载业务读流量；
        写流量接管需应用侧切换连接串（如实说明，不伪装自动切流）。
        结果写入审计日志与请求 note。
        """
        import time as _t
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        if req.get("status") != STATUS_READY:
            raise ValueError("仅就绪（ready）状态的克隆库可做接管检查")
        # 连接信息从 vdb_instances 取（get_clone 是原始行，无 join 字段）
        vrow = db.query_one(
            "SELECT host, port, database_name, username, service, db_type "
            "FROM vdb_instances WHERE id=?", (req.get("vdb_instance_id"),))
        if not vrow:
            raise ValueError("克隆库连接信息缺失（vdb 实例不存在）")
        vdb_host = vrow["host"]
        vdb_port = vrow["port"]
        vdb_db = vrow["database_name"]
        vdb_user = vrow["username"]
        # Oracle：native_conn 第 4 参语义是**服务名**，不是 schema 名——
        # 传 schema 名（C##CLONE_N）会 ORA-12514/DPY-6005（实测）。连接用
        # vdb 注册时持久化的源实例服务名，统计仍按 schema 名（下文 owner=）
        _ora_service = None
        if (vrow.get("db_type") or "").lower() == "oracle" and vrow.get("service"):
            _ora_service = vrow["service"]
        if not (vdb_host and vdb_port and vdb_db):
            raise ValueError("克隆库连接信息缺失（vdb 实例未就绪）")
        from core import native_conn
        src_type = (req.get("source_db_type") or "mysql").lower()
        checks = []
        t0 = _t.time()
        ok_all = True
        # 密码：优先 vdb 实例拉起时的真实密码（加密存储），回退 target_password
        from core.models import get_clone_request as _get_req
        req_full = _get_req(request_id, include_secret=True) or {}
        vprow = db.query_one("SELECT password FROM vdb_instances WHERE id=?",
                             (req.get("vdb_instance_id"),))
        vdb_pw = ""
        if vprow and vprow.get("password"):
            raw = vprow["password"]
            try:
                vdb_pw = db.decrypt_secret(raw) or ""
            except Exception:
                vdb_pw = raw
        if not vdb_pw:
            try:
                vdb_pw = db.decrypt_secret(req_full.get("target_password") or "") or ""
            except Exception:
                vdb_pw = req_full.get("target_password") or ""
        try:
            if src_type == "oracle":
                # Oracle 直连偶发 DPY-6005（171/129 监听抖动实测）：重试 3 次
                from core.restore_extras_clone import _ora_connect_retry
                conn = _ora_connect_retry(lambda _i: native_conn.connect(
                    src_type, vdb_host, int(vdb_port), _ora_service or vdb_db,
                    vdb_user or "", vdb_pw, timeout=15))
            else:
                conn = native_conn.connect(
                    src_type, vdb_host, int(vdb_port), vdb_db,
                    vdb_user or "", vdb_pw, timeout=15)
            try:
                cur = conn.cursor()
                try:
                    # Oracle 语法要求 FROM DUAL（裸 SELECT 1 报 ORA-00923，实测）
                    cur.execute("SELECT 1 FROM DUAL" if src_type == "oracle"
                                else "SELECT 1")
                    checks.append(("connectivity", True, "克隆库直连成功"))
                    n_tables = 0
                    first_rows = None
                    if src_type in ("mysql", "mariadb", "tidb", "oceanbase"):
                        cur.execute("SELECT count(*) FROM information_schema.tables "
                                    "WHERE table_schema=%s", (vdb_db,))
                        n_tables = int(cur.fetchone()[0])
                        if n_tables:
                            cur.execute("SELECT table_name FROM information_schema.tables "
                                        "WHERE table_schema=%s AND table_type='BASE TABLE' LIMIT 1",
                                        (vdb_db,))
                            r = cur.fetchone()
                            if r:
                                fq = "`%s`.`%s`" % (vdb_db.replace("`", "``"),
                                                    str(r[0]).replace("`", "``"))
                                cur.execute("SELECT count(*) FROM %s" % fq)
                                first_rows = int(cur.fetchone()[0])
                    elif src_type == "dameng":
                        # 达梦：克隆实例=独立模式（database_name=模式名），DM8
                        # 无 information_schema.tables，用 dba_tables 按模式统计
                        #（dmPython 占位符 ?）
                        cur.execute("SELECT count(*) FROM dba_tables WHERE owner = ?",
                                    (str(vdb_db or "").upper(),))
                        n_tables = int(cur.fetchone()[0] or 0)
                    else:  # PG 系（库级克隆）/ Oracle schema
                        if src_type == "oracle":
                            q = "SELECT count(*) FROM all_tables WHERE owner=UPPER(:1)"
                            cur.execute(q, (vdb_db,))
                            n_tables = int(cur.fetchone()[0])
                        else:
                            # 库级克隆的表都在 public（金仓/PG），按「当前库全部
                            # 用户表」统计——此前按 table_schema=<库名> 过滤恒 0
                            #（金仓 clone_36 实测 103 张表被误报 0）。
                            # 2026-11-17：金仓内部 schema 排不净导致虚高（fresh 库
                            # sys_catalog/sys_hm/pg_catalog 等 100+ 张），补 sys% 前缀排除。
                            cur.execute(
                                "SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema NOT LIKE 'sys%' AND table_schema "
                                "NOT IN ('pg_catalog','information_schema','anon','src_restrict')")
                            n_tables = int(cur.fetchone()[0] or 0)
                    checks.append(("tables", n_tables > 0,
                                   "表数量 %d" % n_tables))
                    if first_rows is not None:
                        checks.append(("sample_rows", True,
                                       "首表行数 %d" % first_rows))
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            ok_all = False
            checks.append(("connectivity", False, "克隆库直连失败: %s" % str(e)[:150]))
        ok_all = ok_all and all(c[1] for c in checks)
        dur = round(_t.time() - t0, 2)
        summary = "接管检查%s（%.1fs）：%s" % (
            "通过" if ok_all else "未通过", dur,
            "; ".join("%s=%s" % (n, m) for n, ok, m in checks))
        note = ((req.get("note") or "") + "\n" if req.get("note") else "") + \
            "[接管检查 %s] %s（操作者 %s）" % (
                _t.strftime("%Y-%m-%d %H:%M", _t.localtime()), summary,
                operator or "unknown")
        models.update_clone_request(request_id, {"note": note})
        db.add_log("INFO" if ok_all else "WARNING", "clone",
                   "克隆 %s 应急接管检查：%s" % (request_id, summary))
        return {"ok": ok_all, "checks": [
            {"item": n, "pass": bool(ok), "message": m} for n, ok, m in checks],
            "duration_sec": dur, "summary": summary}

    def expire_due_clones(self) -> list:
        """扫描到期（expires_at <= now）的 ready/creating 克隆并自动销毁。供 scheduler 调用。"""
        due = db.query(
            "SELECT * FROM clone_requests WHERE status IN ('ready','creating') "
            "AND expires_at IS NOT NULL AND expires_at <= datetime('now') "
            "ORDER BY id")
        result = []
        for r in due:
            try:
                result.append(self.expire_clone(r["id"]))
            except Exception as e:
                self.logger.warning("[clone] 到期销毁 %s 失败: %s", r["id"], e)
        if result:
            self.logger.info("[clone] 本次自动销毁 %d 个到期克隆", len(result))
        return result

    # ------------------------- 查询 -------------------------
    def _sweep_stuck_creating(self, minutes: int = 45) -> int:
        """把长时间停在 creating 的克隆请求置为 failed（防永久「创建中」）。

        场景：整实例克隆导入耗时很长，平台重启/崩溃会让后台线程消失，请求
        永远停在 creating，前端一直转圈且无法销毁。这里按 updated_at 兜底。
        """
        try:
            cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            rows = models.db.query(
                "SELECT id, note FROM clone_requests WHERE status='creating' "
                "AND (updated_at IS NULL OR updated_at < ?)", (cutoff,))
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for r in rows:
            try:
                models.update_clone_request(int(r["id"]), {
                    "status": STATUS_FAILED,
                    "note": ((r.get("note") or "") + "\n"
                             "拉起超时（>%d 分钟未完成）：可能平台重启中断或目标实例过载，"
                             "请核对目标实例上的 clone_* 库后重试或销毁" % minutes)[-800:]})
                n += 1
            except Exception:  # noqa: BLE001
                pass
        if n:
            self.logger.warning("[clone] %d 个克隆请求停在 creating 超时，已置 failed", n)
        return n

    def list_clones(self) -> list:
        # 列表是最高频入口：顺带把卡死的 creating 兜底置失败（幂等，成本一条 SQL）
        self._sweep_stuck_creating()
        return models.list_clone_requests()

    def get_clone(self, request_id: int) -> Optional[dict]:
        return models.get_clone_request(request_id)

    # ------------------------- 内部：拉起 / 释放 VDB -------------------------
    @staticmethod
    def _parse_mode(req: dict) -> tuple:
        """从 note 前缀解析克隆模式与在线源库：([mode, live_src], 剩余note)。"""
        note = req.get("note") or ""
        mode, src_db = "logical", ""
        m = re.search(r"\[clone_mode=(\w+)\]", note)
        if m:
            mode = m.group(1)
        m2 = re.search(r"\[live_src=([^\]]+)\]", note)
        if m2:
            src_db = m2.group(1)
        rest = re.sub(r"\[clone_mode=\w+\]|\[live_src=[^\]]+\]", "", note).strip()
        return (mode, src_db), rest

    def _launch_vdb(self, req: dict) -> int:
        """按源库类型/克隆模式调用底层真实克隆引擎，并登记 VDB 实例。

        引擎矩阵：
        - mysql/mariadb：logical=流式导入；snapshot=LVM 快照 COW（无 LVM 明确报错）
        - postgresql：logical=pg_restore 导入；template=模板库 COW 秒级克隆
        - oracle：schema=expdp/impdp REMAP 在线 schema 克隆（129 真机验证）
        目标主机：req.target_host（默认本机 127.0.0.1，可指定其他机器 IP）；
        连接密码：req.target_password 优先，未填时沿用源任务密码。
        其余组合明确抛错（不降级仿真，避免"假克隆"）；
        仅 DEMO_MODE != off 时才允许仿真兜底且在 VDB note 中明确标注。
        """
        rec = models.get_record(req["source_record_id"]) or {}
        db_type = rec.get("db_type") or "mysql"
        instance_name = f"clone_{req['id']}"
        (clone_mode, live_src), user_note = self._parse_mode(req)
        req = dict(req)
        req["note"] = user_note
        backup_path = rec.get("backup_path") or ""

        # ---- template / snapshot / schema 模式不依赖备份产物 ----
        if clone_mode in ("template", "snapshot", "schema"):
            password = req.get("target_password") or ""
            if not password:
                src_task = models.get_task(rec.get("task_id"),
                                           include_secret=True) or {}
                password = src_task.get("password") or ""
            if config.DEMO_MODE != "off":
                return self._register_vdb(req, rec, db_type, instance_name,
                                          None, None, None, None,
                                          "DEMO 仿真（COW 模板/快照）")
            res = None
            if db_type == "postgresql" and clone_mode == "template":
                res = restore_extras_clone.pg_template_clone(
                    live_src, instance_name,
                    pg_host=req.get("target_host") or "127.0.0.1",
                    pg_user="postgres", pg_password=password)
            elif db_type in ("mysql", "mariadb") and clone_mode == "snapshot":
                _st0 = models.get_task(rec.get("task_id"), include_secret=True) or {}
                res = restore_extras_clone.mysql_snapshot_clone(
                    instance_name,
                    src_port=int(rec.get("port") or _st0.get("port") or 3306),
                    mysql_host=req.get("target_host") or "127.0.0.1",
                    mysql_password=password)
            elif db_type == "oracle" and clone_mode == "schema":
                src_task = models.get_task(rec.get("task_id"),
                                           include_secret=True) or {}
                host = src_task.get("host") or rec.get("host") or "127.0.0.1"
                port = int(src_task.get("port") or rec.get("port") or 1521)
                service = (src_task.get("db_name") or src_task.get("database")
                           or rec.get("db_name") or "ORCL")
                ssh_host = self._resolve_ssh(rec)
                res = restore_extras_clone.oracle_schema_clone(
                    live_src, instance_name,
                    host=host, port=port, service=service,
                    user="system", password=password,
                    ssh_host=ssh_host)
            else:
                raise RuntimeError(
                    f"db_type={db_type} 不支持克隆模式 {clone_mode}"
                    "（template 仅 PostgreSQL；snapshot 仅 MySQL；schema 仅 Oracle）")
            if not res or not res.get("ok"):
                raise RuntimeError((res or {}).get("message") or "秒级克隆失败")
            elapsed = (res.get("elapsed_sec") and
                       f"（耗时 {res['elapsed_sec']}s）") or ""
            default_port = 5432 if db_type == "postgresql" else (
                1521 if db_type == "oracle" else 3306)
            return self._register_vdb(
                req, rec, db_type, instance_name,
                res.get("port") or default_port,
                "postgres" if db_type == "postgresql" else ("system" if db_type == "oracle" else "root"),
                password,
                f"{'COW 快照/模板' if res.get('cow') else 'Schema'}克隆实例"
                f"{elapsed} | {user_note}",
                service=(service if db_type == "oracle" else None))

        # ---- logical（默认）：基于备份产物逻辑导入 ----
        if not backup_path or not os.path.isfile(backup_path):
            raise RuntimeError(f"备份产物不存在: {backup_path or '-'}")

        target_host = (req.get("target_host") or "127.0.0.1").strip() or "127.0.0.1"
        # backup_records 没有 port 字段（实测恒 None）：默认端口必须回退到
        # 源任务真实端口，否则达梦非 5236 / 金仓非 54321 的实例上，克隆本体
        # 成功而就绪校验/接管检查连错端口（171:5238 实测 -70028）
        src_task0 = models.get_task(rec.get("task_id"), include_secret=True) or {}
        # 默认端口一律「记录 → 源任务 → 引擎默认」三级回退（mysql/pg 与
        # 金仓/达梦/Oracle 同口径）：本机 MySQL 3307 / PG 5785 克隆实测暴露
        # 此前 mysql/postgresql 分支端口写死 3306/5432，克隆打错实例
        port = int(rec.get("port") or src_task0.get("port") or (
            3306 if db_type in ("mysql", "mariadb") else (
                5432 if db_type == "postgresql" else (
                    54321 if db_type == "kingbase" else (
                        5236 if db_type == "dameng" else 1521)))))
        # target_host 支持 host:port 形式（B3 应急接管/自定义实例端口）
        if ":" in target_host:
            _th, _tp = target_host.rsplit(":", 1)
            if _tp.isdigit() and _th:
                target_host, port = _th, int(_tp)
                req = dict(req)
                req["target_host"] = target_host  # vdb 注册存纯主机名
        user = "root" if db_type in ("mysql", "mariadb") else (
            "postgres" if db_type == "postgresql" else (
                "system" if db_type == "kingbase" else (
                    "SYSDBA" if db_type == "dameng" else (
                        "system" if db_type == "oracle" else ""))))
        # 连接密码：目标实例密码优先，未填则沿用源任务密码
        password = req.get("target_password") or ""
        if not password:
            src_task = models.get_task(rec.get("task_id"), include_secret=True) or {}
            password = src_task.get("password") or ""

        simulated = config.DEMO_MODE != "off"
        scope = self._archive_scope(backup_path)
        if not simulated and scope == "full_instance":
            # 整实例克隆：全实例归档（多库 tar）按库拆开，每库落到 <prefix>__<源库>。
            # 此前把多库 tar 当单个 SQL 灌进一个库，直接报 ASCII '\0'（实测）。
            _want = self._parse_databases(req.get("databases"))

            def _prog(idx, total, db):
                try:
                    models.update_clone_request(req["id"], {
                        "note": "整实例克隆中 %d/%d：%s | scope=full_instance"
                                % (idx, total, db)})
                except Exception:  # noqa: BLE001
                    pass

            res = restore_extras.mysql_full_instance_clone(
                backup_path, instance_name,
                mysql_host=target_host, mysql_port=port or 3306,
                mysql_user=user, mysql_password=password,
                databases=_want, progress=_prog)
            if not res.get("ok"):
                raise RuntimeError(f"整实例克隆失败: {res.get('message')}")
            _dbs = res.get("databases") or []
            _note = ("整实例克隆（%d 库 / %d 表：%s）| scope=full_instance | 目标主机 %s"
                     % (res.get("db_count") or 0, res.get("table_total") or 0,
                        ",".join(str(d.get("source", "")) for d in _dbs[:6]),
                        target_host))
            return self._register_vdb(req, rec, db_type, instance_name, port, user,
                                      password, _note)
        if not simulated and scope == "physical":
            # 物理归档（xtrabackup / base.tar.gz 等）不能用「逻辑导入」克隆：
            # 此前会被当 SQL 灌入，报 ASCII '\0'，用户看不出根因。明确拒绝。
            raise RuntimeError(
                "该备份是物理备份归档（tar），逻辑克隆不支持：请改用「数据恢复」的"
                "物理恢复通道，或对逻辑备份记录发起克隆")
        if not simulated:
            engine_name = _CLONE_ENGINE.get(db_type)
            if not engine_name:
                raise RuntimeError(
                    f"db_type={db_type} 暂不支持真实克隆"
                    "（当前支持 mysql/mariadb/postgresql/kingbase/dameng/oracle"
                    " 逻辑导入；秒级克隆见 template/snapshot/schema 模式）")
            if engine_name == "dameng_clone_to_test":
                # 达梦：复用引擎恢复通道（dimp REMAP_SCHEMA=源:目标 + dmPython
                # 自动建目标模式）。DM8 未加引号的模式名一律落大写
                # （CASE_SENSITIVE=1），克隆模式名统一大写，保证 dimp 导入、
                # 就绪校验（dba_tables.owner）、销毁（DROP SCHEMA）三处口径一致。
                from core.engines import get_engine as _get_engine
                dm_task = models.get_task(rec.get("task_id"),
                                          include_secret=True) or {}
                if password and target_host not in ("127.0.0.1", "", None):
                    dm_task = {**dm_task, "password": password}
                eng = _get_engine("dameng", dm_task,
                                  getattr(config, "BACKUP_ROOT", self._storage_root()))
                tgt_schema = instance_name.upper()
                rr = eng.restore(backup_path, target_db=tgt_schema)
                res = {"ok": bool(getattr(rr, "success", False)),
                       "message": getattr(rr, "message", "") or "",
                       "schema": tgt_schema}
                # 达梦无库级克隆语义：VDB database_name 直接存模式名（大写）
                instance_name = tgt_schema
            elif engine_name == "oracle_clone_to_test":
                # Oracle：逻辑 .dmp 产物 → impdp REMAP_SCHEMA=源:CLONE_N。
                # 克隆实例=源实例上的独立 schema（Oracle 无库级隔离，与达梦
                # schema 克隆同构）；模式名统一大写（未引号标识符落大写），
                # 保证 impdp 导入、verify（all_tables.owner）、销毁（drop
                # schema）三处口径一致。源 schema 取自源任务 extra.schemas，
                # 恢复目标服务名由引擎按任务配置解析。
                from core.engines import get_engine as _get_engine
                ora_task = models.get_task(rec.get("task_id"),
                                           include_secret=True) or {}
                eng = _get_engine("oracle", ora_task,
                                  getattr(config, "BACKUP_ROOT", self._storage_root()))
                tgt_schema = instance_name.upper()
                try:
                    # CDB 根容器下普通用户必须 C## 前缀（171 orcldg 实测
                    # ORA-65096/01918）；非 CDB 原样返回
                    tgt_schema = eng.clone_target_schema(tgt_schema)
                except Exception:
                    pass
                rr = eng.restore(backup_path, target_schema=tgt_schema)
                res = {"ok": bool(getattr(rr, "success", False)),
                       "message": getattr(rr, "message", "") or "",
                       "schema": tgt_schema}
                instance_name = tgt_schema
            elif engine_name == "pg_clone_to_test":
                res = restore_extras.pg_clone_to_test(
                    backup_path, instance_name,
                    pg_host=target_host, pg_port=port or None,
                    pg_user=user, pg_password=password)
            elif engine_name == "kb_clone_to_test":
                # 金仓：复用引擎恢复通道（远端 SSH + ksql 建库导入，零安装），
                # 引擎侧已处理 sys_dump/ksql 的版本与 PATH 解析。
                from core.engines import get_engine as _get_engine
                kb_task = models.get_task(rec.get("task_id"),
                                          include_secret=True) or {}
                if password and target_host not in ("127.0.0.1", "", None):
                    kb_task = {**kb_task, "password": password}
                eng = _get_engine("kingbase", kb_task,
                                  getattr(config, "BACKUP_ROOT", self._storage_root()))
                rr = eng.restore(backup_path, target_db=instance_name)
                res = {"ok": bool(getattr(rr, "success", False)),
                       "message": getattr(rr, "message", "") or ""}
            else:
                res = restore_extras.mysql_clone_to_test(
                    backup_path, instance_name,
                    mysql_host=target_host, mysql_port=port or 3306,
                    mysql_user=user, mysql_password=password)
            if not res.get("ok"):
                raise RuntimeError(f"真实克隆失败: {res.get('message')}")

        return self._register_vdb(req, rec, db_type, instance_name, port, user,
                                  password,
                                  ("DEMO 仿真实例" if simulated else "真实克隆实例")
                                  + (f" | 目标主机 {target_host}"
                                     if target_host != "127.0.0.1" else ""),
                                  service=((src_task0.get("db_name")
                                            or rec.get("db_name") or "")
                                           if db_type == "oracle" else None))

    @staticmethod
    def _parse_databases(raw) -> list:
        """解析「整实例克隆要哪些库」：支持 JSON 数组 / 逗号串 / 空（=全部库）。"""
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        txt = str(raw).strip()
        if not txt:
            return []
        if txt.startswith("["):
            try:
                data = json.loads(txt)
                return [str(x).strip() for x in data if str(x).strip()]
            except Exception:  # noqa: BLE001
                pass
        # 支持中英文逗号/换行/空格混排（UI 是从输入框手输的）
        import re as _re
        return [x.strip() for x in _re.split(r"[,，\s]+", txt) if x.strip()]

    @staticmethod
    def _archive_scope(path: str) -> str:
        """探测备份产物范围：single（单库逻辑文件）/ full_instance（全实例多库归档）/ physical。

        只读 tar 成员名，不做解包，成本毫秒级。
        """
        low = (path or "").lower()
        if not (low.endswith(".tar.gz") or low.endswith(".tgz") or low.endswith(".tar")):
            return "single"
        try:
            import tarfile
            with tarfile.open(path, "r:*") as tf:
                names = [n.lstrip("./") for n in tf.getnames()[:600]]
        except Exception:  # noqa: BLE001
            return "single"
        if any(n.endswith("xtrabackup_checkpoints") or n.endswith("backup-my.cnf")
               or n.endswith("mariabackup_checkpoints") for n in names):
            return "physical"
        if any(n.endswith("manifest.json") or n.startswith("dbs/") or "/dbs/" in n
               for n in names):
            return "full_instance"
        # 其它 tar（PG/金仓 base.tar.gz、达梦备份集）同样不是可逻辑导入的产物
        return "physical"

    def _register_vdb(self, req: dict, rec: dict, db_type: str, name: str,
                      port, user: str, password: str, note: str,
                      service: str = None) -> int:
        """登记 VDB 实例元数据（Oracle 额外持久化服务名，供销毁/校验使用）。"""
        vdb_id = models.create_vdb({
            "name": name,
            "source_record_id": req["source_record_id"],
            "task_id": rec.get("task_id"),
            "db_type": db_type,
            "port": port,
            "host": req.get("target_host") or "127.0.0.1",
            "database_name": name,
            "username": user,
            "service": service or "",
            "password": db.encrypt_secret(password or "") if password else "",
            "status": "ready",
            "created_at": db.now_iso(),
            "expires_at": _compute_expires(_default_ttl_days()),
            "note": note + (f" | {req.get('note') or ''}"
                            if req.get("note") else ""),
        })
        return vdb_id

    @staticmethod
    def _storage_root() -> str:
        """备份根目录（引擎构造需要）；缺配置时退回 config 默认值。"""
        try:
            return config.BACKUP_ROOT
        except Exception:
            return os.path.join(os.getcwd(), "backups")

    @staticmethod
    def _resolve_ssh(rec: dict) -> dict:
        """从备份记录关联任务解析 SSH 主机（远端 Oracle 克隆用）。"""
        from core import remote_dump
        task = dict(rec.get("task_row") or {})
        if not task:
            try:
                task = models.get_task(rec.get("task_id"),
                                       include_secret=True) or {}
            except Exception:
                task = {}
        if not task:
            return None
        return remote_dump.resolve_ssh_host(task)

    def verify_clone(self, request_id: int) -> dict:
        """就绪校验：连接克隆库执行探活 + 统计表数量。"""
        req = models.get_clone_request(request_id, include_secret=True)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        if req.get("status") != STATUS_READY or not req.get("vdb_instance_id"):
            raise ValueError("克隆未就绪，无法校验")
        vdb = models.get_vdb(req["vdb_instance_id"])
        if not vdb:
            raise ValueError("VDB 实例元数据缺失")
        password = self._vdb_password(vdb, req)
        if "scope=full_instance" in str(vdb.get("note") or ""):
            # 整实例克隆：库是 <prefix>__<源库>，按前缀统计库数与表数
            res = restore_extras.verify_full_instance_clone(
                vdb.get("host") or "127.0.0.1", vdb.get("port"),
                vdb.get("username"), password,
                vdb.get("database_name") or "")
            self.logger.info("[clone] 请求 %s 整实例校验: %s", request_id, res.get("message"))
            return res
        res = restore_extras.verify_clone_conn(
            vdb.get("db_type"), vdb.get("host") or "127.0.0.1",
            vdb.get("port"), vdb.get("database_name"), vdb.get("username"),
            password=password,
            service=vdb.get("service") or "")
        self.logger.info("[clone] 请求 %s 校验: %s", request_id, res.get("message"))
        return res

    def _release_vdb(self, req: dict) -> None:
        """释放 VDB 实例：调用底层 drop_clone 销毁真实库，并清理元数据。DEMO 下仅置状态。"""
        vdb_id = req.get("vdb_instance_id")
        if not vdb_id:
            return
        vdb = models.get_vdb(vdb_id)
        if vdb:
            if config.DEMO_MODE == "off":
                # 连接密码：目标实例密码优先，未填则沿用源任务密码
                creq = models.get_clone_request(req["id"], include_secret=True) or {}
                password = self._vdb_password(vdb, creq)
                try:
                    if vdb.get("db_type") == "oracle":
                        res = restore_extras_clone.drop_clone_extended(
                            "oracle", vdb.get("name"),
                            host=vdb.get("host") or "127.0.0.1",
                            port=vdb.get("port") or 1521,
                            user="system", password=password,
                            service=vdb.get("service") or "ORCL")
                    else:
                        res = restore_extras.drop_clone(
                            vdb.get("db_type"), vdb.get("name"),
                            host=vdb.get("host") or "127.0.0.1",
                            port=vdb.get("port"),
                            mysql_password=password, pg_password=password)
                    if not res.get("ok"):
                        self.logger.warning("[clone] 释放 VDB %s 失败: %s",
                                            vdb_id, res.get("message"))
                except Exception as e:
                    self.logger.warning("[clone] 释放 VDB %s 失败（忽略）: %s", vdb_id, e)
            try:
                models.delete_vdb(vdb_id)
            except Exception as e:
                self.logger.warning("[clone] 删除 VDB 元数据 %s 失败（忽略）: %s", vdb_id, e)


    # ------------------------- VDB 快照（时间点 / 回滚 / 再克隆） -------------------------
    @staticmethod
    def _vdb_password(vdb: dict, req: dict = None) -> str:
        """统一密码来源：VDB 自己持久化的凭据优先，其次申请单，最后源任务。

        历史缺陷：verify_clone 只读 clone_requests.target_password，接管检查却
        优先 vdb_instances.password，旧 VDB API 甚至不写密码 —— 三处口径不一致。
        """
        pw = db.decrypt_secret(vdb.get("password") or "") or ""
        if not pw and req:
            pw = req.get("target_password") or ""
        if not pw:
            src_task = models.get_task(vdb.get("task_id"), include_secret=True) or {}
            pw = src_task.get("password") or ""
        return pw

    @staticmethod
    def _snapshot_row(snapshot_id: int, request_id: int = None) -> dict:
        if request_id:
            rows = db.query("SELECT * FROM vdb_snapshots WHERE id=? "
                            "AND clone_request_id=?", (snapshot_id, request_id))
        else:
            rows = db.query("SELECT * FROM vdb_snapshots WHERE id=?", (snapshot_id,))
        if not rows:
            raise ValueError("VDB 快照不存在: %s" % snapshot_id)
        return dict(rows[0])

    def _vdb_context(self, request_id: int, require_ready: bool = True):
        req = models.get_clone_request(request_id, include_secret=True)
        if not req:
            raise ValueError("克隆请求不存在: %s" % request_id)
        if require_ready and (req.get("status") != STATUS_READY
                              or not req.get("vdb_instance_id")):
            raise ValueError("克隆未就绪，无法操作快照")
        vdb = models.get_vdb(req.get("vdb_instance_id"))
        if not vdb:
            raise ValueError("VDB 实例元数据缺失")
        return req, vdb, self._vdb_password(vdb, req)

    def list_snapshots(self, request_id: int) -> list:
        return [dict(r) for r in (db.query(
            "SELECT * FROM vdb_snapshots WHERE clone_request_id=? "
            "AND status!='deleted' ORDER BY id DESC", (request_id,)) or [])]

    def create_snapshot(self, request_id: int, name: str = "",
                        created_by: str = "", kind: str = "auto") -> dict:
        """对就绪 VDB 做一次快照。

        kind：
        - ``native``  用数据库自带导出工具产出**标准备份产物**（含 DDL +
          一致性快照，可被平台恢复引擎直接消费）；
        - ``logical`` 平台自研的数据级 gzip JSON（纯 Python 导出，不含 DDL，
          受表数/行数上限约束）；
        - ``auto``    优先 native；平台缺工具或库型不支持时**如实回退**
          logical，并把实际产物类型与回退原因写进 note（不假装是 native）。
        """
        from core import vdb_snapshot as vs
        req, vdb, password = self._vdb_context(request_id)
        kind = str(kind or "auto").lower()
        if kind not in ("auto", "native", "logical"):
            raise ValueError("快照类型不支持: %s（auto/native/logical）" % kind)
        note = ""
        meta = None
        if kind in ("auto", "native"):
            try:
                meta = vs.export_native_snapshot(vdb, password, name=name,
                                                 created_by=created_by)
            except Exception as e:
                if kind == "native":
                    raise
                note = ("原生导出不可用，已回退为数据级快照（不含 DDL）：%s"
                        % str(e)[:160])
                meta = None
        if meta is None:
            meta = vs.export_snapshot(vdb, password, name=name,
                                      created_by=created_by)
        parent = None
        last = db.query("SELECT id FROM vdb_snapshots WHERE clone_request_id=? "
                        "AND status='ready' ORDER BY id DESC LIMIT 1",
                        (request_id,))
        if last:
            parent = last[0]["id"]
        sid = db.execute(
            "INSERT INTO vdb_snapshots(clone_request_id, vdb_instance_id, name,"
            " snap_kind, parent_snapshot_id, backup_path, size_bytes, checksum,"
            " table_count, row_count, status, note, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, vdb["id"], meta["name"], meta["snap_kind"], parent,
             meta["backup_path"], meta["size_bytes"], meta["checksum"],
             meta["table_count"], meta["row_count"], "ready",
             note or meta.get("note") or "",
             db.now_iso(), created_by or "unknown"))
        db.add_log("INFO", "clone",
                   "VDB 快照 %s 创建：克隆 %s，类型 %s，%s 表 / %s 行，%s 字节"
                   % (sid, request_id, meta["snap_kind"], meta["table_count"],
                      meta["row_count"], meta["size_bytes"]))
        # 保留策略：超出上限的最旧快照连产物一起清理（防备份盘被慢慢吃满）
        try:
            expired = vs.apply_retention(request_id=request_id,
                                          keep=_snapshot_keep())
            if expired:
                db.add_log("INFO", "clone",
                           "VDB 快照保留策略：清理超限快照 %s（每个克隆保留 %s 个）"
                           % (expired, _snapshot_keep()))
        except Exception as e:
            self.logger.warning("[clone] 快照保留策略执行失败（忽略）: %s", e)
        return self._snapshot_row(sid)

    def rollback_snapshot(self, request_id: int, snapshot_id: int,
                          drop_new_tables: bool = True,
                          operator: str = "") -> dict:
        """把 VDB 回滚到快照点；先做 sha256 完整性复核，篡改即拒绝。

        按快照类型分派：
        - ``native``  标准备份产物 → 走原生恢复通道（**含 DDL**，表结构变更
          不再是障碍；目标库会被重建）；
        - ``logical`` 数据级 JSON → DELETE + 批量插回（结构变更时如实拒绝）。
        """
        from core import vdb_snapshot as vs
        _req, vdb, password = self._vdb_context(request_id)
        snap = self._snapshot_row(snapshot_id, request_id)
        if snap.get("status") != "ready":
            raise ValueError("快照 %s 不是就绪状态（%s），拒绝回滚"
                             % (snapshot_id, snap.get("status")))
        try:
            actual = vs.sha256_file(snap["backup_path"])
        except Exception as e:
            raise ValueError("快照产物不可读: %s" % e)
        if snap.get("checksum") and actual != snap["checksum"]:
            raise ValueError("快照产物校验和不匹配（可能被篡改），拒绝回滚")
        kind = snap.get("snap_kind") or "logical"
        if kind == "native":
            res = vs.restore_native_snapshot(vdb, password, snap["backup_path"])
        else:
            res = vs.restore_snapshot(vdb, password, snap["backup_path"],
                                      drop_new_tables=drop_new_tables)
        db.add_log("WARNING", "clone",
                   "VDB 快照回滚：克隆 %s 回到快照 %s（%s），操作人 %s，%s"
                   % (request_id, snapshot_id, kind, operator or "unknown",
                      res.get("message")))
        return {**res, "snapshot_id": snapshot_id, "snap_kind": kind}

    def reclone_snapshot(self, request_id: int, snapshot_id: int,
                         target_env: str = "", requested_by: str = "",
                         operator: str = "") -> dict:
        """从快照再克隆：用源备份产物拉起新 VDB，再把快照数据回放进去。

        依赖原备份产物提供表结构（平台不做 DDL 逆向），产物不可用时如实拒绝。
        """
        from core import vdb_snapshot as vs
        req, vdb, password = self._vdb_context(request_id)
        snap = self._snapshot_row(snapshot_id, request_id)
        if snap.get("status") != "ready":
            raise ValueError("快照 %s 不是就绪状态，拒绝再克隆" % snapshot_id)
        if not req.get("source_record_id"):
            raise ValueError("源克隆不是基于备份产物，无法再克隆")
        rec = models.get_record(req["source_record_id"]) or {}
        if not os.path.isfile(rec.get("backup_path") or ""):
            raise ValueError("源备份产物已不可用，无法从快照再克隆"
                             "（请用可用备份产物重新克隆）")
        new_id = models.create_clone_request({
            "source_record_id": req["source_record_id"],
            "target_env": target_env or (req.get("target_env") or "snapshot-clone"),
            "target_host": req.get("target_host") or vdb.get("host") or "127.0.0.1",
            "target_password": password,
            "status": STATUS_CREATING,
            "purpose": "test",
            "requested_by": requested_by or operator or "snapshot",
            "note": "[clone_mode=logical][snapshot_parent=%s]" % snapshot_id,
        })
        try:
            new_req = models.get_clone_request(new_id, include_secret=True)
            vdb_id = self._launch_vdb(new_req)
            models.update_clone_request(
                new_id, {"status": STATUS_READY, "vdb_instance_id": vdb_id})
            _, new_vdb, new_pw = self._vdb_context(new_id)
            applied = vs.restore_snapshot(new_vdb, new_pw,
                                          snap["backup_path"],
                                          drop_new_tables=False)
        except Exception as e:
            models.update_clone_request(
                new_id, {"status": STATUS_FAILED,
                         "note": "快照再克隆失败: %s" % str(e)[:300]})
            db.add_log("WARNING", "clone",
                       "快照 %s 再克隆失败：%s" % (snapshot_id, str(e)[:200]))
            raise ValueError("快照再克隆失败: %s" % e)
        db.add_log("INFO", "clone",
                   "VDB 快照 %s 再克隆为新克隆 %s（VDB %s）"
                   % (snapshot_id, new_id, vdb_id))
        return {"ok": True, "clone_request_id": new_id,
                "vdb_instance_id": vdb_id, "snapshot_id": snapshot_id,
                "applied": applied}

    def delete_snapshot(self, request_id: int, snapshot_id: int,
                        operator: str = "", purge: bool = False) -> dict:
        """删除快照。

        默认**软删**：元数据置 deleted（保留审计轨迹），产物文件留在磁盘。
        ``purge=True`` 时连同产物文件一起物理删除——快照产物是实打实的磁盘
        占用，只软删会让备份盘被慢慢吃满，因此另提供显式的物理清理出口。
        """
        from core import vdb_snapshot as vs
        snap = self._snapshot_row(snapshot_id, request_id)
        db.execute("UPDATE vdb_snapshots SET status='deleted', deleted_at=?, "
                   "note=? WHERE id=?",
                   (db.now_iso(), "由 %s 删除" % (operator or "unknown"),
                    snapshot_id))
        purged = []
        if purge:
            purged = vs.purge_snapshot_files([snapshot_id])
        db.add_log("INFO", "clone",
                   "VDB 快照 %s 已删除（产物%s）"
                   % (snapshot_id, "已清理" if purge else "保留"))
        return {"ok": True, "snapshot_id": snapshot_id,
                "status": "deleted", "backup_path": snap.get("backup_path"),
                "purged": bool(purged and purged[0].get("removed"))}

    def rollback_vdb_snapshot(self, vdb_id: int, snapshot_id: int,
                              operator: str = "") -> dict:
        """按 **VDB 实例**回滚快照（不依赖克隆请求）。

        与 ``rollback_snapshot`` 的差别：后者要求快照挂在某个克隆请求下，
        而「VDB 管理」页创建的快照是按实例创建的（clone_request_id=0），
        没有克隆请求可依——此前这条路径**无法回滚**（功能缺口）。
        校验口径一致：sha256 先复核，篡改即拒。
        """
        from core import vdb_snapshot as vs
        vdb = models.get_vdb(int(vdb_id))
        if not vdb:
            raise ValueError("VDB 不存在: %s" % vdb_id)
        row = db.query_one("SELECT * FROM vdb_snapshots WHERE id=? "
                           "AND vdb_instance_id=?", (int(snapshot_id), int(vdb_id)))
        if not row:
            raise ValueError("快照不存在: %s" % snapshot_id)
        snap = dict(row)
        if snap.get("status") != "ready":
            raise ValueError("快照 %s 不是就绪状态（%s），拒绝回滚"
                             % (snapshot_id, snap.get("status")))
        password = self._vdb_password(vdb, None)
        try:
            actual = vs.sha256_file(snap["backup_path"])
        except Exception as e:
            raise ValueError("快照产物不可读: %s" % e)
        if snap.get("checksum") and actual != snap["checksum"]:
            raise ValueError("快照产物校验和不匹配（可能被篡改），拒绝回滚")
        kind = snap.get("snap_kind") or "logical"
        if kind == "native":
            res = vs.restore_native_snapshot(vdb, password, snap["backup_path"])
        else:
            res = vs.restore_snapshot(vdb, password, snap["backup_path"])
        db.add_log("WARNING", "clone",
                   "VDB 快照回滚：VDB %s 回到快照 %s（%s），操作人 %s，%s"
                   % (vdb_id, snapshot_id, kind, operator or "unknown",
                      res.get("message")))
        return {**res, "snapshot_id": snapshot_id, "snap_kind": kind}

    def purge_orphan_snapshots(self, dry_run: bool = False) -> dict:
        """清理**已删除 / 已过期**快照遗留的产物文件（磁盘治理）。

        此前删除快照只改 status，文件永远留在磁盘——长期跑下来会把备份盘
        吃满。本接口把「元数据已失效但文件还在」的产物清掉，元数据保留。
        """
        from core import vdb_snapshot as vs
        rows = db.query("SELECT id FROM vdb_snapshots WHERE status IN "
                        "('deleted','expired')")
        ids = [r["id"] for r in (rows or [])]
        cleaned = vs.purge_snapshot_files(ids, dry_run=dry_run)
        freed = sum(int(c.get("size_bytes") or 0) for c in cleaned
                    if c.get("removed"))
        if not dry_run and cleaned:
            db.add_log("INFO", "clone",
                       "VDB 快照产物清理：%d 个文件，已释放约 %d 字节"
                       % (len(cleaned), freed))
        return {"ok": True, "dry_run": bool(dry_run), "count": len(cleaned),
                "freed_bytes": freed, "items": cleaned}


# 便捷单例
clone_service = CloneService()
