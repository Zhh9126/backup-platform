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
from datetime import datetime, timedelta

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

# 运行中的切换作业 → 中止信号（threading.Event）。
# 进程内内存态：平台重启后信号丢失——诚实边界是「重启会中断进行中的切换作业」，
# 作业记录仍在 dr_switch_runs 表内（状态如实留在 running，可人工订正）。
_CANCEL_EVENTS: dict = {}

# RTO 达标基准（秒）：单次切换耗时超过该值即视为未达标。
# 链路可在 failover_config.slo_rto_sec 单独覆盖。
DEFAULT_SLO_RTO_SEC = 300.0


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

    # 复制位点核对（GTID 集合，只读）：把「RPO 缺口 / 回切追平量」量化出来。
    # 拿不到位点时如实跳过（不伪造 RPO=0），探测异常不阻断预检。
    position = {}
    try:
        position = _replication_position(link, snap)
    except Exception as e:
        position = {"error": str(e)[:160]}
    if position and not position.get("error"):
        lost = position.get("primary_missing_on_standby") or []
        lag = position.get("standby_missing_on_primary") or []
        if direction == "failover" and lost:
            warns.append("主库有 %d 段事务尚未到达备库——切换将丢失这部分事务"
                         "（RPO 缺口，非零丢失窗口）" % len(lost))
        if direction == "failback" and lag:
            warns.append("原主落后现主 %d 段事务——回切会先追平再提升，"
                         "追平失败将拒绝回切（防丢数据）" % len(lag))

    return {
        "ok": True,
        "ready": not risks,
        "risks": risks,
        "warnings": warns,
        "snapshot": snap,
        "direction": direction,
        "replication_position": position,
        "recommend_force": bool(direction == "failover" and pri.get("reachable")),
    }


