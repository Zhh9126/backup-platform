# -*- coding: utf-8 -*-
"""容灾站点拓扑、候选备机池与 RTO/RPO 目标驱动。

对标来源：航天云网 V5.0 白皮书（分布式节点/任务重调度）、英方 i2Availability
（多对多池化集群按优先级多级接管）。

设计原则（如实）：
- 站点是**配置实体**，不假装有节点代理；多级接管是**编排能力**，真实切换仍走
  dr_failover 的 MySQL 主从通道，候选池只决定"下一个尝试哪台备机"。
- 目标（RTO/RPO）来自链路配置，**驱动预检风险与结果判定**，不承诺物理 RTO。
- 所有表懒建（ensure_schema），兼容老库升级，不侵入 core/db.py 的大 DDL 字符串。
"""
import json
import time

from core import db

_SCHEMA_READY = False

_DDL_SITES = """
CREATE TABLE IF NOT EXISTS dr_sites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    code          TEXT DEFAULT '',
    site_role     TEXT DEFAULT 'standby',   -- primary | standby | tertiary | witness
    site_type     TEXT DEFAULT 'datacenter',-- datacenter | cloud | branch | colocation
    city          TEXT DEFAULT '',
    region        TEXT DEFAULT '',          -- 自由地域标签（华北大区等）
    lat           REAL,
    lon           REAL,
    host          TEXT DEFAULT '',
    enabled       INTEGER DEFAULT 1,
    note          TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT
)
"""

_DDL_CANDIDATES = """
CREATE TABLE IF NOT EXISTS dr_failover_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id       INTEGER NOT NULL,
    site_id       INTEGER,
    name          TEXT DEFAULT '',
    host          TEXT NOT NULL,
    port          INTEGER DEFAULT 3306,
    username      TEXT DEFAULT '',
    password      TEXT DEFAULT '',          -- db.encrypt_secret 密文
    priority      INTEGER DEFAULT 100,      -- 数字小者优先
    enabled       INTEGER DEFAULT 1,
    note          TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT
)
"""

_SITE_FIELDS = ("name", "code", "site_role", "site_type", "city", "region",
                "lat", "lon", "host", "enabled", "note")
_CAND_FIELDS = ("id", "link_id", "site_id", "name", "host", "port", "username",
                "password", "priority", "enabled", "note")
_SITE_ROLES = ("primary", "standby", "tertiary", "witness")
_SITE_TYPES = ("datacenter", "cloud", "branch", "colocation")


