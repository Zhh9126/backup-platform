"""真实 SQLite 写事务下，同时抢占过期租约只能产生一个新持有者。"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import config
from core import db, lease


def test_expired_lease_has_one_winner_with_stale_parallel_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "lease.db"))
    monkeypatch.setattr(db, "_ACTIVE", dict(backend="sqlite"))
    db.init_schema()
    stale = {"name": "race", "owner": "expired", "expires_at": time.time() - 1}
    db.execute("INSERT INTO scheduler_leases(name,owner,acquired_at,heartbeat_at,expires_at) "
               "VALUES (?,?,?,?,?)", ("race","expired",0,0,stale["expires_at"]))
    barrier = threading.Barrier(16)
    local = threading.local()
    def stale_row(name):
        if not getattr(local, "read", False):
            local.read = True
            barrier.wait(5)
        return dict(stale)
    monkeypatch.setattr(lease, "_row", stale_row)
    def acquire(index):
        try:
            return lease.acquire(name="race", owner=f"owner-{index}")["acquired"]
        finally:
            db.reset_thread_conn()
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(acquire, range(16)))
    assert sum(results) == 1
