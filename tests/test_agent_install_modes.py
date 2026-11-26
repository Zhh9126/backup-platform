# -*- coding: utf-8 -*-
"""Agent 一键安装：发布形态选择 / 单元 / 诊断分层（2026-11-25）。

现场问题回归：
- 国产系统（统信 UOS / 麒麟 / 欧拉）与 aarch64 / 龙芯等没有预编译 ELF 包时，
  旧实现直接报「该架构尚无有效 Agent 包」→ 现在自动改用 Python 源码模式；
- 探活超时（防火墙）被误报成「服务没起来」→ 现在区分「本地端口没起来」与
  「本地起来了但平台不可达」，并自动放行防火墙端口。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="agent_modes_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core import agent_distribution as AD  # noqa: E402


class _Chan:
    def recv_exit_status(self):
        return 0


class _Stream:
    def __init__(self, data=b""):
        self._data = data
        # _command() 会用 stdout.channel.recv_exit_status() 取退出码
        self.channel = _Chan()

    def read(self):
        return self._data


class FakeClient:
    """按命令内容返回预置输出，记录所有命令。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def exec_command(self, command, timeout=None):
        self.calls.append(command)
        out = self.handler(command)
        return _Stream(), _Stream(str(out or "").encode()), _Stream(b"")


def _facts(**kw):
    base = {"UID": "0", "OS": "Linux", "ARCH": "aarch64", "GLIBC": "2.28",
            "PY": "3.7", "SYSTEMD": "yes", "SYSTEMCTL": "yes",
            "FIREWALLD": "yes", "UFW": "no", "IPTABLES": "yes",
            "RESTORECON": "yes", "SELINUX": "Disabled",
            "MACHINEID": "0123456789abcdef0123456789abcdef"}
    base.update(kw)
    return "\n".join("%s=%s" % (k, v) for k, v in base.items())


# --------------------------------------------------------------------------- #
# 架构别名 / 发布形态
# --------------------------------------------------------------------------- #
def test_arch_aliases():
    assert AD.normalize_arch("AMD64") == "x86_64"
    assert AD.normalize_arch("arm64") == "aarch64"
    assert AD.normalize_arch("loong64") == "loongarch64"
    assert AD.normalize_arch("sw64") == "sw_64"


def test_python_mode_when_no_elf_package():
    """没有 ELF 包但目标机有 python3 → 统一源码发布物（国产系统常见场景）。"""
    package = AD.resolve_package("aarch64", "2.28", "3.7")
    assert package["mode"] == "python"
    assert package["metadata"]["format"] == "python-source"
    assert package["metadata"]["arch"] == "any"
    assert package["unified"] is True
    assert "统一发布物" in package["note"]


def test_unified_source_is_preferred_even_when_elf_available(monkeypatch):
    """**统一发布物**：即使目标机满足 ELF 条件，也默认用源码包。

    理由：源码包能力随平台代码实时更新（全量最新），ELF 是构建时快照。
    用户口径「只安装一次，所有功能都支持，不区分类型」→ 不按架构分流。
    """
    package_dir = os.path.join(_TMP, "dist", "agents", "linux-x86_64")
    os.makedirs(package_dir, exist_ok=True)
    fake = os.path.join(package_dir, "aidbm-agent")
    with open(fake, "wb") as fp:
        fp.write(b"\x7fELF" + b"\x00" * 60)
    import hashlib
    digest = hashlib.sha256(open(fake, "rb").read()).hexdigest()
    with open(os.path.join(package_dir, "manifest.json"), "w") as fp:
        json.dump({"version": "9.9.9", "os": "linux", "arch": "x86_64",
                   "sha256": digest, "size": os.path.getsize(fake),
                   "glibc": "2.34", "format": "ELF-onefile"}, fp)
    monkeypatch.setattr(AD.config, "BASE_DIR", _TMP)
    package = AD.resolve_package("x86_64", "2.35", "3.9")
    assert package["mode"] == "python"
    assert package["unified"] is True
    assert package["elf_available"] is True
    # 显式指定 ELF 仍可用（运维排查用）
    forced = AD.resolve_package("x86_64", "2.35", "3.9", prefer="elf")
    assert forced["mode"] == "elf" and forced["metadata"]["glibc"] == "2.34"


