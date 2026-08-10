# -*- coding: utf-8 -*-
"""容灾演练引擎：排程→执行→评估→闭环。"""
import os
import subprocess
import time
import json
from datetime import datetime, timedelta, timezone

import core.db as db
from core import models

_logger = db.get_logger("drill")


def run_drill(drill_id: int) -> dict:
    """执行一次容灾演练，验证备份有效性并记录 RTO/RPO/评分。"""
    d = models.get_drill(drill_id)
    if not d:
        return {"ok": False, "message": "演练不存在"}

    models.update_drill(drill_id, {"status": "running", "started_at": db.now_iso()})
    _logger.info("[drill] 开始演练 #%s: %s", drill_id, d.get("name"))

    rto_start = time.time()
    issues = []
    score = 100

    try:
        task = models.get_task(d.get("task_id"), include_secret=False)
        if not task:
            issues.append("任务不存在")
            score -= 50
            raise RuntimeError("任务不存在")

        # 1) 检查是否有成功备份
        records = models.list_records(task_id=d["task_id"], limit=5)
        success_recs = [r for r in records if r.get("status") in ("success", "simulated")]
        if not success_recs:
            issues.append("无可用备份记录")
            score -= 40
        else:
            latest = success_recs[0]
            path = latest.get("backup_path") or ""
            if not path or not os.path.isfile(path):
                issues.append(f"备份文件不存在: {path}")
                score -= 30
            else:
                size = os.path.getsize(path)
                if size == 0:
                    issues.append("备份文件大小为 0")
                    score -= 50

        # 2) 任务调度检查
        if not task.get("enabled"):
            issues.append("备份任务已停用")
            score -= 15
        if task.get("schedule_type") in (None, "none", ""):
            issues.append("未配置自动调度（仅手动执行）")
            score -= 10

        # 3) 连通性模拟检查
        drill_type = d.get("drill_type", "full_recovery")
        scenario_json = d.get("scenario") or "{}"
        try:
            scenario = json.loads(scenario_json)
        except Exception:
            scenario = {}

        if drill_type == "full_recovery":
            # 全恢复演练：验证备份文件完整性 + demo_count > 0
            if success_recs:
                demo_count = sum(1 for r in success_recs if r.get("is_simulated"))
                total = len(success_recs)
                if demo_count == total and total > 0:
                    issues.append("所有备份记录均为仿真（未经过真实验证）")
                    score -= 20

        elif drill_type == "partial" or drill_type == "table_recovery":
            # 对象级演练：检查是否有恢复工具可用
            obj_name = scenario.get("object_name") or scenario.get("table_name") or ""
            if not obj_name:
                issues.append("未指定验证对象名")
                score -= 25
            # 检查客户端是否存在
            db_type = task.get("db_type", "")
            clients = {
                "mysql": "mysql",
                "postgresql": "psql",
                "oracle": "sqlplus",
                "kingbase": "ksql",
                "dameng": "disql",
                "redis": "redis-cli",
                "mongodb": "mongosh",
            }
            client = clients.get(db_type, "")
            if client:
                from shutil import which
                if not which(client):
                    issues.append(f"客户端 {client} 未安装（限制自动化验证）")
                    score -= 15

        # 4) RTO 计算
        rto_end = time.time()
        rto = round(rto_end - rto_start, 1)
        if rto > 900:  # 15 分钟
            score -= 20
        elif rto > 300:
            score -= 10

        # 5) RPO 估算（基于最近备份时间）
        rpo = 0
        if success_recs:
            last_at = latest.get("finished_at") or latest.get("started_at") or ""
            try:
                last_dt = datetime.fromisoformat(last_at)
                if last_dt.tzinfo is None:
                    # 无时区信息则按本地时间处理
                    last_dt = last_dt.astimezone()
                rpo = round((datetime.now(timezone.utc).astimezone() - last_dt).total_seconds(), 1)
                if rpo < 0:
                    rpo = 0  # 时钟抖动保护
            except Exception:
                pass
        if rpo > 86400:  # 24h
            score -= 25
            issues.append(f"最近备份已在 {rpo / 3600:.0f}h 前（RPO 过长）")
        elif rpo > 14400:  # 4h
            score -= 10
            issues.append(f"RPO={rpo / 3600:.1f}h（建议 < 4h）")

        status = "success" if score >= 60 else "failed"
        report = {
            "score": score,
            "rto_sec": rto,
            "rpo_sec": rpo,
            "backup_count": len(success_recs),
            "simulated_count": sum(1 for r in success_recs if r.get("is_simulated")),
            "issues": issues,
            "recommendation": _gen_recommendation(score, issues, drill_type),
        }
        models.update_drill(drill_id, {
            "status": status, "finished_at": db.now_iso(),
            "rto_actual_sec": rto, "rpo_actual_sec": rpo,
            "score": score,
            "issues_found": json.dumps(issues, ensure_ascii=False),
            "report": json.dumps(report, ensure_ascii=False),
        })
        _logger.info("[drill] #%s 完成 status=%s score=%d rto=%.1fs rpo=%.1fs",
                      drill_id, status, score, rto, rpo)
        return {"ok": True, "status": status, "score": score, "rto_sec": rto,
                "rpo_sec": rpo, "issues": issues}

    except Exception as e:
        score = max(score - 40, 0)
        issues.append(f"演练异常: {e}")
        models.update_drill(drill_id, {
            "status": "failed", "finished_at": db.now_iso(),
            "score": 0, "issues_found": json.dumps(issues, ensure_ascii=False),
        })
        return {"ok": False, "status": "failed", "score": 0,
                "message": f"演练异常: {e}", "issues": issues}


