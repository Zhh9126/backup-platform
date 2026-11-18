# -*- coding: utf-8 -*-
"""容灾切换编排（一键切换）。

对标业界容灾系统（甘肃政务云方案 §4.4.3 / Patroni / 云祺 / 鼎甲）的切换流程：

    precheck（健康快照 + 风险判定）
      → fence（旧主写入冻结，防双写）
      → promote（备库提升）
      → redirect（DNS / 连接串牵引）
      → verify（新主可达 + 业务核对清单）
      → 记录 RTO 与逐步耗时

本模块把上述流程封装成「一次点击 = 一次可观测的切换作业」：
- 每次执行落 ``dr_switch_runs`` 表（含预检快照、逐步明细、RTO、操作人）；
- 支持 ``dry_run``（只预检不切换）与 ``execute``；
- 后台线程执行，前端轮询 ``phase/steps`` 得到实时进度；
- 真正的执行复用 :class:`core.dr_failover.FailoverEngine`（fencing/GTID
  分叉检测/failback 追平判据都在那里，编排层不另写一套）。

诚实边界：编排层不认识具体数据库语义，切换能力边界 = FailoverEngine 的能力
（当前为 MySQL 主从：探测/围栏/提升/DNS/回切）。其它库型需先在引擎侧扩展，
编排层不会伪造「切换成功」。
"""
from __future__ import annotations

import json
import threading
import time
import traceback

import core.db as db
import core.models as models

_DDL = """
CREATE TABLE IF NOT EXISTS dr_switch_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id     INTEGER NOT NULL,
    link_name   TEXT DEFAULT '',
    direction   TEXT NOT NULL DEFAULT 'failover',   -- failover | failback
    mode        TEXT NOT NULL DEFAULT 'execute',    -- execute | dry_run
    status      TEXT NOT NULL DEFAULT 'running',    -- running | success | failed | blocked
    phase       TEXT DEFAULT '',
    steps       TEXT DEFAULT '[]',
    precheck    TEXT DEFAULT '{}',
    rto_sec     REAL DEFAULT 0,
    message     TEXT DEFAULT '',
    operator    TEXT DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    created_at  TEXT
)
"""

_schema_ready = False


def ensure_schema() -> None:
    """确保切换作业表存在（首次使用时建表，兼容老库升级）。"""
    global _schema_ready
    if _schema_ready:
        return
    db.execute(_DDL)
    # 迁移：切换范围（full=业务+DNS / business=仅业务 / dns_only=仅 DNS 牵引）
    try:
        db.execute("ALTER TABLE dr_switch_runs ADD COLUMN scope TEXT DEFAULT 'full'")
    except Exception:
        pass
    _schema_ready = True


