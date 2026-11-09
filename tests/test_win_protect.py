# -*- coding: utf-8 -*-
"""Windows 客户端目录防护：命令构造 / 路径护栏 / 状态解析 / API 契约。

如实边界：本文件用注入的 SSH 执行器验证平台侧逻辑与下发的 PowerShell 脚本，
**不是 Windows 真机验证**。真机（当前 192.168.1.24:22 不在线）需后续补测。
"""
import base64
import json

import pytest

from core import win_protect as wp


# ---------------------------------------------------------------- 路径护栏
@pytest.mark.parametrize("p", [
    "D:\\data", "E:/备份/业务", r"\\nas\share\dir",
])
def test_validate_path_accepts_absolute(p):
    out = wp.validate_path(p)
    assert out and ".." not in out


@pytest.mark.parametrize("p", [
    "", "data", "..\\..\\windows", "D:\\a\\..\\b", 'D:\\a" & del *',
    "D:\\a;Remove-Item", "D:\\a|whoami", "C:\\", "D:\\a`whoami",
])
def test_validate_path_rejects_dangerous(p):
    with pytest.raises(wp.WinProtectError):
        wp.validate_path(p)


def test_encoded_command_roundtrip():
    s = "Write-Output '中文路径 D:\\订单'\n$ErrorActionPreference='Stop'"
    raw = base64.b64decode(wp.encode_ps(s)).decode("utf-16le")
    assert raw == s


# ---------------------------------------------------------------- 命令构造
def test_cfa_apply_script_uses_windows_builtin_only():
    s = wp.build_apply_script(["D:\\data", "E:\\备份"], "cfa",
                              ["C:\\Program Files\\app\\app.exe"])
    assert "Set-MpPreference -EnableControlledFolderAccess Enabled" in s
    assert "Add-MpPreference -ControlledFolderAccessProtectedFolders" in s
    assert "Add-MpPreference -ControlledFolderAccessAllowedApplications" in s
    assert "D:\\data" in s and "E:\\备份" in s
    assert "icacls" not in s


def test_acl_mode_denies_delete_with_sid():
    s = wp.build_apply_script(["D:\\archive"], "acl_deny_delete")
    assert "icacls" in s and "/deny" in s and "(DE,DC)" in s
    assert "*S-1-1-0" in s           # 用 SID 避免本地化名称失效
    assert "Test-Path" in s           # 目录不存在要如实失败
    assert "Set-MpPreference" not in s


def test_both_mode_and_remove_script():
    s = wp.build_apply_script(["D:\\data"], "both")
    assert "Set-MpPreference" in s and "icacls" in s
    r = wp.build_remove_script(["D:\\data"], "both")
    assert "Remove-MpPreference" in r and "/remove:d" in r


def test_invalid_mode_rejected():
    with pytest.raises(wp.WinProtectError):
        wp.build_apply_script(["D:\\data"], "format_c")


def test_status_script_and_parser():
    s = wp.build_status_script(["D:\\data"])
    assert "Get-MpPreference" in s and "icacls" in s
    out = ("CFA=Enabled\n"
           "CFA_FOLDERS=D:\\data|E:\\bak\n"
           "CFA_ALLOWED=C:\\app.exe\n"
           "ACL_PATH=D:\\data\nACL_DENY=1\n"
           "ACL_PATH=E:\\bak\nACL_DENY=0\n")
    st = wp.parse_status(out)
    assert st["cfa"] == "Enabled"
    assert st["cfa_folders"] == ["D:\\data", "E:\\bak"]
    assert st["acl"] == {"D:\\data": True, "E:\\bak": False}
    assert wp.parse_status("CFA_ERROR=Defender 不可用")["error"]


# ---------------------------------------------------------------- 编排
def _stub(monkeypatch, scripts, reply="CFA=Enabled PATHS=D:\\data\nACL=DENY_DELETE_OK"):
    monkeypatch.setattr(wp, "_host", lambda hid: {
        "hostname": "127.0.0.1", "port": 22, "username": "u",
        "password": "p", "os_type": "windows"})
    monkeypatch.setattr(wp.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(wp.db, "add_log", lambda *a, **k: None)

    def fake(script):
        scripts.append(script)
        return reply

    return fake


def test_apply_remove_check_inject_executor(monkeypatch):
    scripts = []
    fake = _stub(monkeypatch, scripts)
    r = wp.apply(3, ["D:\\data", "E:\\bak"], "both", exec_fn=fake)
    assert r["ok"] is True and r["paths"] == ["D:\\data", "E:\\bak"]
    assert "Set-MpPreference" in scripts[-1] and "icacls" in scripts[-1]

    wp.remove(3, ["D:\\data"], "both", exec_fn=fake)
    assert "Remove-MpPreference" in scripts[-1] and "/remove:d" in scripts[-1]

    st = wp.check(3, ["D:\\data"], exec_fn=fake)
    assert st["ok"] is True and "Get-MpPreference" in scripts[-1]


def test_apply_propagates_powershell_failure(monkeypatch):
    monkeypatch.setattr(wp, "_host", lambda hid: {"os_type": "windows"})
    monkeypatch.setattr(wp.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(wp.db, "add_log", lambda *a, **k: None)

    def boom(_s):
        raise wp.WinProtectError("PowerShell 执行失败(rc=1): 拒绝访问")

    with pytest.raises(wp.WinProtectError) as e:
        wp.apply(3, ["D:\\data"], exec_fn=boom)
    assert "拒绝访问" in str(e.value)


def test_non_windows_host_rejected(monkeypatch):
    monkeypatch.setattr(wp, "_host", lambda hid: (_ for _ in ()).throw(
        wp.WinProtectError("主机不是 Windows")))
    with pytest.raises(wp.WinProtectError):
        wp.apply(1, ["D:\\data"], exec_fn=lambda s: "ok")


def test_rules_crud_and_allowed_apps(monkeypatch, tmp_path):
    monkeypatch.setattr(wp, "init_table", lambda: None)
    calls = []
    monkeypatch.setattr(wp.db, "execute", lambda sql, params=(): (
        calls.append((sql, params)) or 7))
    rid = wp.add_rule(3, "D:/data", "cfa", ["C:\\app\\a.exe"])
    assert rid == 7
    assert "INSERT INTO win_protect_rules" in calls[0][0]
    assert json.loads(calls[0][1][3]) == ["C:\\app\\a.exe"]
    monkeypatch.setattr(wp.db, "query", lambda *a, **k: [])
    assert wp.list_rules() == []


def test_api_routes_registered():
    from flask import Flask
    from api import api_bp
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1", name="api_v1")
    rules = {}
    for r in app.url_map.iter_rules():
        rules.setdefault(r.rule, set()).update(r.methods)
    for prefix in ("/api", "/api/v1"):
        assert "GET" in rules[prefix + "/win-protect/rules"]
        assert "POST" in rules[prefix + "/win-protect/rules"]
        assert "POST" in rules[prefix + "/win-protect/apply"]
        assert "POST" in rules[prefix + "/win-protect/remove"]
        assert "POST" in rules[prefix + "/win-protect/check"]
        assert "DELETE" in rules[prefix + "/win-protect/rules/<int:rid>"]
