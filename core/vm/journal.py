# -*- coding: utf-8 -*-
"""恢复点链（Journal）管理：增量链解析 + 下一次备份级别决策。

这个模块刻意做成**纯函数式的数据库/时间运算**（不碰网络、不碰 Hypervisor），
因此可以单测覆盖——这正是备份产品最容易出错、也最该被测试的部分：
「该做全量还是增量」「恢复到某个时间点需要哪些产物」。

业界参照
--------
* Veeam 永久增量（forever-incremental）+ 周期性合成全量（synthetic full）：
  平时只搬脏块，按计划**在备份仓库侧**（而非生产侧）把增量链合成一份新的
  全量，既获得全量的恢复速度，又不占用生产存储的 IOPS。
* Rubrik SLA Domain：用「RPO 目标 + 频率」声明期望，系统据此自动编排
  全量/增量节奏并对**偏离 RPO**告警，而不是让用户手算备份计划。
* Zerto Journal：日志结构化的恢复点，任意秒级时间点可回放。
"""
from datetime import datetime, timedelta
from typing import List, Optional

# 默认策略（可被 extra_config.policy 覆盖）
DEFAULT_POLICY = {
    "full_interval_days": 7,       # 强制全量周期（链式安全的兜底）
    "max_increments": 30,          # 增量链最大长度，超过后安排合成全量
    "synthetic_full_enabled": True,  # 是否启用仓库侧合成全量
    "synthetic_interval_days": 1,  # 合成全量周期
    "retention_days": 30,
    "retention_count": 60,
}


def merge_policy(extra_config) -> dict:
    """合并 extra_config.policy 与默认策略。"""
    import json
    pol = dict(DEFAULT_POLICY)
    raw = extra_config
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if isinstance(raw, dict):
        cfg = raw.get("policy")
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if v is not None:
                    pol[k] = v
    return pol