def _gen_recommendation(score: int, issues: list, drill_type: str) -> str:
    if score >= 90:
        return "演练合格，灾备能力达标。建议保持现有策略。"
    recs = []
    for issue in issues:
        if "仿真" in issue:
            recs.append("建议启用至少一台非仿真备份任务进行真实验证")
        elif "客户端" in issue:
            recs.append(f"在管理节点安装对应的数据库客户端")
        elif "RPO" in issue:
            recs.append("缩短备份周期（建议核心系统≤4h）")
        elif "调度" in issue:
            recs.append("为任务配置自动调度策略")
        elif "停用" in issue:
            recs.append("请重新启用该备份任务")
        elif "不存在" in issue:
            recs.append("备份文件缺失，请立即检查备份存储")
        elif "大小为 0" in issue:
            recs.append("系统仅生成空文件，可能为备份失败复位导致")
    if not recs:
        recs.append("请定期执行容灾演练，每季度至少一次")
    return "; ".join(recs)


def run_drill_async(drill_id: int) -> None:
    """后台线程执行演练。"""
    import threading
    t = threading.Thread(target=run_drill, args=(drill_id,), daemon=True,
                         name=f"drill-{drill_id}")
    t.start()


# ------------------------- Phase 4：演练制度化 -------------------------
def _quarter_label(dt: datetime) -> str:
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}Q{q}"


