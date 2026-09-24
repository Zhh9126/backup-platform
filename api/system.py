# -*- coding: utf-8 -*-
"""系统/仪表盘/调度/日志/元信息 API。"""
import os
import logging
from flask import jsonify, request, session
import json

logger = logging.getLogger("api.system")

from auth import login_required
from core import models, scheduler, db
from core.engines import supported_types, ENGINE_DISPLAY, engine_meta_map
import config
from . import api_bp


def _sync_types() -> list:
    """同步/迁移插件注册表里的库型（注册即出现，无需改前端）。"""
    try:
        from core.sync.plugins import registry
        return registry.available()
    except Exception:  # noqa: BLE001 - 注册表异常不该让整个 META 挂掉
        return []


def build_meta() -> dict:
    """构建 META 元信息（``GET /api/meta`` 与页面首屏注入共用同一份）。

    抽出来的原因：前端多个页面的「数据库类型下拉」都依赖
    ``BKP.META.db_types``，而它原先只在 ``app.js`` 的 DOMContentLoaded 里
    ``await /api/meta`` 之后才被填充——比它更早执行的页面脚本（sync.js）
    拿到的是空数组，表现为类型下拉一片空白。现在由模板渲染时直接注入
    （见 app.py 的 context_processor + base.html 的 window.__BKP_META__），
    首屏即有数据；接口仍保留给登录后刷新等场景，两边共用本函数避免漂移。
    """
    # 把 config.DEFAULT_PORTS 与适配器声明的 default_port 合并
    default_ports = dict(config.DEFAULT_PORTS or {})
    for t, info in engine_meta_map().items():
        if info.get("default_port") and t not in default_ports:
            default_ports[t] = info["default_port"]
    return {
        "platform": {"name": config.PLATFORM_NAME,
                     "version": config.PLATFORM_VERSION},
        # API 契约信息：规范路径 /api/v1，旧 /api 保留为兼容（弃用）路径
        "api": {"version": "v1",
                "canonical_prefix": "/api/v1",
                "deprecated_prefix": "/api",
                "spec": "/api/v1/openapi.json",
                "docs": "/api/docs",
                "error_contract": ["code", "message", "details"]},
        "db_types": supported_types(),
        # 可迁移/可同步的库型（= 同步插件注册表，供数据迁移页与数据同步页的
        # 类型下拉使用）；与 db_types（备份引擎全集，含 file 等非数据库类型）
        # 区分开，避免下拉里出现选不动的类型或漏掉已支持的类型。
        "sync_types": _sync_types(),
        "display_names": ENGINE_DISPLAY,
        "db_type_meta": engine_meta_map(),
        "default_ports": default_ports,
        "demo_mode": config.DEMO_MODE,
        "scheduler_enabled": config.SCHEDULER_ENABLED,
        "backup_modes": {
            "logical": "逻辑备份（mysqldump / pg_dump / expdp）",
            "physical": "物理备份（XtraBackup / pg_basebackup / RMAN）",
        },
    }


@api_bp.route("/meta", methods=["GET"])
@login_required
def meta():
    return jsonify(build_meta())


@api_bp.route("/path-info", methods=["GET"])
@login_required
def path_info():
    """路径透明化：Docker 容器内路径 → 宿主机路径（挂载映射）与丢数据风险提示。

    备份日志弹窗与存储管理页调用，解决「容器里备份成功、宿主机找不到文件」
    的困惑；未挂载卷时给出高优先级警告。
    """
    p = (request.args.get("p") or "").strip()
    from core import platform_env
    info = platform_env.path_transparency(p)
    return jsonify({"success": True, **info})