# ---------------- 时间工具 ----------------
def _as_naive_local(dt: Optional[datetime]) -> Optional[datetime]:
    """把可能带时区偏移的时间统一折算成「本地 naive 时间」。

    为什么必须做：平台时间戳由 db.now_iso() 产生，天然带时区
    （形如 '2026-09-16T13:51:20+08:00'）；而本模块的比较基准是
    datetime.now()（naive）。二者直接相减会抛
    "can't subtract offset-naive and offset-aware datetimes"，
    导致**第二次及以后的备份决策/到期清理全线崩溃**。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone().replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)


def parse_dt(s: str) -> Optional[datetime]:
    """宽容解析 'YYYY-MM-DD HH:MM:SS' / ISO8601（带不带时区都行）。

    返回值**一律为本地 naive datetime**，可与 datetime.now() 直接相减。
    解析失败返回 None。
    """
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    try:
        return _as_naive_local(datetime.fromisoformat(s))
    except Exception:
        return None


# ---------------- 恢复点链 ----------------
def resolve_chain(rows: List[dict], target_rp_id: int) -> List[dict]:
    """从目标恢复点回溯到链首全量，返回有序列表 [full, inc1, ..., target]。

    Args:
        rows: 该 VM 的全部恢复点行（含 id / rp_type / parent_rp_id / pit_at）
        target_rp_id: 目标恢复点 id

    Raises:
        ValueError: 链断裂（找不到父节点 / 找不到全量根 / 存在环）
    """
    by_id = {int(r["id"]): r for r in rows}
    if int(target_rp_id) not in by_id:
        raise ValueError("恢复点 %s 不存在" % target_rp_id)

    chain = []
    seen = set()
    cur = by_id[int(target_rp_id)]
    while True:
        cid = int(cur["id"])
        if cid in seen:
            raise ValueError("恢复点链存在环：%s 重复出现" % cid)
        seen.add(cid)
        chain.append(cur)
        parent = cur.get("parent_rp_id")
        if not parent:
            break
        if int(parent) not in by_id:
            raise ValueError("恢复点链断裂：%s 的父节点 %s 不存在（可能已被清理）"
                             % (cid, parent))
        cur = by_id[int(parent)]
    chain.reverse()

    # 链首必须是可用的全量基座
    root = chain[0]
    kind = (root.get("rp_type") or "full")
    if kind == "incremental":
        raise ValueError("恢复点链缺少全量基座（最早节点 %s 是增量）" % root["id"])
    return chain


def chain_artifacts(chain: List[dict]) -> List[str]:
    """取出链上每个恢复点的落盘产物路径（顺序即恢复顺序）。"""
    paths = []
    for rp in chain:
        p = rp.get("artifact_path") or ""
        if not p:
            raise ValueError("恢复点 %s 没有落盘产物，无法恢复" % rp["id"])
        paths.append(p)
    return paths


def increment_count_since_full(chain: List[dict]) -> int:
    """链上全量之后的增量个数。"""
    return max(0, len(chain) - 1)


def last_full_in_chain(rows: List[dict]) -> Optional[dict]:
    """最近一次全量（含合成全量）恢复点。"""
    cands = [r for r in rows if (r.get("rp_type") or "full") != "incremental"]
    if not cands:
        return None
    cands.sort(key=lambda r: (r.get("pit_at") or "", int(r.get("id") or 0)))
    return cands[-1]


def find_rp_by_time(rows: List[dict], target_time: str) -> Optional[dict]:
    """按 PITR 目标时间挑选恢复点：<= 目标时刻的最后一个恢复点。"""
    t = parse_dt(target_time)
    if not t:
        return None
    cands = []
    for r in rows:
        pt = parse_dt(r.get("pit_at") or "")
        if pt and pt <= t:
            cands.append((pt, int(r.get("id") or 0), r))
    if not cands:
        return None
    cands.sort()
    return cands[-1][2]


# ---------------- 备份级别决策 ----------------
def plan_next(rows: List[dict], caps: dict, policy: dict = None,
              now: datetime = None) -> dict:
    """决策下一次应该执行的备份级别。

    Returns:
        {"level": "full"|"incremental", "reason": str, "after_synth": bool}

    决策优先级：
    1. Provider 不支持块级增量 → 永远全量（并如实说明原因，不做假增量）；
    2. 没有任何恢复点 → 全量（首次必须打基座）；
    3. 距离最近全量超过 full_interval_days → 全量（防长链风险）；
    4. 增量链长度达到 max_increments：
       a. 支持合成 → 先在仓库侧合成全量再继续做增量（after_synth=True）
       b. 不支持合成 → 退化为重新全量；
    5. 合成全量到期（距上次合成 >= synthetic_interval_days 且存在链）→ 同上；
    6. 其余 → 增量。
    """
    pol = dict(DEFAULT_POLICY)
    pol.update(policy or {})
    now = now or datetime.now()
    rows = rows or []
    incremental_ok = bool((caps or {}).get("incremental"))

    if not incremental_ok:
        full_days = int(pol.get("full_interval_days") or 0)
        if rows and full_days <= 0:
            return {"level": "full", "reason": "该平台不支持块级增量，每次均为全量",
                    "after_synth": False}
        return {"level": "full",
                "reason": "该平台不支持块级增量（无 CBT/dirty bitmap），按全量执行",
                "after_synth": False}
    if not rows:
        return {"level": "full", "reason": "首次保护：建立全量基座",
                "after_synth": False}

    full = last_full_in_chain(rows)
    if not full:
        return {"level": "full", "reason": "尚无全量基座，先建立全量",
                "after_synth": False}

    full_t = parse_dt(full.get("pit_at") or "")
    full_days = int(pol.get("full_interval_days") or 0)
    if full_t and full_days > 0 and (now - full_t) > timedelta(days=full_days):
        return {"level": "full",
                "reason": "距上次全量 %.1f 天，超过强制全量周期 %d 天"
                          % ((now - full_t).total_seconds() / 86400.0, full_days),
                "after_synth": False}

    # 当前增量链长度
    chain_len = 0
    cur = full
    by_id = {int(r["id"]): r for r in rows}
    # 找到以本次全量为根的最新链：统计该全量之后、按时间排在其后的增量数
    ft = parse_dt(full.get("pit_at") or "")
    for r in rows:
        rt = parse_dt(r.get("pit_at") or "")
        if (r.get("rp_type") or "") == "incremental" and rt and ft and rt > ft:
            chain_len += 1
    _ = cur, by_id
    max_inc = int(pol.get("max_increments") or 0)
    if max_inc > 0 and chain_len >= max_inc:
        if pol.get("synthetic_full_enabled"):
            return {"level": "incremental",
                    "reason": "增量链已达 %d 个（上限 %d），先合成全量再继续增量"
                              % (chain_len, max_inc),
                    "after_synth": True}
        return {"level": "full",
                "reason": "增量链已达 %d 个（上限 %d）且不支持合成全量，重新全量"
                          % (chain_len, max_inc),
                "after_synth": False}

    synth_days = float(pol.get("synthetic_interval_days") or 0)
    if pol.get("synthetic_full_enabled") and synth_days > 0 and chain_len > 0:
        last_synth = None
        for r in rows:
            if (r.get("rp_type") or "") == "synthetic_full":
                t = parse_dt(r.get("pit_at") or "")
                if t and (last_synth is None or t > last_synth):
                    last_synth = t
        ref = last_synth or ft
        if ref and (now - ref) > timedelta(days=synth_days):
            return {"level": "incremental",
                    "reason": "合成全量周期到期（%.1f 天），先合成全量再继续增量"
                              % ((now - ref).total_seconds() / 86400.0),
                    "after_synth": True}

    return {"level": "incremental",
            "reason": "距上次全量 %.1f 天，增量链 %d 个，执行块级增量"
                      % (((now - ft).total_seconds() / 86400.0) if ft else 0.0,
                         chain_len),
            "after_synth": False}


# ---------------- RPO / 合规 ----------------
def current_rpo_minutes(rows: List[dict], now: datetime = None) -> Optional[float]:
    """当前实际 RPO（距最近恢复点的分钟数）；无恢复点返回 None。"""
    now = now or datetime.now()
    latest = None
    for r in rows or []:
        t = parse_dt(r.get("pit_at") or "")
        if t and (latest is None or t > latest):
            latest = t
    if not latest:
        return None
    return (now - latest).total_seconds() / 60.0


def rpo_state(rows: List[dict], rpo_target_min: int, now: datetime = None) -> dict:
    """RPO 达成情况（Rubrik SLA 思路：偏离目标即告警）。

    Returns: {"minutes": float|None, "target": int, "ok": bool, "message": str}
    """
    minutes = current_rpo_minutes(rows, now)
    if minutes is None:
        return {"minutes": None, "target": int(rpo_target_min or 0),
                "ok": False, "message": "尚无恢复点"}
    ok = minutes <= float(rpo_target_min or 0)
    return {
        "minutes": round(minutes, 1), "target": int(rpo_target_min or 0), "ok": ok,
        "message": ("当前 RPO %.1f 分钟，达标（目标 %d 分钟）"
                    % (minutes, rpo_target_min)) if ok
                   else ("当前 RPO %.1f 分钟，超出目标 %d 分钟" % (minutes, rpo_target_min)),
    }


# ---------------- 保留策略 ----------------
def plan_expiry(rows: List[dict], policy: dict = None,
                now: datetime = None) -> List[dict]:
    """按「保留天数 + 保留个数」挑出应到期的恢复点。

    安全约束：**绝不删除仍在被依赖的恢复点**——若某恢复点被清理，其后增量链
    会因缺少父节点而无法恢复，所以本函数按链（以全量为单位）整体判断，
    只有当整条链最早端超出保留范围时才卸载整条链。
    """
    pol = dict(DEFAULT_POLICY)
    pol.update(policy or {})
    now = now or datetime.now()
    days = float(pol.get("retention_days") or 0)
    count = int(pol.get("retention_count") or 0)
    rows = sorted(rows or [], key=lambda r: (parse_dt(r.get("pit_at") or "")
                                             or datetime.min, int(r.get("id") or 0)))
    if len(rows) <= max(count, 0):
        expired = []
    else:
        expired = rows[:len(rows) - count] if count > 0 else []
    out = []
    for r in rows:
        t = parse_dt(r.get("pit_at") or "")
        if days > 0 and t and (now - t) > timedelta(days=days):
            out.append(r)
        elif r in expired and r not in out:
            out.append(r)
    # 去重保序
    seen, uniq = set(), []
    for r in out:
        if int(r["id"]) not in seen:
            seen.add(int(r["id"]))
            uniq.append(r)
    return uniq
