# -*- coding: utf-8 -*-
"""PITR（Point-In-Time Recovery）恢复引擎。

职责边界：
  1. **选点**   —— 依据 ``recovery_journal`` 把「恢复到任意时间点」翻译成一条精确的恢复链；
  2. **校验**   —— 判定链完整性（缺口 / 产物缺失 / 校验和不符 / 位点断裂）并给出可读原因；
  3. **执行**   —— 驱动底层引擎真正落地恢复：

     - File : :meth:`core.engines.file.FileBackupEngine.restore` 传入 ``chain_override``，
       按 全量 → 增量(时间升序) 顺序解包 tar 归档；
     - DB   : :func:`core.restore_extras.mysql_pitr_restore` /
       :func:`core.restore_extras.pg_pitr_restore`，并把停止位点
       （binlog file:pos / WAL LSN）一并下传。

设计约束（与 T01/T02/T03 一致）：
  - 只读 ``core.models``，**不自建 sqlite3 连接**；
  - ``DEMO_MODE=on`` 或任务 ``demo_only`` 或链上存在仿真产物时，一律走仿真恢复，
    绝不触碰真实数据库/目标目录；
  - 每次恢复（含仿真、含失败）都落 ``restore_records``，供恢复记录页审计。

典型用法::

    from core import rt_backup

    pitr = rt_backup.get_pitr()
    plan = pitr.build_plan(task_id=7, target_ts="2025-07-31T10:20:00+08:00")
    if plan.complete:
        result = pitr.restore(7, plan.target_ts, target={"target_dir": "/tmp/r"})
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import core.db as db
import core.models as models

from .journal import RecoveryJournal
from .repo import LogRepository
from .types import (
    FILE_DB_TYPE,
    KIND_DB_LOG,
    KIND_FILE,
    RP_DB_FULL,
    RP_DB_LOG,
    RecoveryPoint,
    RestorePlan,
    RtConfig,
    norm_path,
)

# 恢复记录状态（与 restore_records.status 既有取值保持一致）
ST_SUCCESS = "success"
ST_FAILED = "failed"

# MySQL 系列引擎（走 binlog PITR）
_MYSQL_ENGINES = ("mysql", "mariadb")
# PostgreSQL 系列引擎（走 WAL PITR）
# 拍板 Q4：Kingbase 本期**不**并入 _PG_ENGINES —— 虽然协议兼容，
# 但 pg_pitr_restore 依赖 pg_ctl/recovery.signal 的具体路径与文件名，
# 未在真实金仓环境验证前并入会产生「看似能恢复实则失败」的假可用性。
_PG_ENGINES = ("postgresql", "postgres", "pg")
# T06 已具备日志捕获、但尚不支持自动回放的信创引擎（拍板 Q3/Q4）
_T06_CDC_ENGINES = ("oracle", "kingbase", "dameng")


class PITRRestore:
    """按时间点恢复的服务类。

    **无状态**：不持有任务上下文，所有方法显式接收 ``task_id``，
    因此可以被 :func:`core.rt_backup.get_pitr` 当作单例反复复用。
    """

    def __init__(self, logger=None) -> None:
        self.logger = logger or db.get_logger("rt.pitr")
        self.journal = RecoveryJournal(logger=self.logger)

    # ==================================================================
    # 只读查询
    # ==================================================================
    def points(self, task_id: int, start: str = None, end: str = None,
               kind: str = None, limit: int = 500, offset: int = 0,
               order: str = "desc") -> List[dict]:
        """列出某任务的恢复点（UI 明细表 / 下拉选点用）。"""
        rows = self.journal.list_points(
            int(task_id), start=start, end=end, kind=kind,
            limit=int(limit), offset=int(offset), order=order)
        return [p.to_dict() for p in rows]

    def timeline(self, task_id: int, start: str = None, end: str = None,
                 buckets: int = 200, detail_limit: int = 200) -> dict:
        """时间轴聚合数据（前端 T05 直接消费）。"""
        return self.journal.timeline(int(task_id), start=start, end=end,
                                     buckets=int(buckets),
                                     detail_limit=int(detail_limit))

    def window(self, task_id: int) -> dict:
        """可恢复窗口：``[最早恢复点, 最晚恢复点]`` 及总量统计。"""
        task_id = int(task_id)
        earliest = self.journal.list_points(task_id, limit=1, order="asc")
        latest = self.journal.latest(task_id)
        total = self.journal.count(task_id)
        return {
            "task_id": task_id,
            "earliest": earliest[0].pit_at if earliest else "",
            "latest": latest.pit_at if latest else "",
            "total": total,
            "kind": (KIND_DB_LOG
                     if self.journal.count(task_id, kind=RP_DB_LOG) > 0
                     else KIND_FILE),
        }

    # ==================================================================
    # 恢复计划
    # ==================================================================
    def build_plan(self, task_id: int, target_ts: str = "",
                   strict: bool = True) -> RestorePlan:
        """解析恢复到 ``target_ts`` 的完整计划。

        Args:
            task_id: 备份任务 ID。
            target_ts: 目标时间点（ISO8601）；留空表示「恢复到最新」。
            strict: True 时执行完整的产物存在性/校验和校验（慢但可靠）；
                False 时只做链结构判定（供 UI 高频预览）。

        Returns:
            :class:`RestorePlan`。``complete=False`` 时 ``gap_reason`` 说明原因，
            调用方可选择 ``force=True`` 强行恢复（尽力而为）。
        """
        task_id = int(task_id)
        task = models.get_task(task_id) or {}
        if not task:
            return RestorePlan(task_id=task_id, target_ts=target_ts or "",
                               complete=False,
                               gap_reason=f"任务 {task_id} 不存在")

        rt_cfg = RtConfig.from_task(task)
        target_ts = (target_ts or "").strip() or db.now_iso()

        chain = self.journal.resolve_chain(task_id, target_ts)
        kind = self._infer_kind(chain, rt_cfg)

        if strict:
            ok, reason = self.journal.validate_chain(chain)
        else:
            ok, reason = self._quick_validate(chain)

        base_point = chain[0] if (chain and chain[0].is_full) else None
        archives: List[str] = [p.object_key for p in chain if p.object_key]
        stop_file, stop_pos, stop_lsn = self._stop_position(chain)

        plan = RestorePlan(
            task_id=task_id,
            kind=kind,
            engine=(task.get("db_type") or FILE_DB_TYPE),
            target_ts=target_ts,
            base_point=base_point,
            chain=chain,
            archives=archives,
            stop_binlog_file=stop_file,
            stop_binlog_pos=stop_pos,
            stop_lsn=stop_lsn,
            complete=bool(ok),
            gap_reason=reason,
            total_bytes=sum(int(p.size_bytes or 0) for p in chain),
        )
        self.logger.info("[rt.pitr] task=%s 计划: %s complete=%s %s",
                         task_id, plan.summary(), plan.complete,
                         plan.gap_reason)
        return plan

    def preview(self, task_id: int, target_ts: str = "") -> dict:
        """UI 二次确认弹窗用的轻量预览（不做校验和计算）。"""
        plan = self.build_plan(int(task_id), target_ts, strict=False)
        data = plan.to_dict()
        # 预览只回传链的头尾与条数，避免上千条增量把响应撑爆
        chain = plan.chain
        data["chain"] = [p.to_dict() for p in (chain[:1] + chain[-5:]
                                               if len(chain) > 6 else chain)]
        data["chain_truncated"] = len(chain) > 6
        data["archives"] = [norm_path(a) for a in plan.archives[:5]]
        return data

    @staticmethod
    def _infer_kind(chain: List[RecoveryPoint], rt_cfg: RtConfig) -> str:
        """根据链内容判定恢复语义；链为空时回落到任务配置。"""
        for point in chain:
            if point.rp_kind in (RP_DB_LOG, RP_DB_FULL):
                return KIND_DB_LOG
        if chain:
            return KIND_FILE
        return rt_cfg.capture_kind

    @staticmethod
    def _quick_validate(chain: List[RecoveryPoint]) -> Tuple[bool, str]:
        """结构级快速校验：只看链头是否为全量、产物路径是否登记。"""
        if not chain:
            return False, "恢复链为空：该时间点之前没有任何可用恢复点"
        head = chain[0]
        if not head.is_full:
            return False, (f"链头缺失基准全量：最早可用点 #{head.id} 为 "
                           f"{head.rp_kind}，请选择更晚的时间点")
        missing = [p for p in chain if p.storage_tier == 1 and not p.object_key]
        if missing:
            return False, f"{len(missing)} 个恢复点未登记产物路径"
        return True, ""

    @staticmethod
    def _stop_position(chain: List[RecoveryPoint]) -> Tuple[str, int, str]:
        """从链尾推导 PITR 停止位点 ``(binlog_file, binlog_pos, wal_lsn)``。"""
        stop_file, stop_pos, stop_lsn = "", 0, ""
        for point in chain:
            if point.binlog_end_file:
                stop_file, stop_pos = point.binlog_end_file, int(point.binlog_end_pos or 0)
            elif point.binlog_file:
                stop_file, stop_pos = point.binlog_file, int(point.binlog_pos or 0)
            if point.wal_end_lsn:
                stop_lsn = point.wal_end_lsn
            elif point.wal_lsn:
                stop_lsn = point.wal_lsn
        return stop_file, stop_pos, stop_lsn

    # ==================================================================
    # 恢复执行
    # ==================================================================
    def restore(self, task_id: int, target_ts: str = "", target: dict = None,
                operator: str = "", dry_run: bool = False,
                force: bool = False) -> dict:
        """执行 PITR 恢复。

        Args:
            task_id: 备份任务 ID。
            target_ts: 目标时间点；留空=恢复到最新。
            target: 目标描述。File 用 ``{"target_dir": "/path"}``；
                DB 用 ``{"host","port","user","password","db","data_dir"}``，
                缺省时回落到任务自身连接信息（原地恢复）。
            operator: 操作人（审计用）。
            dry_run: 只出计划不落地。
            force: 链不完整时强行恢复（尽力而为）。

        Returns:
            ``{ok, message, plan, simulated, duration_sec, restore_id}``
        """
        task_id = int(task_id)
        target = dict(target or {})
        started_at = db.now_iso()
        t0 = time.time()

        task = models.get_task(task_id, include_secret=True) or {}
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在",
                    "plan": None, "simulated": False, "duration_sec": 0.0,
                    "restore_id": 0}

        plan = self.build_plan(task_id, target_ts, strict=True)

        if dry_run:
            return {"ok": plan.complete, "message": plan.gap_reason or plan.summary(),
                    "plan": plan.to_dict(), "simulated": False,
                    "dry_run": True, "duration_sec": round(time.time() - t0, 3),
                    "restore_id": 0}

        if not plan.complete and not force:
            message = f"恢复计划不完整，已中止：{plan.gap_reason}"
            restore_id = self._record(task, plan, target, started_at,
                                      ST_FAILED, message, operator)
            self.logger.warning("[rt.pitr] task=%s %s", task_id, message)
            return {"ok": False, "message": message, "plan": plan.to_dict(),
                    "simulated": False,
                    "duration_sec": round(time.time() - t0, 3),
                    "restore_id": restore_id}

        # 仿真判定：演示模式 / demo 任务 / 链上存在仿真产物
        sim_reason = self._simulate_reason(task, plan)
        try:
            if sim_reason:
                outcome = self._simulate(task, plan, target, sim_reason)
            elif plan.kind == KIND_DB_LOG:
                outcome = self._restore_db(task, plan, target)
            else:
                outcome = self._restore_file(task, plan, target)
        except Exception as exc:  # 恢复失败必须落审计，不能只抛栈
            self.logger.error("[rt.pitr] task=%s 恢复异常: %s", task_id, exc)
            outcome = {"ok": False, "message": f"恢复异常: {exc}",
                       "simulated": False}

        duration = round(time.time() - t0, 3)
        status = ST_SUCCESS if outcome.get("ok") else ST_FAILED
        message = outcome.get("message") or ""
        if force and not plan.complete:
            message = f"[强制恢复/链不完整] {message}"

        restore_id = self._record(task, plan, target, started_at, status,
                                  message, operator)
        db.add_log("info" if outcome.get("ok") else "error", "rt.pitr",
                   f"任务 {task_id} PITR 恢复至 {plan.target_ts} "
                   f"{'成功' if outcome.get('ok') else '失败'}：{message}")
        return {
            "ok": bool(outcome.get("ok")),
            "message": message,
            "plan": plan.to_dict(),
            "simulated": bool(outcome.get("simulated")),
            "duration_sec": duration,
            "restore_id": restore_id,
            "detail": outcome.get("detail") or {},
        }

    # ------------------------------------------------------------------
    # 具体恢复路径
    # ------------------------------------------------------------------
    @staticmethod
    def _simulate_reason(task: dict, plan: RestorePlan) -> str:
        """返回非空字符串表示应走仿真恢复，内容即原因。"""
        if config.DEMO_MODE == "on":
            return "DEMO_MODE=on 强制仿真"
        if task.get("demo_only"):
            return "任务标记为演示(demo_only)"
        if any(int(p.is_simulated or 0) for p in plan.chain):
            return "恢复链包含仿真产物，无法执行真实恢复"
        return ""

    def _simulate(self, task: dict, plan: RestorePlan, target: dict,
                  reason: str) -> dict:
        """仿真恢复：只校验链可读，不写目标端。"""
        readable = 0
        for point in plan.chain:
            if point.storage_tier == 1 and point.object_key \
                    and os.path.isfile(point.object_key):
                readable += 1
        self.logger.info("[rt.pitr] task=%s 仿真恢复（%s），链长 %d，可读产物 %d",
                         task.get("id"), reason, len(plan.chain), readable)
        return {
            "ok": True,
            "simulated": True,
            "message": (f"[仿真] {plan.summary()}；{reason}；"
                        f"链上 {readable}/{len(plan.chain)} 个产物可读"),
            "detail": {"readable_objects": readable,
                       "chain_length": len(plan.chain), "reason": reason},
        }

    def _restore_file(self, task: dict, plan: RestorePlan,
                      target: dict) -> dict:
        """文件 PITR：把 base-full + 时间升序的 file-inc 依次解包到目标目录。"""
        target_dir = (target.get("target_dir") or target.get("target_db")
                      or target.get("target_host") or "").strip()
        if not target_dir:
            return {"ok": False, "simulated": False,
                    "message": "文件恢复必须指定目标目录（target_dir）"}
        if not plan.archives:
            return {"ok": False, "simulated": False,
                    "message": "恢复链没有任何可用归档产物"}

        from core.engines import get_engine

        engine = get_engine(FILE_DB_TYPE, task, config.BACKUP_ROOT, self.logger)
        result = engine.restore(
            plan.archives[0],
            target_db=target_dir,
            chain_override=list(plan.archives),
        )
        return {
            "ok": bool(result.success),
            "simulated": bool(result.simulated),
            "message": (f"{result.message}（回放 {len(plan.archives)} 个归档，"
                        f"目标 {norm_path(target_dir)}）"),
            "detail": {"target_dir": norm_path(target_dir),
                       "archives": len(plan.archives)},
        }

    def _restore_db(self, task: dict, plan: RestorePlan, target: dict) -> dict:
        """数据库 PITR：全量恢复 + 日志段回放至停止位点。"""
        if not plan.base_point or not plan.base_point.object_key:
            return {"ok": False, "simulated": False,
                    "message": "恢复链缺少全量基准（db-full），无法执行数据库 PITR"}

        full_path = plan.base_point.object_key
        if not os.path.isfile(full_path):
            return {"ok": False, "simulated": False,
                    "message": f"全量产物缺失：{norm_path(full_path)}"}

        conn = self._target_conn(task, target)
        engine_key = (task.get("db_type") or "").lower()
        log_dir = self._log_dir(plan)

        import core.restore_extras as restore_extras

        if engine_key in _MYSQL_ENGINES:
            conn.update({
                "binlog_file": plan.stop_binlog_file,
                "binlog_pos": int(plan.base_point.binlog_pos or 0),
                "binlog_dir": log_dir,
            })
            raw = restore_extras.mysql_pitr_restore(full_path, plan.target_ts, conn)
        elif engine_key in _PG_ENGINES:
            conn.setdefault("data_dir", target.get("data_dir") or "")
            raw = restore_extras.pg_pitr_restore(full_path, plan.target_ts, conn)
        elif engine_key in _T06_CDC_ENGINES:
            # T06 已能持续捕获这三种引擎的逻辑变更段（.jsonl / WAL），
            # 但「段回放」依赖各自的官方工具链（拍板 Q3/Q4：本期不做）。
            # 这里显式告知用户「已捕获、暂不能自动回放」，避免误以为没备份。
            log_segments = [p for p in plan.chain if p.rp_kind == RP_DB_LOG]
            return {"ok": False, "simulated": False,
                    "message": (
                        f"引擎 {engine_key} 的日志段已持续捕获"
                        f"（本次恢复链含 {len(log_segments)} 个段），"
                        f"但自动回放暂不支持，请使用全量产物 "
                        f"{norm_path(full_path)} 配合数据库自带工具"
                        f"（Oracle RMAN / Kingbase sys_rman / 达梦 DMRMAN）"
                        f"手工前滚到 {plan.target_ts}")}
        else:
            return {"ok": False, "simulated": False,
                    "message": (f"引擎 {engine_key or '未知'} 暂不支持 PITR "
                                f"（本期仅覆盖 MySQL/MariaDB/PostgreSQL）")}

        raw = raw or {}
        log_segments = [p for p in plan.chain if p.rp_kind == RP_DB_LOG]
        suffix = (f"；日志段 {len(log_segments)} 个，停止位点 "
                  f"{plan.stop_binlog_file or plan.stop_lsn or '-'}")
        return {
            "ok": bool(raw.get("ok")),
            "simulated": False,
            "message": f"{raw.get('message') or '恢复完成'}{suffix}",
            "detail": {k: v for k, v in raw.items() if k != "message"},
        }

    @staticmethod
    def _target_conn(task: dict, target: dict) -> Dict[str, Any]:
        """组装目标端连接信息：显式 target 优先，缺省回落任务自身（原地恢复）。"""
        return {
            "host": target.get("host") or task.get("host") or "127.0.0.1",
            "port": int(target.get("port") or task.get("port") or 0) or None,
            "user": target.get("user") or task.get("username") or "root",
            "password": target.get("password") or task.get("password") or "",
            "db": target.get("db") or target.get("target_db")
            or task.get("db_name") or "",
        }

    @staticmethod
    def _log_dir(plan: RestorePlan) -> str:
        """日志段所在目录（供 mysqlbinlog 定位归档 binlog）。"""
        for point in plan.chain:
            if point.rp_kind == RP_DB_LOG and point.object_key:
                return os.path.dirname(point.object_key)
        try:
            return LogRepository(plan.task_id, capture_kind=KIND_DB_LOG).sealed_dir()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _record(self, task: dict, plan: RestorePlan, target: dict,
                started_at: str, status: str, message: str,
                operator: str) -> int:
        """把本次恢复写入 ``restore_records``。失败只告警，不影响恢复结论。"""
        target_desc = (target.get("target_dir") or target.get("db")
                       or target.get("target_db") or task.get("db_name") or "")
        record_id = None
        if plan.base_point is not None and plan.base_point.record_id:
            record_id = int(plan.base_point.record_id)
        try:
            return int(models.create_restore({
                "task_id": int(task.get("id") or 0),
                "record_id": record_id,
                "target_host": (target.get("host") or task.get("host") or ""),
                "target_db": str(target_desc),
                "started_at": started_at,
                "finished_at": db.now_iso(),
                "status": status,
                "message": f"[PITR@{plan.target_ts}] {message}"[:900],
                "operator": operator or "system",
            }))
        except Exception as exc:  # pragma: no cover —— 审计失败不阻断主流程
            self.logger.warning("[rt.pitr] 写恢复记录失败: %s", exc)
            return 0


# ======================================================================
# 模块级便捷入口
# ======================================================================
def build_plan(task_id: int, target_ts: str = "",
               strict: bool = True) -> RestorePlan:
    """便捷入口：解析恢复计划。"""
    return PITRRestore().build_plan(int(task_id), target_ts, strict=strict)


def restore(task_id: int, target_ts: str = "", target: dict = None,
            operator: str = "", dry_run: bool = False,
            force: bool = False) -> dict:
    """便捷入口：执行 PITR 恢复。"""
    return PITRRestore().restore(int(task_id), target_ts, target=target,
                                 operator=operator, dry_run=dry_run,
                                 force=force)


def list_points(task_id: int, start: str = None, end: str = None,
                kind: str = None, limit: int = 500) -> List[dict]:
    """便捷入口：列出恢复点。"""
    return PITRRestore().points(int(task_id), start=start, end=end,
                                kind=kind, limit=limit)
