# -*- coding: utf-8 -*-
"""Windows 文件增量（PowerShell 指纹清单）回归测试。

如实边界：PowerShell 输出用注入的 exec_fn 仿真（验证协议/解析/判据/产物契约），
不是 Windows 真机。真机验证需用户提供已启用 OpenSSH 的 Windows 主机。
"""
import base64
import io
import os
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="win_file_inc_")
os.environ.setdefault("SCHEDULER_ENABLED", "0")
os.environ.setdefault("AIDBM_TESTING", "1")

import pytest  # noqa: E402

from core.engines import win_file as wf  # noqa: E402


def _ps_out(rows, unreadable=0):
    lines = ["__META__\tunreadable=%d" % unreadable]
    lines += ["\t".join(str(x) for x in r) for r in rows]
    body = "\n".join(lines) + "\n"
    return base64.b64encode(body.encode("utf-8")).decode("ascii").encode(), b"", 0


def _decode_cmd(cmd):
    b64 = cmd.split("-EncodedCommand", 1)[1].strip()
    return base64.b64decode(b64).decode("utf-16le")


# ---------------------------------------------------------------- 脚本构造
def test_scan_script_uses_powershell_builtins_only():
    s = wf.build_scan_script(r"D:\备份目录", hash_mode=True, max_files=123)
    assert "Get-ChildItem" in s and "LastWriteTimeUtc" in s
    assert "Get-FileHash" in s and "SHA256" in s
    assert "[Convert]::ToBase64String" in s and "[System.Text.Encoding]::UTF8" in s
    assert "$hashMode = $true" in s and "$maxFiles = 123" in s
    assert "'D:\\备份目录'" in s
    assert "AIDBM_FILE_LIMIT" in s
    assert "pip" not in s and "Invoke-WebRequest" not in s   # 零安装

    s2 = wf.build_scan_script("D:/data", hash_mode=False)
    assert "$hashMode = $false" in s2


def test_scan_script_rejects_unsafe_base():
    with pytest.raises(wf.WinFileError):
        wf.build_scan_script(r"D:\a\..\..\Windows")
    with pytest.raises(wf.WinFileError):
        wf.build_scan_script("")


def test_parse_scan_output_chinese_excludes_and_meta():
    rows = [
        ("中文目录/报告.txt", 12, 1700000000, "a" * 64),
        ("sub/中文.txt", 9, 1700000001, "b" * 64),
        ("tmp/x.log", 3, 1700000002, ""),
        ("@tmp/cache.bin", 5, 1700000003, ""),
    ]
    out, _, _ = _ps_out(rows, unreadable=2)
    files, hashes, meta = wf.parse_scan_output(out.decode(), excludes=["*.log", "@tmp"])
    assert set(files) == {"中文目录/报告.txt", "sub/中文.txt"}
    assert files["中文目录/报告.txt"] == (12, 1700000000)
    assert hashes["sub/中文.txt"] == "b" * 64
    assert meta["unreadable"] == 2 and meta["excluded"] == 2 and meta["total"] == 2


def test_parse_scan_output_rejects_path_traversal_and_bad_payload():
    out, _, _ = _ps_out([("../evil.txt", 1, 1, ""), ("ok.txt", 2, 2, "")])
    files, _, _ = wf.parse_scan_output(out.decode())
    assert set(files) == {"ok.txt"}
    with pytest.raises(wf.WinFileError):
        wf.parse_scan_output("")
    with pytest.raises(wf.WinFileError):
        wf.parse_scan_output("!!!not-base64!!!")


def test_match_excludes_matches_file_py_semantics():
    from core.engines.file import _match_excludes
    rules = ["*.log", "__pycache__", "tmp/cache", "*.tmp"]
    cases = ["a.log", "sub/b.log", "x/__pycache__/y.py", "tmp/cache/z",
             "中文.txt", "a.tmp", "keep.bin"]
    for rel in cases:
        assert wf.match_excludes(rel, rules) == _match_excludes(rel, rules), rel


# ---------------------------------------------------------------- 扫描通道
def test_scan_windows_success_and_hash_mode_flag():
    stdout, stderr, rc = _ps_out([("a.txt", 5, 100, "a" * 64)])
    files, hashes, meta = wf.scan_windows(
        None, "D:/data", exec_fn=lambda cmd: (stdout, stderr, rc))
    assert files == {"a.txt": (5, 100)}
    assert hashes["a.txt"] == "a" * 64
    assert meta["hash_mode"] is False


def test_scan_windows_failures_are_honest():
    with pytest.raises(wf.WinFileError) as e:
        wf.scan_windows(None, "D:/data", exec_fn=lambda c: (b"", b"access denied", 1))
    assert "rc=1" in str(e.value)
    with pytest.raises(wf.WinFileError) as e2:
        wf.scan_windows(None, "D:/data",
                        exec_fn=lambda c: (b"", b"AIDBM_FILE_LIMIT", 1))
    assert "超过指纹清单上限" in str(e2.value)
    with pytest.raises(wf.WinFileError):
        wf.scan_windows(None, "D:/data", exec_fn=lambda c: (b"", b"", 0))


