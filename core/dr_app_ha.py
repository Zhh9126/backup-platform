# -*- coding: utf-8 -*-
"""应用级容灾辅助：出站应用探活 + 切换后业务验证（零客户端安装）。

定位（产品边界）：数据级容灾由实时同步/CDP 承担，本模块只回答
「业务现在是否可用」，不再造第二套复制引擎：
- 平台侧出站 HTTP 探活（应用无需安装 agent / 依赖，符合离线约束）；
- 切换后业务验证（SQL 断言 / HTTP 断言），失败即判定「数据库已切换但业务不可用」；
- 默认关闭，存量容灾链路零破坏。

安全：探活 URL 走 core.net_guard（SSRF 防护）；业务验证 SQL 只允许 SELECT，
连接凭据复用 failover_config 中已加密的主/备连接，不在配置里另存明文口令。
"""
import time
import urllib.error
import urllib.request

from core import db, native_conn, net_guard

SAFE_CHECK_KEYS = ("name", "type", "target", "db_type", "database",
                   "query", "expect", "url", "expect_status",
                   "expect_body", "timeout")
DEFAULT_TIMEOUT = 3


def probe_app(cfg: dict) -> dict:
    """出站探测应用健康端点。

    cfg: {enabled, url, expect_status, expect_body, timeout}
    未配置 / 禁用时返回 ok=True（不把「没配」当成故障）。
    """
    cfg = cfg or {}
    url = str(cfg.get("url") or "").strip()
    if not url or cfg.get("enabled", True) is False:
        return {"configured": bool(url), "enabled": False, "ok": True,
                "url": url, "reason": "未启用应用探活"}
    ok, reason = net_guard.validate_url(url)
    if not ok:
        return {"configured": True, "enabled": True, "ok": False,
                "url": url, "error": "探活目标被安全策略拒绝: %s" % reason}
    timeout = max(1, int(cfg.get("timeout") or DEFAULT_TIMEOUT))
    expect_status = int(cfg.get("expect_status") or 200)
    t0 = time.monotonic()
    status, body = 0, ""
    try:
        r = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return {"configured": True, "enabled": True, "ok": False,
                "url": url, "status": status,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    body_ok = (str(cfg["expect_body"]) in body
               if cfg.get("expect_body") else True)
    return {"configured": True, "enabled": True,
            "ok": status == expect_status and body_ok, "url": url,
            "status": status, "expect_status": expect_status,
            "body_match": body_ok,
            "latency_ms": int((time.monotonic() - t0) * 1000)}


def _default_target(phase: str) -> str:
    # failover 后新主是原备库；failback 后新主是原主库
    return "standby_conn" if phase == "failover" else "primary_conn"


def _sql_check(check: dict, cfg: dict, phase: str, decrypt_conn) -> tuple:
    query = str(check.get("query") or "").strip()
    if not query.lower().startswith("select"):
        return False, "业务验证 SQL 只允许 SELECT（禁止写生产库）"
    target = check.get("target") or _default_target(phase)
    conn_cfg = (cfg or {}).get(target) or {}
    if decrypt_conn:
        conn_cfg = decrypt_conn(conn_cfg)
    if not conn_cfg.get("host"):
        return False, "缺少 %s 连接配置，无法执行业务验证" % target
    conn = None
    try:
        conn = native_conn.connect(
            check.get("db_type") or "mysql", conn_cfg.get("host"),
            int(conn_cfg.get("port") or 3306), check.get("database") or None,
            conn_cfg.get("user") or conn_cfg.get("username") or "root",
            conn_cfg.get("password") or "",
            timeout=int(check.get("timeout") or 5))
        cur = conn.cursor()
        cur.execute(query)
        row = cur.fetchone()
        got = "" if row is None else str(row[0])
        expect = str(check.get("expect"))
        return got == expect, "expect=%s got=%s" % (expect, got)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_post_switch_verification(cfg: dict, phase: str = "failover",
                                 decrypt_conn=None) -> dict:
    """切换完成后验证业务可用性；失败不代表数据库切换失败，但业务判据不通过。

    cfg["post_switch_verification"] = {"enabled": bool, "checks": [...]}
    checks 元素：{"type":"sql"|"http", ...}，SQL 凭据复用 failover_config。
    """
    vcfg = (cfg or {}).get("post_switch_verification") or {}
    checks = [c for c in (vcfg.get("checks") or []) if isinstance(c, dict)][:20]
    if not vcfg.get("enabled") or not checks:
        return {"configured": bool(checks), "ok": True, "checks": [],
                "message": "未启用切换后业务验证（默认关闭，存量链路零破坏）"}
    results = []
    for i, c in enumerate(checks):
        name = str(c.get("name") or ("check-%d" % (i + 1)))
        t0 = time.monotonic()
        try:
            if (c.get("type") or "http") == "sql":
                ok, detail = _sql_check(c, cfg, phase, decrypt_conn)
            else:
                pr = probe_app({**c, "enabled": True})
                ok = bool(pr.get("ok"))
                detail = str(pr.get("error") or
                             ("status=%s" % pr.get("status")))
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, str(e)[:160])
        results.append({"name": name, "ok": bool(ok), "detail": detail,
                        "ms": int((time.monotonic() - t0) * 1000)})
    failed = [r for r in results if not r["ok"]]
    ok_all = not failed
    msg = ("切换后业务验证通过（%d 项）" % len(results) if ok_all else
           "切换后业务验证未通过（%d/%d 项失败）：数据库已切换但业务不可用，"
           "请核查应用并考虑回切" % (len(failed), len(results)))
    if failed:
        db.add_log("ERROR", "dr_failover", msg)
    return {"configured": True, "ok": ok_all, "checks": results,
            "message": msg}


def sanitize_checks(checks) -> list:
    """只保留白名单键，杜绝把 password/任意字段写进 failover_config。"""
    return [{k: v for k, v in c.items() if k in SAFE_CHECK_KEYS}
            for c in (checks or []) if isinstance(c, dict)][:20]
