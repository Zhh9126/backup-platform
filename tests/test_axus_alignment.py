# -*- coding: utf-8 -*-
"""AXUS 清单未对齐项的补齐回归。

覆盖：60 秒调度下限 / 备份结束通知 / 重删周期隔离 / 增量镜像完整树 /
Windows 防护页面与缓存戳。真机 Windows 未在线，防护的命令层用例见
tests/test_win_protect.py，本文件只做静态契约。
"""
import io
import json
import os
import re
import tarfile
import threading

import pytest

from core import dedup_source as ds
from core import models
from core.engines.base import BackupResult, BackupStatus, BackupType
from core.engines.file import FileBackupEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 60 秒下限
@pytest.mark.parametrize("raw,expect", [
    (0, 1), (-5, 1), ("0", 1), ("1", 1), ("60", 60), (None, None),
    ("", None), ("abc", None), (0.9, 1),
])
def test_interval_clamped_to_min_60_seconds(raw, expect):
    out = models._clamp_interval({"interval_minutes": raw})
    assert out["interval_minutes"] == expect


def test_mixed_sub_schedule_minutes_clamped():
    d = {"full_schedule_type": "interval", "full_schedule_expr": "0",
         "incremental_schedule_type": "interval", "incremental_schedule_expr": "-3"}
    models._clamp_interval(d)
    assert d["full_schedule_expr"] == "1"
    assert d["incremental_schedule_expr"] == "1"
    # cron 类型下 expr 是 cron 表达式，不能被当成分钟数钳掉
    d2 = {"full_schedule_type": "cron", "full_schedule_expr": "*/5 * * * *"}
    models._clamp_interval(d2)
    assert d2["full_schedule_expr"] == "*/5 * * * *"


def test_create_task_applies_clamp(monkeypatch):
    seen = {}

    def fake_execute(sql, params=()):
        cols = sql.split("(")[1].split(")")[0].split(",")
        seen.update(dict(zip([c.strip() for c in cols], params)))
        return 1

    monkeypatch.setattr(models.db, "execute", fake_execute)
    monkeypatch.setattr(models.db, "now_iso", lambda: "now")
    from core import license as license_mod
    monkeypatch.setattr(license_mod, "check_quota", lambda *a, **k: None)
    models.create_task({"db_type": "mysql", "name": "t", "biz_system": "b",
                        "interval_minutes": 0, "schedule_type": "interval"})
    assert seen.get("interval_minutes") == 1


# ---------------------------------------------------------------- 通知
def test_notify_defaults_enabled_for_success_and_failure():
    import config
    assert config.NOTIFY_DEFAULTS["enabled"] is True
    assert config.NOTIFY_DEFAULTS["on_success"] is True
    assert config.NOTIFY_DEFAULTS["on_failure"] is True


def test_notify_async_does_not_block_and_never_raises(monkeypatch):
    from core import notifier
    done = threading.Event()
    seen = []

    def fake_notify(self, event, title, text, html=None):
        seen.append(event)
        done.set()

    monkeypatch.setattr(notifier.Notifier, "notify", fake_notify)
    n = notifier.Notifier()
    n.notify_async("success", "title", "text")
    assert done.wait(2)
    assert seen == ["success"]


def test_notify_without_channels_is_explicit(monkeypatch):
    from core import notifier
    logs = []
    monkeypatch.setattr(notifier.db, "add_log", lambda *a, **k: logs.append(a))
    n = notifier.Notifier()
    n.cfg = {"enabled": True, "on_success": True, "on_failure": True,
             "channels": []}
    n.notify("success", "t", "b")
    assert any("未配置渠道" in str(x) for x in logs)


# ---------------------------------------------------------------- 重删周期
def test_dedup_cycle_isolates_file_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_store_dir", lambda: str(tmp_path))
    g = ds._file_store_dir()
    c1 = ds._file_store_dir("c41")
    c2 = ds._file_store_dir("c42")
    assert g.endswith("files") and "cycles" not in g
    assert "cycles" in c1 and "c41" in c1
    assert c1 != c2 and os.path.isdir(c1) and os.path.isdir(c2)