# ======================================================================
# 预检
# ======================================================================
def precheck(link_id: int, direction: str = "failover",
             force: bool = False, scope: str = "full") -> dict:
    """切换前预检：健康快照 + 风险清单 + 是否允许直接执行。

    scope: full=业务+DNS（完整切换）/ business=仅业务主备切换 /
    dns_only=仅 DNS 牵引（不碰数据库，只要求 DNS 配置与目标可达）。

    返回::

        {ok, ready, risks[], warnings[], snapshot{}, direction, recommend_force}
    """
    from core import dr_failover

    scope = str(scope or "full").lower()
    if scope not in ("full", "business", "dns_only"):
        scope = "full"
    link = models.get_disaster_link(int(link_id))
    if not link:
        return {"ok": False, "ready": False, "risks": ["链路不存在"],
                "warnings": [], "snapshot": {}, "scope": scope}
    try:
        snap = dr_failover.health_snapshot(int(link_id))
    except Exception as e:
        return {"ok": False, "ready": False,
                "risks": ["健康快照失败: %s" % str(e)[:200]],
                "warnings": [], "snapshot": {}, "scope": scope}

    risks, warns = [], []
    pri = snap.get("primary") or {}
    std = snap.get("standby") or {}
    dns = snap.get("dns") or {}

    if scope == "dns_only":
        # 仅 DNS 牵引：不检查数据库角色，只要求 DNS 已配置且目标端可达
        if not dns.get("domain"):
            risks.append("链路未配置 DNS（domain），无法执行 DNS 切换——"
                         "请在「容灾链路」页 failover-config 中填写 DNS 配置")
        else:
            tgt = std if direction == "failover" else pri
            if not tgt.get("reachable"):
                warns.append("DNS 切换目标 %s 当前不可达（牵引后业务将指向不可达端点）"
                             % (tgt.get("host") or "-"))
            if dns.get("in_sync") and direction == "failover":
                warns.append("DNS 当前已指向预期地址，本次切换可能无实际变化")
        return {"ok": True, "ready": not risks, "risks": risks,
                "warnings": warns, "snapshot": snap, "direction": direction,
                "scope": scope, "recommend_force": False}

    if direction == "failover":
        if pri.get("reachable") and not force:
            risks.append("主库 %s:%s 仍可达——生产仍在写入，直接提升备库会造成双主；"
                         "确认要切换请勾选「强制切换（主库假死/已隔离）」"
                         % (pri.get("host"), pri.get("port")))
        if not std.get("reachable"):
            risks.append("备库不可达：%s" % (std.get("error") or std.get("host")))
        else:
            if not std.get("is_replica"):
                warns.append("备库当前不是复制角色（可能已是主库），请核对拓扑")
            if int(std.get("behind") or 0) > 0:
                warns.append("备库延迟 %s 秒，切换可能丢最近事务（RPO 受影响）"
                             % std.get("behind"))
    else:  # failback
        if std.get("reachable"):
            pass
        if pri.get("reachable"):
            warns.append("原主可达：回切会先检查 GTID 分叉再追平，过程中写入会短暂中断")

    if dns.get("domain") and dns.get("in_sync") is False:
        warns.append("DNS 当前解析(%s)与预期(%s)不一致，切换后会重新牵引"
                     % (dns.get("resolved_ip"), dns.get("expected_ip")))

    # 回切前置：现主（备端）上必须存在可认证的复制账号，否则 rejoin 会卡在
    # IO=Connecting 直到 60s 超时才失败（2026-11-13 170/171 真机实测：
    # 171 上的 dr_repl 密码与 170 不一致 → Access denied → 回切超时）。
    if direction == "failback":
        try:
            cfg = dr_failover._load_config(dict(link))
            ru = dr_failover._decrypt_conn(cfg.get("replication_user") or {})
            ru_user = (ru or {}).get("user") or ""
            ru_pw = (ru or {}).get("password") or ""
            if not ru_user:
                risks.append("未配置 replication_user：回切时旧主无法作为从库接入现主")
            elif std.get("reachable"):
                from core import native_conn
                c = None
                try:
                    c = native_conn.connect("mysql", std.get("host"),
                                            int(std.get("port") or 3306), None,
                                            ru_user, ru_pw, timeout=5)
                    cur = c.cursor()
                    cur.execute("SELECT 1")
                    cur.fetchone()
                except Exception as e:
                    risks.append(
                        "复制账号 %s 在现主 %s 上认证失败（%s）——回切会卡在追平阶段，"
                        "请先在现主创建同名复制账号并授予 REPLICATION SLAVE"
                        % (ru_user, std.get("host"), str(e)[:140]))
                finally:
                    try:
                        if c:
                            c.close()
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        "ok": True,
        "ready": not risks,
        "risks": risks,
        "warnings": warns,
        "snapshot": snap,
        "direction": direction,
        "recommend_force": bool(direction == "failover" and pri.get("reachable")),
    }


