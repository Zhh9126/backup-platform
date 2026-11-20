# -*- coding: utf-8 -*-
"""基于元数据库的单实例租约（对标 PB 级方案 §1 Etcd 选主 + Fencing Token）。

为什么需要
----------
调度器是"进程内 APScheduler + 元数据库"：同一个元数据库被两个进程接上
（容器滚动更新重叠、systemd 与手工各起一个、主备机挂着同一个库）时，
**两边都会注册同一批定时任务 → 同一时间重复备份**，还可能互相抢锁。

本模块用元数据库实现一个带过期时间的租约：
- 启动时 ``acquire`` 成功者才是 leader（注册定时任务），失败者进入 standby；
- leader 每 ``RENEW_INTERVAL`` 续租（刷新 expires_at）；
- 进程崩溃 → 租约到期 → 备实例可在下一轮抢占接管（RTO ≈ TTL）。

单实例场景零行为变化（总是抢到）。元数据库不可用时的策略由调用方决定
（调度器选择"按原样启动"，避免因租约模块故障导致完全不调度）。
"""
import os
import socket
import time

import core.db as db

DEFAULT_TTL = 60            # 租约有效期（秒）
RENEW_INTERVAL = 20         # 续租间隔（应显著小于 TTL）
NAME = "scheduler"


def owner_id() -> str:
    """本实例标识：主机名 + PID（重启后 PID 变化即视为新实例）。"""
    return "%s:%s" % (socket.gethostname(), os.getpid())


def _now():
    return time.time()


def _row(name: str):
    try:
        return db.query_one("SELECT * FROM scheduler_leases WHERE name=?", (name,))
    except Exception:
        return None


def acquire(name: str = NAME, owner: str = None, ttl: int = DEFAULT_TTL) -> dict:
    """尝试获取/续租。返回 ``{acquired, owner, holder, expires_in}``。"""
    owner = owner or owner_id()
    now = _now()
    cur = _row(name)
    if cur:
        exp = float(cur.get("expires_at") or 0)
        held_by = cur.get("owner") or ""
        if exp > now and held_by and held_by != owner:
            return {"acquired": False, "owner": owner, "holder": held_by,
                    "expires_in": round(exp - now, 1),
                    "reason": "另一个实例持有租约（未过期）"}
        rc = db.execute_rowcount(
            "UPDATE scheduler_leases SET owner=?, heartbeat_at=?, expires_at=? "
            "WHERE name=? AND (owner=? OR expires_at<=?)",
            (owner, now, now + ttl, name, owner, now))
        if rc == 0:
            holder = _row(name) or {}
            return {"acquired": False, "owner": owner,
                    "holder": holder.get("owner", ""), "expires_in": 0,
                    "reason": "租约已被其他实例抢先获取"}
        return {"acquired": True, "owner": owner, "holder": owner,
                "expires_in": ttl, "reason": "续租成功"}
    try:
        db.execute(
            "INSERT INTO scheduler_leases(name, owner, acquired_at, "
            "heartbeat_at, expires_at) VALUES(?,?,?,?,?)",
            (name, owner, now, now, now + ttl))
    except Exception as exc:  # 唯一键冲突=别人抢先
        return {"acquired": False, "owner": owner, "holder": "",
                "expires_in": 0, "reason": "插入冲突: %s" % exc}
    return {"acquired": True, "owner": owner, "holder": owner,
            "expires_in": ttl, "reason": "首次获取"}


def renew(name: str = NAME, owner: str = None, ttl: int = DEFAULT_TTL) -> dict:
    """续租（不抢占）。返回 ``{ok, reason}``。"""
    owner = owner or owner_id()
    now = _now()
    rc = db.execute_rowcount(
        "UPDATE scheduler_leases SET heartbeat_at=?, expires_at=? "
        "WHERE name=? AND owner=? AND expires_at>?", (now, now + ttl, name, owner, now))
    if rc:
        return {"ok": True, "expires_in": ttl}
    row = _row(name)
    return {"ok": False, "holder": (row or {}).get("owner"),
            "reason": "已不是持有者（可能被抢占）"}


def release(name: str = NAME, owner: str = None) -> bool:
    owner = owner or owner_id()
    try:
        db.execute_rowcount("DELETE FROM scheduler_leases WHERE name=? AND owner=?",
                            (name, owner))
        return True
    except Exception:
        return False


def status(name: str = NAME) -> dict:
    row = _row(name) or {}
    now = _now()
    exp = float(row.get("expires_at") or 0)
    return {"name": name, "owner": row.get("owner"),
            "expires_in": round(exp - now, 1) if exp else None,
            "active": bool(exp > now),
            "heartbeat_at": row.get("heartbeat_at"),
            "self": owner_id()}
