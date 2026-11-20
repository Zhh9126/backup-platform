# -*- coding: utf-8 -*-
"""容灾统一事件时间线（2026-11-20）。

把容灾域的四类事件聚合成**一条**时间线，供「容灾总览」与故障排查使用：

- ``switch``     切换 / 回切作业（``dr_switch_runs``）
- ``drill``      切换演练 / 真实恢复演练（``drills``）
- ``inspection`` 容灾巡检（``dr_inspections``，含展开的具体失败检查项）
- ``alert``      主库连续探测失败告警（自动监控状态 ``dr_monitor``）
- ``gap``        保护目标缺口（``core/goals.py`` 当前评估快照，非历史事件）

为什么需要它：此前这四类事件分散在不同页面（切换历史在「一键切换」、
演练在「切换演练」、巡检在「容灾巡检」、告警在链路卡片徽章），
真出故障时值班人员要来回切页才能拼出「最近到底发生了什么」。

诚实边界：
- 只聚合**平台自身已落库**的事实，不做推断补全；某类事件查不到就少一类，
  绝不用「看起来合理」的默认值填充。
- 时间过滤在 Python 侧做（库里混有 ISO 与 DATETIME 两种格式，
  在 SQL 里比较会漏数据），代价是各源取最近 N 条后在内存过滤——
  这四类事件都是低频，人工操作产生，取 200 条足够。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import core.db as db
import core.models as models

KINDS = ("switch", "drill", "inspection", "alert", "gap")

KIND_LABELS = {
    "switch": "切换",
    "drill": "演练",
    "inspection": "巡检",
    "alert": "告警",
    "gap": "目标缺口",
}

# 保护目标缺口（goals.protection_gaps 的 code）→ 事件标题
_GAP_TITLES = {
    "rpo_no_cdp": "RPO 目标未达成（未开启实时保护）",
    "interval_gt_rpo": "备份间隔超出 RPO 目标",
    "cross_site_no_offsite": "异地容灾目标落空（未配异地副本）",
}

# 演练评分及格线（与 compliance 口径保持一致）
DRILL_PASS_SCORE = 60
DEFAULT_SLO_RTO_SEC = 300.0

# 各状态 → 严重级别（前端据此着色）
_SEVERITY = {
    "success": "info", "completed": "info", "ok": "info", "passed": "info",
    "failed": "error", "fail": "error", "failure": "error", "error": "error",
    "warn": "warn", "blocked": "warn", "cancelled": "warn", "running": "warn",
    "pending": "warn", "skipped": "warn",
}

_SWITCH_SCOPE = {"full": "业务+DNS", "business": "仅业务", "dns_only": "仅DNS"}
_DRILL_TYPE = {
    "full_recovery": "全量恢复演练", "real_restore": "真实恢复演练",
    "switch": "切换演练", "switch_drill": "切换演练",
}


def _norm_ts(raw) -> str:
    """时间规范化：统一成 ``YYYY-MM-DDTHH:MM:SS``，用于跨源排序。

    库内混有 ISO（带 ``T`` 与时区偏移）与 DATETIME（空格分隔）两种格式，
    直接按字符串排序会错乱，这里统一成可比较的前缀。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    s = s.split("+")[0].split(".")[0]
    return s


