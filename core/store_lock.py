"""同一块存储根目录的跨线程、跨进程协调。"""
from contextlib import contextmanager
from functools import wraps
import inspect
import os
import threading

_LOCKS = {}
_GUARD = threading.Lock()


@contextmanager
def locked(root):
    root = os.path.realpath(root)
    os.makedirs(root, exist_ok=True)
    with _GUARD:
        lock = _LOCKS.setdefault(root, threading.RLock())
    with lock:
        with open(os.path.join(root, ".store.lock"), "a+b") as handle:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def coordinated(function):
    signature = inspect.signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        from core import chunk_store
        arguments = signature.bind(*args, **kwargs).arguments
        with locked(chunk_store._root(arguments.get("store_root", ""))):
            return function(*args, **kwargs)
    return call
