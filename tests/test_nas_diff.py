# -*- coding: utf-8 -*-
"""NAS 快照差量「探测位」单测（PB 级方案 §2）。

诚实性要求：
  - 探测只做连通/认证判断，401/403 与"连不上"必须区分（现场排障信息不同）；
  - 变更清单接口必须显式 NotImplementedError，**不得返回伪造数据**；
  - 未知厂商给明确错误，不静默。
"""
import urllib.error

import pytest

from core import nas_diff


def test_vendors_catalog():
    vs = nas_diff.vendors()
    assert set(vs) == {"netapp", "isilon", "dorado"}
    for v in vs.values():
        assert v["name"] and v["diff_api"] and v["port"]


def test_probe_unknown_vendor():
    r = nas_diff.probe("no_such", "10.0.0.1")
    assert r["ok"] is False
    assert "不支持" in r["reason"]


def test_probe_empty_host():
    r = nas_diff.probe("netapp", "")
    assert r["ok"] is False and "地址" in r["reason"]


def _fake_get(status, body="{}"):
    def _g(url, username="", password="", verify_tls=False, timeout=8, headers=None):
        return status, body
    return _g


def test_probe_auth_failure_is_distinguishable(monkeypatch):
    monkeypatch.setattr(nas_diff, "_http_get", _fake_get(401, '{"error":"auth"}'))
    r = nas_diff.probe("netapp", "10.0.0.9", username="ro", password="bad")
    assert r["ok"] is False
    assert r["status"] == 401
    assert "认证失败" in r["reason"]


def test_probe_success(monkeypatch):
    monkeypatch.setattr(nas_diff, "_http_get", _fake_get(200, '{"num_records":1}'))
    r = nas_diff.probe("isilon", "10.0.0.9")
    assert r["ok"] is True and r["auth_ok"] is True
    assert r["diff_implemented"] is False
    assert "仅探测" in r["capability"]


def test_probe_connection_error(monkeypatch):
    monkeypatch.setattr(nas_diff, "_http_get", _fake_get(-1, "连接失败: refused"))
    r = nas_diff.probe("dorado", "10.0.0.9")
    assert r["ok"] is False
    assert "连接失败" in r["reason"] or r["status"] == -1


@pytest.mark.parametrize("vendor", ["netapp", "isilon", "dorado"])
def test_diff_not_implemented(vendor):
    with pytest.raises(NotImplementedError) as ei:
        nas_diff.list_snapshot_changes(vendor)
    msg = str(ei.value)
    assert "尚未实现" in msg and "真机" in msg


def test_diff_unknown_vendor():
    with pytest.raises(ValueError):
        nas_diff.list_snapshot_changes("xxx")