def _disp_ts(raw) -> str:
    """展示用时间：``2026-11-20 10:00:00``。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    return s.replace("T", " ", 1).split("+")[0][:19]


def _sev(status: str) -> str:
    return _SEVERITY.get(str(status or "").lower(), "info")


def _since(days: int) -> str:
    days = max(1, min(int(days or 14), 365))
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _in_window(ts: str, since: str) -> bool:
    return bool(ts) and ts >= since


def _sort_key(e: dict) -> tuple:
    """时间线排序键。

    事件的 ``id`` 类型并不统一：切换/演练/告警是整数，而「巡检失败项展开」
    与「保护目标缺口」是 ``"<父ID>-<子键>"`` 形式的字符串。直接放进同一个
    元组排序会抛 ``'<' not supported between instances of 'int' and 'str'``，
    因此这里拆成「时间 + 数值部分 + 字符串」三段，类型恒定可比。
    """
    ident = e.get("id")
    if isinstance(ident, bool):
        num = 0.0
    elif isinstance(ident, (int, float)):
        num = float(ident)
    else:
        num = 0.0
    return (e.get("ts") or "", num, str(ident or ""))


# ------------------------------------------------------------------ 各源

def _slo_map() -> dict:
    """各链路的 RTO SLO 基准（秒）。

    取自链路 ``failover_config.slo_rto_sec``；未配置时用默认 300 秒
    （与 ``dr_orchestrator.stats`` 的默认口径保持一致）。
    """
    out = {}
    try:
        for lk in models.list_disaster_links() or []:
            lid = int(lk.get("id") or 0)
            if not lid:
                continue
            val = None
            raw = lk.get("failover_config") or ""
            if str(raw).strip():
                try:
                    val = (json.loads(raw) or {}).get("slo_rto_sec")
                except Exception:
                    val = None
            try:
                out[lid] = float(val) if val else DEFAULT_SLO_RTO_SEC
            except (TypeError, ValueError):
                out[lid] = DEFAULT_SLO_RTO_SEC
    except Exception:
        pass
    return out


def _collect_switch(since: str, limit: int) -> list:
    out = []
    try:
        from core import dr_orchestrator
        dr_orchestrator.ensure_schema()
        rows = dr_orchestrator.list_runs(limit=limit)
    except Exception:
        rows = []
    slos = _slo_map()
    for r in rows or []:
        at = r.get("created_at") or r.get("started_at") or ""
        ts = _norm_ts(at)
        if not _in_window(ts, since):
            continue
        direction = r.get("direction") or "failover"
        scope = _SWITCH_SCOPE.get(r.get("scope") or "full", "业务+DNS")
        title = ("回切" if direction == "failback" else "切换") + " · " + scope
        status = r.get("status") or ""
        rto = float(r.get("rto_sec") or 0)
        link_id = r.get("link_id")
        slo = slos.get(int(link_id or 0), DEFAULT_SLO_RTO_SEC)
        # SLO 判定：只有**成功**且 RTO>0 才谈达标；失败作业的 RTO 无意义
        slo_breached = bool(status == "success" and rto > 0 and rto > slo)
        out.append({
            "kind": "switch",
            "kind_label": KIND_LABELS["switch"],
            "id": r.get("id"),
            "at": _disp_ts(at),
            "ts": ts,
            "title": title,
            "status": status,
            "severity": "error" if slo_breached else _sev(status),
            "link_id": link_id,
            "link_name": r.get("link_name") or "",
            "summary": r.get("message") or "",
            "rto_sec": rto,
            "detail": {"direction": direction, "scope": r.get("scope") or "full",
                       "mode": r.get("mode") or "execute",
                       "operator": r.get("operator") or "",
                       "slo_rto_sec": slo,
                       "slo_breached": slo_breached},
        })
    return out


def _collect_drill(since: str, limit: int) -> list:
    out = []
    try:
        rows = models.list_drills() or []
    except Exception:
        rows = []
    for r in rows[:limit]:
        at = r.get("started_at") or r.get("created_at") or ""
        ts = _norm_ts(at)
        if not _in_window(ts, since):
            continue
        status = r.get("status") or ""
        dtype = _DRILL_TYPE.get(r.get("drill_type") or "", r.get("drill_type") or "演练")
        score = r.get("score")
        passed = None
        if score is not None:
            try:
                passed = int(score) >= DRILL_PASS_SCORE
            except (TypeError, ValueError):
                passed = None
        bits = []
        if score is not None:
            bits.append("评分 %s" % score)
            if passed is False:
                bits.append("未达 %d 分及格线" % DRILL_PASS_SCORE)
        if r.get("rto_actual_sec"):
            bits.append("RTO %.1fs" % float(r.get("rto_actual_sec") or 0))
        if r.get("rpo_actual_sec"):
            bits.append("RPO %.1fs" % float(r.get("rpo_actual_sec") or 0))
        out.append({
            "kind": "drill",
            "kind_label": KIND_LABELS["drill"],
            "id": r.get("id"),
            "at": _disp_ts(at),
            "ts": ts,
            "title": "%s · %s" % (r.get("name") or "演练", dtype),
            "status": status,
            "severity": "error" if passed is False else _sev(status),
            "link_id": r.get("task_id"),
            "link_name": "",
            "summary": " · ".join(bits) or (r.get("notes") or "")[:120],
            "rto_sec": float(r.get("rto_actual_sec") or 0),
            "detail": {"drill_type": r.get("drill_type") or "",
                       "score": score,
                       "passed": passed,
                       "pass_score": DRILL_PASS_SCORE,
                       "issues_found": (r.get("issues_found") or "")[:300]},
        })
    return out


def _collect_inspection(since: str, limit: int) -> list:
    """容灾巡检事件。

    除「本轮巡检汇总」外，把**具体失败/警告的检查项**也展开成独立事件——
    否则用户只看到"巡检失败"，还得自己去翻详情才知道是哪条链路、哪一项挂了。
    """
    out = []
    try:
        from core import dr_inspection
        rows = dr_inspection.list_inspections(limit=limit)
    except Exception:
        rows = []
    for r in rows or []:
        at = r.get("created_at") or ""
        ts = _norm_ts(at)
        if not _in_window(ts, since):
            continue
        overall = r.get("overall") or ""
        summary = ("链路 %s 条：正常 %s / 警告 %s / 失败 %s"
                   % (r.get("links_total") or 0, r.get("ok_count") or 0,
                      r.get("warn_count") or 0, r.get("fail_count") or 0))
        out.append({
            "kind": "inspection",
            "kind_label": KIND_LABELS["inspection"],
            "id": r.get("id"),
            "at": _disp_ts(at),
            "ts": ts,
            "title": "容灾巡检",
            "status": overall,
            "severity": _sev(overall),
            "link_id": None,
            "link_name": "",
            "summary": summary,
            "rto_sec": 0.0,
            "detail": {"triggered_by": r.get("triggered_by") or "",
                       "ok_count": r.get("ok_count") or 0,
                       "warn_count": r.get("warn_count") or 0,
                       "fail_count": r.get("fail_count") or 0},
        })
        # 展开具体失败/警告项（需要 detail，列表接口不含）
        detail = None
        try:
            detail = dr_inspection.get_inspection(int(r.get("id") or 0))
        except Exception:
            detail = None
        items = (detail or {}).get("detail") or []
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        for lk in items or []:
            if not isinstance(lk, dict):
                continue
            checks = lk.get("checks") or []
            bad = [c for c in checks
                   if isinstance(c, dict) and str(c.get("status") or "") in ("fail", "warn")]
            for c in bad:
                cstatus = str(c.get("status") or "")
                out.append({
                    "kind": "inspection",
                    "kind_label": KIND_LABELS["inspection"],
                    "id": "%s-%s" % (r.get("id"), lk.get("id")),
                    "at": _disp_ts(at),
                    "ts": ts,
                    "title": "巡检项 %s · %s" % (lk.get("name") or "-",
                                              c.get("item") or "-"),
                    "status": cstatus,
                    "severity": "error" if cstatus == "fail" else "warn",
                    "link_id": lk.get("id"),
                    "link_name": lk.get("name") or "",
                    "summary": str(c.get("message") or "")[:200],
                    "rto_sec": 0.0,
                    "detail": {"inspection_id": r.get("id"),
                               "item": c.get("item") or "",
                               "status": cstatus,
                               "active_site": lk.get("active_site") or "",
                               "error": str(lk.get("error") or "")[:200]},
                })
    return out


def _collect_alert(since: str, limit: int) -> list:
    """主库连续失败告警（自动监控状态里标记 alerted 的链路）。"""
    out = []
    try:
        from core import dr_monitor
        states = dr_monitor.state_view() or {}
    except Exception:
        states = {}
    if not isinstance(states, dict):
        return out
    names = {}
    try:
        for lk in models.list_disaster_links() or []:
            names[str(lk.get("id"))] = lk.get("name") or ""
    except Exception:
        pass
    for key, st in states.items():
        if not isinstance(st, dict):
            continue
        alerted = bool(st.get("alerted"))
        auto_err = st.get("auto_failover_error")
        if not alerted and not auto_err:
            continue
        at = st.get("alerted_at") or st.get("last_check_at") or ""
        ts = _norm_ts(at)
        if not _in_window(ts, since):
            continue
        title = "主库连续探测失败" if alerted else "自动切换失败"
        summary = (st.get("last_error") or st.get("auto_failover_error")
                   or "连续 %s 次探测失败" % (st.get("fails") or 0))
        out.append({
            "kind": "alert",
            "kind_label": KIND_LABELS["alert"],
            "id": key,
            "at": _disp_ts(at),
            "ts": ts,
            "title": title,
            "status": "failed",
            "severity": "error",
            "link_id": int(key) if str(key).isdigit() else None,
            "link_name": names.get(str(key), ""),
            "summary": str(summary)[:200],
            "rto_sec": 0.0,
            "detail": {"consecutive_failures": st.get("fails") or 0,
                       "auto_failover_error": auto_err or "",
                       "alerted_at": st.get("alerted_at") or ""},
        })
    return out[:limit]


def _collect_gap(since: str, limit: int) -> list:
    """保护目标缺口（**当前评估快照**，非历史事件）。

    取自 ``core/goals.py`` 的保护策略校验（RPO 目标未达成 / 备份间隔超目标 /
    异地副本缺失）。它回答的是「**此刻**还有哪些保护目标没达成」。

    诚实边界：缺口可能已经存在很久，只是此刻才被评估到；因此时间戳用
    **评估时刻**并打 ``snapshot=True`` 标记，前端据此显示为「当前评估」
    而不是伪造一个"事件发生时间"。这类事件没有链路归属（是备份任务维度），
    因此按链路过滤时与巡检一样按全局事件保留。
    """
    out = []
    try:
        from core import goals
        data = goals.protection_gaps() or {}
    except Exception:
        data = {}
    gaps = data.get("gaps") or []
    now = db.now_iso()
    ts = _norm_ts(now)
    for g in gaps[:limit]:
        if not isinstance(g, dict):
            continue
        level = str(g.get("level") or "warn")
        code = str(g.get("code") or "")
        out.append({
            "kind": "gap",
            "kind_label": KIND_LABELS["gap"],
            "id": "%s-%s" % (g.get("task_id"), code),
            "at": _disp_ts(now),
            "ts": ts,
            "title": _GAP_TITLES.get(code, "保护目标缺口"),
            "status": level,
            "severity": "error" if level == "fail" else "warn",
            "link_id": None,
            "link_name": "",
            "summary": str(g.get("message") or "")[:200],
            "rto_sec": 0.0,
            "snapshot": True,
            "detail": {"code": code, "level": level,
                       "task_id": g.get("task_id"),
                       "task_name": g.get("task_name") or "",
                       "db_type": g.get("db_type") or "",
                       "rpo_target_min": g.get("rpo_target_min"),
                       "evaluated_at": now},
        })
    return out


# ------------------------------------------------------------------ 汇总

def timeline(days: int = 14, kinds: str = "", link_id: int = None,
             limit: int = 200) -> dict:
    """容灾统一事件时间线。

    参数：
    - ``days``   时间窗口（默认 14 天）
    - ``kinds``  逗号分隔的类型过滤（空 = 全部）
    - ``link_id`` 只看某条链路（None = 全部）
    - ``limit``  返回条数上限

    返回::

        {"events": [...], "counts": {kind: n}, "severity_counts": {...},
         "days": n, "generated_at": iso}
    """
    days = max(1, min(int(days or 14), 365))
    limit = max(1, min(int(limit or 200), 1000))
    want = {k.strip() for k in str(kinds or "").split(",") if k.strip()}
    want = {k for k in want if k in KINDS}
    since = _since(days)

    events = []
    if not want or "switch" in want:
        events += _collect_switch(since, limit)
    if not want or "drill" in want:
        events += _collect_drill(since, limit)
    if not want or "inspection" in want:
        events += _collect_inspection(since, limit)
    if not want or "alert" in want:
        events += _collect_alert(since, limit)
    if not want or "gap" in want:
        events += _collect_gap(since, limit)

    if link_id:
        lid = int(link_id)
        # 巡检汇总与保护目标缺口是全局事件（不绑定单链路），其余按 link_id 过滤
        events = [e for e in events
                  if e.get("link_id") == lid
                  or (e["kind"] == "inspection" and e.get("link_id") is None)
                  or (e["kind"] == "gap" and e.get("link_id") is None)]

    events.sort(key=_sort_key, reverse=True)
    events = events[:limit]

    counts = {k: 0 for k in KINDS}
    sev_counts = {"info": 0, "warn": 0, "error": 0}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        sev_counts[e["severity"]] = sev_counts.get(e["severity"], 0) + 1

    return {
        "events": events,
        "counts": counts,
        "severity_counts": sev_counts,
        "days": days,
        "generated_at": db.now_iso(),
    }


def compliance(days: int = 30) -> dict:
    """容灾合规概览：演练合规率 + 切换成功率 + 平均 RTO。

    口径说明（如实）：
    - 演练合规率 = 近 N 天「已完成」演练中评分达标的比例（60 分及格）
    - 切换成功率 = 近 N 天切换作业中 success 的比例（不含 dry_run 试运行）
    """
    days = max(1, min(int(days or 30), 365))
    since = _since(days)

    drill_ok = drill_total = 0
    try:
        for r in models.list_drills() or []:
            ts = _norm_ts(r.get("started_at") or r.get("created_at") or "")
            if not _in_window(ts, since):
                continue
            if str(r.get("status") or "") not in ("completed", "success", "passed"):
                continue
            drill_total += 1
            try:
                if int(r.get("score") or 0) >= 60:
                    drill_ok += 1
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    sw_total = sw_ok = 0
    rto_vals = []
    try:
        from core import dr_orchestrator
        dr_orchestrator.ensure_schema()
        for r in dr_orchestrator.list_runs(limit=500):
            ts = _norm_ts(r.get("created_at") or "")
            if not _in_window(ts, since):
                continue
            if (r.get("mode") or "") == "dry_run":
                continue          # 试运行不计入成功率
            sw_total += 1
            if (r.get("status") or "") == "success":
                sw_ok += 1
                try:
                    v = float(r.get("rto_sec") or 0)
                    if v > 0:
                        rto_vals.append(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    return {
        "days": days,
        "drill_total": drill_total,
        "drill_pass": drill_ok,
        "drill_rate": round(drill_ok * 100.0 / drill_total, 1) if drill_total else 0.0,
        "switch_total": sw_total,
        "switch_ok": sw_ok,
        "switch_rate": round(sw_ok * 100.0 / sw_total, 1) if sw_total else 0.0,
        "avg_rto": round(sum(rto_vals) / len(rto_vals), 1) if rto_vals else 0.0,
    }
