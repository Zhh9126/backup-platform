# -*- coding: utf-8 -*-
"""备份插件（Agent 执行器）下载侧完整性能力回归。

覆盖：能力自报协商、落盘字节数硬校验、端到端 sha256 对账、
老版本插件与大文件的诚实降级，以及插件侧 /fs digest 静态契约。
"""
import hashlib
import json
import os

import pytest

from core import agent_client
from core.agent_client import AgentError, AgentTransport


class FakeTransport(AgentTransport):
    """只替换网络层，保留真实 file_get 逻辑。"""

    def __init__(self, caps, payload, remote_sha=None):
        self.caps = caps
        self._payload = payload
        self._remote_sha = remote_sha
        self.label = "fake-agent"
        self.digest_calls = 0

    def capabilities(self, refresh=False):
        return self.caps

    def _seal(self, obj):
        return json.dumps(obj).encode()

    def _open_json(self, data):
        return json.loads(data.decode())

    def _request(self, method, path, body=None, timeout=None, stream=False):
        if method == "POST" and path == "/fs":
            req = json.loads(body.decode())
            if req.get("op") == "stat":
                return 200, {}, json.dumps(
                    {"exists": True, "size": len(self._payload)}).encode()
            if req.get("op") == "digest":
                self.digest_calls += 1
                return 200, {}, json.dumps(
                    {"exists": True, "size": len(self._payload),
                     "sha256": self._remote_sha}).encode()
        if method == "GET" and path.startswith("/file") and stream:
            return 200, {}, object()
        raise AssertionError("意外请求: %s %s" % (method, path))

    def _iter_chunked(self, resp):
        yield self._payload


NEW_CAPS = {"version": "1.1.2", "os": "linux", "arch": "x86_64",
            "features": ["exec", "resume", "fs", "fs-digest"],
            "compress": [], "integrity": ["sha256"],
            "algo": "AES-256-GCM", "chunk": 512 * 1024,
            "allow_dirs": ["/tmp"]}
OLD_CAPS = {"version": "1.1.1", "features": ["exec", "resume", "fs"],
            "compress": [], "algo": "HMAC-SHA256"}


def test_negotiation_new_plugin():
    t = FakeTransport(NEW_CAPS, b"x")
    n = t.negotiation()
    assert n["integrity_check"] is True
    assert n["integrity_algo"] == "sha256"
    assert n["resume"] is True
    assert n["transport_gzip"] is False
    assert n["version"] == "1.1.2"


def test_negotiation_old_plugin_no_false_claim():
    t = FakeTransport(OLD_CAPS, b"x")
    n = t.negotiation()
    assert n["integrity_check"] is False
    assert n["integrity_algo"] == "size-only"
    assert n["transport_gzip"] is False


def test_full_download_digest_ok(tmp_path):
    payload = b"aidbm-backup-artifact-\xe4\xb8\xad\xe6\x96\x87" * 100
    sha = hashlib.sha256(payload).hexdigest()
    t = FakeTransport(NEW_CAPS, payload, remote_sha=sha)
    out = str(tmp_path / "artifact.bin")
    assert t.file_get("/tmp/artifact.bin", out) == len(payload)
    with open(out, "rb") as f:
        assert f.read() == payload
    assert t.digest_calls == 1


def test_truncated_download_rejected(tmp_path):
    """gzip 尾块丢失 / 流中断：落盘字节数少于远端，必须报错不得静默成功。"""
    t = FakeTransport(NEW_CAPS, b"short", remote_sha=None)
    t._payload = b"short"
    # stat 报的远端大小比实际流大，模拟中断
    original = t._request

    def patched(method, path, body=None, timeout=None, stream=False):
        if method == "POST" and path == "/fs" and \
                json.loads(body.decode()).get("op") == "stat":
            return 200, {}, json.dumps({"exists": True, "size": 99}).encode()
        return original(method, path, body, timeout, stream)

    t._request = patched
    with pytest.raises(AgentError, match="产物不完整"):
        t.file_get("/tmp/artifact.bin", str(tmp_path / "artifact.bin"))


def test_digest_mismatch_rejected(tmp_path):
    payload = b"aidbm-real-content"
    t = FakeTransport(NEW_CAPS, payload, remote_sha="0" * 64)
    with pytest.raises(AgentError, match="sha256 对账不一致"):
        t.file_get("/tmp/artifact.bin", str(tmp_path / "artifact.bin"))


def test_old_plugin_degrades_to_size_check(tmp_path):
    payload = b"legacy-agent-content"
    t = FakeTransport(OLD_CAPS, payload)
    assert t.file_get("/tmp/artifact.bin",
                      str(tmp_path / "artifact.bin")) == len(payload)
    assert t.digest_calls == 0


def test_large_file_skips_hash_honestly(tmp_path):
    """插件对超阈值文件返回 sha256=None，平台只做大小校验且不报错。"""
    payload = b"big"
    t = FakeTransport(NEW_CAPS, payload, remote_sha=None)
    assert t.file_get("/tmp/big.bin", str(tmp_path / "big.bin")) == len(payload)
    assert t.digest_calls == 1


def test_resume_skips_digest(tmp_path):
    """断点续传时本地前段字节不在本次流里，不能做全文件哈希对账。"""
    payload = b"tail-only"
    out = str(tmp_path / "resume.bin")
    with open(out, "wb") as f:
        f.write(b"head-")
    t = FakeTransport(NEW_CAPS, payload, remote_sha="0" * 64)

    def patched(method, path, body=None, timeout=None, stream=False):
        if method == "POST" and path == "/fs":
            req = json.loads(body.decode())
            if req.get("op") == "stat":
                return 200, {}, json.dumps(
                    {"exists": True, "size": 5 + len(payload)}).encode()
        if method == "GET" and path.startswith("/file"):
            return 200, {}, object()
        raise AssertionError("续传路径不应请求 digest: %s" % path)

    t._request = patched
    assert t.file_get("/tmp/resume.bin", out) == 5 + len(payload)
    assert t.digest_calls == 0


def test_plugin_source_contract():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agent", "backup_agent.py"),
              encoding="utf-8") as f:
        src = f.read()
    assert '"fs-digest"' in src
    assert '"integrity": ["sha256"]' in src
    assert 'elif op == "digest":' in src
    assert '"sha256": None, "skipped": "over_limit"' in src
    assert 'VERSION = "1.1.2"' in src
