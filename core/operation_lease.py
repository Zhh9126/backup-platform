"""Renewable operation lease with a distinct owner for each invocation."""
from contextlib import contextmanager
import threading
import uuid
from core import lease


@contextmanager
def operation_lease(name, ttl=90):
    owner = lease.owner_id() + ":" + uuid.uuid4().hex
    if not lease.acquire(name, owner=owner, ttl=ttl).get("acquired"):
        raise ValueError("该操作正在执行，请等待当前操作完成")
    stopped, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not stopped.wait(max(1, ttl / 3)):
            try:
                if not lease.renew(name, owner=owner, ttl=ttl).get("ok"):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    worker = threading.Thread(target=heartbeat, name="operation-lease", daemon=True)
    worker.start()
    def ensure_owned():
        if lost.is_set() or not lease.renew(name, owner=owner, ttl=ttl).get("ok"):
            raise ValueError("执行租约已丢失，停止发布本次结果")
    try:
        yield ensure_owned
    finally:
        stopped.set()
        worker.join(timeout=2)
        lease.release(name, owner=owner)
