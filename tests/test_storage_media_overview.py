# -*- coding: utf-8 -*-
"""存储介质统一总览回归（2026-11-13）。

把分散在「存储池 / 对象存储 / 磁带库 / NAS / FTP / NFS / 外挂」的介质收敛成
一张表：状态 + 用量 + 最近备份。这里锁定行模型与用量采集的诚实语义：
- 本地/挂载点用量精确（disk_usage），取不到就返回空而不是 0；
- 远端介质默认"未采集"，由「探测」接口按需拉取，避免总览页被慢介质拖死；
- summary 计数与 rows 一致。
"""
import os

from core import storage_media as sm


def test_status_of_four_states():
    assert sm._status_of({"enabled": 0})[0] == "disabled"
    assert sm._status_of({"enabled": 1, "last_error": "boom"})[0] == "error"
    assert sm._status_of({"enabled": 1, "last_error": "",
                          "last_test_at": "2026-11-13T10:00:00"})[0] == "ok"
    assert sm._status_of({"enabled": 1})[0] == "untested"


def test_local_usage_real_and_missing(tmp_path):
    u = sm._local_usage(str(tmp_path))
    assert u.get("capacity_bytes", 0) > 0
    assert 0 <= (u.get("used_pct") or 0) <= 100
    assert sm._local_usage(str(tmp_path / "no_such_dir")) == {}
    assert sm._local_usage("") == {}


def test_media_overview_shape_and_summary_consistency():
    d = sm.media_overview()
    assert {"rows", "summary", "generated_at"} <= set(d)
    rows, s = d["rows"], d["summary"]
    assert s["total"] == len(rows)
    assert s["ok"] + s["error"] + s["untested"] + s["disabled"] == len(rows)
    for r in rows:
        assert {"key", "category", "name", "type", "status", "used_bytes",
                "recent_backup", "detail_url"} <= set(r)
        assert r["status"] in ("ok", "error", "untested", "disabled")


def test_probe_missing_target():
    res = sm.probe_media(999999)
    assert res.get("ok") is False
    assert "不存在" in (res.get("message") or "")


def test_local_mount_rows_have_precise_usage():
    """local/mount 类型的行必须带精确用量（有目录时）。"""
    d = sm.media_overview()
    checked = 0
    for r in d["rows"]:
        if r["type"] in ("local", "mount") and r.get("endpoint") and \
                os.path.isdir(r["endpoint"]):
            assert r["used_bytes"] and r["capacity_bytes"], r
            assert r["probe"] is False, "本地介质无需探测（用量已精确）"
            checked += 1
    if not checked:
        import pytest
        pytest.skip("当前没有可读的 local/mount 介质")
