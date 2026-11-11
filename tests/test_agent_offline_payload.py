# -*- coding: utf-8 -*-
"""Agent 黑盒载荷离线可用性回归（2026-11-11 现场缺陷）。

现场现象：CentOS7 自带 python3.7 没有 cryptography，旧 loader 在模块顶层
硬导入 AESGCM，systemd 服务启动即失败并 Restart=always 无限重启，
8466 端口永不监听；平台「安装并启动」却只报「探活失败」，注册记录残留。

修复口径：默认下发 v2 纯标准库载荷（HMAC-SHA256 认证流加密），
unit 不传 loader 不需要的 --payload，安装结果拆分 install/probe 两段。
"""
import ast
import json
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import agent_payload  # noqa: E402

TOKEN = "W3QJF7nCnWxrItngt0IqRgWqjavIMyupr8GwaRehB7w"


def test_default_payload_is_stdlib_v2():
    """默认载荷必须是零第三方依赖的 v2，而不是 AES-GCM。"""
    _, payload = agent_payload.build_payload("print('hi')", TOKEN)
    env = json.loads(payload.decode("utf-8"))
    assert env["v"] == 2
    assert "ct" in env and "tag" in env
    assert agent_payload.payload_version(payload) == 2


def test_loader_has_no_toplevel_thirdparty_import():
    loader, _ = agent_payload.build_payload("print('hi')", TOKEN)
    top_modules = []
    for node in ast.parse(loader).body:
        if isinstance(node, ast.ImportFrom):
            top_modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            top_modules += [a.name for a in node.names]
    assert not any("cryptography" in m for m in top_modules)
    # systemd unit 兼容：loader 仍认识 --payload，且只支持必需参数
    assert 'ap.add_argument("--payload"' in loader
    assert 'ap.add_argument("--token"' in loader


def test_loader_executes_payload_with_stdlib_only():
    """真实起子进程执行 loader，验证解密 + exec 全链路不依赖第三方包。"""
    marker = "OFFLINE_LOADER_OK"
    loader, payload = agent_payload.build_payload(
        "print('%s')\n" % marker, TOKEN)
    d = tempfile.mkdtemp(prefix="ag_payload_")
    loader_path = os.path.join(d, "loader.py")
    payload_path = os.path.join(d, "payload.enc")
    with open(loader_path, "w", encoding="utf-8") as f:
        f.write(loader)
    with open(payload_path, "wb") as f:
        f.write(payload)
    r = subprocess.run(
        [sys.executable, loader_path, "--token", TOKEN,
         "--port", "18466", "--payload", payload_path],
        capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert marker in r.stdout


def test_tampered_payload_is_rejected():
    loader, payload = agent_payload.build_payload("print('hi')", TOKEN)
    env = json.loads(payload.decode("utf-8"))
    env["ct"] = env["ct"][:-4] + "AAAA"
    ns = {"__name__": "loader_test"}
    exec(loader, ns)
    with pytest.raises(ValueError):
        ns["_decrypt"](env, TOKEN)


def test_v1_aes_payload_still_supported_when_available():
    """平台侧有 cryptography 时仍可生成 v1，loader v1 分支保持兼容。"""
    if not agent_payload._has_aes():
        pytest.skip("平台未安装 cryptography")
    loader, payload = agent_payload.build_payload(
        "print('hi')", TOKEN, prefer_aes=True)
    env = json.loads(payload.decode("utf-8"))
    assert env["v"] == 1
    ns = {"__name__": "loader_test"}
    exec(loader, ns)
    assert ns["_decrypt"](env, TOKEN) == "print('hi')"


def test_systemd_unit_and_install_contract():
    """unit 不再传 --payload，安装流程有退出码校验、回滚与分段状态。"""
    src = open(os.path.join(ROOT, "api", "agents.py"),
               encoding="utf-8").read()
    assert "ExecStart=%s /opt/aidbm-agent/%s --token %s --port %d\\n" in src
    assert "--payload /opt/aidbm-agent/%s\\n" not in src
    assert "systemctl restart aidbm-agent" in src
    for frag in ('"install": {"ok": False}', '"probe": {"ok": False}',
                 "recv_exit_status()", "_rollback()", "diagnostics",
                 '"hint"', "payload_format", "journalctl -u aidbm-agent"):
        assert frag in src, frag


def test_agentless_page_shows_failure_reason():
    tpl = open(os.path.join(ROOT, "templates", "agentless.html"),
               encoding="utf-8").read()
    assert "服务未启动" in tpl
    assert "最后检查" in tpl
    assert "查看远端服务日志" in tpl
    assert "r.details && r.details.hint" in tpl