# ======================================================================
# 执行
# ======================================================================
def start_switch(link_id: int, direction: str = "failover",
                 mode: str = "execute", force: bool = False,
                 operator: str = "", scope: str = "full") -> dict:
    """发起一次切换作业（后台线程执行，立即返回 run_id）。

    scope: full=业务+DNS（默认）/ business=仅业务主备切换（跳过 DNS）/
    dns_only=仅 DNS 牵引（业务切换已完成时的补偿步骤）。
    """
    from core import dr_failover

    ensure_schema()
    direction = "failback" if str(direction).lower() == "failback" else "failover"
    mode = "dry_run" if str(mode).lower() in ("dry_run", "dryrun", "preview") else "execute"
    scope = str(scope or "full").lower()
    if scope not in ("full", "business", "dns_only"):
        scope = "full"
    link = models.get_disaster_link(int(link_id))
    if not link:
        return {"ok": False, "message": "链路不存在"}
    if not (link.get("failover_config") or "").strip():
        return {"ok": False, "message": "该链路未配置切换参数（主备连接/DNS），"
                                        "请先在「容灾链路」页填写 failover-config"}

    pre = precheck(int(link_id), direction=direction, force=force, scope=scope)
    now = db.now_iso()
    status = "blocked" if (pre.get("risks") and not force) else (
        "success" if mode == "dry_run" else "running")
    run_id = db.execute(
        "INSERT INTO dr_switch_runs (link_id, link_name, direction, mode, scope, "
        "status, phase, steps, precheck, rto_sec, message, operator, started_at, "
        "finished_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(link_id), link.get("name") or "", direction, mode, scope, status,
         "预检完成" if mode == "dry_run" else "预检",
         json.dumps([], ensure_ascii=False),
         json.dumps(pre, ensure_ascii=False, default=str),
         0.0,
         ("预检未通过：" + "；".join(pre.get("risks") or [])) if status == "blocked"
         else ("预检完成（试运行，不执行切换）" if mode == "dry_run" else "开始执行"),
         operator or "", now, now if mode == "dry_run" or status == "blocked" else "",
         now))

    if mode == "dry_run" or status == "blocked":
        return {"ok": status != "blocked", "run_id": run_id, "status": status,
                "precheck": pre, "message": "预检完成" if status != "blocked" else
                "预检未通过：" + "；".join(pre.get("risks") or [])}

    def _runner():
        t0 = time.monotonic()
        steps: list = []
        try:
            engine = dr_failover.FailoverEngine(link)
            # 进度回调：FailoverEngine._step 每完成一步就刷新 DB，前端可实时看到
            def _on_step(step: dict):
                steps.append(step)
                db.execute(
                    "UPDATE dr_switch_runs SET steps=?, phase=? WHERE id=?",
                    (json.dumps(steps, ensure_ascii=False, default=str),
                     "%s · %s" % ("✔" if step.get("ok") else "✘", step.get("name")),
                     run_id))
            engine.on_step = _on_step
            if scope == "dns_only":
                result = engine.switch_dns_only(direction)
            else:
                engine.skip_dns = (scope == "business")
                result = (engine.failback() if direction == "failback"
                          else engine.failover(force=force))
            rto = float(result.get("rto_sec") or round(time.monotonic() - t0, 1))
            scope_label = {"full": "业务+DNS", "business": "仅业务",
                           "dns_only": "仅DNS"}[scope]
            db.execute(
                "UPDATE dr_switch_runs SET status='success', phase='完成', steps=?, "
                "rto_sec=?, message=?, finished_at=? WHERE id=?",
                (json.dumps(result.get("steps") or steps, ensure_ascii=False, default=str),
                 rto,
                 ("切换完成（%s）：%s → %s，RTO %.1fs" %
                  (scope_label, direction, result.get("to") or "-", rto)),
                 db.now_iso(), run_id))
        except Exception as e:
            db.execute(
                "UPDATE dr_switch_runs SET status='failed', phase='失败', steps=?, "
                "message=?, finished_at=? WHERE id=?",
                (json.dumps(steps, ensure_ascii=False, default=str),
                 "切换失败: %s" % str(e)[:400], db.now_iso(), run_id))
            db.add_log("error", "dr.switch",
                       "链路 %s %s 失败: %s" % (link_id, direction, str(e)[:200]))

    threading.Thread(target=_runner, name="dr-switch-%s" % run_id, daemon=True).start()
    return {"ok": True, "run_id": run_id, "status": "running",
            "precheck": pre, "message": "切换作业已启动"}


