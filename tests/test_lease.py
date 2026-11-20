# -*- coding: utf-8 -*-
"""调度器单实例租约单测（对标 PB 级方案 §1 Etcd 选主）。

用内存假 DB 替换 core.db 的三个原语，纯逻辑验证抢占语义（不碰真实元数据库）：
  - 首个实例获取成功；第二个实例在租约有效期内被拒；
  - 同 owner 重复获取 = 续租成功；
  - 租约过期后其他实例可抢占（崩溃接管）；
  - renew 非持有者失败；release 后立即可被别人获取。
"""
import time

import pytest

import core.db as db
from core import lease


class FakeDB:
    def __init__(self):
        self.rows = {}

    def query_one(self, sql, params=()):
        name = params[0] if params else None
        r = self.rows.get(name)
        return dict(r) if r else None

    def execute_rowcount(self, sql, params=()):
        if sql.startswith("UPDATE scheduler_leases SET owner"):
            owner, hb, exp, name, contender, now = params
            r = self.rows.get(name)
            if not r or (r["owner"] != contender and r["expires_at"] > now):
                return 0
            r.update(owner=owner, heartbeat_at=hb, expires_at=exp)
            return 1
        if sql.startswith("UPDATE scheduler_leases SET heartbeat_at"):
            hb, exp, name, owner, now = params
            r = self.rows.get(name)
            if not r or r.get("owner") != owner or r["expires_at"] <= now:
                return 0
            r.update(heartbeat_at=hb, expires_at=exp)
            return 1
        if sql.startswith("DELETE"):
            name, owner = params
            r = self.rows.get(name)
            if r and r.get("owner") == owner:
                self.rows.pop(name, None)
                return 1
            return 0
        return 0

    def execute(self, sql, params=()):
        name = params[0]
        if name in self.rows:
            raise Exception("UNIQUE constraint failed")
        self.rows[name] = {"name": name, "owner": params[1],
                           "acquired_at": params[2], "heartbeat_at": params[3],
                           "expires_at": params[4]}
        return 1


@pytest.fixture
def fake(monkeypatch):
    f = FakeDB()
    monkeypatch.setattr(db, "query_one", f.query_one)
    monkeypatch.setattr(db, "execute_rowcount", f.execute_rowcount)
    monkeypatch.setattr(db, "execute", f.execute)
    return f


def test_first_acquire_succeeds(fake):
    r = lease.acquire(owner="A")
    assert r["acquired"] is True
    assert lease.status()["owner"] == "A"


def test_second_instance_rejected_while_valid(fake):
    assert lease.acquire(owner="A")["acquired"] is True
    r = lease.acquire(owner="B")
    assert r["acquired"] is False
    assert r["holder"] == "A"
    assert "未过期" in r["reason"]


def test_same_owner_reacquire_is_renew(fake):
    lease.acquire(owner="A", ttl=60)
    old = fake.rows["scheduler"]["expires_at"]
    time.sleep(0.01)
    r = lease.acquire(owner="A", ttl=60)
    assert r["acquired"] is True
    assert fake.rows["scheduler"]["expires_at"] >= old


def test_expired_lease_can_be_taken_over(fake):
    lease.acquire(owner="A", ttl=60)
    fake.rows["scheduler"]["expires_at"] = time.time() - 5     # 模拟崩溃未续租
    r = lease.acquire(owner="B")
    assert r["acquired"] is True
    assert lease.status()["owner"] == "B"


def test_renew_by_non_owner_fails(fake):
    lease.acquire(owner="A")
    r = lease.renew(owner="B")
    assert r["ok"] is False
    assert r["holder"] == "A"


def test_release_allows_immediate_takeover(fake):
    lease.acquire(owner="A")
    assert lease.release(owner="A") is True
    r = lease.acquire(owner="B")
    assert r["acquired"] is True


def test_status_shape(fake):
    lease.acquire(owner="A", ttl=30)
    st = lease.status()
    assert st["name"] == "scheduler"
    assert st["owner"] == "A"
    assert st["active"] is True
    assert st["expires_in"] is not None
    assert st["self"] == lease.owner_id()


def test_expired_owner_cannot_renew(fake):
    lease.acquire(owner="A")
    fake.rows["scheduler"]["expires_at"] = time.time() - 1
    assert not lease.renew(owner="A")["ok"]


def test_concurrent_stale_reader_cannot_overwrite_new_holder(fake, monkeypatch):
    lease.acquire(owner="old")
    fake.rows["scheduler"]["expires_at"] = time.time() - 1
    stale = dict(fake.rows["scheduler"])
    assert lease.acquire(owner="winner")["acquired"]
    monkeypatch.setattr(lease, "_row", lambda name: stale)
    assert not lease.acquire(owner="loser")["acquired"]
    assert fake.rows["scheduler"]["owner"] == "winner"
