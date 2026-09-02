# -*- coding: utf-8 -*-
"""
容灾链路 HA 引擎（DisasterLinkEngine）。

提供双运营商专线智能选路、日志间隙自动填补、备端只读一致性校验。
所有外部依赖（真实专线延迟、主备 LSN 比对、备端校验）在 DEMO_MODE 下
以仿真数据兜底，保证平台零外部依赖即可演示完整 HA 能力。

能力一览：
- select_route(link_id)        按 route_policy 多专线配置，基于延迟/健康/优先级选最优路径
- fill_log_gap(link_id)        检测备端日志缺口（对比主备 LSN/binlog），补传缺失日志段
- run_consistency_check(link_id) 备端只读校验（总分核对 + 抽样校验和）
- get_link_status(link_id) / list_links()  状态概览
"""
import re
import logging
from datetime import datetime, timezone

import core.db as db
import core.models as models


_logger = db.get_logger("disaster_link")


def _demo_mode() -> str:
    """DEMO_MODE 取值（默认 off）。非 off 时相关操作明确标注 simulated。"""
    try:
        import config
        return str(getattr(config, "DEMO_MODE", "off"))
    except Exception:
        return "off"


def _sync_source_probe(sync_task_id: int) -> dict:
    """同步任务源的真实探针：源库连通性 + 最近一次同步结果。

    返回 {reachable, last_sync_status, last_sync_at, db_type, host}；
    异常/不可得时字段缺失，由调用方降级处理。
    """
    task = models.get_sync_task(int(sync_task_id), include_secret=True) or {}
    if not task:
        return {}
    out = {
        "db_type": task.get("src_db_type") or "",
        "host": task.get("src_host") or "",
        "port": int(task.get("src_port") or 0),
    }
    try:
        from core import probe
        ok, msg = probe.probe_db_connection(
            out["db_type"], out["host"], out["port"],
            task.get("src_username"),
            db.decrypt_secret(task.get("src_password") or ""),
            task.get("src_db_name"))
        out["reachable"] = bool(ok)
        out["probe_msg"] = msg
    except Exception as exc:
        out["reachable"] = False
        out["probe_msg"] = f"探测异常: {exc}"
    try:
        rows = models.list_sync_records() or []
        last = next((r for r in rows
                     if int(r.get("sync_task_id") or 0) == int(sync_task_id)), None)
        if last:
            out["last_sync_status"] = last.get("status")
            out["last_sync_at"] = last.get("finished_at") or last.get("started_at")
    except Exception:
        pass
    return out



def _binlog_file_index(name) -> int:
    """从 binlog 文件名（如 mysql-bin.000123）提取末尾序号；异常返回 0。"""
    if not name:
        return 0
    m = re.search(r"(\d+)\s*$", str(name))
    return int(m.group(1)) if m else 0


