# -*- coding: utf-8 -*-
"""沙箱能力探测单测（PB 级方案 §6）。

要求：
  - 结构完整、每项都有 available + reason，绝不抛异常；
  - 探测结论如实：AIDBM 现阶段演练是库级隔离，探测结果只描述宿主能力；
  - netns 探测不得残留命名空间（探测后自动删除）。
"""
import subprocess

from core import sandbox_probe as sp


def test_probe_netns_structure():
    r = sp.probe_netns()
    assert {"item", "name", "available", "reason", "hint"} <= set(r)
    assert isinstance(r["available"], bool)
    assert r["reason"]


def test_probe_kvm_reports_reason_when_unavailable(monkeypatch):
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    r = sp.probe_kvm()
    assert r["available"] is False
    assert "不存在" in r["reason"]


def test_probe_nbd_unavailable(monkeypatch):
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp.os.path, "isdir", lambda p: False)
    r = sp.probe_nbd()
    assert r["available"] is False
    assert "nbd" in r["reason"]


def test_probe_iscsi_absent(monkeypatch):
    monkeypatch.setattr(sp.shutil, "which", lambda *a: None)
    r = sp.probe_iscsi()
    assert r["available"] is False


def test_overall_readiness_shape():
    rep = sp.probe_sandbox_readiness()
    assert len(rep["checks"]) == 4
    assert rep["isolation_level"] in ("netns+kvm", "netns", "db-level")
    assert rep["current_drill_level"] == "db-level"
    assert "库级" in rep["note"]
    assert isinstance(rep["in_container"], bool)


def test_never_raises_on_broken_env(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sp, "_run", _boom)
    rep = sp.probe_sandbox_readiness()      # 内部 _run 被替换，探测应仍返回结构
    assert len(rep["checks"]) == 4


def test_netns_probe_cleans_up(monkeypatch):
    """成功路径必须调用 delete（不留残留命名空间）。"""
    calls = []

    def _fake_run(cmd, timeout=8):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(sp.shutil, "which", lambda x: "/usr/sbin/ip")
    monkeypatch.setattr(sp, "_run", _fake_run)
    r = sp.probe_netns()
    assert r["available"] is True
    assert any(c[:3] == ["ip", "netns", "add"] for c in calls)
    assert any(c[:3] == ["ip", "netns", "delete"] for c in calls)
