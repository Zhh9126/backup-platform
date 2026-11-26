# -*- coding: utf-8 -*-
"""Agent 自更新模块（装一次即可长期跟随平台版本）。

覆盖：清单比对、摘要校验、原子替换、目录穿越防护、重启失败如实标记。
"""
import hashlib
import io
import os
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

import selfupdate  # noqa: E402


def _make_bundle(files=None):
    """打包一份源码包（内容寻址用）。"""
    files = files or {"backup_agent.py": "# v2\n", "plugins.py": "# v2\n",
                      "iso9660.py": "# v2\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    return blob, hashlib.sha256(blob).hexdigest()


def test_state_file_roundtrip():
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    state = os.path.join(tmp, "state.json")
    original = selfupdate.STATE_FILE
    selfupdate.STATE_FILE = state
    try:
        assert selfupdate._read_state() == {}
        assert selfupdate._write_state({"sha256": "abc", "version": "1.0.0"})
        assert selfupdate._read_state()["sha256"] == "abc"
        # 脏数据不抛异常
        with open(state, "w", encoding="utf-8") as fp:
            fp.write("not-json")
        assert selfupdate._read_state() == {}
    finally:
        selfupdate.STATE_FILE = original


def test_apply_replaces_files_atomically():
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    install = os.path.join(tmp, "opt")
    os.makedirs(install, exist_ok=True)
    for name in selfupdate.REQUIRED_FILES:
        with open(os.path.join(install, name), "w", encoding="utf-8") as fp:
            fp.write("# old\n")
    blob, _sha = _make_bundle()
    updater = selfupdate.SelfUpdater(platform_url="http://x", token="t",
                                     state_file=os.path.join(tmp, "s.json"),
                                     install_dir=install)
    assert updater._apply(blob) is True
    for name in selfupdate.REQUIRED_FILES:
        with open(os.path.join(install, name), encoding="utf-8") as fp:
            assert fp.read() == "# v2\n"
    # 临时文件不应残留
    assert not [f for f in os.listdir(install) if f.endswith(".new")]


def test_apply_rejects_bundle_missing_files():
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    install = os.path.join(tmp, "opt")
    os.makedirs(install, exist_ok=True)
    blob, _sha = _make_bundle(files={"backup_agent.py": "# v2\n"})
    updater = selfupdate.SelfUpdater(platform_url="http://x", token="t",
                                     state_file=os.path.join(tmp, "s.json"),
                                     install_dir=install)
    try:
        updater._apply(blob)
        raise AssertionError("缺文件应拒绝")
    except ValueError as exc:
        assert "缺少文件" in str(exc)


def test_apply_ignores_path_traversal_members():
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    install = os.path.join(tmp, "opt")
    os.makedirs(install, exist_ok=True)
    blob, _sha = _make_bundle(files={
        "backup_agent.py": "# v2\n", "plugins.py": "# v2\n",
        "iso9660.py": "# v2\n", "../evil.py": "print('boom')\n"})
    updater = selfupdate.SelfUpdater(platform_url="http://x", token="t",
                                     state_file=os.path.join(tmp, "s.json"),
                                     install_dir=install)
    updater._apply(blob)
    assert not os.path.exists(os.path.join(tmp, "evil.py"))


def test_run_once_reports_current_when_sha_matches(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    install = os.path.join(tmp, "opt")
    os.makedirs(install, exist_ok=True)
    _blob, sha = _make_bundle()
    state_file = os.path.join(tmp, "s.json")
    updater = selfupdate.SelfUpdater(platform_url="http://x", token="t",
                                     state_file=state_file,
                                     install_dir=install)
    assert selfupdate._write_state({"sha256": sha}, state_file) is True
    monkeypatch.setattr(updater, "manifest", lambda: {"sha256": sha, "version": "9.9.9"})
    status, note = updater.run_once()
    assert status == "current" and "最新" in note


def test_run_once_detects_digest_mismatch(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent_selfupdate_")
    install = os.path.join(tmp, "opt")
    os.makedirs(install, exist_ok=True)
    blob, _sha = _make_bundle()
    updater = selfupdate.SelfUpdater(platform_url="http://x", token="t",
                                     state_file=os.path.join(tmp, "s.json"),
                                     install_dir=install)
    monkeypatch.setattr(updater, "manifest",
                        lambda: {"sha256": "deadbeef", "version": "9.9.9"})
    # 让 download() 自己校验摘要（用本地字节代替网络下载）
    monkeypatch.setattr(selfupdate, "_http_get", lambda *_a, **_k: blob)
    status, note = updater.run_once()
    assert status == "error"
    assert "摘要不符" in note


def test_run_once_without_platform_url_is_honest():
    updater = selfupdate.SelfUpdater(platform_url="", token="")
    status, note = updater.run_once()
    assert status == "error" and "未配置" in note


def test_start_from_env_disabled_without_config():
    old = {k: os.environ.pop(k, None) for k in
           ("AIDBM_PLATFORM_URL", "AIDBM_AGENT_TOKEN", "AIDBM_AGENT_SELFUPDATE")}
    try:
        assert selfupdate.start_from_env() is None
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def test_start_from_env_returns_none_when_explicitly_disabled():
    old = dict(os.environ)
    os.environ["AIDBM_PLATFORM_URL"] = "http://x"
    os.environ["AIDBM_AGENT_TOKEN"] = "t"
    os.environ["AIDBM_AGENT_SELFUPDATE"] = "0"
    try:
        assert selfupdate.start_from_env() is None
    finally:
        os.environ.clear()
        os.environ.update(old)
