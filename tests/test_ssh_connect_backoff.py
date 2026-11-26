# -*- coding: utf-8 -*-
"""SSH 建连健壮性单测（2026-11-26 现场 banner 握手失败专项）。

覆盖：
  - 同一主机的并发握手被**串行化**（并发建连收敛为 1，避免打满 sshd
    MaxStartups —— 现场每天 02:00 定时任务集中触发时必现）；
  - 握手抖动按指数退避重试（带抖动），不是失败即放弃；
  - 认证失败不重试，且错误文案带可执行处置；
  - 握手彻底失败的错误文案区分 banner / 超时两类原因。
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paramiko  # noqa: E402
from core.engines import file as F  # noqa: E402


class _FakeTransport:
    def __init__(self, alive=True):
        self._alive = alive

    def is_active(self):
        return self._alive

    def send_ignore(self):
        if not self._alive:
            raise OSError("dead")

    def set_keepalive(self, *a, **k):
        pass

    def close(self):
        self._alive = False


class _FakeClient:
    def __init__(self, failures=0, exc=None):
        self.failures = failures
        self.exc = exc or paramiko.ssh_exception.SSHException(
            "Error reading SSH protocol banner")
        self.attempts = 0
        self.transport = _FakeTransport()

    def set_missing_host_key_policy(self, *a, **k):
        pass

    def connect(self, *a, **k):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc

    def get_transport(self):
        return self.transport

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_pool():
    with F._ssh_lock:
        F._ssh_pool.clear()
    F._host_connect_locks.clear()
    yield
    with F._ssh_lock:
        F._ssh_pool.clear()
    F._host_connect_locks.clear()


def test_concurrent_handshakes_are_serialized(monkeypatch):
    """4 个线程同时要同一台主机 → 只允许 1 次真实握手，其余复用。"""
    created = []

    def _factory():
        c = _FakeClient()
        created.append(c)
        return c

    monkeypatch.setattr(paramiko, "SSHClient", _factory)
    slept = []
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))

    results = []
    errors = []

    def _worker():
        try:
            results.append(F._get_ssh_client("root@127.0.0.1:22", password="p"))
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(created) == 1, "并发握手应被串行化为 1 次"
    assert created[0].attempts == 1
    # 池化复用：4 个线程拿到同一个连接对象
    assert len(set(id(c) for c in results)) == 1


def test_handshake_jitter_retries_with_backoff(monkeypatch):
    """前两次握手失败 → 退避重试，第 3 次成功。"""
    client = _FakeClient(failures=2)
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    slept = []
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))

    got = F._get_ssh_client("root@127.0.0.1:22", password="p")
    assert got is client
    assert client.attempts == 3
    assert len(slept) == 2 and slept[1] > slept[0], "退避应递增且带抖动"


def test_auth_failure_is_not_retried_and_actionable(monkeypatch):
    """认证失败：不重试，错误文案含账号/处置。"""
    client = _FakeClient(failures=99,
                          exc=paramiko.ssh_exception.AuthenticationException("bad password"))
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError) as ei:
        F._get_ssh_client("root@10.0.0.9:22", password="p")
    assert client.attempts == 1, "认证失败不应重试"
    assert "认证失败" in str(ei.value) and "10.0.0.9" in str(ei.value)


def test_banner_failure_message_is_actionable(monkeypatch):
    """握手彻底失败 → 文案区分 banner 拒绝，并给出 MaxStartups 处置。"""
    client = _FakeClient(failures=99)
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError) as ei:
        F._get_ssh_client("root@10.0.0.9:22", password="p")
    msg = str(ei.value)
    assert "MaxStartups" in msg and "banner" in msg.lower()
    assert "10.0.0.9" in msg


def test_timeout_failure_message_mentions_port(monkeypatch):
    client = _FakeClient(failures=99, exc=OSError("timed out"))
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError) as ei:
        F._get_ssh_client("root@10.0.0.9:22", password="p")
    assert "22 端口" in str(ei.value)
