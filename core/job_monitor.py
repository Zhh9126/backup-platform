# -*- coding: utf-8 -*-
"""备份任务作业监控（对标 Commvault/Veeam 作业监控：failed job triage /
missed jobs / SLA-RPO 合规）。

用户已有专业数据库监控平台——本模块**不做数据库指标监控**，只监控
「备份任务本身」的健康度：

- 今日执行概览：成功/失败/运行中计数（backup_records 当日窗口）
- 需要关注的任务（failed job triage）：
  * 连续失败：最近一次执行失败且连续失败次数 >= 2（Commvault 同款语义）
  * 执行逾期：启用且配置了调度间隔的任务，距最近成功备份超过
    2 倍调度间隔（未按计划执行 / missed job）
  * 长期未成功：距最近成功备份超过 24 小时（无论是否配置调度）
- 最近执行流：最近 N 条备份记录（任务/库型/状态/大小/耗时/时间）

诚实边界：RPO 口径为调度间隔的 2 倍（启发式），非保护策略里的
rpo_target_min（那是保护差距 goals 模块的口径，两者互补不重复）。
"""
from datetime import datetime, timedelta

from core import db


def _now() -> datetime:
    return datetime.now()


def _parse_ts(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def summary(limit: int = 50) -> dict:
    now = _now()
    today = now.strftime("%Y-%m-%d")

    # ---- 今日执行概览 ----
    today_rows = db.query(
        "SELECT status, COUNT(*) AS n FROM backup_records "
        "WHERE started_at >= ? GROUP BY status", (today,)) or []
    cnt = {r["status"]: int(r["n"]) for r in today_rows}
    running_tasks = db.query(
        "SELECT COUNT(DISTINCT task_id) AS n FROM backup_records "
        "WHERE status='running'") or []
    today_stats = {
        "success": cnt.get("success", 0),
        "failed": cnt.get("failed", 0),
        "running": int(running_tasks[0]["n"]) if running_tasks else 0,
    }

    # ---- 任务维度聚合 ----
    tasks = db.query(
        "SELECT id, name, db_type, host, port, db_name, enabled, "
        "interval_minutes, last_status, last_run_at "
        "FROM backup_tasks ORDER BY id") or []
    recs = db.query(
        "SELECT task_id, status, started_at, size_bytes, duration_sec, message "
        "FROM backup_records ORDER BY id DESC LIMIT 5000") or []
    by_task = {}
    for r in recs:
        by_task.setdefault(r["task_id"], []).append(r)

    attention = []
    for t in tasks:
        tid = t["id"]
        hist = by_task.get(tid, [])
        last_success = next((r for r in hist
                             if r["status"] == "success"), None)
        last_rec = hist[0] if hist else None
        consec_fail = 0
        for r in hist:
            if r["status"] == "failed":
                consec_fail += 1
            else:
                break
        ls = _parse_ts(last_success["started_at"]) if last_success else None
        mins_since_ok = int((now - ls).total_seconds() // 60) if ls else None
        interval = int(t.get("interval_minutes") or 0)
        reasons = []
        # 连续失败分诊不要求「启用中」：手动执行（enabled=0）连续失败同样
        # 需要关注；调度类（missed/never_ran/stale）保持启用前提。
        if consec_fail >= 2:
            reasons.append({"kind": "consecutive_fail", "severity": "high",
                            "note": "连续失败 %d 次" % consec_fail})
        if int(t.get("enabled") or 0) and interval > 0 and mins_since_ok is not None \
                and mins_since_ok > interval * 2:
            reasons.append({"kind": "missed", "severity": "medium",
                            "note": "距最近成功 %s 分钟（调度间隔 %d 分钟，"
                                    "超过 2 倍视为未按计划执行）"
                                    % (mins_since_ok, interval)})
        if int(t.get("enabled") or 0) and ls is None and not hist:
            reasons.append({"kind": "never_ran", "severity": "medium",
                            "note": "启用以来从未执行过备份"})
        if int(t.get("enabled") or 0) and mins_since_ok is not None \
                and mins_since_ok > 24 * 60 and "missed" not in \
                [r["kind"] for r in reasons]:
            reasons.append({"kind": "stale", "severity": "medium",
                            "note": "距最近成功超过 24 小时"})
        if reasons:
            attention.append({
                "task_id": tid, "name": t["name"],
                "db_type": t["db_type"], "host": t["host"], "port": t["port"],
                "db_name": t["db_name"], "enabled": int(t.get("enabled") or 0),
                "last_status": t.get("last_status"),
                "last_run_at": t.get("last_run_at"),
                "last_success_at": last_success["started_at"] if last_success else None,
                "consecutive_fail": consec_fail,
                "minutes_since_success": mins_since_ok,
                "reasons": reasons,
            })

    # ---- 最近执行流 ----
    recent = []
    for r in recs[:limit]:
        recent.append({
            "id": r["task_id"], "status": r["status"],
            "started_at": r["started_at"], "size_bytes": int(r.get("size_bytes") or 0),
            "duration_sec": r.get("duration_sec"),
            "message": (r.get("message") or "")[:120],
        })
    # 附任务名（一次查询映射）
    tnames = {t["id"]: t["name"] for t in tasks}
    for r in recent:
        r["task_name"] = tnames.get(r["id"], "#%s" % r["id"])

    severity_rank = {"high": 0, "medium": 1}
    attention.sort(key=lambda a: (min(severity_rank.get(rr["severity"], 9)
                                      for rr in a["reasons"]), a["task_id"]))
    return {
        "today": today_stats,
        "attention": attention,
        "recent": recent,
        "task_total": len(tasks),
        "enabled_total": sum(1 for t in tasks if int(t.get("enabled") or 0)),
        "generated_at": db.now_iso(),
    }