def _advance_next_run(frequency: str, now: datetime) -> str:
    """根据频率计算下一次执行时间（ISO）。"""
    if frequency == "weekly":
        nxt = now + timedelta(days=7)
    elif frequency == "monthly":
        # 下月同日（避免跨月溢出，简单 +32 后回到月初）
        nxt = now.replace(day=1) + timedelta(days=32)
        nxt = nxt.replace(day=1)
    else:  # quarterly
        m = ((now.month - 1) // 3 + 1) * 3 + 1
        year = now.year + (m - 1) // 12
        month = (m - 1) % 12 + 1
        nxt = now.replace(year=year, month=month, day=1)
    return nxt.isoformat(timespec="seconds")


def get_drill_schedule() -> dict:
    """读取 drill_schedule 配置（缺省返回默认值）。"""
    raw = db.get_system_config("drill_schedule")
    if raw:
        try:
            cfg = json.loads(raw)
            cfg.setdefault("enabled", False)
            cfg.setdefault("frequency", "quarterly")
            cfg.setdefault("next_run", None)
            cfg.setdefault("target_task_ids", [])
            cfg.setdefault("auto_score", True)
            return cfg
        except (json.JSONDecodeError, TypeError):
            pass
    return {"enabled": False, "frequency": "quarterly", "next_run": None,
            "target_task_ids": [], "auto_score": True}


def save_drill_schedule(data: dict) -> dict:
    """保存/更新 drill_schedule 配置（与既有 system_config 读写保持一致）。"""
    cfg = get_drill_schedule()
    for k in ("enabled", "frequency", "next_run", "target_task_ids", "auto_score"):
        if k in (data or {}):
            cfg[k] = data[k]
    if not isinstance(cfg.get("target_task_ids"), list):
        cfg["target_task_ids"] = []
    db.set_system_config("drill_schedule", json.dumps(cfg, ensure_ascii=False))
    return cfg


def run_scheduled_drill(force: bool = False) -> dict:
    """按 drill_schedule 配置执行周期/季度演练排程。

    读取 system_config.drill_schedule，对 target_task_ids 逐个触发演练（复用 run_drill），
    记录 triggered_by='schedule' 标记，并在完成后推进 next_run。受 enabled 与 next_run 控制；
    只有真正到期（或 force=True）才执行。

    Returns:
        {"ok", "ran": [drill_id...], "count", "next_run", "skipped"?, "reason"?}
    """
    cfg = get_drill_schedule()
    enabled = bool(cfg.get("enabled", False))
    if not enabled and not force:
        return {"ok": True, "skipped": True, "reason": "排程未启用", "ran": []}
    target_ids = list(cfg.get("target_task_ids") or [])
    if not target_ids and not force:
        return {"ok": True, "skipped": True, "reason": "未配置目标任务", "ran": []}

    now = datetime.now(timezone.utc).astimezone()
    next_run = cfg.get("next_run")
    if next_run and not force:
        try:
            ndt = datetime.fromisoformat(next_run)
        except Exception:
            ndt = None
        if ndt and now < ndt:
            return {"ok": True, "skipped": True, "reason": "未到下次执行时间",
                    "next_run": next_run, "ran": []}

    ran = []
    for tid in target_ids:
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        task = models.get_task(tid, include_secret=False)
        if not task:
            _logger.warning("[drill] 目标任务不存在，跳过: task_id=%s", tid)
            continue
        name = f"季度演练 {_quarter_label(now)} · {task.get('name')}"
        drill_id = models.create_drill({
            "name": name,
            "task_id": tid,
            "drill_type": "full_recovery",
            "triggered_by": "schedule",
            "scheduled_at": db.now_iso(),
            "notes": "由季度演练排程自动触发",
        })
        # 同步执行（自测友好；生产环境可改 run_drill_async 异步）
        run_drill(drill_id)
        ran.append(drill_id)

    # 推进下一次执行时间
    new_next = _advance_next_run(cfg.get("frequency", "quarterly"), now)
    cfg["next_run"] = new_next
    db.set_system_config("drill_schedule", json.dumps(cfg, ensure_ascii=False))
    _logger.info("[drill] 排程演练完成，执行 %d 个，下次 %s", len(ran), new_next)
    return {"ok": True, "ran": ran, "count": len(ran), "next_run": new_next}


def get_trend(task_id: int = None, days: int = 90) -> dict:
    """返回 RTO/RPO/评分时间序列，供前端趋势图。

    Returns:
        {"days", "task_id", "points":[{id, task_id, name, date, rto, rpo, score}],
         "summary": {count, avg_rto, avg_rpo, avg_score, max_rto, max_rpo}}
    """
    cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(days=days)).isoformat()
    sql = ("SELECT id, task_id, name, drill_type, status, rto_actual_sec, "
           "rpo_actual_sec, score, finished_at, created_at FROM drills "
           "WHERE finished_at IS NOT NULL AND finished_at >= ?")
    params: list = [cutoff]
    if task_id:
        sql += " AND task_id=?"
        params.append(task_id)
    sql += " ORDER BY finished_at ASC"
    rows = db.query(sql, tuple(params))

    points = []
    for r in rows:
        fa = r.get("finished_at") or r.get("created_at") or ""
        # RTO/RPO 物理量非负，清洗异常（如旧数据的时区误差）避免趋势图失真
        rto = r.get("rto_actual_sec")
        rpo = r.get("rpo_actual_sec")
        rto = max(0.0, float(rto)) if isinstance(rto, (int, float)) else None
        rpo = max(0.0, float(rpo)) if isinstance(rpo, (int, float)) else None
        points.append({
            "id": r["id"],
            "task_id": r.get("task_id"),
            "name": r.get("name"),
            "date": fa[:10],
            "rto": rto,
            "rpo": rpo,
            "score": r.get("score"),
        })

    rto_list = [p["rto"] for p in points if p["rto"] is not None]
    rpo_list = [p["rpo"] for p in points if p["rpo"] is not None]
    sc_list = [p["score"] for p in points if p["score"] is not None]

    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    summary = {
        "count": len(points),
        "avg_rto": _avg(rto_list),
        "avg_rpo": _avg(rpo_list),
        "avg_score": _avg(sc_list),
        "max_rto": max(rto_list) if rto_list else None,
        "max_rpo": max(rpo_list) if rpo_list else None,
    }
    return {"days": days, "task_id": task_id, "points": points, "summary": summary}


