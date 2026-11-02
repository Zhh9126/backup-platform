# -*- coding: utf-8 -*-
"""Agent 执行通道单测：加密 / Agent 进程内起服 / SDK exec / 文件双向 / 流。

真实链路：测试内起 agent 子进程（随机端口+token），SDK 走 HTTP 完成全部
能力验证；SSH 兼容层由引擎集成测试覆盖（真机 170/171）。
"""
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import crypto  # noqa: E402


# ---------------- crypto ----------------

def test_crypto_roundtrip():
    key = crypto.derive_key("tok-abc")
    env = crypto.encrypt_bytes(key, "你好 backup".encode("utf-8"))
    assert env["enc"] == 1
    assert crypto.decrypt_bytes(key, env) == "你好 backup".encode("utf-8")


def test_crypto_tamper_rejected():
    key = crypto.derive_key("tok-abc")
    env = crypto.encrypt_bytes(key, b"data")
    raw = bytearray(base64.b64decode(env["ct"]))
    raw[0] ^= 0xFF
    env["ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):
        crypto.decrypt_bytes(key, env)


def test_file_encrypt_roundtrip(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(1024 * 1024 + 7))
    enc = str(tmp_path / "a.enc")
    dec = str(tmp_path / "a.dec")
    key = crypto.derive_key("tok-file")
    crypto.encrypt_file(str(src), enc, key)
    assert crypto.is_encrypted_file(enc)
    crypto.decrypt_file(enc, dec, key)
    assert hashlib.sha256(src.read_bytes()).digest() == \
        hashlib.sha256(open(dec, "rb").read()).digest()


# ---------------- agent 真实链路 ----------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def agent_proc():
    port = _free_port()
    token = "unittest-token-" + base64.b32encode(os.urandom(10)).decode()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "agent", "backup_agent.py"),
         "--token", token, "--port", str(port), "--bind", "127.0.0.1",
         "--allow-dirs", "/tmp"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # 等待就绪
    from core.agent_client import AgentTransport
    t = None
    for _ in range(30):
        try:
            t = AgentTransport("127.0.0.1", port, token, timeout=3)
            t.health()
            break
        except Exception:
            time.sleep(0.5)
    assert t is not None, "agent 启动失败"
    yield t
    proc.terminate()
    proc.wait(timeout=5)


def test_capabilities(agent_proc):
    caps = agent_proc.capabilities()
    assert caps["version"]
    assert "gzip" in caps["compress"]


def test_exec(agent_proc):
    r = agent_proc.exec("echo hello-agent && echo err-line >&2")
    assert r["rc"] == 0
    assert b"hello-agent" in r["out"]
    assert b"err-line" in r["err"]


def test_exec_encrypted_envelope(agent_proc):
    # capabilities.enc=True 时 /exec 请求体应为 AES-GCM 信封（发篡改数据应 4xx/403）
    assert agent_proc.caps.get("enc") is True or \
        agent_proc.caps.get("algo") == "AES-256-GCM"
    bad = json.dumps({"enc": 1, "n": base64.b64encode(b"0" * 12).decode(),
                      "ct": base64.b64encode(b"junk").decode()}).encode()
    import http.client
    conn = http.client.HTTPConnection(agent_proc.host, agent_proc.port, timeout=5)
    conn.request("POST", "/exec", body=bad,
                 headers={"X-AIDBM-Token": agent_proc.token,
                          "Content-Length": str(len(bad))})
    resp = conn.getresponse()
    assert resp.status in (403, 500)  # 解密失败拒绝


def test_file_roundtrip(agent_proc, tmp_path):
    local = tmp_path / "up.bin"
    payload = os.urandom(5 * 1024 * 1024 + 123)
    local.write_bytes(payload)
    remote = "/tmp/aidbm_test_up.bin"
    agent_proc.file_put(str(local), remote)
    down = tmp_path / "down.bin"
    agent_proc.file_get(remote, str(down))
    assert down.read_bytes() == payload
    agent_proc.fs("remove", remote)


