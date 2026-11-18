# -*- coding: utf-8 -*-
"""备份策略的结构化调度模型（2026-11-13 重构）。

背景：旧版「备份策略」只有 RPO/RTO 两个数字 + 一段手写 JSON，
与任务的实际调度（schedule_type / cron_expr / interval_minutes / 全量周期 /
保留策略）完全脱节——策略改了任务不会变，等于摆设。

本模块把策略调度变成**可计算、可下发、可预览**的结构：

- ``normalize_schedule``：兼容旧版 type/mode/frequency，归一为统一结构；
- ``schedule_to_task_fields``：翻译成任务字段（cron / interval / 混合全量+增量）；
- ``retention_to_task_fields``：保留天数/份数/锁定天数 → 任务字段；
- ``describe_schedule``：人话摘要（"每 6 小时增量 + 周日 02:00 全量"）；
- ``next_fire_times``：接下来 N 次执行时间（APScheduler cron 解析）；
- ``sync_bound_tasks``：级联下发给所有绑定任务并重载调度器。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import core.models as models

try:                       # 平台已依赖 APScheduler；缺失时预览降级为空
    from apscheduler.triggers.cron import CronTrigger
except Exception:          # pragma: no cover
    CronTrigger = None

_WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _iso_duration_to_minutes(freq: str) -> int:
    """把旧版 ``PT15M / PT1H / P1D / P1W`` 频率换算成分钟（无法解析返回 0）。"""
    s = (freq or "").strip()
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", s)
    if m and any(m.groups()):
        d, h, mi = (int(x) if x else 0 for x in m.groups())
        return d * 1440 + h * 60 + mi
    m2 = re.match(r"^P(\d+)W$", s)
    return int(m2.group(1)) * 7 * 1440 if m2 else 0


def _cron_for_week(days: list, time_str: str) -> str:
    """按周几 + 时间生成 cron（crontab 语义：0=周日，平台 UI 0=周一）。"""
    parts = str(time_str or "02:00").split(":")
    hh = int(parts[0] or 2)
    mm = int(parts[1] or 0) if len(parts) > 1 else 0
    dows = sorted({(int(d) + 1) % 7 for d in (days or [0]) if str(d).lstrip("-").isdigit()})
    return "%d %d * * %s" % (mm, hh, ",".join(str(d) for d in dows))


def normalize_schedule(raw: dict) -> dict:
    """把策略里的调度配置归一为统一结构（兼容旧版 type/mode/frequency）。

    返回结构::

        {
          "enabled": True,
          "type": "cron"|"interval",        # 主调度（mixed 模式下即增量节奏）
          "cron_expr": "0 2 * * *",
          "interval_minutes": 1440,
          "full_mode": "full_only"|"mixed", # 是否有独立的全量周期
          "full_type": "week"|"cron",
          "full_days": [6],                 # 0=周一 … 6=周日
          "full_time": "02:00",
          "full_cron_expr": "",
          "incremental_type": "",
          "incremental_cron_expr": "",
          "incremental_interval_minutes": 0,
          "backup_mode": "",                # ""=不改变任务既有备份模式
          "parallel": 1,
        }
    """
    raw = dict(raw or {})
    sched = dict(raw.get("schedule") or {}) if isinstance(raw.get("schedule"), dict) else {}
    for k in ("type", "cron_expr", "interval_minutes", "enabled"):
        if k not in sched and k in raw:
            sched[k] = raw[k]
    if not sched.get("cron_expr") and not sched.get("interval_minutes") and raw.get("frequency"):
        mins = _iso_duration_to_minutes(str(raw.get("frequency")))
        if mins:
            sched["type"] = "interval"
            sched["interval_minutes"] = mins
    stype = str(sched.get("type") or ("cron" if sched.get("cron_expr") else "interval")).lower()
    try:
        interval = int(sched.get("interval_minutes") or 1440)
    except (TypeError, ValueError):
        interval = 1440
    out = {
        "enabled": bool(sched.get("enabled", raw.get("enabled", True))),
        "type": "cron" if stype == "cron" else "interval",
        "cron_expr": str(sched.get("cron_expr") or "").strip(),
        "interval_minutes": max(1, interval),
        "full_mode": str(raw.get("full_mode") or "full_only"),
        "full_type": str(raw.get("full_type") or "week"),
        "full_days": [int(d) for d in (raw.get("full_days") or [6])
                      if str(d).lstrip("-").isdigit()],
        "full_time": str(raw.get("full_time") or "02:00"),
        "full_cron_expr": str(raw.get("full_cron_expr") or "").strip(),
        "incremental_type": str(raw.get("incremental_type") or "").strip(),
        "incremental_cron_expr": str(raw.get("incremental_cron_expr") or "").strip(),
        "incremental_interval_minutes": int(raw.get("incremental_interval_minutes") or 0),
        "backup_mode": str(raw.get("backup_mode") or "").strip(),
        "parallel": max(1, int(raw.get("parallel") or 1)),
    }
    if out["type"] == "cron" and not out["cron_expr"]:
        out["cron_expr"] = "0 2 * * *"
    if out["full_mode"] == "mixed":
        if out["full_type"] not in ("week", "cron"):
            out["full_type"] = "week"
        if out["full_type"] == "week" and not out["full_days"]:
            out["full_days"] = [6]
    return out


def schedule_to_task_fields(sched: dict) -> dict:
    """调度结构 → 任务字段（可直接用于 create_task / update_task）。"""
    s = normalize_schedule(sched)
    out: dict = {}
    if not s.get("enabled"):
        return out                      # 策略停用：不擅自改任务调度
    if s["full_mode"] == "mixed":
        inc_type = s.get("incremental_type") or s["type"]
        if inc_type == "cron":
            out["schedule_type"] = "cron"
            out["cron_expr"] = s.get("incremental_cron_expr") or s["cron_expr"]
            out["interval_minutes"] = None
        else:
            out["schedule_type"] = "interval"
            out["interval_minutes"] = (s.get("incremental_interval_minutes")
                                       or s["interval_minutes"])
            out["cron_expr"] = ""
        out["mixed_backup"] = 1
        if s["full_type"] == "week":
            out["full_schedule_type"] = "week"
            out["full_schedule_days"] = ",".join(str(d) for d in s["full_days"])
            out["full_schedule_expr"] = ""
        else:
            out["full_schedule_type"] = "cron"
            out["full_schedule_expr"] = (s["full_cron_expr"]
                                         or _cron_for_week(s["full_days"], s["full_time"]))
            out["full_schedule_days"] = ""
    else:
        out["mixed_backup"] = 0
        out["full_schedule_type"] = ""
        out["full_schedule_expr"] = ""
        out["full_schedule_days"] = ""
        if s["type"] == "cron":
            out["schedule_type"] = "cron"
            out["cron_expr"] = s["cron_expr"]
            out["interval_minutes"] = None
        else:
            out["schedule_type"] = "interval"
            out["interval_minutes"] = s["interval_minutes"]
            out["cron_expr"] = ""
    if s.get("backup_mode") in ("logical", "physical"):
        out["backup_mode"] = s["backup_mode"]
    return out


def retention_to_task_fields(policy: dict) -> dict:
    """保留策略 → 任务**列**字段（retention_days / retention_count）。

    注意：不可变锁 ``lock_days`` 不是表列，存在任务 ``extra_options``
    （见 :func:`policy_extra_fields`）。
    """
    r = (policy or {}).get("retention") or {}
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            r = {}
    out: dict = {}
    for src, dst in (("days", "retention_days"), ("count", "retention_count")):
        try:
            v = int(r.get(src) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            out[dst] = v
    return out


def policy_extra_fields(policy: dict) -> dict:
    """策略 → 任务 ``extra_options`` 需合并的键（不可变锁天数等）。

    平台的保留锁是任务级 extra（``retention_lock_days``），不是表列；
    这里单独返回，由调用方与任务既有 extra_options 合并后写入。
    """
    r = (policy or {}).get("retention") or {}
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            r = {}
    out: dict = {}
    if "lock_days" not in r:
        return out
    try:
        lock = int(r.get("lock_days") or 0)
    except (TypeError, ValueError):
        lock = 0
    # 显式写 0 也要下发：策略把锁定改成 0 时必须能**清除**任务上已有的锁设置，
    # 否则旧的不可变锁会一直粘着任务（实测 2026-11-13 改策略后仍为 7 天）。
    out["retention_lock_days"] = max(0, lock)
    return out


def policy_task_fields(policy: dict) -> dict:
    """策略 → 任务字段（调度 + 保留 + 备份模式），供绑定/级联同步使用。"""
    pol = policy or {}
    bs = pol.get("backup_strategy") or {}
    if isinstance(bs, str):
        try:
            bs = json.loads(bs)
        except Exception:
            bs = {}
    out = schedule_to_task_fields(bs)
    out.update(retention_to_task_fields(pol))
    return out


def describe_schedule(sched: dict) -> str:
    """把调度翻译成运维能读懂的一句话。"""
    s = normalize_schedule(sched)

    def _main() -> str:
        if s["type"] == "cron":
            return "按 cron「%s」" % s["cron_expr"]
        m = s["interval_minutes"]
        if m % 1440 == 0:
            return "每 %d 天" % (m // 1440)
        if m % 60 == 0:
            return "每 %d 小时" % (m // 60)
        return "每 %d 分钟" % m

    if not s.get("enabled"):
        return "未启用调度"
    if s["full_mode"] == "mixed":
        if s["full_type"] == "week":
            days = "、".join(_WEEK_CN[int(d) % 7] for d in s["full_days"])
            full = "%s %s 全量" % (days, s["full_time"])
        else:
            full = "cron「%s」全量" % (s["full_cron_expr"]
                                     or _cron_for_week(s["full_days"], s["full_time"]))
        inc = _main() + ("增量" if s["type"] == "interval" else "增量")
        return "%s + %s" % (inc, full)
    return "%s执行全量" % _main()


def next_fire_times(sched: dict, count: int = 3) -> list:
    """按调度推算接下来 N 次执行时间（无法推算时返回空列表）。"""
    s = normalize_schedule(sched)
    if not s.get("enabled") or CronTrigger is None:
        return []
    out: list = []
    try:
        from datetime import timedelta
        now = datetime.now()
        if s["type"] == "interval":
            # 间隔模式：按 interval_minutes 推 N 次（mixed 时是增量节奏）
            step = (s.get("incremental_interval_minutes")
                    or s["interval_minutes"]) * 60
            t = now
            for _ in range(count):
                t = t + timedelta(seconds=step)
                out.append(t.strftime("%Y-%m-%d %H:%M"))
            if s["full_mode"] == "mixed" and s["full_type"] == "week":
                trig2 = CronTrigger.from_crontab(_cron_for_week(s["full_days"], s["full_time"]))
                nf = trig2.get_next_fire_time(None, now)
                if nf:
                    out.append(nf.strftime("%Y-%m-%d %H:%M") + "（全量）")
            return out
        if s["full_mode"] == "mixed":
            cron = s.get("incremental_cron_expr") or s["cron_expr"]
            trig = CronTrigger.from_crontab(cron)
            t = now
            for _ in range(count):
                t = trig.get_next_fire_time(None, t)
                if not t:
                    break
                out.append(t.strftime("%Y-%m-%d %H:%M"))
                t = t.replace(second=min(59, t.second + 1))
            if s["full_type"] == "week":
                trig2 = CronTrigger.from_crontab(_cron_for_week(s["full_days"], s["full_time"]))
                nf = trig2.get_next_fire_time(None, now)
                if nf:
                    out.append(nf.strftime("%Y-%m-%d %H:%M") + "（全量）")
        elif s["type"] == "cron":
            trig = CronTrigger.from_crontab(s["cron_expr"])
            t = now
            for _ in range(count):
                t = trig.get_next_fire_time(None, t)
                if not t:
                    break
                out.append(t.strftime("%Y-%m-%d %H:%M"))
                t = t.replace(second=min(59, t.second + 1))
    except Exception:
        return []
    return out


def policy_preview(policy: dict) -> dict:
    """策略预览：人话摘要 + 下次执行时间 + 将下发的任务字段。"""
    pol = policy or {}
    bs = pol.get("backup_strategy") or {}
    if isinstance(bs, str):
        try:
            bs = json.loads(bs)
        except Exception:
            bs = {}
    sched = normalize_schedule(bs)
    return {
        "summary": describe_schedule(sched),
        "next_runs": next_fire_times(sched),
        "schedule": sched,
        "task_fields": policy_task_fields(pol),
        "rpo_target_min": int(pol.get("rpo_target_min") or 0),
        "rto_target_min": int(pol.get("rto_target_min") or 0),
    }


def sync_bound_tasks(policy_id: int) -> dict:
    """把策略的调度/保留级联下发给所有绑定任务，并重载调度器。"""
    pol = models.get_protection_policy(int(policy_id))
    if not pol:
        return {"ok": False, "updated": 0, "fields": {}, "tasks": [],
                "message": "策略不存在"}
    fields = policy_task_fields(pol)
    # 带上 policy_id：走 update_task 的策略绑定分支，额外把等级/RPO/RTO 与
    # extra_options 里的不可变锁天数一起刷新（保持「策略是唯一事实源」）
    fields["policy_id"] = int(policy_id)
    tasks = models.list_tasks_by_policy(int(policy_id)) or []
    updated, failed = [], []
    for t in tasks:
        try:
            models.update_task(int(t["id"]), dict(fields))
            updated.append({"id": t["id"], "name": t.get("name")})
        except Exception as e:
            failed.append({"id": t.get("id"), "name": t.get("name"), "error": str(e)})
    reloaded = False
    if updated:
        try:
            import core.scheduler as scheduler
            scheduler.reload_scheduler()
            reloaded = True
        except Exception:
            pass
    return {"ok": not failed, "updated": len(updated), "fields": fields,
            "tasks": updated, "failed": failed, "scheduler_reloaded": reloaded}