def test_merge_hash_changes_detects_silent_content_change():
    files = {"a": (1, 10), "b": (1, 10), "c": (1, 10)}
    cur = {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}
    prev = {"a": "a" * 64, "b": "0" * 64, "c": "c" * 64}
    out = wf.merge_hash_changes(files, cur, ["c"], prev)
    assert set(out) == {"b", "c"}
    # 任一侧缺指纹时不做全量重传
    assert wf.merge_hash_changes(files, cur, [], {}) == []
    assert wf.merge_hash_changes(files, {}, [], prev) == []


# ---------------------------------------------------------------- 传输 / 产物
class _FakeSFTP:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def get(self, remote, local):
        self.calls.append(remote)
        key = remote.split("D:/data/", 1)[-1]
        if key not in self.data:
            raise IOError("no such file")
        with open(local, "wb") as f:
            f.write(self.data[key])

    def close(self):
        pass


class _FakeClient:
    def __init__(self, data):
        self.sftp = _FakeSFTP(data)

    def open_sftp(self):
        return self.sftp


def test_pull_changed_and_tar_changed_with_deleted():
    cli = _FakeClient({"a.txt": b"AAA", "sub/中文.txt": "中文".encode("utf-8")})
    dest = os.path.join(_TMP, "stage1")
    ok, failures = wf.pull_changed(cli, r"D:\data", ["a.txt", "sub/中文.txt"], dest)
    assert not failures and len(ok) == 2
    assert cli.sftp.calls[0] == "D:/data/a.txt"
    arc = os.path.join(_TMP, "inc1.tar.gz")
    wf.tar_changed(dest, ok, ["gone.txt"], arc)
    with tarfile.open(arc, "r:gz") as tf:
        names = {m.name for m in tf.getmembers()}
        assert {"a.txt", "sub/中文.txt", ".deleted.txt"}.issubset(names)
        deleted = tf.extractfile(".deleted.txt").read().decode()
        assert "gone.txt" in deleted
    assert wf.read_deleted_manifest(arc) == ["gone.txt"]


def test_pull_changed_rejects_unsafe_paths():
    cli = _FakeClient({"a.txt": b"A"})
    ok, failures = wf.pull_changed(cli, "D:/data", ["../evil", "a.txt"],
                                   os.path.join(_TMP, "stage2"))
    assert ok == ["a.txt"] and len(failures) == 1


def test_delete_command_is_pure_ascii_and_path_guarded():
    cmd = wf.win_delete_command(r"D:\restore", ["a.txt", "sub/中文.txt", "../../evil"])
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert cmd.isascii()
    script = _decode_cmd(cmd)
    assert "Remove-Item" in script and "WIN_DELETED_OK" in script
    assert "GetFullPath" in script and "StartsWith" in script
    assert "a.txt" in script and "sub/中文.txt" in script
    assert "../../evil" not in script
    assert wf.win_delete_command("D:/x", []) == ""


# ---------------------------------------------------------------- 引擎集成
def _engine(extra=None, tag=""):
    from core.engines.file import FileBackupEngine
    task = {
        "id": 8801, "name": "win-inc-e2e", "db_type": "file",
        "backup_type": "full", "compress": 0,
        "extra_options": {
            "source_type": "remote", "source_host": "win@127.0.0.1:22",
            "source_paths": ["D:/data" + tag], "target_type": "local",
            **(extra or {}),
        },
    }
    return FileBackupEngine(task, _TMP, logger=None)


def test_engine_full_establishes_baseline_then_incremental(monkeypatch):
    from core.engines import file as fmod
    from core.engines.base import BackupType

    eng = _engine({"win_hash_verify": True}, tag="_a")
    monkeypatch.setattr(eng, "_ssh_host_is_windows", lambda *a, **k: True)
    monkeypatch.setattr(eng, "_resolve_src_pairs", lambda *a, **k: [("D:/data", "dir")])
    monkeypatch.setattr(fmod, "_get_ssh_client", lambda *a, **k: object())
    monkeypatch.setattr(eng, "_atomic_write_archive",
                        lambda writer, path: writer(path))
    monkeypatch.setattr(eng, "_final_archive_path", lambda p: p)
    monkeypatch.setattr(eng, "_tar_windows_to_local",
                        lambda src, path: _write_dummy_tar(path))

    state = {"round": 0}
    roster = [
        {"a.txt": (5, 100), "sub/中文.txt": (9, 101)},
        {"a.txt": (8, 200), "new.txt": (3, 300)},   # a 改、中文.txt 删、new 增
    ]
    roster_hashes = [
        {"a.txt": "a" * 64, "sub/中文.txt": "b" * 64},
        {"a.txt": "c" * 64, "new.txt": "d" * 64},
    ]

    def fake_scan(client, base, excludes=None, hash_mode=False,
                  max_files=wf.DEFAULT_MAX_FILES, timeout=900, exec_fn=None):
        i = min(state["round"], 1)
        return dict(roster[i]), dict(roster_hashes[i]), {"unreadable": 0}

    def fake_pull(client, base, changed, dest_dir):
        for rel in changed:
            p = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(("data-" + rel).encode("utf-8"))
        return list(changed), []

    monkeypatch.setattr(wf, "scan_windows", fake_scan)
    monkeypatch.setattr(wf, "pull_changed", fake_pull)

    full = eng.backup(BackupType.FULL)
    assert full.success, full.message
    snap = eng._load_snapshot()
    assert snap == roster[0]
    assert eng._load_snapshot_hashes()["a.txt"] == "a" * 64

    state["round"] = 1
    inc = eng.backup(BackupType.INCREMENTAL)
    assert inc.success, inc.message
    assert "增量备份完成" in inc.message
    with tarfile.open(inc.backup_path, "r:gz") as tf:
        names = {m.name for m in tf.getmembers() if m.isfile()}
        assert "a.txt" in names and "new.txt" in names
        assert "sub/中文.txt" not in names
        assert ".deleted.txt" in names
        assert "sub/中文.txt" in tf.extractfile(".deleted.txt").read().decode()
    assert eng._load_snapshot() == roster[1]


