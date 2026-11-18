# -*- coding: utf-8 -*-
"""容灾巡检引擎（对标中科热备/鼎甲定期巡检）。

巡检三块（重点容灾环境）：
  1. 环境健康 —— 生产端可达性（次） + 容灾端可达性与复制状态（主）；
  2. 数据复制 —— 备库 IO/SQL 线程与延迟（behind 秒）；
  3. DNS 运行 —— dnsmasq 记录清单可读 + 逐域名解析回读一致性。

产出：dr_inspections 一条记录（overall=ok/warn/fail + 逐链路逐项明细 JSON），
供 /dr-inspection 页展示与导出；调度 job ``dr_inspection`` 按
system_config ``dr_inspection_cron``（默认每日 08:00）自动巡检。

只读探测，不改任何配置（与 goals/drill 评估口径一致：巡检=探测非修复）。
"""
from __future__ import annotations

import json
import logging

import core.db as db

logger = logging.getLogger("dr_inspection")

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dr_inspections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  overall TEXT DEFAULT 'ok',            -- ok | warn | fail
  links_total INTEGER DEFAULT 0,
  ok_count INTEGER DEFAULT 0,
  warn_count INTEGER DEFAULT 0,
  fail_count INTEGER DEFAULT 0,
  summary TEXT DEFAULT '',
  detail TEXT DEFAULT '[]',             -- JSON: [{link..., checks:[...]}]
  dns_summary TEXT DEFAULT '{}',        -- JSON: DNS 服务级巡检摘要
  triggered_by TEXT DEFAULT 'manual',   -- manual | schedule
  created_at TEXT
);
"""

TABLE_READY = False
MAX_KEEP = 200

CFG_ENABLED = "dr_inspection_enabled"
CFG_CRON = "dr_inspection_cron"
DEFAULT_CRON = "0 8 * * *"


def _ensure_table() -> None:
    global TABLE_READY
    if TABLE_READY:
        return
    db.execute(TABLE_DDL)
    TABLE_READY = True


# ---------------------------------------------------------------- 配置
def get_config() -> dict:
    return {
        "enabled": str(db.get_system_config(CFG_ENABLED, "1")) not in ("0", "false", "off"),
        "cron": db.get_system_config(CFG_CRON, DEFAULT_CRON) or DEFAULT_CRON,
    }


def save_config(enabled: bool, cron: str) -> dict:
    cron = (cron or "").strip() or DEFAULT_CRON
    # cron 白名单校验：5 段
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError("cron 表达式必须为 5 段（分 时 日 月 周），如 0 8 * * *")
    db.set_system_config(CFG_ENABLED, "1" if enabled else "0")
    db.set_system_config(CFG_CRON, cron)
    from core import scheduler
    try:
        scheduler.reload_scheduler()
    except Exception as e:  # noqa: BLE001
        logger.warning("巡检调度重载失败（下次 start_scheduler 生效）: %s", e)
    return get_config()


# ---------------------------------------------------------------- 单链路巡检
def _check(name: str, ok: bool, level: str = "", message: str = "") -> dict:
    return {"item": name, "status": level or ("ok" if ok else "fail"),
            "message": message or ""}


def inspect_link(link: dict) -> dict:
    """巡检单条容灾链路，返回 {id,name,overall,checks[],active_site}。"""
    from core import dr_failover
    from core.dr_orchestrator import _active_site, _parse_region

    out = {"id": link.get("id"), "name": link.get("name"),
           "overall": "fail", "checks": [], "active_site": "unknown",
           "error": ""}
    if not (link.get("failover_config") or "").strip():
        out["overall"] = "warn"
        out["checks"].append(_check("切换参数", False, "warn",
                                    "未配置 failover-config（主备连接/DNS），无法深度巡检"))
        return out
    try:
        snap = dr_failover.health_snapshot(int(link["id"]))
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
        out["checks"].append(_check("健康快照", False, "fail",
                                    "探测失败: %s" % str(e)[:160]))
        return out

    pri = snap.get("primary") or {}
    std = snap.get("standby") or {}
    dns = snap.get("dns") or {}
    h = {"primary": pri, "standby": std, "dns": dns}
    p_host = pri.get("host") or str(link.get("primary_site") or "").split(":")[0]
    s_host = std.get("host") or str(link.get("dr_site") or "").split(":")[0]
    out["active_site"] = _active_site(h, p_host, s_host)

    # 生产端（次）
    out["checks"].append(_check(
        "生产环境可达（%s）" % p_host, bool(pri.get("reachable")),
        "ok" if pri.get("reachable") else "fail",
        "可达" if pri.get("reachable") else (pri.get("error") or "不可达")))
    # 容灾端（重点）
    if std.get("host"):
        out["checks"].append(_check(
            "容灾环境可达（%s）" % s_host, bool(std.get("reachable")),
            "ok" if std.get("reachable") else "fail",
            "可达" if std.get("reachable") else (std.get("error") or "不可达")))
        if std.get("reachable"):
            if std.get("is_replica"):
                io_ok = str(std.get("io")).lower() in ("yes", "on", "1", "true")
                sql_ok = str(std.get("sql")).lower() in ("yes", "on", "1", "true")
                out["checks"].append(_check(
                    "复制状态 IO/SQL", io_ok and sql_ok,
                    "ok" if (io_ok and sql_ok) else "fail",
                    "IO=%s SQL=%s" % (std.get("io"), std.get("sql"))))
                behind = int(std.get("behind") or 0)
                out["checks"].append(_check(
                    "复制延迟", behind <= 60,
                    "ok" if behind == 0 else ("warn" if behind <= 60 else "fail"),
                    "%s 秒" % behind))
            else:
                out["checks"].append(_check(
                    "复制状态", False, "warn",
                    "容灾端当前不是复制角色（可能已提升为主，请核对拓扑）"))
    else:
        out["checks"].append(_check("容灾环境", False, "warn",
                                    "未配置 standby_conn，无法探测容灾端"))
    # DNS
    if dns.get("domain"):
        in_sync = dns.get("in_sync")
        out["checks"].append(_check(
            "DNS 解析一致性（%s）" % dns.get("domain"),
            bool(in_sync),
            "ok" if in_sync else "fail",
            "解析 %s / 预期 %s" % (dns.get("resolved_ip") or "失败",
                                  dns.get("expected_ip") or "-")))
    # 应用级探活（未配置时不产生条目）
    app = snap.get("app") or {}
    if app.get("configured"):
        out["checks"].append(_check(
            "业务健康端点", bool(app.get("ok")),
            "ok" if app.get("ok") else "fail",
            str(app.get("message") or "")[:160]))

    statuses = [c["status"] for c in out["checks"]]
    if "fail" in statuses:
        out["overall"] = "fail"
    elif "warn" in statuses:
        out["overall"] = "warn"
    else:
        out["overall"] = "ok"
    return out


# ---------------------------------------------------------------- DNS 服务级
def inspect_dns_service() -> dict:
    """DNS 服务级巡检：记录清单可读 + 逐条解析回读。"""
    out = {"ok": True, "records": [], "errors": []}
    try:
        from core import dr_dns
        records = dr_dns.list_records()
        out["records"] = records
        for r in records:
            try:
                resolved = dr_dns.resolve_domain(r["domain"])
                r["resolved"] = resolved
                r["consistent"] = bool(resolved and resolved == r["ip"])
                if not r["consistent"]:
                    out["errors"].append("%s 解析(%s)与配置(%s)不一致"
                                         % (r["domain"], resolved, r["ip"]))
            except Exception as e:  # noqa: BLE001
                r["resolved"] = ""
                r["consistent"] = False
                out["errors"].append("%s 解析失败: %s" % (r["domain"], str(e)[:80]))
        if out["errors"]:
            out["ok"] = False
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["errors"].append("DNS 记录读取失败: %s" % str(e)[:120])
    return out


# ---------------------------------------------------------------- 巡检主入口
def run_inspection(triggered_by: str = "manual") -> dict:
    """执行一轮容灾巡检并落库。"""
    from core import models

    _ensure_table()
    links = models.list_disaster_links() or []
    detail = [inspect_link(lk) for lk in links]
    dns_sum = inspect_dns_service()

    ok_n = sum(1 for d in detail if d["overall"] == "ok")
    warn_n = sum(1 for d in detail if d["overall"] == "warn")
    fail_n = sum(1 for d in detail if d["overall"] == "fail")
    if fail_n or (not dns_sum.get("ok") and links):
        overall = "fail"
    elif warn_n or (not dns_sum.get("ok") and not links):
        overall = "warn" if warn_n else "ok"
    else:
        overall = "ok"
    if not links:
        overall = "warn"
    summary = "链路 %d：正常 %d / 警告 %d / 异常 %d%s" % (
        len(detail), ok_n, warn_n, fail_n,
        ("；DNS " + ("正常" if dns_sum.get("ok") else "异常"))
        if links else "；尚未创建容灾链路")

    rid = db.execute(
        "INSERT INTO dr_inspections(overall, links_total, ok_count, warn_count, "
        "fail_count, summary, detail, dns_summary, triggered_by, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (overall, len(detail), ok_n, warn_n, fail_n, summary,
         json.dumps(detail, ensure_ascii=False, default=str),
         json.dumps(dns_sum, ensure_ascii=False, default=str),
         triggered_by or "manual", db.now_iso()))
    # 保留最近 MAX_KEEP 条
    try:
        db.execute("DELETE FROM dr_inspections WHERE id NOT IN "
                   "(SELECT id FROM dr_inspections ORDER BY id DESC LIMIT ?)",
                   (MAX_KEEP,))
    except Exception:
        pass
    db.add_log("info" if overall == "ok" else "warning", "dr.inspection",
               "容灾巡检 #%d（%s）：%s" % (rid, triggered_by, summary))
    return get_inspection(rid)


def list_inspections(limit: int = 50) -> list:
    _ensure_table()
    rows = db.query("SELECT id, overall, links_total, ok_count, warn_count, "
                    "fail_count, summary, triggered_by, created_at "
                    "FROM dr_inspections ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit or 50), 500)),))
    return [dict(r) for r in rows]


def get_inspection(rid: int) -> dict:
    _ensure_table()
    row = db.query_one("SELECT * FROM dr_inspections WHERE id=?", (int(rid),))
    if not row:
        return {}
    d = dict(row)
    for k in ("detail", "dns_summary"):
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k == "detail" else "{}"))
        except Exception:
            d[k] = [] if k == "detail" else {}
    return d


def delete_inspection(rid: int) -> bool:
    _ensure_table()
    if not db.query_one("SELECT id FROM dr_inspections WHERE id=?", (int(rid),)):
        return False
    db.execute("DELETE FROM dr_inspections WHERE id=?", (int(rid),))
    return True