@api_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    tasks = models.list_tasks(include_secret=False)
    records = models.list_records(limit=500)
    from collections import Counter
    from datetime import datetime
    status_counter = Counter(r["status"] for r in records)
    db_counter = Counter(t["db_type"] for t in tasks)
    total_size = sum((r.get("size_bytes") or 0) for r in records)
    db_task_count = sum(1 for t in tasks if t.get("db_type") != "file")
    file_task_count = sum(1 for t in tasks if t.get("db_type") == "file")
    total_size_gb = round(total_size / (1024 ** 3), 2)
    # ---- 运营态势指标（保护/RPO/趋势/风险/容量/实时/告警） ----
    insights = _insights(tasks, records, total_size)
    # ---- 综合健康评分 (0~100) ----
    health, health_details = _calc_health(tasks, records, insights)
    # ---- 压缩率统计 ----
    compression_ratio = 0
    try:
        import os
        compressed = sum(1 for r in records if (r.get("backup_path") or "").endswith(".gz"))
        comp_ratio = round(compressed / max(len(records), 1) * 100)
    except Exception:
        comp_ratio = 0
    # ---- 构造任务名/业务系统 索引（recent_records 展示用） ----
    task_name_map = {t["id"]: t.get("name") for t in tasks}
    task_biz_map = {t["id"]: t.get("biz_system") or t.get("name") for t in tasks}
    task_host_map = {t["id"]: t.get("host") for t in tasks}
    task_mode_map = {t["id"]: t.get("backup_mode") for t in tasks}
    # ---- 存储池加密任务数（extra_options.encrypt_pool === true） ----
    encrypt_pool_tasks = 0
    for t in tasks:
        try:
            _eo = json.loads(t.get("extra_options") or "{}")
        except Exception:
            _eo = {}
        if _eo.get("encrypt_pool") is True:
            encrypt_pool_tasks += 1

    def _enrich(r: dict) -> dict:
        """在 record dict 上补仪表盘需要的展示字段（中文 + 关联任务信息）。"""
        tid = r.get("task_id")
        mode = r.get("backup_mode") or task_mode_map.get(tid) or ""
        return {
            "id": r.get("id"),
            "task_id": tid,
            "task_name": task_name_map.get(tid) or "-",
            "biz_system": task_biz_map.get(tid) or "-",
            "host_ip": r.get("host_ip") or task_host_map.get(tid) or "-",
            "db_type": r.get("db_type"),
            "db_type_display": r.get("db_type_display") or config.DB_DISPLAY_NAMES.get(r.get("db_type"), r.get("db_type") or "-"),
            "backup_type": r.get("backup_type"),
            "backup_type_display": r.get("backup_type_display") or config.BACKUP_TYPE_DISPLAY_NAMES.get(r.get("backup_type"), r.get("backup_type") or "-"),
            "backup_mode": mode,
            "backup_mode_display": config.BACKUP_MODE_DISPLAY_NAMES.get(mode, mode or "-"),
            "status": r.get("status"),
            "status_display": config.BACKUP_STATUS_DISPLAY_NAMES.get(r.get("status"), r.get("status") or "-"),
            "duration_sec": r.get("duration_sec"),
            "size_bytes": r.get("size_bytes"),
            "size_human": r.get("size_human") or db.human_size(r.get("size_bytes") or 0),
            "started_at": r.get("started_at"),
        }

    recent = [_enrich(r) for r in records[:10]]
    return jsonify({
        "task_count": len(tasks),
        "db_task_count": db_task_count,
        "file_task_count": file_task_count,
        "record_count": len(records),
        "status_counter": {
            k: v for k, v in dict(status_counter).items()
        },
        "status_counter_display": {
            k: config.BACKUP_STATUS_DISPLAY_NAMES.get(k, k)
            for k, v in dict(status_counter).items()
        },
        "db_counter": dict(db_counter),
        "db_counter_display": {
            k: config.DB_DISPLAY_NAMES.get(k, k)
            for k, v in dict(db_counter).items()
        },
        "total_size": total_size,
        "total_size_gb": total_size_gb,
        "total_size_human": db.human_size(total_size),
        "recent_records": recent,
        "recent_tasks": tasks[:5],
        "health_score": health,
        "health_details": health_details,
        "compression_pct": comp_ratio,
        "encrypt_pool_tasks": encrypt_pool_tasks,
        # ---- 运营态势（保护 / 合规 / 趋势 / 风险 / 容量 / 实时 / 告警） ----
        "protection": insights.get("protection") or {},
        "rpo": insights.get("rpo") or {},
        "trend": insights.get("trend") or [],
        "attention": insights.get("attention") or [],
        "recovery": insights.get("recovery") or {},
        "capacity": insights.get("capacity") or {},
        "rt": insights.get("rt") or {},
        "alerts": insights.get("alerts") or {},
        "next_runs": insights.get("next_runs") or [],
        # ---- 本轮补齐的四项指标 ----
        "drills": insights.get("drills") or {},
        "replication": insights.get("replication") or {},
        "wow": insights.get("wow") or {},
        "objects": insights.get("objects") or {},
    })