def _engine(tmp_path, extra=None):
    return FileBackupEngine(
        {"id": 7, "name": "t", "db_type": "file",
         "extra_options": json.dumps(extra or {})}, str(tmp_path))


def test_dedup_cycle_id_default_global_and_cycle(tmp_path, monkeypatch):
    import core.engines.file as fmod
    monkeypatch.setattr(fmod.db, "query_one", lambda *a, **k: {"id": 42})
    assert _engine(tmp_path)._dedup_cycle_id() is None
    assert _engine(tmp_path, {"dedup_scope": "cycle"})._dedup_cycle_id() == "c42"
    monkeypatch.setattr(fmod.db, "query_one", lambda *a, **k: None)
    assert _engine(tmp_path, {"dedup_scope": "cycle"})._dedup_cycle_id() == "c0"
    monkeypatch.setattr(fmod.db, "query_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert _engine(tmp_path, {"dedup_scope": "cycle"})._dedup_cycle_id() == "c0"


# ---------------------------------------------------------------- 镜像完整树
def _make_inc_tar(path: str) -> None:
    with tarfile.open(path, "w:gz") as t:
        for name, data in (("a.txt", b"new-a"), (".deleted.txt", b"b.txt\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def test_mirror_incremental_full_tree_matches_source(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    latest = out / "mirror_latest"
    latest.mkdir()
    (latest / "a.txt").write_bytes(b"old-a")
    (latest / "b.txt").write_bytes(b"old-b")
    tar = str(out / "inc.tar.gz")
    _make_inc_tar(tar)
    eng = _engine(tmp_path, {"mirror_full_tree": True})
    res = BackupResult(True, BackupStatus.SUCCESS, backup_path=tar,
                       message="增量备份成功")
    r = eng._mirror_postprocess(res, str(out), BackupType.INCREMENTAL)
    assert os.path.isdir(r.backup_path)
    assert (open(os.path.join(r.backup_path, "a.txt"), "rb").read()) == b"new-a"
    assert not os.path.exists(os.path.join(r.backup_path, "b.txt"))
    assert (latest / "b.txt").exists() is False      # 删除清单已回放
    changes = [p for p in os.listdir(str(out)) if p.startswith("mirror_inc_changes_")]
    assert len(changes) == 1
    assert (out / changes[0] / "a.txt").read_bytes() == b"new-a"
    assert "增量完整树" in r.message


def test_mirror_incremental_default_keeps_sparse_tree(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "mirror_latest").mkdir()
    tar = str(out / "inc.tar.gz")
    _make_inc_tar(tar)
    eng = _engine(tmp_path)      # 默认不开完整树
    res = BackupResult(True, BackupStatus.SUCCESS, backup_path=tar,
                       message="增量备份成功")
    r = eng._mirror_postprocess(res, str(out), BackupType.INCREMENTAL)
    assert (open(os.path.join(r.backup_path, "a.txt"), "rb").read()) == b"new-a"
    assert not any(p.startswith("mirror_inc_changes_") for p in os.listdir(str(out)))


# ---------------------------------------------------------------- 前端契约
def test_frontend_controls_and_cache_stamp():
    t = io.open(os.path.join(ROOT, "templates/tasks.html"), encoding="utf-8").read()
    assert 'id="t_interval_minutes"' in t and 'min="1"' in t
    f = io.open(os.path.join(ROOT, "templates/file_backup.html"), encoding="utf-8").read()
    assert 'id="f_dedup_scope"' in f and 'id="f_mirror_full_tree"' in f
    assert 'win_protect.js?v=' in f
    b = io.open(os.path.join(ROOT, "templates/base.html"), encoding="utf-8").read()
    # 只要求「升过戳」，不写死某个版本：后续每次改 JS 升戳不应打破回归
    m = re.search(r"app\.js\?v=(\d+)", b)
    assert m and int(m.group(1)) >= 2026111002, "app.js 缓存戳未升"
