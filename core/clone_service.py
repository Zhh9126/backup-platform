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
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import config
import core.models as models
import core.db as db
from core import restore_extras
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
}


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
                      target_password: str = "") -> dict:
        """提交克隆申请。

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

        req_id = models.create_clone_request({
            "source_record_id": source_record_id,
            "target_env": target_env,
            "target_host": target_host,
            "target_password": target_password or "",
            "status": STATUS_PENDING,
            "requested_by": requested_by,
            "note": note,
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
            self.logger.info("[clone] 申请 #%s 已提交，免审批直通拉起（env=%s, by=%s）",
                             req_id, target_env, requested_by)
            self._provision_async(req_id)
        else:
            self.logger.info("[clone] 申请 #%s 已提交，等待审批（env=%s, by=%s）",
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
                self.logger.info("[clone] 请求 #%s 开始拉起 VDB", request_id)
                vdb_id = self._launch_vdb(req)
                ttl = _default_ttl_days()
                expires = _compute_expires(ttl)
                models.update_clone_request(request_id, {
                    "status": STATUS_READY,
                    "vdb_instance_id": vdb_id,
                    "expires_at": expires,
                })
                self.logger.info("[clone] 请求 #%s VDB #%s 已就绪，到期 %s",
                                 request_id, vdb_id, expires)
            except Exception as exc:
                self.logger.error("[clone] 请求 #%s 拉起失败: %s", request_id, exc)
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

        # 2) 拉起 VDB 实例
        req = models.get_clone_request(request_id, include_secret=True)
        req["status"] = STATUS_CREATING
        models.update_clone_request(request_id, {"status": STATUS_CREATING,
                                                  "approved_by": approved_by})
        self.logger.info("[clone] 请求 #%s 审批通过，开始拉起 VDB", request_id)
        vdb_id = self._launch_vdb(req)
        ttl = _default_ttl_days()
        expires = _compute_expires(ttl)
        models.update_clone_request(request_id, {
            "status": STATUS_READY,
            "vdb_instance_id": vdb_id,
            "expires_at": expires,
        })
        self.logger.info("[clone] 请求 #%s VDB #%s 已就绪，到期 %s",
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
        self.logger.info("[clone] 请求 #%s 已驳回（by=%s）", request_id, by)
        return self.get_clone(request_id)

    # ------------------------- 销毁 -------------------------
    def destroy_clone(self, request_id: int) -> dict:
        """手动销毁：释放 VDB 实例并置 deleted。"""
        req = self.get_clone(request_id)
        if not req:
            raise ValueError(f"克隆请求不存在: {request_id}")
        self._release_vdb(req)
        models.update_clone_request(request_id, {"status": STATUS_DELETED})
        self.logger.info("[clone] 请求 #%s 已手动销毁", request_id)
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
        self.logger.info("[clone] 请求 #%s 已到期自动销毁", request_id)
        return self.get_clone(request_id)

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
                self.logger.warning("[clone] 到期销毁 #%s 失败: %s", r["id"], e)
        if result:
            self.logger.info("[clone] 本次自动销毁 %d 个到期克隆", len(result))
        return result

    # ------------------------- 查询 -------------------------
    def list_clones(self) -> list:
        return models.list_clone_requests()

    def get_clone(self, request_id: int) -> Optional[dict]:
        return models.get_clone_request(request_id)

    # ------------------------- 内部：拉起 / 释放 VDB -------------------------
    def _launch_vdb(self, req: dict) -> int:
        """按源库类型调用底层真实克隆引擎，并登记 VDB 实例。

        真实引擎：mysql / mariadb（目标主机实例建库 + 流式导入）
                 postgresql（目标主机实例建库 + 导入）
        目标主机：req.target_host（默认本机 127.0.0.1，可指定其他机器 IP）；
        连接密码：req.target_password 优先，未填时沿用源任务密码。
        其余类型明确抛错（不降级仿真，避免"假克隆"）；
        仅 DEMO_MODE != off 时才允许仿真兜底且在 VDB note 中明确标注。
        """
        rec = models.get_record(req["source_record_id"]) or {}
        db_type = rec.get("db_type") or "mysql"
        instance_name = f"clone_{req['id']}"
        backup_path = rec.get("backup_path") or ""
        if not backup_path or not os.path.isfile(backup_path):
            raise RuntimeError(f"备份产物不存在: {backup_path or '-'}")

        target_host = (req.get("target_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = 3306 if db_type in ("mysql", "mariadb") else (
            5432 if db_type == "postgresql" else 0)
        user = "root" if db_type in ("mysql", "mariadb") else (
            "postgres" if db_type == "postgresql" else "")
        # 连接密码：目标实例密码优先，未填则沿用源任务密码
        password = req.get("target_password") or ""
        if not password:
            src_task = models.get_task(rec.get("task_id"), include_secret=True) or {}
            password = src_task.get("password") or ""

        simulated = config.DEMO_MODE != "off"
        if not simulated:
            engine_name = _CLONE_ENGINE.get(db_type)
            if not engine_name:
                raise RuntimeError(
                    f"db_type={db_type} 暂不支持真实克隆"
                    "（当前支持 mysql/mariadb/postgresql；其余类型请先确认克隆目标方案）")
            if engine_name == "pg_clone_to_test":
                res = restore_extras.pg_clone_to_test(
                    backup_path, instance_name,
                    pg_host=target_host, pg_port=port or None,
                    pg_user=user, pg_password=password)
            else:
                res = restore_extras.mysql_clone_to_test(
                    backup_path, instance_name,
                    mysql_host=target_host, mysql_port=port or 3306,
                    mysql_user=user, mysql_password=password)
            if not res.get("ok"):
                raise RuntimeError(f"真实克隆失败: {res.get('message')}")

        vdb_id = models.create_vdb({
            "name": instance_name,
            "source_record_id": req["source_record_id"],
            "task_id": rec.get("task_id"),
            "db_type": db_type,
            "port": port,
            "host": target_host,
            "database_name": instance_name,
            "username": user,
            "status": "ready",
            "created_at": db.now_iso(),
            "expires_at": _compute_expires(_default_ttl_days()),
            "note": ("DEMO 仿真实例" if simulated else "真实克隆实例")
                    + (f" | 目标主机 {target_host}" if target_host != "127.0.0.1" else "")
                    + (f" | {req.get('note') or ''}"),
        })
        return vdb_id

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
        # 连接密码：与拉起逻辑一致——目标实例密码优先，未填则沿用源任务密码
        password = req.get("target_password") or ""
        if not password:
            src_task = models.get_task(vdb.get("task_id"), include_secret=True) or {}
            password = src_task.get("password") or ""
        res = restore_extras.verify_clone_conn(
            vdb.get("db_type"), vdb.get("host") or "127.0.0.1",
            vdb.get("port"), vdb.get("database_name"), vdb.get("username"),
            password=password)
        self.logger.info("[clone] 请求 #%s 校验: %s", request_id, res.get("message"))
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
                password = creq.get("target_password") or ""
                if not password:
                    src_task = models.get_task(vdb.get("task_id"), include_secret=True) or {}
                    password = src_task.get("password") or ""
                try:
                    res = restore_extras.drop_clone(
                        vdb.get("db_type"), vdb.get("name"),
                        host=vdb.get("host") or "127.0.0.1",
                        port=vdb.get("port"),
                        mysql_password=password, pg_password=password)
                    if not res.get("ok"):
                        self.logger.warning("[clone] 释放 VDB #%s 失败: %s",
                                            vdb_id, res.get("message"))
                except Exception as e:
                    self.logger.warning("[clone] 释放 VDB #%s 失败（忽略）: %s", vdb_id, e)
            try:
                models.delete_vdb(vdb_id)
            except Exception as e:
                self.logger.warning("[clone] 删除 VDB 元数据 #%s 失败（忽略）: %s", vdb_id, e)


# 便捷单例
clone_service = CloneService()
