# -*- coding: utf-8 -*-
"""纳管主机「测试连接」跨平台探测单测（2026-11-26 Windows 纳管专项）。

背景：现场 Windows 主机（OpenSSH for Windows）纳管测试报「连接已建立但命令
执行异常」——探测命令写死 Linux 语法 `uname -a; id; echo __OK__`，cmd 不认识。
本文件锁定：按系统类型选命令、未知类型自动识别、失败文案可执行。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import ssh_hosts  # noqa: E402


class _FakeChan:
    def __init__(self, rc):
        self._rc = rc

    def recv_exit_status(self):
        return self._rc


class _FakeStream:
    def __init__(self, text: str, rc: int):
        self._raw = text.encode("utf-8")
        self.channel = _FakeChan(rc)

    def read(self):
        return self._raw


class _FakeClient:
    """按命令返回预设结果；记录所有尝试过的命令。"""

    def __init__(self, script):
        self.script = script          # {前缀: (stdout, stderr, rc)}
        self.attempts = []

    def set_missing_host_key_policy(self, *a, **k):
        pass

    def connect(self, *a, **k):
        return True

    def exec_command(self, cmd, timeout=20):
        self.attempts.append(cmd)
        for key, val in self.script.items():
            if key in cmd:
                out, err, rc = val
                return None, _FakeStream(out, rc), _FakeStream(err, rc)
        return None, _FakeStream("", "1"), _FakeStream("command not found", 1)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    """隔离 paramiko 与 DB。"""
    import paramiko
    monkeypatch.setattr(paramiko, "SSHClient", lambda: _FAKE["client"])
    monkeypatch.setattr(ssh_hosts.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(ssh_hosts.db, "now_iso", lambda: "now")
    yield


_FAKE = {}


def _host(monkeypatch, os_type):
    _FAKE["host"] = {"id": 1, "hostname": "10.0.0.7", "port": 22,
                     "username": "u", "password": "p", "auth_type": "password",
                     "private_key": "", "os_type": os_type}
    monkeypatch.setattr(ssh_hosts, "get_host",
                        lambda *a, **k: dict(_FAKE["host"]))


def test_windows_host_uses_cmd_syntax(monkeypatch):
    _host(monkeypatch, "windows")
    _FAKE["client"] = _FakeClient({
        "cmd /c": ("Microsoft Windows [Version 10.0.19045.3803]\nWIN-HOST\n__OK__\n",
                   "", 0)})
    res = ssh_hosts.test_connection(1)
    assert res["ok"] is True
    assert res["os_type"] == "windows"
    assert "cmd /c" in _FAKE["client"].attempts[0]
    assert "识别为 Windows" in res["message"]
    assert "Microsoft Windows" in res["banner"]
    # 登记与识别一致时不应再提示"改为 windows"
    assert "建议" not in res["message"]


def test_unknown_host_auto_detects_windows(monkeypatch):
    """登记类型未标 → 先 Linux 后 Windows，自动识别并回填识别结果。"""
    _host(monkeypatch, "")
    _FAKE["client"] = _FakeClient({
        "uname": ("", "'uname' is not recognized", 1),
        "cmd /c": ("WIN\n__OK__\n", "", 0)})
    res = ssh_hosts.test_connection(1)
    assert res["ok"] is True and res["os_type"] == "windows"
    assert len(_FAKE["client"].attempts) == 2, "应先试 Linux 再试 Windows"
    assert res["message"].startswith("连接成功")


def test_linux_host_still_works(monkeypatch):
    _host(monkeypatch, "linux")
    _FAKE["client"] = _FakeClient({
        "uname": ("Linux host 5.10 x86_64\nuid=0(root)\n__OK__\n", "", 0)})
    res = ssh_hosts.test_connection(1)
    assert res["ok"] is True and res["os_type"] == "linux"
    assert "Linux host" in res["banner"]


def test_all_probes_fail_is_actionable(monkeypatch):
    """两组都失败：错误文案必须带真实 stderr + 处置指引，不再只报"命令执行异常"。"""
    _host(monkeypatch, "")
    _FAKE["client"] = _FakeClient({})        # 全部 rc=1 + command not found
    res = ssh_hosts.test_connection(1)
    assert res["ok"] is False
    assert "command not found" in res["message"]
    assert "Windows" in res["message"] and "操作系统" in res["message"]


def test_mismatched_registration_is_hinted(monkeypatch):
    """登记为 linux 但实际是 Windows → 成功后提示改登记。"""
    _host(monkeypatch, "linux")
    _FAKE["client"] = _FakeClient({
        "uname": ("", "'uname' is not recognized", 1),
        "cmd /c": ("WIN\n__OK__\n", "", 0)})
    res = ssh_hosts.test_connection(1)
    assert res["ok"] is True and res["os_type"] == "windows"
    assert "改为 windows" in res["message"]


def test_probe_scripts_dispatch():
    win = [k for _c, k in ssh_hosts._probe_scripts("windows")]
    assert win == ["windows"]
    assert [k for _c, k in ssh_hosts._probe_scripts("")] == ["linux", "windows"]
    assert [k for _c, k in ssh_hosts._probe_scripts("linux")][0] == "linux"