def _parse_dt(val):
    """把记录时间解析为 aware datetime。

    库里存的是带时区偏移的本地时间（如 2026-09-16T19:40:48+08:00，见 db.now_iso），
    必须用 fromisoformat 保留偏移后再比较；旧实现用 datetime.utcnow() 与之相减，
    东八区会恒定偏差 8 小时，导致"最近备份时效"判定失真。
    """
    if not val:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(val))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.strptime(str(val)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _insights(tasks, records, total_size):
    """首页"运营态势"指标（对标 Veeam / Commvault / Rubrik 的保护与合规视图）。

    返回保护覆盖、RPO 合规、近 7 天趋势、待处理风险、恢复点窗口、容量预测、
    实时保护状态、告警概览、即将执行任务。全部取真实数据；任一段出错只丢该段，
    不影响仪表盘其余部分。
    """
    from datetime import datetime, timezone, timedelta
    out = {
        "protection": {}, "rpo": {}, "trend": [], "attention": [],
        "recovery": {}, "capacity": {}, "rt": {}, "alerts": {}, "next_runs": [],
        # 补齐此前"明确未实现"的四项首页指标（演练通过率 / 副本复制成功率 /
        # 环比 / 保护对象覆盖率），口径见各自计算段注释。
        "drills": {}, "replication": {}, "wow": {}, "objects": {},
    }
    now = datetime.now().astimezone()

    # ---- 每个任务最近的**成功**备份时间 ----
    # 注意：不能用入参 records（只取最近 500 条）来判断"某任务是否被保护过"——
    # 压测/高频任务会挤占样本，导致绝大多数任务被误判为"从未备份"。
    # 这里直接按 task_id 聚合全表，结果才真实。
    last_ok = {}
    try:
        for row in db.query(
                "SELECT task_id, MAX(COALESCE(finished_at, started_at)) AS t "
                "FROM backup_records WHERE status IN ('success','simulated') "
                "GROUP BY task_id"):
            dt = _parse_dt(row.get("t"))
            if dt:
                last_ok[row.get("task_id")] = dt
    except Exception as exc:
        logger.debug("last_ok 聚合失败: %s", exc)

    # 1) 保护态势：启用任务中"已有成功备份"的占比
    try:
        enabled_tasks = [t for t in tasks if t.get("enabled")]
        protected = [t for t in enabled_tasks if last_ok.get(t.get("id"))]
        out["protection"] = {
            "total": len(tasks),
            "enabled": len(enabled_tasks),
            "protected": len(protected),
            "unprotected": len(enabled_tasks) - len(protected),
            "coverage_pct": round(len(protected) / max(len(enabled_tasks), 1) * 100),
        }
    except Exception as exc:
        logger.debug("protection 计算失败: %s", exc)

    # 2) RPO 合规：启用任务最近成功备份是否超出其 RPO 目标（rpo_target_min）
    try:
        items = []
        compliant = 0
        for t in [x for x in tasks if x.get("enabled")]:
            tid = t.get("id")
            try:
                target = int(t.get("rpo_target_min") or 0) or 1440  # 缺省按 24h
            except Exception:
                target = 1440
            dt = last_ok.get(tid)
            if not dt:
                items.append({
                    "task_id": tid, "name": t.get("name") or f"task_{tid}",
                    "last_ok_at": None, "age_min": None,
                    "target_min": target, "overdue_min": None, "never": True,
                })
                continue
            age_min = int((now - dt).total_seconds() // 60)
            overdue = age_min - target
            if overdue <= 0:
                compliant += 1
            items.append({
                "task_id": tid, "name": t.get("name") or f"task_{tid}",
                "last_ok_at": dt.isoformat(), "age_min": age_min,
                "target_min": target, "overdue_min": max(overdue, 0), "never": False,
            })
        enabled_n = max(len([x for x in tasks if x.get("enabled")]), 1)
        out["rpo"] = {
            "enabled": len([x for x in tasks if x.get("enabled")]),
            "compliant": compliant,
            "violated": len(items) - compliant,
            "rate_pct": round(compliant / enabled_n * 100),
            "items": sorted(items, key=lambda x: (-(x["overdue_min"] or 0), x["name"]))[:8],
        }
    except Exception as exc:
        logger.debug("rpo 计算失败: %s", exc)

    # 3) 近 7 天备份趋势（成功 / 失败）——同样走 SQL 全表聚合
    try:
        buckets = []
        for i in range(6, -1, -1):
            day = (now - timedelta(days=i)).date()
            buckets.append({"date": day.isoformat(), "success": 0, "failed": 0, "total": 0})
        idx = {b["date"]: b for b in buckets}
        since = (now - timedelta(days=7)).date().isoformat()
        for row in db.query(
                "SELECT substr(started_at,1,10) AS d, "
                "SUM(CASE WHEN status IN ('success','simulated') THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS bad, COUNT(*) AS n "
                "FROM backup_records WHERE substr(started_at,1,10) >= ? GROUP BY d", (since,)):
            b = idx.get(row.get("d"))
            if b:
                b["success"] = row.get("ok") or 0
                b["failed"] = row.get("bad") or 0
                b["total"] = row.get("n") or 0
        out["trend"] = buckets
    except Exception as exc:
        logger.debug("trend 计算失败: %s", exc)

    # 4) 待处理风险（逾期 / 从未备份 / 最近失败 / 实时异常 / 缺调度）
    try:
        att = []
        for it in (out["rpo"].get("items") or []):
            if it.get("never"):
                att.append({"kind": "never", "severity": "high", "task_id": it["task_id"],
                            "title": it["name"],
                            "detail": f"启用任务从未成功备份（RPO 目标 {it['target_min']} 分钟）"})
            elif (it.get("overdue_min") or 0) > 0:
                hrs = round((it["age_min"] or 0) / 60, 1)
                att.append({"kind": "rpo", "severity": "high", "task_id": it["task_id"],
                            "title": it["name"],
                            "detail": f"已 {hrs}h 未成功备份，超出 RPO 目标 {it['target_min']} 分钟"})
        for r in records[:200]:
            if r.get("status") == "failed" and len(att) < 12:
                tname = next((t.get("name") for t in tasks if t.get("id") == r.get("task_id")),
                             f"task_{r.get('task_id')}")
                att.append({"kind": "failed", "severity": "medium", "task_id": r.get("task_id"),
                            "title": tname,
                            "detail": f"备份失败于 {str(r.get('started_at') or '')[:16]}"})
                if len([a for a in att if a["kind"] == "failed"]) >= 4:
                    break
        no_sched = [t for t in tasks if t.get("enabled")
                    and t.get("schedule_type") in (None, "none", "")]
        if no_sched:
            att.append({"kind": "sched", "severity": "low", "task_id": None,
                        "title": f"{len(no_sched)} 个启用任务未配调度",
                        "detail": "不会自动执行备份，需补配调度或停用"})
        out["attention"] = att[:12]
    except Exception as exc:
        logger.debug("attention 计算失败: %s", exc)

    # 5) 恢复点窗口：全部成功恢复点覆盖的时间跨度
    try:
        row = db.query_one(
            "SELECT MIN(COALESCE(finished_at, started_at)) AS oldest, "
            "MAX(COALESCE(finished_at, started_at)) AS latest, COUNT(*) AS n "
            "FROM backup_records WHERE status IN ('success','simulated')") or {}
        oldest, latest = _parse_dt(row.get("oldest")), _parse_dt(row.get("latest"))
        out["recovery"] = {
            "points": row.get("n") or 0,
            "latest_at": latest.isoformat() if latest else None,
            "oldest_at": oldest.isoformat() if oldest else None,
            "window_hours": round((latest - oldest).total_seconds() / 3600, 1)
            if (oldest and latest) else 0,
        }
    except Exception as exc:
        logger.debug("recovery 计算失败: %s", exc)

    # 6) 容量与增长预测（备份目录所在磁盘）
    try:
        import shutil
        root = config.BACKUP_ROOT or "/opt/backup-platform"
        if not os.path.isdir(root):
            root = "/"
        usage = shutil.disk_usage(root)
        since = (now - timedelta(days=7)).date().isoformat()
        growth_bytes = 0
        try:
            grow = db.query_one(
                "SELECT COALESCE(SUM(size_bytes),0) AS s FROM backup_records "
                "WHERE substr(started_at,1,10) >= ?", (since,)) or {}
            growth_bytes = int(grow.get("s") or 0)
        except Exception:
            growth_bytes = 0
        per_day = growth_bytes / 7.0 if growth_bytes else 0
        out["capacity"] = {
            "used_bytes": usage.used, "total_bytes": usage.total, "free_bytes": usage.free,
            "used_human": db.human_size(usage.used), "free_human": db.human_size(usage.free),
            "backup_bytes": total_size, "backup_human": db.human_size(total_size),
            "growth_7d_bytes": growth_bytes, "growth_7d_human": db.human_size(growth_bytes),
            "growth_per_day_bytes": int(per_day),
            "eta_days": int(usage.free // per_day) if per_day > 0 else None,
            "used_pct": round(usage.used / max(usage.total, 1) * 100),
        }
    except Exception as exc:
        logger.debug("capacity 计算失败: %s", exc)

    # 7) 实时保护（CDP / 日志流）运行状态
    try:
        from core import rt_backup
        rows = models.list_rt_tasks(only_enabled=True)
        monitor = rt_backup.get_health_monitor()
        rt_items, running, degraded = [], 0, 0
        for row in rows:
            tid = int(row.get("id") or 0)
            if tid <= 0:
                continue
            try:
                h = monitor.of(tid).to_dict()
            except Exception:
                h = {}
            st = (h.get("daemon_status") or "").lower()
            if st == "running":
                running += 1
            else:
                degraded += 1
            rt_items.append({"task_id": tid, "name": row.get("name") or f"task_{tid}",
                             "status": st or "unknown",
                             "reason": (h.get("degrade_reason") or "")[:120]})
        out["rt"] = {"total": len(rt_items), "running": running, "degraded": degraded,
                     "items": rt_items[:5]}
    except Exception as exc:
        logger.debug("rt 计算失败: %s", exc)

    # 8) AI 告警概览
    try:
        from core import ai_alert as ai_engine
        stats = ai_engine.AIPredictor().get_prediction_stats(days=7) or {}
        latest = stats.get("latest") or {}
        lvl = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        metrics = []
        for name, info in latest.items():
            lv = (info or {}).get("risk_level") or ""
            if lv in lvl:
                lvl[lv] += 1
            metrics.append({"metric": name, "risk_level": lv,
                            "risk_score": (info or {}).get("risk_score")})
        out["alerts"] = {"critical": lvl["critical"], "high": lvl["high"],
                         "medium": lvl["medium"], "low": lvl["low"],
                         "total": len(latest), "metrics": metrics}
    except Exception as exc:
        logger.debug("alerts 计算失败: %s", exc)

    # 9) 即将执行的调度任务
    try:
        jobs = []
        for j in (scheduler.scheduler_status() or {}).get("jobs") or []:
            nxt = j.get("next_run")
            jid = str(j.get("id") or "")
            tid = None
            for sep in ("_", "-"):
                if sep in jid and jid.rsplit(sep, 1)[-1].isdigit():
                    tid = int(jid.rsplit(sep, 1)[-1])
                    break
            if tid and nxt:
                jobs.append({"task_id": tid,
                             "name": next((t.get("name") for t in tasks if t.get("id") == tid),
                                          f"task_{tid}"),
                             "next_at": nxt})
        jobs.sort(key=lambda x: x["next_at"])
        out["next_runs"] = jobs[:5]
    except Exception as exc:
        logger.debug("next_runs 计算失败: %s", exc)

    # 10) 恢复演练通过率（drill）——备份"可恢复性"的可信证据。
    # 通过率只按**已出结果**的演练计算（success/failed），pending/running 不计入分母，
    # 否则刚建完未跑的演练会把通过率稀释成假象。
    try:
        cnt = {}
        for row in db.query("SELECT status, COUNT(*) AS n FROM drills GROUP BY status"):
            cnt[str(row.get("status") or "unknown").lower()] = int(row.get("n") or 0)
        passed = cnt.get("success", 0) + cnt.get("passed", 0)
        failed = cnt.get("failed", 0)
        finished = passed + failed
        avg_score = None
        try:
            _s = db.query_one("SELECT AVG(score) AS s FROM drills WHERE score IS NOT NULL") or {}
            avg_score = round(float(_s.get("s")), 1) if _s.get("s") is not None else None
        except Exception:
            avg_score = None
        items = []
        for r in db.query(
                "SELECT id, name, status, score, task_id, finished_at, scheduled_at "
                "FROM drills ORDER BY COALESCE(finished_at, started_at, scheduled_at) DESC "
                "LIMIT 8"):
            items.append({
                "id": r.get("id"), "name": r.get("name"), "status": r.get("status"),
                "score": r.get("score"), "task_id": r.get("task_id"),
                "finished_at": r.get("finished_at") or r.get("scheduled_at"),
            })
        last_dt = _parse_dt(items[0]["finished_at"]) if items else None
        interval_days = 90
        try:
            _cfg = db.query_one(f"SELECT value FROM system_config WHERE {db.qcol('key')}='drill_schedule'")
            if _cfg and _cfg.get("value"):
                interval_days = int((json.loads(_cfg["value"]) or {}).get("interval_days") or 90)
        except Exception:
            interval_days = 90
        overdue_days = int((now - last_dt).days) if last_dt else None
        out["drills"] = {
            "total": sum(cnt.values()), "passed": passed, "failed": failed,
            "finished": finished,
            "pass_rate_pct": round(passed / finished * 100) if finished else None,
            "avg_score": avg_score,
            "last_at": last_dt.isoformat() if last_dt else None,
            "interval_days": interval_days, "overdue_days": overdue_days,
            "is_overdue": (overdue_days is None) or (overdue_days > interval_days),
            "items": items,
        }
    except Exception as exc:
        logger.debug("drills 计算失败: %s", exc)

    # 11) 副本 / 异地复制成功率（L1→L2→L3 与磁带）
    # 口径：近 7 天成功备份中，storage_tier 含远端层级（minio/s3/tape）的比例。
    # 未配置任何远端目标时 rate_pct 返回 None（而不是 0%）——"没配副本"与
    # "配了但全失败"是两件事，混为一谈会误报。
    try:
        days = 7
        since_iso = (now - timedelta(days=days)).isoformat()
        targets = []
        for t in (db.query("SELECT id, name, type, tier, last_error FROM storage_targets "
                           "WHERE enabled=1 ORDER BY tier, id") or []):
            targets.append({"id": t.get("id"), "name": t.get("name"), "type": t.get("type"),
                            "tier": t.get("tier"), "last_error": (t.get("last_error") or "")[:200]})
        remote_types = ("minio", "s3", "tape")
        has_remote = any((t.get("type") in remote_types) for t in targets)
        name_map = {t.get("id"): t.get("name") for t in tasks}
        ok_n = not_n = 0
        unreplicated = []
        for r in db.query(
                "SELECT id, task_id, started_at, size_bytes, storage_tier FROM backup_records "
                "WHERE status IN ('success','simulated') "
                "AND COALESCE(finished_at, started_at) >= ? "
                "ORDER BY COALESCE(finished_at, started_at) DESC", (since_iso,)):
            tier = str(r.get("storage_tier") or "")
            if any(x in tier for x in remote_types):
                ok_n += 1
            else:
                not_n += 1
                if len(unreplicated) < 10:
                    unreplicated.append({
                        "record_id": r.get("id"), "task_id": r.get("task_id"),
                        "task_name": name_map.get(r.get("task_id")) or f"task_{r.get('task_id')}",
                        "storage_tier": tier or "local",
                        "started_at": r.get("started_at"),
                        "size_human": db.human_size(r.get("size_bytes") or 0),
                    })
        total = ok_n + not_n
        out["replication"] = {
            "window_days": days, "targets": targets, "has_remote_target": has_remote,
            "total": total, "replicated": ok_n, "not_replicated": not_n,
            "rate_pct": round(ok_n / total * 100) if (total and has_remote) else None,
            "failed_targets": [t for t in targets if t.get("last_error")],
            "unreplicated_items": unreplicated,
        }
    except Exception as exc:
        logger.debug("replication 计算失败: %s", exc)

    # 12) 环比：本周 vs 上周（两个 7 天窗口口径一致才可比；不足两周数据时 delta 为 None）
    try:
        def _window(d_from, d_to):
            row = db.query_one(
                "SELECT SUM(CASE WHEN status IN ('success','simulated') THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS bad, COUNT(*) AS n "
                "FROM backup_records WHERE substr(started_at,1,10) >= ? AND substr(started_at,1,10) < ?",
                (d_from, d_to)) or {}
            ok, bad = int(row.get("ok") or 0), int(row.get("bad") or 0)
            fin = ok + bad
            return {"ok": ok, "bad": bad, "total": int(row.get("n") or 0),
                    "finished": fin, "rate_pct": round(ok / fin * 100) if fin else None}

        today = now.date()
        cur_from = (today - timedelta(days=6)).isoformat()
        cur_to = (today + timedelta(days=1)).isoformat()
        prev_from = (today - timedelta(days=13)).isoformat()
        prev_to = cur_from
        cur, prev = _window(cur_from, cur_to), _window(prev_from, prev_to)
        delta = None
        if cur["rate_pct"] is not None and prev["rate_pct"] is not None:
            delta = cur["rate_pct"] - prev["rate_pct"]
        out["wow"] = {
            "current": cur, "previous": prev, "delta_pp": delta,
            "current_label": f"{cur_from} ~ {today.isoformat()}",
            "previous_label": f"{prev_from} ~ {(today - timedelta(days=7)).isoformat()}",
        }
    except Exception as exc:
        logger.debug("wow 计算失败: %s", exc)

    # 13) 保护对象覆盖率（实例级 / 库级）
    # 任务级覆盖会把"同一库配了 3 个任务"重复计数，掩盖真正的保护缺口，
    # 因此按 (host, db_name) 归并为库级对象、按 host 归并为实例对象。
    try:
        obj, inst = {}, {}
        for t in tasks:
            if (t.get("db_type") or "") == "file":
                continue
            host = (t.get("host") or "-").strip() or "-"
            dbn = (t.get("db_name") or "-").strip() or "-"
            dt = last_ok.get(t.get("id"))
            o = obj.setdefault((host, dbn), {"host": host, "db_name": dbn, "tasks": 0,
                                             "covered": False, "last_ok_at": None})
            o["tasks"] += 1
            i = inst.setdefault(host, {"host": host, "tasks": 0, "dbs": set(),
                                       "covered": False, "last_ok_at": None})
            i["tasks"] += 1
            i["dbs"].add(dbn)
            if dt:
                iso = dt.isoformat()
                o["covered"] = True
                i["covered"] = True
                if (o["last_ok_at"] is None) or iso > o["last_ok_at"]:
                    o["last_ok_at"] = iso
                if (i["last_ok_at"] is None) or iso > i["last_ok_at"]:
                    i["last_ok_at"] = iso
        db_total = len(obj)
        db_covered = sum(1 for v in obj.values() if v["covered"])
        inst_total = len(inst)
        inst_covered = sum(1 for v in inst.values() if v["covered"])
        out["objects"] = {
            "db": {
                "total": db_total, "covered": db_covered, "uncovered": db_total - db_covered,
                "rate_pct": round(db_covered / db_total * 100) if db_total else None,
                "items": sorted([dict(v) for v in obj.values() if not v["covered"]],
                                key=lambda x: (x["host"], x["db_name"]))[:10],
            },
            "instance": {
                "total": inst_total, "covered": inst_covered,
                "uncovered": inst_total - inst_covered,
                "rate_pct": round(inst_covered / inst_total * 100) if inst_total else None,
                "items": sorted([{"host": v["host"], "tasks": v["tasks"],
                                  "databases": len(v["dbs"]),
                                  "last_ok_at": v["last_ok_at"]}
                                 for v in inst.values() if not v["covered"]],
                                key=lambda x: x["host"])[:10],
            },
        }
    except Exception as exc:
        logger.debug("objects 计算失败: %s", exc)

    return out


def _calc_health(tasks, records, insights=None):
    """综合健康评分（0~100）：保护覆盖 25 + 备份成功率 30 + 调度完备 15 + RPO 合规 20 + 备份时效 10。

    与旧实现的差异（均为真实性修复）：
    - 旧"任务覆盖 30 分"只要存在任务就恒满分，无区分度 → 改为**保护覆盖**
      （启用任务中真正有成功备份的比例）；
    - 新增 **RPO 合规** 维度（业界核心 SLA 指标），逾期任务直接扣分；
    - 旧"同步延迟"用 datetime.utcnow() 与带 +08:00 偏移的本地时间相减，
      恒定偏差 8 小时 → 改为 aware 时间比较。
    """
    from datetime import datetime, timezone
    insights = insights or {}
    details = []
    score = 0

    # 1. 保护覆盖 (max 25)
    prot = insights.get("protection") or {}
    if prot:
        p_total, p_ok = prot.get("enabled") or 0, prot.get("protected") or 0
        p_score = int(25 * (p_ok / max(p_total, 1))) if p_total else 0
    else:
        p_total, p_ok, p_score = len([t for t in tasks if t.get("enabled")]), 0, 0
    score += p_score
    details.append(f"保护覆盖: {p_score}/25 ({p_ok}/{p_total} 个启用任务已有成功备份)")

    # 2. 备份成功率 (max 30)
    if records:
        ok = sum(1 for r in records if r.get("status") in ("success", "simulated"))
        rate = ok / len(records)
        rec_score = int(30 * rate)
        score += rec_score
        details.append(f"备份成功率: {rec_score}/30 ({ok}/{len(records)} 条)")
    else:
        details.append("备份成功率: 0/30 (暂无记录)")

    # 3. 调度完备 (max 15)
    enabled = sum(1 for t in tasks if t.get("enabled"))
    scheduled = sum(1 for t in tasks if t.get("enabled") and t.get("schedule_type") not in (None, "none", ""))
    sched_score = int(15 * (scheduled / max(enabled, 1))) if enabled > 0 else 0
    score += sched_score
    details.append(f"调度完备: {sched_score}/15 ({scheduled}/{max(enabled,1)} 个启用任务已配调度)")

    # 4. RPO 合规 (max 20)
    rpo = insights.get("rpo") or {}
    if rpo and (rpo.get("enabled") or 0) > 0:
        r_score = int(20 * ((rpo.get("compliant") or 0) / rpo["enabled"]))
    else:
        r_score = 0
    score += r_score
    details.append(f"RPO 合规: {r_score}/20 ({rpo.get('compliant', 0)}/{rpo.get('enabled', 0)} 个任务在 RPO 内)")

    # 5. 备份时效 (max 10)：最近一次成功备份距现在多久
    fresh_score, lag_txt = 0, "无成功备份"
    latest_ok = (insights.get("recovery") or {}).get("latest_at")
    dt = _parse_dt(latest_ok)
    if dt:
        from datetime import timezone as _tz
        now = datetime.now().astimezone()
        lag_h = (now - dt).total_seconds() / 3600
        if lag_h < 24:
            fresh_score, lag_txt = 10, f"最近成功备份 {lag_h:.1f}h 前"
        elif lag_h < 72:
            fresh_score, lag_txt = 6, f"最近成功备份 {lag_h:.1f}h 前"
        elif lag_h < 168:
            fresh_score, lag_txt = 3, f"最近成功备份 {lag_h / 24:.1f}d 前"
        else:
            lag_txt = f"最近成功备份 {lag_h / 24:.0f}d 前"
    score += fresh_score
    details.append(f"备份时效: {fresh_score}/10 ({lag_txt})")
    return min(score, 100), details


@api_bp.route("/scheduler", methods=["GET"])
@login_required
def sched_status():
    return jsonify(scheduler.scheduler_status())


@api_bp.route("/scheduler/reload", methods=["POST"])
@login_required
def sched_reload():
    scheduler.reload_scheduler()
    return jsonify(scheduler.scheduler_status())


@api_bp.route("/logs", methods=["GET"])
@login_required
def logs():
    limit = min(int(request.args.get("limit", 200)), 1000)
    level = (request.args.get("level") or "").strip().upper()
    source = (request.args.get("source") or "").strip()
    keyword = (request.args.get("q") or request.args.get("keyword") or "").strip()
    since = (request.args.get("since") or "").strip()

    def _int_or_none(name):
        raw = (request.args.get(name) or "").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    task_id = _int_or_none("task_id")
    record_id = _int_or_none("record_id")
    with_detail = (request.args.get("with_detail") or "1") not in ("0", "false", "no")
    rows = models.list_logs(limit=limit, level=level, source=source,
                            task_id=task_id, record_id=record_id,
                            keyword=keyword, since=since, with_detail=with_detail)
    sources = models.list_log_sources()
    try:
        from core import logging_setup
        locations = logging_setup.log_locations()
    except Exception:
        locations = {}
    return jsonify({
        "ok": True,
        "logs": rows,
        "count": len(rows),
        "sources": sources,
        "log_dir": locations.get("log_dir", ""),
        "log_dir_source": locations.get("log_dir_source", ""),
        "error_log": locations.get("error_log", ""),
    })


@api_bp.route("/logs/clear", methods=["POST"])
@login_required
def clear_logs():
    deleted = models.clear_logs()
    logger.info("system logs cleared by user (%s rows)", deleted)
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/notify-config", methods=["GET"])
@login_required
def get_notify_config():
    import json
    raw = db.get_system_config("notify")
    cfg = json.loads(raw) if raw else dict(config.NOTIFY_DEFAULTS)
    # 不回显密码明文
    channels = []
    for ch in cfg.get("channels", []):
        c = dict(ch)
        if c.get("type") == "email":
            c.pop("smtp_password", None)
        channels.append(c)
    return jsonify({
        "enabled": cfg.get("enabled", config.NOTIFY_DEFAULTS.get("enabled", False)),
        "on_success": cfg.get("on_success", config.NOTIFY_DEFAULTS.get("on_success", False)),
        "on_failure": cfg.get("on_failure", config.NOTIFY_DEFAULTS.get("on_failure", True)),
        "channels": channels,
    })


@api_bp.route("/notify-config", methods=["POST"])
@login_required
def save_notify_config():
    import json
    data = request.get_json(force=True, silent=True) or {}
    # 兼容两种入参：
    #   新格式（前端当前使用）：{enabled, on_success, on_failure, channels: [...]}
    #   旧格式：{notify: "<json string>"}
    if isinstance(data.get("notify"), (str, dict)):
        raw = data["notify"]
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    else:
        # 防静默丢字段：识别到平铺 SMTP 字段（契约要求 channels 数组结构）时
        # 直接 400 提示，而不是忽略后返回"保存成功"造成配置丢失假象
        flat_smtp = {"smtp_host", "smtp_port", "smtp_user", "smtp_password",
                     "username", "password", "from_addr", "to_addrs", "to"}
        suspect = sorted(flat_smtp & set(data.keys()))
        if suspect and "channels" not in data:
            return jsonify({"error": (
                "通知配置格式不正确：请使用 {enabled, on_success, on_failure, "
                "channels: [{type:'email', smtp_host, smtp_port, smtp_user, "
                "smtp_password, from_addr, to, use_tls}]} 结构"
                f"（检测到疑似误传的字段: {', '.join(suspect)}）")}), 400
        cfg = {
            "enabled": bool(data.get("enabled", False)),
            "on_success": bool(data.get("on_success", False)),
            "on_failure": bool(data.get("on_failure", True)),
            "channels": data.get("channels", []),
        }
    # 合并旧配置（保留未改动字段，如已存密码）
    old_raw = db.get_system_config("notify")
    old = json.loads(old_raw) if old_raw else {}
    old_channels = {c.get("smtp_host"): c for c in old.get("channels", [])}
    new_channels = []
    for ch in cfg.get("channels", []):
        if ch.get("type") != "email":
            new_channels.append(ch)
            continue
        c = {
            "type": "email",
            "smtp_host": ch.get("smtp_host", ""),
            "smtp_port": int(ch.get("smtp_port", 25)),
            "smtp_user": ch.get("smtp_user", ""),
            "from_addr": ch.get("from_addr", ch.get("smtp_user", "")),
            "to": [x.strip() for x in (ch.get("to", "") or "").split(",") if x.strip()],
            "use_tls": bool(ch.get("use_tls")),
        }
        pw = ch.get("smtp_password")
        if pw:  # 仅在有新密码时覆盖
            c["smtp_password"] = pw
        elif ch.get("smtp_host") in old_channels:
            c["smtp_password"] = old_channels[ch["smtp_host"]].get("smtp_password", "")
        new_channels.append(c)
    cfg = {
        "enabled": bool(cfg.get("enabled")),
        "on_success": bool(cfg.get("on_success")),
        "on_failure": bool(cfg.get("on_failure")),
        "channels": new_channels,
    }
    db.set_system_config("notify", json.dumps(cfg, ensure_ascii=False))
    return jsonify({"ok": True, "channels": len(new_channels), "enabled": cfg["enabled"]})


@api_bp.route("/notify-config/test", methods=["POST"])
@login_required
def test_notify_config():
    """使用当前已保存的邮件渠道发送一封测试邮件。

    用于在「保存通知配置」后立即验证 SMTP 是否通畅：避免配错
    （主机/端口/密码/授权码）后还要等下一次失败备份才察觉。
    """
    import json
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    raw = db.get_system_config("notify")
    if not raw:
        return jsonify({"ok": False, "error": "尚未保存通知配置"}), 400
    cfg = json.loads(raw)
    email_ch = next((c for c in cfg.get("channels", []) if c.get("type") == "email"), None)
    if not email_ch:
        return jsonify({"ok": False, "error": "未配置邮件渠道"}), 400
    # 基础字段校验
    missing = []
    if not email_ch.get("smtp_host"): missing.append("smtp_host")
    if not email_ch.get("smtp_user"): missing.append("smtp_user")
    if not email_ch.get("to"): missing.append("to")
    if missing:
        return jsonify({"ok": False, "error": f"字段缺失: {', '.join(missing)}"}), 400
    if not email_ch.get("smtp_password"):
        return jsonify({
            "ok": False,
            "error": "SMTP 密码未填写。QQ/163/Gmail 等需要的是「授权码」，不是登录密码。",
        }), 400
    # 组装并发送（HTML 卡片样式）
    title = "[AIDBM] 通知测试邮件"
    text = (
        f"发送时间: {db.now_iso()}\n"
        f"发件人: {email_ch.get('from_addr') or email_ch.get('smtp_user')}\n"
        f"收件人: {', '.join(email_ch.get('to', []))}\n"
        f"SMTP 主机: {email_ch.get('smtp_host')}:{email_ch.get('smtp_port', 25)}\n"
        f"使用 TLS: {email_ch.get('use_tls')}\n\n"
        "如果你看到这封邮件，说明通知配置正确，备份告警/巡检异常会通过该渠道送达。\n"
        "若未收到，请检查：(1) 邮箱垃圾箱；(2) QQ/163 需要「授权码」而非登录密码；"
        "(3) QQ 邮箱请使用 smtp.qq.com:465 + SSL。"
    )
    try:
        from core.email_template import render_test_email
        html = render_test_email({
            "from_addr": email_ch.get("from_addr") or email_ch.get("smtp_user"),
            "smtp_user": email_ch.get("smtp_user"),
            "smtp_host": email_ch.get("smtp_host"),
            "smtp_port": email_ch.get("smtp_port", 25),
            "use_tls": email_ch.get("use_tls"),
            "to": email_ch.get("to", []),
        })
    except Exception:
        html = None
    # 多部分邮件：HTML 优先，纯文本兜底
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = email_ch.get("from_addr") or email_ch.get("smtp_user")
    msg["To"] = ", ".join(email_ch["to"])
    port = int(email_ch.get("smtp_port", 25))
    host = email_ch["smtp_host"]
    timeout = 15
    try:
        # QQ/Gmail/163 普遍 465 用 SSL；其它 port+use_tls 用 STARTTLS
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as s:
                s.login(email_ch["smtp_user"], email_ch["smtp_password"])
                s.sendmail(msg["From"], email_ch["to"], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                if email_ch.get("use_tls"):
                    s.starttls()
                    s.ehlo()
                if email_ch.get("smtp_user"):
                    s.login(email_ch["smtp_user"], email_ch["smtp_password"])
                s.sendmail(msg["From"], email_ch["to"], msg.as_string())
        db.add_log("INFO", "notify", f"test email sent to {', '.join(email_ch['to'])}")
        return jsonify({"ok": True, "message": "测试邮件发送成功，请检查收件箱（含垃圾邮件）"})
    except smtplib.SMTPAuthenticationError as e:
        return jsonify({
            "ok": False,
            "error": f"认证失败：账号或密码错误（QQ/163/Gmail 需使用「授权码」）。详情: {e}",
        }), 400
    except smtplib.SMTPConnectError as e:
        return jsonify({
            "ok": False,
            "error": f"无法连接 SMTP {host}:{port}。检查主机名/端口/防火墙/SSL。详情: {e}",
        }), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"发送失败: {e}"}), 500


# ------------------------- 存储池加密密钥 (KMS) -------------------------
@api_bp.route("/pool-crypto", methods=["GET"])
@login_required
def get_pool_crypto():
    """返回当前存储池加密密钥配置（不回显密钥明文）。"""
    import json
    raw = db.get_system_config("pool_crypto")
    if not raw:
        return jsonify({
            "ok": True,
            "configured": False,
            "mode": "local",
            "active": bool(os.environ.get("BACKUP_POOL_KEY")),
            "local_key_set": False,
            "kms_provider": "",
            "kms_endpoint": "",
            "kms_key_id": "",
            "kms_access_key": "",
            "kms_configured": False,
        })
    cfg = json.loads(raw)
    mode = cfg.get("mode", "local")
    return jsonify({
        "ok": True,
        "configured": True,
        "mode": mode,
        "active": True,
        "local_key_set": bool(cfg.get("pool_key")),
        "kms_provider": cfg.get("kms_provider", ""),
        "kms_endpoint": cfg.get("kms_endpoint", ""),
        "kms_key_id": cfg.get("kms_key_id", ""),
        "kms_access_key": cfg.get("kms_access_key", ""),
        "kms_configured": bool(cfg.get("kms_endpoint") and cfg.get("kms_key_id")),
    })


@api_bp.route("/pool-crypto", methods=["POST"])
@login_required
def save_pool_crypto():
    """保存存储池加密密钥配置（本地密钥库 / KMS）。

    body: {
      mode: "local" | "kms",
      pool_key: <明文主密钥，仅 local 模式，留空表示不修改>,
      kms_provider, kms_endpoint, kms_key_id, kms_access_key, kms_secret,
      local_fallback_key: <KMS 不可达时的回退主密钥>
    }
    """
    import json
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "local")
    cfg = {}
    if mode == "local":
        # 仅当填写了新密钥才更新（避免每次保存把密钥清空）
        new_key = (data.get("pool_key") or "").strip()
        old_raw = db.get_system_config("pool_crypto")
        old_key = ""
        if old_raw:
            try:
                old_key = json.loads(old_raw).get("pool_key", "")
            except Exception:
                old_key = ""
        cfg = {
            "mode": "local",
            "pool_key": new_key or old_key,
        }
        if not cfg["pool_key"]:
            return jsonify({"ok": False, "error": "本地密钥库模式下必须填写主密钥"}), 400
    else:
        # KMS 模式：保存连接参数，主密钥运行时从 KMS 拉取
        cfg = {
            "mode": "kms",
            "kms_provider": data.get("kms_provider", "custom"),
            "kms_endpoint": (data.get("kms_endpoint") or "").strip(),
            "kms_key_id": (data.get("kms_key_id") or "").strip(),
            "kms_access_key": (data.get("kms_access_key") or "").strip(),
            "kms_secret": (data.get("kms_secret") or "").strip(),
            "local_fallback_key": (data.get("local_fallback_key") or "").strip(),
        }
        if not cfg["kms_endpoint"] or not cfg["kms_key_id"]:
            return jsonify({"ok": False, "error": "KMS 模式需填写 endpoint 与 key_id"}), 400
    db.set_system_config("pool_crypto", json.dumps(cfg, ensure_ascii=False))
    # 保存后立即自检：用测试文件加密→解密，验证密钥真实可用
    try:
        from core import crypto_pool as cp
        st = cp.self_test()
        return jsonify({
            "ok": True,
            "self_test": st,
            "message": "存储池加密密钥已保存，自检通过（AES-256-GCM 可用）",
        })
    except Exception as e:
        logger.warning("pool_crypto 自检失败: %s", e)
        return jsonify({
            "ok": True,
            "self_test": {"ok": False, "error": str(e)},
            "message": "配置已保存，但密钥自检失败（加密可能未生效，请检查密钥/环境变量）",
        })


@api_bp.route("/pool-crypto/test", methods=["POST"])
@login_required
def test_pool_crypto():
    """测试 KMS 连通性（仅 KMS 模式有意义）。"""
    import json
    data = request.get_json(force=True, silent=True) or {}
    provider = (data.get("kms_provider") or "custom").lower()
    endpoint = (data.get("kms_endpoint") or "").strip()
    key_id = (data.get("kms_key_id") or "").strip()
    if not endpoint or not key_id:
        return jsonify({"ok": False, "error": "需填写 endpoint 与 key_id"}), 400
    cfg = {
        "mode": "kms",
        "kms_provider": provider,
        "kms_endpoint": endpoint,
        "kms_key_id": key_id,
        "kms_access_key": (data.get("kms_access_key") or "").strip(),
        "kms_secret": (data.get("kms_secret") or "").strip(),
    }
    from core import crypto_pool as cp
    pw = cp._resolve_kms_passphrase(cfg)
    if pw:
        return jsonify({"ok": True, "message": "KMS 连通成功，已取回主密钥明文"})
    return jsonify({
        "ok": False,
        "error": "KMS 不可达或凭证无效（请确认 endpoint/key_id/access_key/secret，或网络是否可达）",
    }), 400


# ======================================================================
# 外部 API 调用令牌管理（页面会话鉴权；供外部系统调用的 Bearer Token）
# ======================================================================
@api_bp.route("/tokens", methods=["GET"])
@login_required
def api_list_tokens():
    return jsonify({"success": True, "data": models.list_api_tokens()})


@api_bp.route("/tokens", methods=["POST"])
@login_required
def api_create_token():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "令牌名称必填"}), 400
    plain = models.create_api_token(name, created_by=session.get("user") or "system")
    return jsonify({"success": True, "token": plain,
                    "warning": "令牌明文仅此一次展示，请立即保存；平台仅存哈希"}), 201


@api_bp.route("/tokens/<int:token_id>", methods=["DELETE"])
@login_required
def api_revoke_token(token_id):
    ok = models.revoke_api_token(token_id)
    if not ok:
        return jsonify({"error": "令牌不存在"}), 404
    return jsonify({"success": True, "message": "令牌已吊销"})