def test_engine_hash_mode_detects_silent_rewrite(monkeypatch):
    """mtime 与 size 都没变，但内容被改写 → 精确模式仍要检出。"""
    from core.engines import file as fmod
    from core.engines.base import BackupType
    eng = _engine({"win_hash_verify": True}, tag="_b")
    monkeypatch.setattr(eng, "_ssh_host_is_windows", lambda *a, **k: True)
    monkeypatch.setattr(eng, "_resolve_src_pairs", lambda *a, **k: [("D:/data", "dir")])
    monkeypatch.setattr(fmod, "_get_ssh_client", lambda *a, **k: object())
    monkeypatch.setattr(eng, "_atomic_write_archive", lambda writer, path: writer(path))
    monkeypatch.setattr(eng, "_final_archive_path", lambda p: p)
    monkeypatch.setattr(eng, "_tar_windows_to_local",
                        lambda src, path: _write_dummy_tar(path))
    state = {"round": 0}

    def fake_scan(client, base, excludes=None, hash_mode=False,
                  max_files=wf.DEFAULT_MAX_FILES, timeout=900, exec_fn=None):
        h = "a" * 64 if state["round"] == 0 else "f" * 64
        return {"a.txt": (5, 100)}, {"a.txt": h}, {"unreadable": 0}

    def fake_pull(client, base, changed, dest_dir):
        assert changed == ["a.txt"]        # 精确模式把它补进变化集
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "a.txt"), "wb") as f:
            f.write(b"rewritten")
        return list(changed), []

    monkeypatch.setattr(wf, "scan_windows", fake_scan)
    monkeypatch.setattr(wf, "pull_changed", fake_pull)
    assert eng.backup(BackupType.FULL).success
    state["round"] = 1
    r = eng.backup(BackupType.INCREMENTAL)
    assert r.success and "变化=1" in r.message


def test_engine_scan_failure_never_saves_empty_baseline(monkeypatch):
    from core.engines import file as fmod
    from core.engines.base import BackupType
    eng = _engine(tag="_c")
    monkeypatch.setattr(eng, "_ssh_host_is_windows", lambda *a, **k: True)
    monkeypatch.setattr(eng, "_resolve_src_pairs", lambda *a, **k: [("D:/data", "dir")])
    monkeypatch.setattr(fmod, "_get_ssh_client", lambda *a, **k: object())
    monkeypatch.setattr(eng, "_atomic_write_archive", lambda writer, path: writer(path))
    monkeypatch.setattr(eng, "_final_archive_path", lambda p: p)
    monkeypatch.setattr(eng, "_tar_windows_to_local",
                        lambda src, path: _write_dummy_tar(path))
    monkeypatch.setattr(wf, "scan_windows",
                        lambda *a, **k: (_ for _ in ()).throw(wf.WinFileError("清单失败")))
    assert eng.backup(BackupType.FULL).success
    assert eng._load_snapshot() is None       # 关键：不写空快照


def _write_dummy_tar(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("a.txt")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))


def test_frontend_controls_and_cache_stamp():
    tpl = io.open(os.path.join(ROOT, "templates", "file_backup.html"),
                  encoding="utf-8").read()
    js = io.open(os.path.join(ROOT, "static", "js", "app.js"),
                 encoding="utf-8").read()
    base = io.open(os.path.join(ROOT, "templates", "base.html"),
                   encoding="utf-8").read()
    assert 'id="f_win_hash_verify"' in tpl
    assert 'id="f_win_max_files"' in tpl
    assert "Get-FileHash" in tpl
    assert "win_hash_verify" in js and "win_max_files" in js
    import re
    m = re.search(r"app\.js\?v=(\d+)", base)
    assert m and int(m.group(1)) >= 2026111002, "app.js 缓存戳未升"
