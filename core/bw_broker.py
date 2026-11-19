# -*- coding: utf-8 -*-
"""全局带宽仲裁（Max-Min 公平水填充，对标 PB 级方案 §5.1）。

问题
----
此前限速只有**任务级** `bandwidth_limit`：10 个任务各限 100 MB/s 并发跑，
总出口仍是 1 GB/s，出口被打满；反之若把总配额平均硬分，低需求任务会浪费额度。

算法
----
Max-Min 公平分配（带权重）：
  1. 把全局配额按权重等比分配；
  2. 需求低于份额的任务先满足其需求（封顶），释放的额度进入下一轮；
  3. 直到没有任务被削顶，剩余额度留在池中（不强行摊给不需要的任务）。

实现要点
--------
- **默认关闭**：`system_config.global_bandwidth_kbps` 未配置（0）时，本模块
  直接返回任务自身限速，零行为变化。
- 每 ``INTERVAL``（5s）重算一次，避免每次读写片段都做全量计算。
- 活跃任务按 ``last_seen`` 过期（120s）自动剔除，无需显式注销——任务进程
  被强杀也不会留下永久的虚假占用。
"""
import threading
import time

INTERVAL = 5.0          # 重算间隔（秒），对标方案「每 5 秒动态回收」
STALE_SECONDS = 120.0   # 超过该时长未更新活跃标记即视为已结束
MIN_ALLOC_KBPS = 64     # 单任务保底额度，避免被饿死

_LOCK = threading.RLock()
_ACTIVE = {}            # task_id -> {"limit": int, "weight": float, "seen": float, "alloc": int}
_LAST_CALC = 0.0
_LAST_TOTAL = 0


def _global_quota() -> int:
    """全局配额（KB/s）；未配置/非法返回 0（表示不启用仲裁）。"""
    try:
        from core import db
        return max(0, int(db.get_system_config("global_bandwidth_kbps") or 0))
    except Exception:
        return 0


def water_fill(total_kbps: int, demands: dict, weights: dict = None) -> dict:
    """Max-Min 公平水填充。

    ``demands``: {key: 需求 KB/s 或 None（表示「有多少用多少」）}
    ``weights``: {key: 权重}，缺省 1.0

    返回 {key: 分配 KB/s}。需求为 None 的 key 参与分剩余额度。
    """
    weights = weights or {}
    result = {k: 0 for k in demands}
    if total_kbps <= 0 or not demands:
        return result

    remaining = float(total_kbps)
    active = set(demands)
    # 第一轮：需求有限且低于等比份额的，直接封顶满足
    while active and remaining > 0:
        wsum = sum(max(0.0, float(weights.get(k, 1.0))) for k in active) or float(len(active))
        capped = None
        for k in list(active):
            d = demands[k]
            if d is None:
                continue
            share = remaining * max(0.0, float(weights.get(k, 1.0))) / wsum
            if d < share:
                result[k] = int(d)
                remaining -= d
                active.discard(k)
                capped = k
                break
        if capped is None:
            break
    # 第二轮：剩下的按权重等比分配（含无限需求者）
    if active and remaining > 0:
        wsum = sum(max(0.0, float(weights.get(k, 1.0))) for k in active) or float(len(active))
        for k in active:
            result[k] = int(remaining * max(0.0, float(weights.get(k, 1.0))) / wsum)
    return result


def _recompute(now: float, total: int):
    global _LAST_CALC, _LAST_TOTAL
    dead = [k for k, v in _ACTIVE.items() if now - v["seen"] > STALE_SECONDS]
    for k in dead:
        _ACTIVE.pop(k, None)

    if total <= 0:
        for v in _ACTIVE.values():
            v["alloc"] = v["limit"]
    else:
        demands = {k: (v["limit"] if v["limit"] > 0 else None) for k, v in _ACTIVE.items()}
        weights = {k: v["weight"] for k, v in _ACTIVE.items()}
        alloc = water_fill(total, demands, weights)
        for k, v in _ACTIVE.items():
            a = int(alloc.get(k) or 0)
            if a and a < MIN_ALLOC_KBPS:
                a = MIN_ALLOC_KBPS
            v["alloc"] = a
    _LAST_CALC = now
    _LAST_TOTAL = total


def effective_limit(task_id, task_limit_kbps: int, weight: float = 1.0) -> int:
    """返回该任务本次应使用的限速（KB/s），0 表示不限。

    - 未配置全局配额 → 原样返回 ``task_limit_kbps``（零行为变化）；
    - 已配置 → 返回仲裁后的额度（可能小于任务自身配置）。
    """
    total = _global_quota()
    if total <= 0:
        return int(task_limit_kbps or 0)
    now = time.time()
    tid = int(task_id or 0)
    with _LOCK:
        existed = tid in _ACTIVE
        _ACTIVE[tid] = {
            "limit": int(task_limit_kbps or 0),
            "weight": float(weight or 1.0),
            "seen": now,
            "alloc": _ACTIVE.get(tid, {}).get("alloc", 0),
        }
        # 新任务加入必须立刻重算一次，否则要等下一个 5s 周期才拿到额度
        if (now - _LAST_CALC >= INTERVAL or _LAST_TOTAL != total
                or not existed or not _ACTIVE[tid].get("alloc")):
            _recompute(now, total)
        return int(_ACTIVE[tid].get("alloc") or 0)


def snapshot() -> dict:
    """当前仲裁状态（供页面/诊断使用）。"""
    total = _global_quota()
    now = time.time()
    with _LOCK:
        if now - _LAST_CALC >= INTERVAL or _LAST_TOTAL != total:
            _recompute(now, total)
        rows = [{"task_id": k, "task_limit_kbps": v["limit"],
                 "allocated_kbps": v["alloc"],
                 "idle_sec": round(now - v["seen"], 1)}
                for k, v in sorted(_ACTIVE.items())]
    return {
        "enabled": total > 0,
        "global_quota_kbps": total,
        "interval_sec": INTERVAL,
        "active": rows,
        "allocated_sum_kbps": sum(r["allocated_kbps"] for r in rows),
        "note": ("全局配额已启用，按权重 Max-Min 公平分配；需求低于份额的任务先满足，"
                 "剩余额度留在池中" if total > 0
                 else "未配置 global_bandwidth_kbps，按任务各自 bandwidth_limit 限速"),
    }


def reset():
    """清空状态（测试用）。"""
    global _LAST_CALC, _LAST_TOTAL
    with _LOCK:
        _ACTIVE.clear()
        _LAST_CALC = 0.0
        _LAST_TOTAL = 0
