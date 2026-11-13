# -*- coding: utf-8 -*-
"""运行实例注册表：让「启动自愈」在多 worker / 多容器部署下不再互杀。

单进程部署时，启动时把遗留的 ``running`` 记录标记失败是安全的；但
``gunicorn -w 4`` 或多个容器共享同一个元数据库时，B 进程启动会把 A 进程
**正在跑**的备份/恢复误判为僵尸并标记失败（用户看到"任务被中断"）。

本模块用元数据库里的一条轻量心跳记录区分「还活着的 worker」与「已消亡的
worker」：

* 每个进程启动时 ``register()`` 登记 ``first_seen``（只在首次写入）；
* 调度器周期任务每 20s ``heartbeat()`` 刷新 ``last_beat``；
* 启动自愈调用 ``alive_workers()`` 拿到存活集合与最早的 ``first_seen``，
  只修正「开始时间早于所有存活 worker 首次登记时间」的记录。

单进程场景下存活集合只有自己、``first_seen`` 就是本次启动时间，因此所有
历史遗留记录都早于它——行为与原来"全部修正"完全一致。
"""
import json
import os
import socket
import time
from datetime import datetime

import core.db as db

_KEY = "runtime_workers"
TTL_SEC = 90          # 心跳有效期：20s 一次心跳，90s 未更新视为消亡
RUNNER_ID = "%s:%d:%d" % (socket.gethostname(), os.getpid(), int(time.time()))


def _load() -> dict:
    try:
        raw = db.get_system_config(_KEY)
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    try:
        db.set_system_config(_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def register() -> str:
    """登记本进程；``first_seen`` 只在首次登记时写入（重启即新 runner）。"""
    data = _load()
    now = time.time()
    entry = data.get(RUNNER_ID)
    if not isinstance(entry, dict):
        entry = {}
    entry["first_seen"] = float(entry.get("first_seen") or now)
    entry["last_beat"] = now
    entry["pid"] = os.getpid()
    entry["host"] = socket.gethostname()
    data[RUNNER_ID] = entry
    _save(data)
    return RUNNER_ID


def heartbeat() -> None:
    """刷新本进程心跳（调度器 20s 周期任务调用；顺带清理过期条目）。"""
    data = _load()
    now = time.time()
    entry = data.get(RUNNER_ID)
    if not isinstance(entry, dict):
        entry = {"first_seen": now}
    entry["last_beat"] = now
    entry["pid"] = os.getpid()
    entry["host"] = socket.gethostname()
    data[RUNNER_ID] = entry
    for rid in list(data.keys()):
        if rid == RUNNER_ID:
            continue
        try:
            beat = float((data.get(rid) or {}).get("last_beat") or 0)
        except (TypeError, ValueError):
            beat = 0.0
        if now - beat > TTL_SEC * 10:      # 15 分钟未更新才清理，避免误删
            data.pop(rid, None)
    _save(data)


def alive_workers(ttl: int = TTL_SEC) -> dict:
    """存活的 worker：``{runner_id: first_seen_ts}``（含本进程）。"""
    now = time.time()
    out = {}
    for rid, entry in _load().items():
        if not isinstance(entry, dict):
            continue
        try:
            beat = float(entry.get("last_beat") or 0)
            first = float(entry.get("first_seen") or beat)
        except (TypeError, ValueError):
            continue
        if rid == RUNNER_ID or (now - beat) <= ttl:
            out[rid] = first
    return out


def alive_since() -> float:
    """所有存活 worker 中最早的登记时间（epoch 秒）；无信息时返回 0。"""
    vals = list(alive_workers().values())
    return min(vals) if vals else 0.0


def parse_started(value) -> float:
    """把 DB 里的时间戳（naive / ISO / 带时区）解析成 epoch 秒，失败返回 0。"""
    s = str(value or "").strip()
    if not s:
        return 0.0
    s2 = s.replace("Z", "+00:00")
    for attempt in (s2, s2[:19]):
        try:
            dt = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    return 0.0
