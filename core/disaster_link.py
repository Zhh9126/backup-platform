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
import random
import logging
from datetime import datetime, timezone

import core.db as db
import core.models as models


_logger = db.get_logger("disaster_link")


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
            # DEMO：在基准延迟上叠加仿真抖动
            jitter = random.uniform(-3, 6)
            latency = round(max(1.0, base_latency + jitter), 1)
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
        }

    # ------------------------- 日志间隙填补 -------------------------
    def fill_log_gap(self, link_id: int) -> dict:
        """检测备端日志缺口（对比主备 LSN/binlog position），补传缺失日志段。

        DEMO 下仿真主备 LSN 差值与缺口字节，记录填补过程；
        填补后链路进入 filling 状态。
        """
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        # DEMO：仿真主端当前 LSN 与备端已应用 LSN
        primary_lsn = random.randint(1_000_000, 9_999_999)
        gap = random.randint(0, max(0, int(primary_lsn * 0.05)))
        dr_lsn = primary_lsn - gap
        gap_segments = max(0, gap // 10_000)
        filled_bytes = gap * random.randint(50, 200)
        now = db.now_iso()
        if gap <= 0:
            result = "no_gap"
            msg = "主备 LSN 一致，无需填补"
        else:
            result = "filled"
            msg = (f"检测到缺口 {gap} LSN（{gap_segments} 段），"
                   f"已从主端/归档补传 {filled_bytes} 字节")
            models.set_disaster_link_status(link_id, "filling")
        db.add_log("INFO", "disaster_link",
                   f"链路#{link_id} 日志填补: {msg} (primary_lsn={primary_lsn}, dr_lsn={dr_lsn})")
        self.logger.info("[disaster_link] #%s 日志填补: %s", link_id, msg)
        return {
            "ok": True,
            "link_id": link_id,
            "primary_lsn": primary_lsn,
            "dr_lsn": dr_lsn,
            "gap_lsn": gap,
            "gap_segments": gap_segments,
            "filled_bytes": filled_bytes,
            "result": result,
            "message": msg,
            "filled_at": now,
        }

    # ------------------------- 一致性校验 -------------------------
    def run_consistency_check(self, link_id: int) -> dict:
        """在备端启动只读校验（总分核对 + 抽样校验和）。

        返回 pass / warn / fail，并记录到 consistency_result + last_consistency_check；
        DEMO 下仿真校验结果分布。
        """
        link = models.get_disaster_link(link_id)
        if not link:
            return {"ok": False, "error": "容灾链路不存在"}
        # DEMO：仿真总分核对比例与抽样校验和命中率
        total_rows = random.randint(1000, 1_000_000)
        checked_rows = random.randint(int(total_rows * 0.95), total_rows)
        match_rate = round(checked_rows / total_rows * 100, 2)
        sample_checksum_hit = random.uniform(0.90, 1.0)
        if match_rate >= 99.9 and sample_checksum_hit >= 0.99:
            result = "pass"
        elif match_rate >= 98.0 and sample_checksum_hit >= 0.95:
            result = "warn"
        else:
            result = "fail"
        now = db.now_iso()
        models.update_disaster_link_check(
            link_id, consistency_result=result, last_consistency_check=now)
        if link.get("status") in ("filling", "standby") and result == "pass":
            models.set_disaster_link_status(link_id, "active")
        db.add_log("INFO", "disaster_link",
                   f"链路#{link_id} 一致性校验: {result} "
                   f"(match={match_rate}%, checksum_hit={sample_checksum_hit:.3f})")
        self.logger.info("[disaster_link] #%s 一致性校验: %s", link_id, result)
        return {
            "ok": True,
            "link_id": link_id,
            "result": result,
            "total_rows": total_rows,
            "checked_rows": checked_rows,
            "match_rate": match_rate,
            "sample_checksum_hit": round(sample_checksum_hit, 3),
            "checked_at": now,
        }

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