def test_file_resume(agent_proc, tmp_path):
    # 断点续传：先传一半（手工 PUT），file_put 应从断点续传且对账通过
    payload = os.urandom(3 * 1024 * 1024)
    local = tmp_path / "r.bin"
    local.write_bytes(payload)
    remote = "/tmp/aidbm_test_resume.bin"
    import urllib.parse
    half = len(payload) // 2
    q = urllib.parse.urlencode({"path": remote, "offset": 0, "truncate": "1"})
    st, _h, _d = agent_proc._request("PUT", "/file?" + q,
                                     body=payload[:half], timeout=60)
    assert st == 200
    agent_proc.file_put(str(local), remote)
    down = tmp_path / "r2.bin"
    agent_proc.file_get(remote, str(down))
    assert down.read_bytes() == payload
    agent_proc.fs("remove", remote)


def test_exec_stream(agent_proc):
    it = agent_proc.exec_stream(
        "for i in 1 2 3 4 5; do echo chunk-$i; sleep 0.1; done")
    data = b""
    done = None
    for frame in it:
        if frame.startswith(b"{") and b"__done" in frame[:64]:
            done = json.loads(frame.decode())
        else:
            data += frame
    assert done and done["rc"] == 0
    assert b"chunk-1" in data and b"chunk-5" in data


def test_gzip_transfer(agent_proc, tmp_path):
    # 压缩传输：GET /file?gz=1（SDK file_get 当前走原样；此处直接验压缩流可解）
    import gzip as _gzip
    import urllib.parse
    payload = b"A" * (1024 * 1024)
    remote = "/tmp/aidbm_test_gz.bin"
    agent_proc.file_put_bytes = None
    local = tmp_path / "gz.bin"
    local.write_bytes(payload)
    agent_proc.file_put(str(local), remote)
    q = urllib.parse.urlencode({"path": remote, "gz": "1"})
    st, _h, resp = agent_proc._request("GET", "/file?" + q, stream=True,
                                       timeout=60)
    assert st == 200
    raw = b""
    for frame in agent_proc._iter_chunked(resp):
        raw += frame
    # chunked 尾帧后是 gzip 全量（X-Content-Gzip=1）
    assert _gzip.decompress(raw) == payload
    agent_proc.fs("remove", remote)


# ---------------- 安全评审 2026-11-02 修复项 ----------------

def test_capabilities_hmac_required(agent_proc):
    """caps 上报 hmac=required（版本 ≥1.1.0）。"""
    caps = agent_proc.capabilities()
    assert caps.get("hmac") == "required"
    assert caps.get("version") >= "1.1.0"


def test_unsigned_body_rejected(agent_proc):
    """明文请求不带 sig → 403（完整性校验必选，无绕过路径）。"""
    import json as _json
    import urllib.request
    port = agent_proc.port
    req = urllib.request.Request(
        "http://127.0.0.1:%d/exec" % port,
        data=_json.dumps({"cmd": "echo hi"}).encode(),
        headers={"Content-Type": "application/json",
                 "X-AIDBM-Token": agent_proc.token},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("无 sig 请求应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_bad_sig_rejected(agent_proc):
    """sig 错误 → 403。"""
    import json as _json
    import urllib.request
    port = agent_proc.port
    req = urllib.request.Request(
        "http://127.0.0.1:%d/exec" % port,
        data=_json.dumps({"cmd": "echo hi", "sig": "deadbeef"}).encode(),
        headers={"Content-Type": "application/json",
                 "X-AIDBM-Token": agent_proc.token},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("错误 sig 应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_oversized_body_413(agent_proc):
    """超过 MAX_BODY 的请求体 → 413（OOM 防护）。"""
    import json as _json
    import urllib.request
    port = agent_proc.port
    big = "x" * (17 * 1024 * 1024)
    body = _json.dumps({"cmd": big}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/exec" % port, data=body,
        headers={"Content-Type": "application/json",
                 "X-AIDBM-Token": agent_proc.token},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
        raise AssertionError("超大请求应被 413 拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 413


def test_plaintext_response_signed(agent_proc, tmp_path):
    """AES 可用时响应为加密信封；明文响应（若有）必带 sig——SDK 校验通过。"""
    # exec 走完整 _respond 链路：SDK _open_json 会校验 enc=1 或 sig
    r = agent_proc.exec("echo signed-check")
    assert b"signed-check" in r["out"]
