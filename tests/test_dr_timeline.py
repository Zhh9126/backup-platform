# -*- coding: utf-8 -*-
"""容灾统一事件时间线回归（2026-11-20）。

覆盖 core/dr_timeline.py 的关键口径：
1. 时间规范化：ISO 与 DATETIME 两种格式统一后可正确排序；
2. 四类事件（切换 / 演练 / 巡检 / 告警）都能进时间线；
3. 类型过滤与链路过滤生效；
4. 合规概览：演练合规率 / 切换成功率 / 平均 RTO 口径正确（dry_run 不计入）。
"""
from datetime import datetime, timedelta

from core import dr_timeline as tl


def _ts(days_ago=0, sep="T"):
    t = datetime.now() - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d" + sep + "%H:%M:%S")


# ---------------------------------------------------------------------
# 时间规范化与排序
# ---------------------------------------------------------------------

def test_norm_ts_unifies_formats():
    a = tl._norm_ts("2026-11-20T10:00:00+08:00")
    b = tl._norm_ts("2026-11-20 10:00:00")
    assert a == b == "2026-11-20T10:00:00"


def test_norm_ts_empty():
    assert tl._norm_ts(None) == ""
    assert tl._norm_ts("") == ""


def test_disp_ts():
    assert tl._disp_ts("2026-11-20T10:00:00+08:00") == "2026-11-20 10:00:00"
    assert tl._disp_ts("") == ""


def test_severity_mapping():
    assert tl._sev("success") == "info"
    assert tl._sev("failed") == "error"
    assert tl._sev("cancelled") == "warn"
    assert tl._sev("blocked") == "warn"
    assert tl._sev("未知状态") == "info"


# ---------------------------------------------------------------------
# 事件聚合
# ---------------------------------------------------------------------

def _patch_sources(monkeypatch, switch=None, drill=None, inspection=None,
                   alert=None, gap=None):
    monkeypatch.setattr(tl, "_collect_switch",
                        lambda since, limit: list(switch or []), raising=False)
    monkeypatch.setattr(tl, "_collect_drill",
                        lambda since, limit: list(drill or []), raising=False)
    monkeypatch.setattr(tl, "_collect_inspection",
                        lambda since, limit: list(inspection or []), raising=False)
    monkeypatch.setattr(tl, "_collect_alert",
                        lambda since, limit: list(alert or []), raising=False)
    monkeypatch.setattr(tl, "_collect_gap",
                        lambda since, limit: list(gap or []), raising=False)


def _ev(kind, ts, sev="info", link_id=None, eid=1):
    return {"kind": kind, "kind_label": tl.KIND_LABELS[kind], "id": eid,
            "at": ts, "ts": ts, "title": kind, "status": "success",
            "severity": sev, "link_id": link_id, "link_name": "",
            "summary": "", "rto_sec": 0.0, "detail": {}}


def test_timeline_aggregates_five_sources(monkeypatch):
    _patch_sources(monkeypatch,
                   switch=[_ev("switch", _ts(0), eid=1)],
                   drill=[_ev("drill", _ts(1), eid=2)],
                   inspection=[_ev("inspection", _ts(2), eid=3)],
                   alert=[_ev("alert", _ts(3), sev="error", eid=4)],
                   gap=[_ev("gap", _ts(0), sev="warn", eid=5)])
    res = tl.timeline(days=30)
    kinds = {e["kind"] for e in res["events"]}
    assert kinds == {"switch", "drill", "inspection", "alert", "gap"}
    assert res["counts"]["switch"] == 1
    assert res["counts"]["gap"] == 1
    assert res["severity_counts"]["error"] == 1
    assert res["days"] == 30


def test_timeline_sorted_desc(monkeypatch):
    _patch_sources(monkeypatch,
                   switch=[_ev("switch", _ts(3), eid=1)],
                   drill=[_ev("drill", _ts(1), eid=2)])
    res = tl.timeline(days=30)
    tss = [e["ts"] for e in res["events"]]
    assert tss == sorted(tss, reverse=True)