# ======================================================================
# 查询
# ======================================================================
def get_run(run_id: int) -> dict:
    ensure_schema()
    row = db.query_one("SELECT * FROM dr_switch_runs WHERE id=?", (int(run_id),))
    return _decode(row) if row else {}


def list_runs(link_id: int = None, limit: int = 50) -> list:
    ensure_schema()
    sql = "SELECT * FROM dr_switch_runs"
    params = []
    if link_id:
        sql += " WHERE link_id=?"
        params.append(int(link_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 500)))
    return [_decode(r) for r in db.query(sql, tuple(params))]


def _decode(row: dict) -> dict:
    d = dict(row)
    for k in ("steps", "precheck"):
        raw = d.get(k)
        if isinstance(raw, str) and raw.strip():
            try:
                d[k] = json.loads(raw)
            except Exception:
                d[k] = [] if k == "steps" else {}
        elif not raw:
            d[k] = [] if k == "steps" else {}
    return d


def _parse_region(raw) -> dict:
    """地域字段解析：JSON {"city","lat","lon"} → dict（容错非法值）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _active_site(health: dict, primary_host: str, standby_host: str) -> str:
    """判定当前承载业务的端点：DNS 解析优先，无 DNS 时按主库可达性。

    返回 primary | standby | unknown。不伪造：判据不足时如实 unknown。
    """
    dns = (health or {}).get("dns") or {}
    if dns.get("resolved_ip"):
        if standby_host and dns["resolved_ip"] == standby_host:
            return "standby"
        if primary_host and dns["resolved_ip"] == primary_host:
            return "primary"
        return "unknown"
    pri = (health or {}).get("primary") or {}
    if pri.get("reachable"):
        return "primary"
    return "unknown"


def overview(light: bool = False) -> dict:
    """容灾总览：链路清单 + 最近切换 + 演练合规（供 /dr 页面 / 大屏）。

    ``light=True`` 供大屏高频轮询使用：**跳过逐链路实时探活**
    （每条链路都要 SSH/DB 探测，链路多时单次可达 10~20 秒，5 秒一次的大屏
    刷新会被它拖死）。此时只返回配置态数据，health 置空并标记
    ``health_skipped=True``，由前端保留上一次探测结果，不伪造健康值。
    """
    from core import dr_failover

    ensure_schema()
    links = models.list_disaster_links() or []
    rows = []
    for lk in links:
        item = {
            "id": lk.get("id"), "name": lk.get("name"),
            "status": lk.get("status"), "primary_site": lk.get("primary_site"),
            "dr_site": lk.get("dr_site"), "source_kind": lk.get("source_kind"),
            "has_failover_config": bool((lk.get("failover_config") or "").strip()),
            "health": None, "health_error": "",
            # 地域（地图定位）：{"city","lat","lon"} JSON
            "primary_region": _parse_region(lk.get("primary_region")),
            "dr_region": _parse_region(lk.get("dr_region")),
        }
        if light:
            item["health_skipped"] = True
            item["active_site"] = "unknown"
            rows.append(item)
            continue
        try:
            item["health"] = dr_failover.health_snapshot(int(lk["id"]))
        except Exception as e:
            item["health_error"] = str(e)[:200]
        # 当前承载：DNS 解析 IP 对比主备 host；无 DNS 按主库可达性
        h = item["health"] or {}
        p_host = ((h.get("primary") or {}).get("host")
                  or str(lk.get("primary_site") or "").split(":")[0])
        s_host = ((h.get("standby") or {}).get("host")
                  or str(lk.get("dr_site") or "").split(":")[0])
        item["active_site"] = (_active_site(h, p_host, s_host)
                               if h else "unknown")
        rows.append(item)

    runs = list_runs(limit=10)
    drills = {}
    try:
        drills = models.drill_stats() if hasattr(models, "drill_stats") else {}
    except Exception:
        drills = {}
    return {"links": rows, "recent_runs": runs, "drills": drills,
            "generated_at": db.now_iso()}