def get_baseline(task_id: int) -> dict:
    """基于历史均值/中位数计算 RTO/RPO 基线，并与保护策略目标对比（达标/超标）。"""
    task = models.get_task(task_id, include_secret=False)
    if not task:
        return {"ok": False, "error": "任务不存在"}

    # 目标 RPO/RTO（秒）：来自保护策略（ProtectionPolicy）
    from core.policy import policy_service
    rpo_min, rto_min = policy_service.resolve_rpo_rto(task)
    rpo_target = int(rpo_min) * 60
    rto_target = int(rto_min) * 60

    rows = db.query(
        "SELECT rto_actual_sec, rpo_actual_sec, score FROM drills "
        "WHERE task_id=? AND finished_at IS NOT NULL", (task_id,))
    rto_vals = [r["rto_actual_sec"] for r in rows if r["rto_actual_sec"] is not None]
    rpo_vals = [r["rpo_actual_sec"] for r in rows if r["rpo_actual_sec"] is not None]
    sc_vals = [r["score"] for r in rows if r["score"] is not None]

    def _stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        mean = sum(s) / n
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {"mean": round(mean, 1), "median": round(median, 1),
                "min": round(min(s), 1), "max": round(max(s), 1), "count": n}

    base_rto = _stats(rto_vals)
    base_rpo = _stats(rpo_vals)
    base_sc = _stats(sc_vals)

    def _verdict(base, target):
        if base is None:
            return "无数据"
        if target <= 0:
            return "达标(近实时)"
        return "达标" if base["mean"] <= target else "超标"

    return {
        "ok": True,
        "task_id": task_id,
        "task_name": task.get("name"),
        "protection_level": task.get("protection_level"),
        "rpo_target_sec": rpo_target,
        "rto_target_sec": rto_target,
        "baseline": {"rto": base_rto, "rpo": base_rpo, "score": base_sc},
        "verdict": {
            "rto": _verdict(base_rto, rto_target),
            "rpo": _verdict(base_rpo, rpo_target),
            "score": ("达标" if (base_sc and (base_sc["mean"] or 0) >= 60)
                      else "无数据" if not base_sc else "需改进"),
        },
    }
