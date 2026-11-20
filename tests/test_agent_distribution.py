"""单文件发布、受限执行和网络失败语义回归。"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from unittest.mock import Mock

import pytest
import config
from core import agent_distribution as distribution, agent_hosts, crypto
from core.agent_client import AgentTransport, AgentError


def _agent():
    path = Path(__file__).resolve().parents[1] / "agent/backup_agent.py"
    spec = importlib.util.spec_from_file_location("_agent_unit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_token_is_always_url_safe(monkeypatch):
    monkeypatch.setattr(crypto.os, "urandom", lambda n: b"\xff" * n)
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", crypto.generate_token())


def test_update_rejects_unsafe_token_before_writing(monkeypatch):
    write = Mock()
    monkeypatch.setattr(agent_hosts.db, "execute", write)
    with pytest.raises(ValueError):
        agent_hosts.update_host(1, {"token": "bad\nEnvironment=payload"})
    write.assert_not_called()


def test_unit_uses_only_executable_and_protected_configuration():
    unit = distribution.unit_text(8466)
    assert "ExecStart=/opt/aidbm-agent/aidbm-agent --port 8466" in unit
    assert "EnvironmentFile=/etc/aidbm-agent/agent.env" in unit
    assert "--token" not in unit
    assert ".py" not in unit


def test_modified_package_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    directory = tmp_path / "dist/agents/linux-x86_64"
    directory.mkdir(parents=True)
    content = b"\x7fELFfixture"
    binary = directory / "aidbm-agent"
    binary.write_bytes(content)
    manifest = {"os": "linux", "arch": "x86_64", "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert distribution.artifact()[0] == binary
    binary.write_bytes(content + b"tampered")
    with pytest.raises(ValueError, match="摘要或大小"):
        distribution.artifact()


def test_mutating_request_is_not_automatically_replayed():
    import http.client
    transport = AgentTransport("host", 8466, "token")
    conn = Mock()
    conn.getresponse.side_effect = http.client.RemoteDisconnected()
    transport._connection = Mock(return_value=conn)
    with pytest.raises(AgentError):
        transport._request("POST", "/exec", b"command")
    assert conn.request.call_count == 1


def test_truncated_chunked_stream_fails_and_closes_connection():
    response = Mock()
    response.fp = io.BytesIO(b"")
    with pytest.raises(AgentError, match="提前结束"):
        list(AgentTransport._iter_chunked(response))
    response.close.assert_called_once()
    response._aidbm_connection.close.assert_called_once()


def test_sync_output_is_bounded_without_blocking_pipe(monkeypatch):
    module = _agent()
    monkeypatch.setattr(module, "MAX_SYNC_OUT", 1024)
    rc, output, errors = module._capture_command(
        "head -c 1048576 /dev/zero; printf err >&2", 5)
    assert rc == 0 and len(output) == 1024 and errors == b"err"


def test_sync_timeout_returns_124():
    module = _agent()
    rc, _, errors = module._capture_command("sleep 5", .05)
    assert rc == 124 and errors


def test_connection_limit_returns_503_and_releases_slots():
    import http.client
    module = _agent()
    server = module.BoundedHTTPServer(("127.0.0.1", 0), module.Handler, 2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sockets = []
    try:
        for _ in range(2):
            sockets.append(socket.create_connection(server.server_address, timeout=2))
        deadline = time.monotonic() + 3
        while server._slots._value and time.monotonic() < deadline:
            time.sleep(.01)
        assert server._slots._value == 0
        conn = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            assert response.status == 503
            response.read()
        finally:
            conn.close()
    finally:
        for connection in sockets:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
