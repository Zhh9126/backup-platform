# -*- coding: utf-8 -*-
"""容灾 GTID 分叉判定回归（2026-11-14）。

背景：旧实现把 @@gtid_executed 文本按行切成字符串集合做差集，
「旧主 u:1-6」与「现主 u:1-184」字符串不相等 → 误判分叉，
导致 failback 被 100% 误拦。本用例锁定「按事务区间判包含」的语义。
"""
from core.dr_failover import _parse_gtid_set, gtid_missing, _merge_spans

U1 = "fbf1b9c0-c81d-11f1-a932-005056296480"
U2 = "55cdff31-d1b5-11f1-bb51-0050562404cb"


def test_parse_single_interval():
    m = _parse_gtid_set(U1 + ":1-184")
    assert m == {U1: [(1, 184)]}


def test_parse_multiple_uuids_and_commas():
    text = "%s:1-4,\n%s:1-184" % (U2, U1)
    m = _parse_gtid_set(text)
    assert m[U2] == [(1, 4)]
    assert m[U1] == [(1, 184)]


def test_parse_multi_segments_and_single_numbers():
    m = _parse_gtid_set(U1 + ":1-6:8:10-12")
    assert m[U1] == [(1, 6), (8, 8), (10, 12)]


def test_subset_is_not_divergence():
    """旧主 1-6 是现主 1-184 的子集 → 无分叉（本次修复的核心场景）。"""
    old = _parse_gtid_set(U1 + ":1-6")
    new = _parse_gtid_set(U1 + ":1-184")
    assert gtid_missing(old, new) == []


def test_real_divergence_detected():
    """旧主多出 185-200 → 必须如实报出缺失区间。"""
    old = _parse_gtid_set(U1 + ":1-200")
    new = _parse_gtid_set(U1 + ":1-184")
    assert gtid_missing(old, new) == [(U1, 185, 200)]


def test_unknown_uuid_is_divergence():
    old = _parse_gtid_set(U2 + ":1-4")
    new = _parse_gtid_set(U1 + ":1-184")
    assert gtid_missing(old, new) == [(U2, 1, 4)]


def test_hole_in_middle():
    """现主缺中间段 50-60 → 只报该段，不误报两侧。"""
    old = _parse_gtid_set(U1 + ":1-100")
    new = _parse_gtid_set(U1 + ":1-49:61-100")
    assert gtid_missing(old, new) == [(U1, 50, 60)]


def test_merge_spans_overlapping():
    assert _merge_spans([(1, 5), (4, 9), (20, 21)]) == [(1, 9), (20, 21)]


def test_empty_inputs():
    assert gtid_missing({}, _parse_gtid_set(U1 + ":1-9")) == []
    assert _parse_gtid_set("") == {}