def ensure_schema():
    """懒建表 + 老库迁移（重复调用幂等）。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    db.execute(_DDL_SITES)
    db.execute(_DDL_CANDIDATES)
    db.execute("CREATE INDEX IF NOT EXISTS idx_dr_sites_role ON dr_sites(site_role)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_dr_cand_link ON dr_failover_candidates(link_id, priority)")
    # 目标列：链路表加 RTO/RPO 目标（老库升级兜底）
    for col, typedef in (("rto_target_sec", "REAL"), ("rpo_target_min", "REAL")):
        try:
            db.execute("ALTER TABLE disaster_links ADD COLUMN %s %s" % (col, typedef))
        except Exception:
            pass
    _SCHEMA_READY = True


def _num(v, default=None, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _bool(v):
    return 1 if v not in (0, False, "0", "false", "False", None, "") else 0


def _json(v):
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


# ------------------------------------------------------------------ 站点 CRUD
def list_sites() -> list:
    ensure_schema()
    return db.query("SELECT * FROM dr_sites ORDER BY id")


def get_site(site_id: int):
    ensure_schema()
    return db.query_one("SELECT * FROM dr_sites WHERE id=?", (int(site_id),))


def create_site(data: dict) -> int:
    ensure_schema()
    d = {k: data.get(k) for k in _SITE_FIELDS}
    if not d.get("name"):
        raise ValueError("站点名称必填")
    d["site_role"] = d.get("site_role") if d.get("site_role") in _SITE_ROLES else "standby"
    d["site_type"] = d.get("site_type") if d.get("site_type") in _SITE_TYPES else "datacenter"
    d["lat"] = _num(d.get("lat"))
    d["lon"] = _num(d.get("lon"))
    d["enabled"] = _bool(d["enabled"]) if "enabled" in d else 1
    now = db.now_iso()
    d["created_at"] = now
    d["updated_at"] = now
    cols = list(d.keys())
    return db.execute("INSERT INTO dr_sites ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols))), tuple(d.values()))


def update_site(site_id: int, data: dict) -> bool:
    ensure_schema()
    if not get_site(site_id):
        return False
    d = {k: data.get(k) for k in _SITE_FIELDS if k in data}
    if not d:
        return False
    if "site_role" in d and d["site_role"] not in _SITE_ROLES:
        d["site_role"] = "standby"
    if "site_type" in d and d["site_type"] not in _SITE_TYPES:
        d["site_type"] = "datacenter"
    for k in ("lat", "lon"):
        if k in d:
            d[k] = _num(d[k])
    if "enabled" in d:
        d["enabled"] = _bool(d["enabled"])
    d["updated_at"] = db.now_iso()
    cols = list(d.keys())
    db.execute("UPDATE dr_sites SET {} WHERE id=?".format(
        ",".join("%s=?" % c for c in cols)), tuple(d.values()) + (int(site_id),))
    return True


def delete_site(site_id: int) -> dict:
    ensure_schema()
    used = db.query_one(
        "SELECT COUNT(*) AS n FROM dr_failover_candidates WHERE site_id=?", (int(site_id),)) or {}
    if int(used.get("n") or 0) > 0:
        return {"ok": False, "error": "该站点已被候选备机引用，请先解除引用"}
    db.execute("DELETE FROM dr_sites WHERE id=?", (int(site_id),))
    return {"ok": True}


# --------------------------------------------------------------- 候选备机池
def list_candidates(link_id: int) -> list:
    ensure_schema()
    rows = db.query(
        "SELECT * FROM dr_failover_candidates WHERE link_id=? "
        "ORDER BY priority, id", (int(link_id),))
    return [{**r, "has_password": bool(r.get("password")),
             "password": "***" if r.get("password") else ""} for r in rows]


def get_candidate(cid: int):
    ensure_schema()
    row = db.query_one("SELECT * FROM dr_failover_candidates WHERE id=?", (int(cid),))
    if not row:
        return None
    return {**row, "has_password": bool(row.get("password")),
            "password": "***" if row.get("password") else ""}


def upsert_candidate(data: dict) -> int:
    ensure_schema()
    d = {k: data.get(k) for k in _CAND_FIELDS if k in data}
    if not d.get("link_id"):
        raise ValueError("link_id 必填")
    if not d.get("host"):
        raise ValueError("备机 host 必填")
    d["link_id"] = int(d["link_id"])
    d["port"] = int(_num(d.get("port"), 3306, int))
    d["priority"] = int(_num(d.get("priority"), 100, int))
    d["enabled"] = _bool(d["enabled"]) if "enabled" in d else 1
    pw = d.get("password")
    if pw and pw != "***":
        d["password"] = db.encrypt_secret(str(pw))
    else:
        d.pop("password", None)  # 空/掩码表示保留原密码
    now = db.now_iso()
    d["updated_at"] = now
    cid = d.pop("id", None)
    if cid:
        cols = list(d.keys())
        db.execute("UPDATE dr_failover_candidates SET {} WHERE id=?".format(
            ",".join("%s=?" % c for c in cols)), tuple(d.values()) + (int(cid),))
        return int(cid)
    d["created_at"] = now
    cols = list(d.keys())
    return db.execute("INSERT INTO dr_failover_candidates ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols))), tuple(d.values()))


def delete_candidate(cid: int) -> bool:
    ensure_schema()
    db.execute("DELETE FROM dr_failover_candidates WHERE id=?", (int(cid),))
    return True


def decrypt_candidate(row: dict) -> dict:
    """给切换引擎使用：把密文密码还原为明文。"""
    out = dict(row or {})
    pw = out.get("password") or ""
    if pw:
        try:
            out["password"] = db.decrypt_secret(pw)
        except Exception:
            out["password"] = ""
    return out


def candidate_conn(row: dict) -> dict:
    """把候选行转成 dr_failover 期望的 conn 配置。"""
    r = decrypt_candidate(row)
    return {"host": r.get("host"), "port": r.get("port"),
            "user": r.get("username") or "", "password": r.get("password") or ""}


# ------------------------------------------------------- 候选池健康与多级接管
def probe_candidate(row: dict, check_replica: bool = True) -> dict:
    """候选备机健康探测：可达性 +（可选）复制角色/延迟。

    只探测、不改动任何数据库状态；失败如实返回 reason，不伪造可用。
    """
    from core import dr_failover as fo
    conn = candidate_conn(row)
    result = {"id": row.get("id"), "name": row.get("name") or conn["host"],
              "host": conn["host"], "port": conn["port"],
              "priority": row.get("priority"), "healthy": False, "reason": ""}
    try:
        probe = fo._probe_mysql(conn["host"], conn["port"], conn["user"], conn["password"], timeout=4)
    except Exception as e:
        result["reason"] = "探测异常: %s" % str(e)[:120]
        return result
    if not probe.get("reachable"):
        result["reason"] = probe.get("error") or "不可达"
        return result
    result["healthy"] = True
    if check_replica:
        try:
            st = fo._replica_status(conn["host"], conn["port"], conn["user"], conn["password"])
            result["is_replica"] = bool(st.get("is_replica"))
            result["behind"] = st.get("behind")
            result["io"] = st.get("io")
            result["sql"] = st.get("sql")
            # 具备接管条件 = 有复制角色 + IO/SQL 双 Yes + 延迟在阈值内
            # （对标英方"优先接管条件"：第 1 台不具备则尝试第 2 台）
            io_sql_ok = (st.get("io") == "Yes" and st.get("sql") == "Yes")
            behind = _num(st.get("behind"), None, int)
            if not result["is_replica"]:
                result["healthy"] = False
                result["reason"] = "当前不是复制角色（可能已是主库）"
            elif not io_sql_ok:
                result["healthy"] = False
                result["reason"] = "复制线程异常 IO=%s SQL=%s（不具备接管条件）" % (
                    st.get("io"), st.get("sql"))
            elif behind is None:
                result["healthy"] = False
                result["reason"] = "复制延迟未知，无法判定接管条件"
            elif behind > 30:
                result["healthy"] = False
                result["reason"] = "复制延迟 %s 秒，超过接管阈值 30s" % behind
        except Exception as e:
            # 探测异常 = 状态未知，绝不能沿用前面置的 healthy=True
            # （"未知"被当"可接管"会切到丢数据的节点，P1-7）。
            result["healthy"] = False
            result["reason"] = "复制状态探测失败: %s" % str(e)[:120]
    return result


def ranked_candidates(link_id: int, check_replica: bool = True) -> dict:
    """按优先级返回候选池健康排序，供预检与多级接管使用。

    返回 {candidates:[{...healthy,reason}], usable:[...], skipped:[...]}
    """
    ensure_schema()
    rows = db.query(
        "SELECT * FROM dr_failover_candidates WHERE link_id=? AND enabled=1 "
        "ORDER BY priority, id", (int(link_id),))
    cands, usable = [], []
    for r in rows:
        p = probe_candidate(r, check_replica=check_replica)
        cands.append(p)
        if p.get("healthy"):
            usable.append(p)
    return {"candidates": cands, "usable": usable,
            "skipped": [c for c in cands if not c.get("healthy")]}


# --------------------------------------------------------------- 目标驱动
def get_targets(link_id: int) -> dict:
    ensure_schema()
    row = db.query_one(
        "SELECT rto_target_sec, rpo_target_min FROM disaster_links WHERE id=?", (int(link_id),)) or {}
    cfg = {}
    link = db.query_one("SELECT failover_config FROM disaster_links WHERE id=?", (int(link_id),)) or {}
    try:
        raw = link.get("failover_config") or "{}"
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "rto_target_sec": _num(row.get("rto_target_sec"),
                               _num(cfg.get("slo_rto_sec"), 300.0)),
        "rpo_target_min": _num(row.get("rpo_target_min"), None),
        "source": "link_column" if row.get("rto_target_sec") is not None else "config_default",
    }


def set_targets(link_id: int, rto_target_sec=None, rpo_target_min=None) -> dict:
    ensure_schema()
    updates = {}
    if rto_target_sec is not None:
        v = _num(rto_target_sec)
        if v is None or v <= 0:
            raise ValueError("rto_target_sec 必须为正数")
        updates["rto_target_sec"] = v
    if rpo_target_min is not None:
        v = _num(rpo_target_min)
        if v is None or v < 0:
            raise ValueError("rpo_target_min 必须为非负数")
        updates["rpo_target_min"] = v
    if not updates:
        return get_targets(link_id)
    updates["updated_at"] = db.now_iso()
    cols = list(updates.keys())
    db.execute("UPDATE disaster_links SET {} WHERE id=?".format(
        ",".join("%s=?" % c for c in cols)), tuple(updates.values()) + (int(link_id),))
    return get_targets(link_id)


def judge_rto(rto_sec, target_sec) -> dict:
    """实测 RTO 与目标比对；无实测值时如实 unknown，不判达标。"""
    if rto_sec is None:
        return {"met": None, "status": "unknown", "reason": "无实测 RTO"}
    if target_sec is None:
        return {"met": None, "status": "unknown", "reason": "未配置 RTO 目标"}
    rto, target = _num(rto_sec), _num(target_sec)
    if rto is None or target is None:
        return {"met": None, "status": "unknown", "reason": "RTO 数值非法"}
    return {"met": rto <= target, "status": "met" if rto <= target else "breached",
            "actual_sec": rto, "target_sec": target}


def judge_rpo(behind_sec, target_min) -> dict:
    """复制延迟（秒）与 RPO 目标（分钟）比对。目标为 0 表示近实时，>30s 延迟即不达标。"""
    if target_min is None:
        return {"met": None, "status": "unknown", "reason": "未配置 RPO 目标"}
    target, behind = _num(target_min), _num(behind_sec)
    if target is None or behind is None:
        return {"met": None, "status": "unknown", "reason": "RPO 数值非法"}
    limit_sec = 30.0 if target == 0 else target * 60.0
    met = behind <= limit_sec
    return {"met": met, "status": "met" if met else "breached",
            "behind_sec": behind, "target_min": target, "limit_sec": limit_sec}


# --------------------------------------------------------------- 就绪度聚合
def readiness(link_id: int = None, light: bool = False) -> dict:
    """容灾就绪度：站点/候选池/目标/演练与巡检终态，只报真实探测结果。

    light=True 跳过候选池逐个探测（与 dr_overview 的 ?light=1 语义一致）。
    """
    from core import models
    ensure_schema()
    links = ([models.get_disaster_link(int(link_id))] if link_id
             else models.list_disaster_links())
    links = [l for l in links if l]
    now = db.now_iso()
    items = []
    for link in links:
        lid = link.get("id")
        targets = get_targets(lid)
        cands = ({"candidates": [], "usable": [], "skipped": []} if light
                 else ranked_candidates(lid, check_replica=True))
        sites = db.query(
            "SELECT s.id,s.name,s.site_role,s.city FROM dr_sites s "
            "JOIN dr_failover_candidates c ON c.site_id=s.id WHERE c.link_id=? "
            "GROUP BY s.id", (lid,))
        items.append({
            "link_id": lid,
            "link_name": link.get("name"),
            "status": link.get("status"),
            "enabled": link.get("enabled"),
            "sites": sites,
            "candidates_total": len(cands.get("candidates") or []),
            "candidates_usable": len(cands.get("usable") or []),
            "candidates_skipped": cands.get("skipped") or [],
            "targets": targets,
            "consistency": link.get("consistency_result") or "unknown",
            "skipped_probe": bool(light),
            "probe_basis": "skipped" if light else "reachable+replica_io_sql+lag",
        })
    total = len(items)
    return {
        "generated_at": now,
        "total_links": total,
        "enabled_links": sum(1 for i in items if i.get("enabled")),
        "with_candidates": sum(1 for i in items if i.get("candidates_total")),
        "with_targets": sum(1 for i in items if i.get("targets", {}).get("rto_target_sec")),
        "items": items,
        "honest_note": "站点为配置实体、候选池为编排能力；探测失败如实标记，不代表生产可切换性承诺",
    }