def test_elf_fallback_when_target_has_no_python(monkeypatch):
    """目标机没有 python3 → 兜底用 ELF，并如实提示能力快照。"""
    package_dir = os.path.join(_TMP, "dist", "agents", "linux-x86_64")
    os.makedirs(package_dir, exist_ok=True)
    fake = os.path.join(package_dir, "aidbm-agent")
    with open(fake, "wb") as fp:
        fp.write(b"\x7fELF" + b"\x00" * 60)
    import hashlib
    digest = hashlib.sha256(open(fake, "rb").read()).hexdigest()
    with open(os.path.join(package_dir, "manifest.json"), "w") as fp:
        json.dump({"version": "9.9.9", "os": "linux", "arch": "x86_64",
                   "sha256": digest, "size": os.path.getsize(fake),
                   "glibc": "2.17", "format": "ELF-onefile"}, fp)
    monkeypatch.setattr(AD.config, "BASE_DIR", _TMP)
    package = AD.resolve_package("x86_64", "2.17", "none")
    assert package["mode"] == "elf"
    assert package["unified"] is False
    assert "python3" in package["fallback_reason"]


def test_no_python_no_package_message_is_actionable():
    try:
        AD.resolve_package("mips64", "2.28", "none")
        raise AssertionError("应当报错")
    except ValueError as exc:
        text = str(exc)
        assert "python3" in text and "build_agent.py" in text


def test_source_bundle_contains_agent_files():
    bundle = AD.source_bundle()
    meta = bundle["metadata"]
    assert meta["format"] == "python-source"
    assert meta["version"] and meta["sha256"] and meta["size"] > 0
    assert "backup_agent.py" in meta["files"]
    assert os.path.isfile(bundle["path"])
    # 同一源码重复打包摘要稳定（内容寻址暂存的前提）
    assert AD.source_bundle()["metadata"]["sha256"] == meta["sha256"]
    import tarfile
    with tarfile.open(bundle["path"], "r:gz") as tar:
        names = tar.getnames()
    assert "backup_agent.py" in names and "plugins.py" in names
    # 自更新模块必须随包下发（装一次即可跟随平台版本）
    assert "selfupdate.py" in names


def test_agent_manifest_exposes_update_pointer():
    """自更新清单：版本 + 摘要 + 下载路径，供 Agent 端比对。"""
    manifest = AD.agent_manifest()
    assert manifest["version"] and manifest["sha256"]
    assert manifest["download_path"].endswith("/download")
    assert manifest["manifest_path"].endswith("/manifest")
    assert "backup_agent.py" in manifest["files"]


def test_agent_source_dir_resolves():
    source = AD.agent_source_dir()
    assert (source / "backup_agent.py").is_file()


# --------------------------------------------------------------------------- #
# systemd 单元
# --------------------------------------------------------------------------- #
def test_unit_text_modes():
    elf = AD.unit_text(8466, "elf")
    assert "ExecStart=/opt/aidbm-agent/aidbm-agent --port 8466" in elf
    assert "PYTHONUNBUFFERED" not in elf
    # CentOS7 systemd 219 不认识 [Unit] 段的 StartLimit*，会刷 "Unknown lvalue"
    assert "StartLimitIntervalSec" not in elf
    py = AD.unit_text(9000, "python")
    assert "--port 9000" in py
    assert "PYTHONUNBUFFERED=1" in py and "PYTHONDONTWRITEBYTECODE=1" in py
    assert "Restart=on-failure" in py and "EnvironmentFile=/etc/aidbm-agent/agent.env" in py


def test_launcher_uses_target_python():
    assert AD.LAUNCHER.startswith("#!/bin/sh")
    assert "backup_agent.py" in AD.LAUNCHER and "python3" in AD.LAUNCHER