def test_timeline_kind_filter(monkeypatch):
    _patch_sources(monkeypatch,
                   switch=[_ev("switch", _ts(0), eid=1)],
                   drill=[_ev("drill", _ts(1), eid=2)])
    res = tl.timeline(days=30, kinds="drill")
    assert {e["kind"] for e in res["events"]} == {"drill"}


def test_timeline_link_filter(monkeypatch):
    _patch_sources(monkeypatch,
                   switch=[_ev("switch", _ts(0), link_id=7, eid=1),
                           _ev("switch", _ts(0), link_id=9, eid=2)],
                   inspection=[_ev("inspection", _ts(0), link_id=None, eid=3)])
    res = tl.timeline(days=30, link_id=7)
    ids = {e["id"] for e in res["events"]}
    assert 2 not in ids          # 其它链路被过滤
    assert 1 in ids
    assert 3 in ids              # 巡检是全局事件，保留


def test_timeline_limit(monkeypatch):
    _patch_sources(monkeypatch,
                   switch=[_ev("switch", _ts(0), eid=i) for i in range(10)])
    res = tl.timeline(days=30, limit=3)
    assert len(res["events"]) == 3


def test_timeline_empty_is_honest(monkeypatch):
    _patch_sources(monkeypatch)
    res = tl.timeline(days=30)
    assert res["events"] == []
    assert res["counts"] == {k: 0 for k in tl.KINDS}
    assert res["severity_counts"] == {"info": 0, "warn": 0, "error": 0}


# ---------------------------------------------------------------------
# 合规概览
# ---------------------------------------------------------------------

def test_compliance_rates(monkeypatch):
    drills = [
        {"status": "completed", "score": 90, "started_at": _ts(1)},
        {"status": "completed", "score": 40, "started_at": _ts(2)},
        {"status": "failed", "score": 0, "started_at": _ts(3)},
        {"status": "pending", "score": None, "started_at": _ts(4)},
    ]
    runs = [
        {"status": "success", "mode": "execute", "rto_sec": 10.0,
         "created_at": _ts(1)},
        {"status": "success", "mode": "execute", "rto_sec": 20.0,
         "created_at": _ts(2)},
        {"status": "failed", "mode": "execute", "rto_sec": 0,
         "created_at": _ts(3)},
        {"status": "success", "mode": "dry_run", "rto_sec": 0,
         "created_at": _ts(4)},
    ]
    monkeypatch.setattr(tl.models, "list_drills", lambda: drills)
    from core import dr_orchestrator
    monkeypatch.setattr(dr_orchestrator, "list_runs", lambda limit=500: runs)
    res = tl.compliance(days=30)
    # 只有 2 次「已完成」演练，评分 ≥60 的算 1 次 → 50%
    assert res["drill_total"] == 2
    assert res["drill_pass"] == 1
    assert res["drill_rate"] == 50.0
    # dry_run 不计入成功率 → 3 次真实切换，2 次成功
    assert res["switch_total"] == 3
    assert res["switch_ok"] == 2
    assert res["switch_rate"] == round(200.0 / 3, 1)
    assert res["avg_rto"] == 15.0


def test_compliance_empty_is_honest(monkeypatch):
    monkeypatch.setattr(tl.models, "list_drills", lambda: [])
    from core import dr_orchestrator
    monkeypatch.setattr(dr_orchestrator, "list_runs", lambda limit=500: [])
    res = tl.compliance(days=30)
    assert res["drill_total"] == 0 and res["drill_rate"] == 0.0
    assert res["switch_total"] == 0 and res["switch_rate"] == 0.0
    assert res["avg_rto"] == 0.0


def test_compliance_ignores_out_of_window(monkeypatch):
    old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
    monkeypatch.setattr(tl.models, "list_drills",
                        lambda: [{"status": "completed", "score": 100,
                                  "started_at": old}])
    from core import dr_orchestrator
    monkeypatch.setattr(dr_orchestrator, "list_runs", lambda limit=500: [])
    res = tl.compliance(days=30)
    assert res["drill_total"] == 0