def _replication_position(link: dict, snap: dict = None) -> dict:
    """只读探测两端 GTID 已执行集合，量化 RPO 缺口 / 回切追平量。

    返回::

        {gtid_enabled, primary_uuids, standby_uuids,
         primary_missing_on_standby: [(uuid, a, b)],  # 主有备无 → 切换会丢失
         standby_missing_on_primary: [(uuid, a, b)]}  # 备有主无 → 旧主落后

    探测不到（未开 GTID / 连接失败 / 权限不足）时返回 {}——调用方据此如实
    跳过量化提示，绝不伪造 RPO=0。
    """
    from core import dr_failover

    cfg = dr_failover._load_config(dict(link))
    pc = dr_failover._decrypt_conn(cfg.get("primary_conn") or {})
    sc = dr_failover._decrypt_conn(cfg.get("standby_conn") or {})
    if not pc or not sc:
        return {}
    p_text = dr_failover._fetch_gtid_executed(pc)
    s_text = dr_failover._fetch_gtid_executed(sc)
    if not p_text and not s_text:
        return {}          # 未开启 GTID：如实无量化信息
    p_map = dr_failover._parse_gtid_set(p_text)
    s_map = dr_failover._parse_gtid_set(s_text)
    return {
        "gtid_enabled": True,
        "primary_uuids": len(p_map),
        "standby_uuids": len(s_map),
        "primary_missing_on_standby": dr_failover.gtid_missing(p_map, s_map),
        "standby_missing_on_primary": dr_failover.gtid_missing(s_map, p_map),
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

    # 中止信号：登记到进程内表，POST /dr/switch-runs/<id>/cancel 触发它
    cancel_ev = threading.Event()
    _CANCEL_EVENTS[int(run_id)] = cancel_ev
    _emit_switch_event("dr.switch.started", link, direction, scope,
                       run_id=run_id, operator=operator, precheck=pre)

    def _runner():
        t0 = time.monotonic()
        steps: list = []
        try:
            engine = dr_failover.FailoverEngine(link)
            engine.cancel_event = cancel_ev
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
            ok_msg = ("切换完成（%s）：%s → %s，RTO %.1fs" %
                      (scope_label, direction, result.get("to") or "-", rto))
            db.execute(
                "UPDATE dr_switch_runs SET status='success', phase='完成', steps=?, "
                "rto_sec=?, message=?, finished_at=? WHERE id=?",
                (json.dumps(result.get("steps") or steps, ensure_ascii=False, default=str),
                 rto, ok_msg, db.now_iso(), run_id))
            _emit_switch_event("dr.switch.success", link, direction, scope,
                               run_id=run_id, operator=operator, rto=rto,
                               message=ok_msg, result=result)
        except Exception as e:
            cancelled = cancel_ev.is_set() or "中止" in str(e)
            status_txt = "cancelled" if cancelled else "failed"
            phase_txt = "已中止" if cancelled else "失败"
            fail_msg = ("切换被用户中止: %s" % str(e)[:400] if cancelled
                        else "切换失败: %s" % str(e)[:400])
            db.execute(
                "UPDATE dr_switch_runs SET status=?, phase=?, steps=?, "
                "message=?, finished_at=? WHERE id=?",
                (status_txt, phase_txt,
                 json.dumps(steps, ensure_ascii=False, default=str),
                 fail_msg, db.now_iso(), run_id))
            db.add_log("WARNING" if cancelled else "error", "dr.switch",
                       "链路 %s %s %s: %s" % (link_id, direction, phase_txt, str(e)[:200]))
            _emit_switch_event(
                "dr.switch.cancelled" if cancelled else "dr.switch.failure",
                link, direction, scope, run_id=run_id, operator=operator,
                message=fail_msg)
        finally:
            _CANCEL_EVENTS.pop(int(run_id), None)

    threading.Thread(target=_runner, name="dr-switch-%s" % run_id, daemon=True).start()
    return {"ok": True, "run_id": run_id, "status": "running",
            "precheck": pre, "message": "切换作业已启动"}


def cancel_run(run_id: int) -> dict:
    """中止一次进行中的切换作业。

    中止语义（诚实边界）：信号在**步骤边界**生效——正在执行的步骤
    （SSH/数据库操作）不强杀，该步骤跑完后停止后续步骤。作业状态置
    ``cancelled``，已完成步骤全部留痕。
    """
    ensure_schema()
    run = get_run(int(run_id))
    if not run:
        return {"ok": False, "error": "作业不存在"}
    status = str(run.get("status") or "")
    if status != "running":
        return {"ok": False, "status": status,
                "error": "作业当前状态为 %s，无法中止" % (status or "-")}
    ev = _CANCEL_EVENTS.get(int(run_id))
    if ev is None:
        return {"ok": False, "status": status,
                "error": "作业不在本进程内（平台重启或已结束），无法中止——请人工核对拓扑"}
    ev.set()
    db.execute("UPDATE dr_switch_runs SET phase='中止中' WHERE id=?", (int(run_id),))
    db.add_log("WARNING", "dr.switch",
               "切换作业 %s 收到中止请求（步骤边界生效）" % run_id)
    return {"ok": True, "status": "cancelling", "run_id": int(run_id),
            "message": "已发送中止请求：当前步骤完成后停止"}


def _emit_switch_event(event: str, link: dict, direction: str, scope: str,
                        run_id: int = 0, operator: str = "",
                        rto: float = 0.0, message: str = "",
                        precheck: dict = None, result: dict = None) -> None:
    """切换事件外发：webhook（事件中心）+ 通知通道（邮件/短信等）。

    通知是**旁路**：发送失败只记日志，绝不改变切换结果。``started`` 事件
    只进 webhook（事件中心），不触发「失败」类通知，避免值班误判。
    """
    payload = {
        "run_id": int(run_id or 0),
        "link_id": int(link.get("id") or 0),
        "link_name": link.get("name") or "",
        "direction": direction,
        "scope": scope,
        "operator": operator or "",
        "rto_sec": float(rto or 0),
        "message": message or "",
    }
    if precheck:
        payload["risks"] = precheck.get("risks") or []
        payload["warnings"] = precheck.get("warnings") or []
    if result:
        payload["to"] = result.get("to") or ""
    try:
        from core import webhooks
        webhooks.emit_async(event, payload)
    except Exception as e:
        try:
            db.add_log("WARNING", "dr.switch",
                       "切换事件 webhook 发送失败 %s: %s" % (event, str(e)[:160]))
        except Exception:
            pass
    kind = ("开始" if event.endswith("started") else
            "成功" if event.endswith("success") else
            "中止" if event.endswith("cancelled") else "失败")
    if kind == "开始":
        return
    try:
        from core import notifier
        title = "[%s] 容灾切换 %s" % (kind, payload.get("link_name")
                                    or payload.get("link_id"))
        text = payload.get("message") or ""
        if payload.get("rto_sec"):
            text += "\nRTO: %.1f 秒" % payload["rto_sec"]
        notifier.Notifier(None, db.get_logger("notify")).notify_async(
            "success" if kind == "成功" else "failure", title, text)
    except Exception:
        pass


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


def stats(link_id: int = None, days: int = 30,
          slo_rto_sec: float = None) -> dict:
    """切换作业统计：次数 / 成功率 / RTO 均值与极值 / SLO 达标率 / 趋势序列。

    趋势序列按时间正序返回（前端直接画折线）：
    ``[{id, at, rto, status, direction, link_name}]``。
    SLO 基准取值：显式参数 > 链路 ``failover_config.slo_rto_sec`` > 默认 300 秒。
    """
    ensure_schema()
    days = max(1, min(int(days or 30), 365))
    # 与 db.now_iso() 的 isoformat 前缀对齐（YYYY-MM-DDTHH:MM:SS），字典序可比
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    sql = "SELECT * FROM dr_switch_runs WHERE created_at>=?"
    params = [since]
    if link_id:
        sql += " AND link_id=?"
        params.append(int(link_id))
    sql += " ORDER BY id ASC"
    rows = [_decode(r) for r in db.query(sql, tuple(params))]

    target = slo_rto_sec
    if target is None:
        target = _slo_target(link_id)
    target = float(target or DEFAULT_SLO_RTO_SEC)

    cnt = {"success": 0, "failed": 0, "cancelled": 0, "blocked": 0}
    rto_vals, series = [], []
    for r in rows:
        st = str(r.get("status") or "")
        cnt[st] = cnt.get(st, 0) + 1
        rto = float(r.get("rto_sec") or 0)
        if st == "success" and rto > 0:
            rto_vals.append(rto)
        series.append({
            "id": r.get("id"),
            "at": r.get("created_at") or r.get("started_at") or "",
            "rto": rto,
            "status": st,
            "direction": r.get("direction") or "",
            "link_name": r.get("link_name") or "",
        })
    total = len(rows)
    done = cnt.get("success", 0)
    hit = len([v for v in rto_vals if v <= target])
    return {
        "days": days,
        "total": total,
        "success": done,
        "failed": cnt.get("failed", 0),
        "cancelled": cnt.get("cancelled", 0),
        "blocked": cnt.get("blocked", 0),
        "success_rate": round(done * 100.0 / total, 1) if total else 0.0,
        "avg_rto": round(sum(rto_vals) / len(rto_vals), 1) if rto_vals else 0.0,
        "max_rto": round(max(rto_vals), 1) if rto_vals else 0.0,
        "min_rto": round(min(rto_vals), 1) if rto_vals else 0.0,
        "slo_rto_sec": target,
        "slo_hit_rate": round(hit * 100.0 / done, 1) if done else 0.0,
        "series": series,
    }


def _slo_target(link_id: int = None) -> float:
    """SLO 基准（秒）：链路 ``failover_config.slo_rto_sec`` 优先，其次默认值。"""
    try:
        links = models.list_disaster_links() or []
        if link_id:
            links = [l for l in links if int(l.get("id") or 0) == int(link_id)]
        for lk in links:
            raw = lk.get("failover_config") or ""
            if not str(raw).strip():
                continue
            try:
                cfg = json.loads(raw)
            except Exception:
                continue
            v = cfg.get("slo_rto_sec")
            if v:
                return float(v)
    except Exception:
        pass
    return DEFAULT_SLO_RTO_SEC


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