# --------------------------------------------------------------------------- #
# 预检 / 诊断
# --------------------------------------------------------------------------- #
def test_precheck_picks_python_mode_and_reports_facts(monkeypatch):
    # df -Pk /opt | tail -1 的真实输出只有一行：precheck 取第 4 列（Available KB）
    df = "/dev/vda1 51200000 1000 1024000 1% /opt"
    client = FakeClient(lambda cmd: _facts() if "uname" in cmd else df)
    package, facts = AD.precheck(client)
    assert package["mode"] == "python"
    # facts 中探针字段保持大写入参原样（后续 _open_firewall 等按 "FIREWALLD" 取值）
    assert facts["ARCH"] == "aarch64" and facts["mode"] == "python"
    assert facts["machine_id"] == "0123456789abcdef0123456789abcdef"


def test_precheck_rejects_non_root_and_no_systemd():
    client = FakeClient(lambda cmd: _facts(UID="1000"))
    try:
        AD.precheck(client)
        raise AssertionError("非 root 应拒绝")
    except ValueError as exc:
        assert "root" in str(exc)
    client = FakeClient(lambda cmd: _facts(SYSTEMD="no"))
    try:
        AD.precheck(client)
        raise AssertionError("无 systemd 应拒绝")
    except ValueError as exc:
        assert "systemd" in str(exc)


def test_probe_message_distinguishes_firewall_from_service_down():
    firewall = AD._probe_message(
        {"ok": False, "error": "timed out", "local_listen": True}, 8466)
    assert "已监听" in firewall and "防火墙" in firewall
    down = AD._probe_message(
        {"ok": False, "error": "connection refused", "local_listen": False}, 8466)
    assert "未监听" in down
    mismatch = AD._probe_message(
        {"ok": False, "error": "Agent 版本/运行形态不匹配", "local_listen": True}, 8466)
    assert "身份校验未通过" in mismatch


def test_open_firewall_skips_when_no_active_firewall():
    def handler(cmd):
        if "iptables -S INPUT 2>/dev/null | head -1" in cmd:
            return "-P INPUT ACCEPT"
        if "grep -cE" in cmd:
            return "0"
        return ""

    client = FakeClient(handler)
    facts = {"FIREWALLD": "no", "UFW": "no", "IPTABLES": "yes"}
    result = AD._open_firewall(client, 8466, facts)
    assert result["ok"] is True
    assert "无需放行" in result["actions"][0]
    assert not any("--add-port" in c or "-I INPUT" in c for c in client.calls)


def test_open_firewall_adds_firewalld_rule():
    def handler(cmd):
        if "is-active firewalld" in cmd:
            return "active"
        return ""

    client = FakeClient(handler)
    result = AD._open_firewall(client, 8466, {"FIREWALLD": "yes"})
    assert result["ok"] is True and "firewalld" in result["actions"][0]
    joined = "\n".join(client.calls)
    assert "--add-port=8466/tcp" in joined and "--reload" in joined


def test_open_firewall_failure_never_blocks_install():
    def handler(cmd):
        raise RuntimeError("boom")

    client = FakeClient(handler)
    result = AD._open_firewall(client, 8466, {"FIREWALLD": "yes"})
    assert result["warnings"], "取消防火墙异常必须如实记录"


# --------------------------------------------------------------------------- #
# 发布包 API
# --------------------------------------------------------------------------- #
def test_packages_api_always_offers_python_mode():
    import app as app_module
    client = app_module.create_app().test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"id": 1, "username": "admin", "role": "admin"}
        sess["username"] = "admin"
    r = client.get("/api/v1/agents/packages")
    assert r.status_code == 200
    packages = r.get_json()["packages"]
    assert any(p["mode"] == "python" for p in packages), packages
    # 源码包可下载
    r = client.get("/api/v1/agents/package?arch=source")
    assert r.status_code == 200
    assert r.headers.get("X-Artifact-Mode") == "python-source"
