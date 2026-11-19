# -*- coding: utf-8 -*-
"""主机内核/IO 调优诊断单测（对标 PB 级方案 §3.2）。

只读检查：断言结构完整、比较方向正确、绝不出现「自动改系统」的副作用。
"""
from core import host_tuning


def test_cmp_directions():
    assert host_tuning._cmp("5", "5", "le") is True
    assert host_tuning._cmp("10", "5", "le") is False
    assert host_tuning._cmp("262144", "262144", "ge") is True
    assert host_tuning._cmp("1024", "262144", "ge") is False
    assert host_tuning._cmp("abc", "5", "le") is None


def test_check_sysctl_covers_recommendations():
    rows = host_tuning.check_sysctl()
    assert len(rows) == len(host_tuning.RECOMMENDED_SYSCTL)
    for r in rows:
        assert {"item", "actual", "recommend", "ok", "hint", "apply_cmd"} <= set(r)
        assert r["apply_cmd"].startswith("sysctl -w ")


def test_check_io_scheduler_structure():
    rows = host_tuning.check_io_scheduler()
    for r in rows:
        assert r["type"] in ("HDD", "SSD/NVMe")
        assert r["recommend"] in ("none", "mq-deadline")
        # NVMe/SSD 建议 none，机械盘建议 mq-deadline
        assert (r["recommend"] == "none") == (r["type"] == "SSD/NVMe")


def test_check_io_uring_reports_kernel():
    r = host_tuning.check_io_uring()
    assert "kernel" in r and r["kernel"]
    assert isinstance(r["ok"], bool)


def test_inspect_shape_and_verdict():
    rep = host_tuning.inspect()
    assert rep["summary"]["total"] == len(rep["checks"])
    assert rep["summary"]["verdict"] in ("建议调优", "符合推荐")
    assert rep["summary"]["ok"] + rep["summary"]["advice"] + \
        rep["summary"]["unknown"] == rep["summary"]["total"]
    assert "disk" in rep and "platform" in rep


def test_no_side_effects():
    """诊断必须是只读的：调用前后 sysctl 值不变。"""
    before = host_tuning._read_sysctl("vm.swappiness")
    host_tuning.inspect()
    assert host_tuning._read_sysctl("vm.swappiness") == before