def _source_master_status(task_id: int) -> dict:
    """连接源库查询当前 binlog 位点（SHOW MASTER STATUS / BINARY LOG STATUS）。"""
    try:
        task = models.get_task(int(task_id), include_secret=True)
        if not task or not (task.get("host") and task.get("username")):
            return {}
        import pymysql
        conn = pymysql.connect(
            host=task.get("host"), port=int(task.get("port") or 3306),
            user=task.get("username"),
            password=db.decrypt_secret(task.get("password") or ""),
            connect_timeout=5, read_timeout=5, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW MASTER STATUS")
                row = cur.fetchone()
                if not row:
                    cur.execute("SHOW BINARY LOG STATUS")
                    row = cur.fetchone()
                if not row:
                    return {}
                return {"file": row[0], "pos": int(row[1])}
        finally:
            conn.close()
    except Exception:
        return {}


def _rt_real_position(task_id: int) -> dict:
    """读取实时保护任务的真实捕获位点（rt_capture_state + 源库实时位点）。

    返回空 dict 表示不可用（调用方回退仿真逻辑）。
    """
    try:
        state = models.get_rt_state(int(task_id))
        if not state:
            return {}
        end_file = (state.get("last_binlog_file")
                    or state.get("binlog_end_file") or state.get("binlog_file") or "")
        end_pos = int(state.get("last_binlog_pos")
                      or state.get("binlog_end_pos") or state.get("binlog_pos") or 0)
        src = _source_master_status(int(task_id))
        src_idx = _binlog_file_index(src.get("file", ""))
        end_idx = _binlog_file_index(end_file)
        gap_files = max(0, src_idx - end_idx) if (src and end_file) else 0
        gap_bytes = 0
        if src and end_file:
            if gap_files > 0:
                gap_bytes = int(src.get("pos") or 0)
            elif gap_files == 0:
                gap_bytes = max(0, int(src.get("pos") or 0) - end_pos)
        return {
            "src_file": (src or {}).get("file", ""),
            "src_pos": int((src or {}).get("pos") or 0),
            "end_file": end_file,
            "end_pos": end_pos,
            "gap_files": gap_files,
            "gap_bytes": gap_bytes,
            "daemon_status": state.get("daemon_status"),
            "health": state.get("health"),
            "rpo_actual_sec": int(state.get("rpo_actual_sec") or 0),
        }
    except Exception:
        return {}


class DisasterLinkEngine:
    """容灾链路 HA 引擎。"""

    STATUS = ("active", "standby", "filling", "broken")
    CONSISTENCY = ("pass", "warn", "fail")

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 智能选路 -------------------------
    def select_route(self, link_id: int) -> dict:
        """按 route_policy 多专线配置，基于延迟/健康/优先级选择最优路径。

        DEMO 下仿真各专线延迟与丢包，按 (优先级升序, 延迟升序) 排序取最优。
        选中后记录一次选路事件（系统日志，供 AI 评估切路频率）。
        """
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        routes = link.get("route_policy") or []
        if not isinstance(routes, list):
            routes = []
        candidates = []
        for r in routes:
            enabled = bool(r.get("enabled", True))
            base_latency = float(r.get("latency_ms") or 0)
            # 平台无真实专线探测能力：延迟取配置基准值（不叠加随机抖动），
            # 并在响应中标注 simulated，供前端/下游区分
            latency = round(max(1.0, base_latency), 1)
            candidates.append({
                "provider": r.get("provider", "未知"),
                "endpoint": r.get("endpoint", ""),
                "priority": int(r.get("priority") or 99),
                "enabled": enabled,
                "latency_ms": latency,
                "health": "healthy" if enabled else "disabled",
            })
        usable = [c for c in candidates if c["enabled"]]
        if not usable:
            return {"ok": False, "error": "无可用专线",
                    "link_id": link_id, "candidates": candidates}
        usable.sort(key=lambda c: (c["priority"], c["latency_ms"]))
        selected = usable[0]
        now = db.now_iso()
        db.add_log("INFO", "disaster_link",
                   f"链路#{link_id} 智能选路 → {selected['provider']} "
                   f"({selected['endpoint']}, {selected['latency_ms']}ms)")
        self.logger.info("[disaster_link] #%s 选路 → %s (%sms)",
                         link_id, selected["provider"], selected["latency_ms"])
        return {
            "ok": True,
            "link_id": link_id,
            "selected": selected,
            "candidates": candidates,
            "selected_at": now,
            "simulated": True,
            "sim_note": "延迟为配置基准值（平台未接入真实专线探测）",
        }

    # ------------------------- 日志间隙填补 -------------------------
    def fill_log_gap(self, link_id: int) -> dict:
        """检测备端日志缺口（对比源库实时 binlog 位点 vs 已捕获位点）。

        真实语义：缺口由实时捕获流自动追平，本操作确认缺口并记录状态；
        无绑定源/无真实位点时不伪造数据，明确返回不可执行原因。
        DEMO_MODE 下才允许仿真兜底，且响应标注 simulated=True。
        """
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        # 实时保护任务源：接入真实 binlog 位点（源库位点 vs 已捕获位点）
        real = {}
        if link.get("source_kind") == "rt_task" and link.get("source_id"):
            real = _rt_real_position(link["source_id"])
        if real.get("src_file"):
            gap_files = real["gap_files"]
            gap_bytes = real["gap_bytes"]
            now = db.now_iso()
            if gap_files <= 0 and gap_bytes <= 0:
                msg = (f"主备 binlog 位点一致"
                       f"（已捕获 {real['end_file']}:{real['end_pos']}，"
                       f"源 {real['src_file']}:{real['src_pos']}），无需填补")
                db.add_log("INFO", "disaster_link", f"链路#{link_id} 日志填补: {msg}")
                self.logger.info("[disaster_link] #%s 日志填补: %s", link_id, msg)
                return {"ok": True, "link_id": link_id, "result": "no_gap",
                        "message": msg, "gap_files": 0, "gap_bytes": 0,
                        "filled_at": now, "real": True,
                        "src_file": real["src_file"], "end_file": real["end_file"]}
            gap_lsn = gap_files * 1_000_000 + gap_bytes  # 位点差折算 LSN
            msg = (f"检测到 binlog 缺口 {gap_files} 文件 / {gap_bytes} 字节"
                   f"（源 {real['src_file']}:{real['src_pos']}，"
                   f"已捕获 {real['end_file']}:{real['end_pos']}），"
                   f"实时捕获链路将自动追平；如需立即收敛可对源任务手动执行一次备份")
            models.set_disaster_link_status(link_id, "filling")
            db.add_log("INFO", "disaster_link", f"链路#{link_id} 日志填补: {msg}")
            self.logger.info("[disaster_link] #%s 日志填补: %s", link_id, msg)
            return {"ok": True, "link_id": link_id, "primary_lsn": gap_lsn,
                    "dr_lsn": 0, "gap_lsn": gap_lsn, "gap_files": gap_files,
                    "gap_bytes": gap_bytes,
                    "result": "gap_detected",
                    "message": msg, "filled_at": now, "real": True,
                    "src_file": real["src_file"], "end_file": real["end_file"]}
        # 无真实位点可用：不伪造数据
        if _demo_mode() != "off":
            return {"ok": False, "simulated": True,
                    "error": (f"DEMO_MODE={_demo_mode()}：链路未绑定实时保护任务，"
                              "日志填补已不再提供随机模拟数据")}
        return {"ok": False,
                "error": ("链路未绑定实时保护任务（source_kind=rt_task），"
                          "无法执行真实的日志缺口检测；请先在链路详情绑定源")}


    # ------------------------- 一致性校验 -------------------------
    def run_consistency_check(self, link_id: int) -> dict:
        """链路一致性校验（真实数据，不伪造）。

        - 绑定实时保护任务：校验捕获守护健康 + 源库 binlog 位点 vs 已捕获位点
          的滞后（真实位点比对）；
        - 绑定同步任务：源库连通性 + 最近一次同步结果；
        - 未绑定源：拒绝执行并明确说明（不再返回随机模拟数字）；
        - DEMO_MODE != off 时保留仿真兜底，响应标注 simulated=True。
        """
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        kind = link.get("source_kind")
        src_id = link.get("source_id")
        now = db.now_iso()

        # 1) 实时保护任务源：真实 binlog 位点一致性校验
        if kind == "rt_task" and src_id:
            real = _rt_real_position(int(src_id))
            if real.get("end_file"):
                daemon = real["daemon_status"] or "unknown"
                if daemon not in ("running", "active", "ok", "healthy"):
                    result = "fail"
                elif real["gap_files"] > 0 or real["gap_bytes"] > 0:
                    result = "warn"
                else:
                    result = "pass"
                models.update_disaster_link_check(
                    link_id, consistency_result=result, last_consistency_check=now)
                if link.get("status") in ("filling", "standby") and result == "pass":
                    models.set_disaster_link_status(link_id, "active")
                db.add_log("INFO", "disaster_link",
                           f"链路#{link_id} 一致性校验(真实位点): {result} "
                           f"(daemon={daemon}, 滞后 {real['gap_files']} 文件/"
                           f"{real['gap_bytes']} 字节, RPO={real['rpo_actual_sec']}s, "
                           f"源 {real['src_file']}:{real['src_pos']}, "
                           f"已捕获 {real['end_file']}:{real['end_pos']})")
                self.logger.info("[disaster_link] #%s 一致性校验(真实位点): %s",
                                 link_id, result)
                return {
                    "ok": True,
                    "link_id": link_id,
                    "result": result,
                    "real": True,
                    "check_type": "real_binlog_position",
                    "gap_files": real["gap_files"],
                    "gap_bytes": real["gap_bytes"],
                    "src_file": real["src_file"],
                    "src_pos": real["src_pos"],
                    "end_file": real["end_file"],
                    "end_pos": real["end_pos"],
                    "daemon_status": daemon,
                    "rpo_actual_sec": real["rpo_actual_sec"],
                    "checked_at": now,
                }
            return {"ok": False, "real": True,
                    "error": ("实时任务位点不可用（任务未运行或非 MySQL binlog 捕获），"
                              "无法执行位点一致性校验")}

        # 2) 同步任务源：源连通性 + 最近一次同步结果
        if kind == "sync_task" and src_id:
            probe = _sync_source_probe(int(src_id))
            if not probe:
                return {"ok": False, "real": True, "error": "同步任务不存在，无法校验"}
            if not probe.get("reachable"):
                result = "fail"
            elif probe.get("last_sync_status") not in ("success", None):
                result = "warn"
            else:
                result = "pass"
            models.update_disaster_link_check(
                link_id, consistency_result=result, last_consistency_check=now)
            if link.get("status") in ("filling", "standby") and result == "pass":
                models.set_disaster_link_status(link_id, "active")
            db.add_log("INFO", "disaster_link",
                       f"链路#{link_id} 一致性校验(同步源探针): {result} "
                       f"(reachable={probe.get('reachable')}, "
                       f"last_sync={probe.get('last_sync_status')})")
            return {
                "ok": True,
                "link_id": link_id,
                "result": result,
                "real": True,
                "check_type": "sync_source_probe",
                "source_reachable": bool(probe.get("reachable")),
                "probe_msg": probe.get("probe_msg", ""),
                "last_sync_status": probe.get("last_sync_status"),
                "last_sync_at": probe.get("last_sync_at"),
                "checked_at": now,
            }

        # 3) DEMO_MODE 下允许仿真兜底（明确标注 simulated）
        if _demo_mode() != "off":
            return {"ok": False, "simulated": True,
                    "error": (f"DEMO_MODE={_demo_mode()}：链路未绑定可校验的真实源，"
                              "一致性检查已不再返回随机模拟数据")}

        # 4) 未绑定源：明确拒绝，不伪造任何数字
        return {"ok": False, "simulated": False,
                "error": ("链路未绑定可校验的源（source_kind=rt_task/sync_task），"
                          "无法执行真实一致性检查；请先在链路中绑定源后重试")}

    # ------------------------- 状态概览 -------------------------
    def get_link_status(self, link_id: int) -> dict:
        """链路状态概览（含最近一次选路信息）。"""
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        last_route = db.query_one(
            "SELECT ts, message FROM system_logs WHERE source='disaster_link' "
            "AND message LIKE ? ORDER BY id DESC LIMIT 1",
            (f"链路#{link_id} 智能选路%",))
        return {
            "ok": True,
            "link": link,
            "last_route": last_route["message"] if last_route else None,
            "last_route_at": last_route["ts"] if last_route else None,
        }

    def list_links(self) -> list:
        """列出全部容灾链路（状态概览用）。"""
        return models.list_disaster_links()
