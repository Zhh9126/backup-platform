# -*- coding: utf-8 -*-
"""容灾时间线增强回归（2026-11-20 第二轮）。

覆盖本轮新增的三处能力：
1. **保护目标缺口入线**（kind=gap）：来自 core/goals.py 的**当前评估快照**，
   必须打 snapshot 标记（不得伪装成历史事件），且按链路过滤时按全局事件保留；
2. **切换事件 SLO 判定**：RTO 超过链路 SLO 目标才标记超标；
   失败作业的 RTO 无意义，**不得**标超标；
3. **演练合格判定**与**巡检失败项展开**：不合格演练要标出来，
   巡检要能看到「哪条链路、哪一项挂了」。
"""
from datetime import datetime, timedelta

from core import dr_timeline as tl


def _ts(days_ago=0):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------
# 1. 保护目标缺口
# ---------------------------------------------------------------------

def _patch_goals(monkeypatch, gaps):
    from core import goals
    monkeypatch.setattr(goals, "protection_gaps",
                        lambda: {"gaps": gaps, "fail_count": 1, "warn_count": 0,
                                 "total": len(gaps)})


def test_gap_source_marks_snapshot(monkeypatch):
    """缺口是「此刻的评估结果」而非历史事件——必须带 snapshot 标记。"""
    _patch_goals(monkeypatch, [
        {"task_id": 11, "task_name": "订单库", "db_type": "mysql",
         "rpo_target_min": 5, "level": "fail", "code": "rpo_no_cdp",
         "message": "RPO 目标 5 分钟但未开启实时保护"},
    ])
    evs = tl._collect_gap(tl._since(30), 50)
    assert len(evs) == 1
    e = evs[0]
    assert e["kind"] == "gap"
    assert e["snapshot"] is True          # 关键：不得伪装成历史事件
    assert e["severity"] == "error"        # level=fail → error
    assert e["detail"]["code"] == "rpo_no_cdp"
    assert e["detail"]["task_id"] == 11
    assert "RPO" in e["title"]


def test_gap_warn_level_maps_to_warn(monkeypatch):
    _patch_goals(monkeypatch, [
        {"task_id": 12, "task_name": "库", "db_type": "mysql",
         "rpo_target_min": 15, "level": "warn", "code": "interval_gt_rpo",
         "message": "备份间隔大于 RPO 目标"},
    ])
    e = tl._collect_gap(tl._since(30), 50)[0]
    assert e["severity"] == "warn"
    assert e["snapshot"] is True


def test_gap_source_empty_is_honest(monkeypatch):
    """没有缺口就是没有——不得编造。"""
    _patch_goals(monkeypatch, [])
    assert tl._collect_gap(tl._since(30), 50) == []


def test_gap_survives_link_filter(monkeypatch):
    """缺口是备份任务维度、无链路归属 → 按链路过滤时按全局事件保留。"""
    monkeypatch.setattr(tl, "_collect_switch", lambda s, l: [], raising=False)
    monkeypatch.setattr(tl, "_collect_drill", lambda s, l: [], raising=False)
    monkeypatch.setattr(tl, "_collect_inspection", lambda s, l: [], raising=False)
    monkeypatch.setattr(tl, "_collect_alert", lambda s, l: [], raising=False)
    _patch_goals(monkeypatch, [
        {"task_id": 11, "task_name": "T", "db_type": "mysql",
         "rpo_target_min": 5, "level": "fail", "code": "rpo_no_cdp",
         "message": "m"},
    ])
    res = tl.timeline(days=30, link_id=7)
    assert [e["kind"] for e in res["events"]] == ["gap"]


def test_mixed_id_types_do_not_break_sorting(monkeypatch):
    """事件 id 类型不统一（整数 / "<父ID>-<子键>" 字符串）——排序不得抛类型错。

    回归：巡检失败项展开与保护目标缺口的 id 是字符串，与切换/演练的整数 id
    混在一起比较会抛 '<' not supported between instances of 'int' and 'str'。
    """
    monkeypatch.setattr(tl, "_collect_switch", lambda s, l: [
        {"kind": "switch", "kind_label": "切换", "id": 4, "at": _ts(0),
         "ts": _ts(0), "title": "切换", "status": "success", "severity": "info",
         "link_id": 1, "link_name": "L", "summary": "", "rto_sec": 1.0,
         "detail": {}}], raising=False)
    monkeypatch.setattr(tl, "_collect_drill", lambda s, l: [], raising=False)
    monkeypatch.setattr(tl, "_collect_inspection", lambda s, l: [
        {"kind": "inspection", "kind_label": "巡检", "id": "3-7",
         "at": _ts(0), "ts": _ts(0), "title": "巡检项", "status": "fail",
         "severity": "error", "link_id": 7, "link_name": "L", "summary": "",
         "rto_sec": 0.0, "detail": {}}], raising=False)
    monkeypatch.setattr(tl, "_collect_alert", lambda s, l: [], raising=False)
    monkeypatch.setattr(tl, "_collect_gap", lambda s, l: [
        {"kind": "gap", "kind_label": "目标缺口", "id": "11-rpo_no_cdp",
         "at": _ts(0), "ts": _ts(0), "title": "缺口", "status": "fail",
         "severity": "error", "link_id": None, "link_name": "", "summary": "",
         "rto_sec": 0.0, "detail": {}}], raising=False)
    res = tl.timeline(days=30)
    assert len(res["events"]) == 3
    assert res["counts"]["switch"] == 1
    assert res["counts"]["gap"] == 1


# ---------------------------------------------------------------------
# 2. 切换事件 SLO 判定
# ---------------------------------------------------------------------

def _patch_switch_src(monkeypatch, runs, slo=300.0):
    from core import dr_orchestrator
    monkeypatch.setattr(dr_orchestrator, "ensure_schema", lambda: None, raising=False)
    monkeypatch.setattr(dr_orchestrator, "list_runs", lambda limit=50: runs, raising=False)
    monkeypatch.setattr(tl, "_slo_map", lambda: {1: slo}, raising=False)


def test_switch_marks_slo_breach(monkeypatch):
    _patch_switch_src(monkeypatch, [
        {"id": 1, "status": "success", "direction": "failover", "scope": "full",
         "rto_sec": 900.0, "created_at": _ts(0), "link_id": 1,
         "message": "完成", "link_name": "L"},
    ], slo=300.0)
    e = tl._collect_switch(tl._since(30), 50)[0]
    assert e["detail"]["slo_breached"] is True
    assert e["detail"]["slo_rto_sec"] == 300.0
    assert e["severity"] == "error"


def test_switch_within_slo_not_breached(monkeypatch):
    _patch_switch_src(monkeypatch, [
        {"id": 1, "status": "success", "direction": "failover", "scope": "full",
         "rto_sec": 15.0, "created_at": _ts(0), "link_id": 1,
         "message": "完成", "link_name": "L"},
    ], slo=300.0)
    e = tl._collect_switch(tl._since(30), 50)[0]
    assert e["detail"]["slo_breached"] is False
    assert e["severity"] == "info"


def test_failed_switch_rto_never_marked_breach(monkeypatch):
    """失败作业的 RTO 无意义——不得标「超标」，避免误报。"""
    _patch_switch_src(monkeypatch, [
        {"id": 1, "status": "failed", "direction": "failover", "scope": "full",
         "rto_sec": 9999.0, "created_at": _ts(0), "link_id": 1,
         "message": "失败", "link_name": "L"},
    ], slo=300.0)
    e = tl._collect_switch(tl._since(30), 50)[0]
    assert e["detail"]["slo_breached"] is False
    assert e["severity"] == "error"      # 仍然是失败，只是原因不是超标


# ---------------------------------------------------------------------
# 3. 演练合格判定
# ---------------------------------------------------------------------

def _patch_drills(monkeypatch, drills):
    monkeypatch.setattr(tl.models, "list_drills", lambda: drills, raising=False)


def test_drill_below_pass_score_is_flagged(monkeypatch):
    _patch_drills(monkeypatch, [
        {"id": 1, "status": "completed", "score": 45, "drill_type": "switch",
         "name": "切换演练", "started_at": _ts(1)},
    ])
    e = tl._collect_drill(tl._since(30), 50)[0]
    assert e["detail"]["passed"] is False
    assert e["severity"] == "error"
    assert "未达" in e["summary"]


def test_drill_passing_is_ok(monkeypatch):
    _patch_drills(monkeypatch, [
        {"id": 1, "status": "completed", "score": 88, "drill_type": "switch",
         "name": "切换演练", "started_at": _ts(1)},
    ])
    e = tl._collect_drill(tl._since(30), 50)[0]
    assert e["detail"]["passed"] is True
    assert e["severity"] == "info"


def test_drill_without_score_leaves_pass_unknown(monkeypatch):
    _patch_drills(monkeypatch, [
        {"id": 1, "status": "pending", "score": None, "drill_type": "switch",
         "name": "演练", "started_at": _ts(1)},
    ])
    e = tl._collect_drill(tl._since(30), 50)[0]
    assert e["detail"]["passed"] is None      # 未知 ≠ 不合格


# ---------------------------------------------------------------------
# 4. 巡检失败项展开
# ---------------------------------------------------------------------

def test_inspection_expands_failed_check_items(monkeypatch):
    from core import dr_inspection
    monkeypatch.setattr(dr_inspection, "list_inspections",
                        lambda limit=50: [{"id": 3, "overall": "fail",
                                           "links_total": 2, "ok_count": 1,
                                           "warn_count": 0, "fail_count": 1,
                                           "summary": "s", "triggered_by": "manual",
                                           "created_at": _ts(0)}], raising=False)
    monkeypatch.setattr(dr_inspection, "get_inspection", lambda rid: {
        "detail": [
            {"id": 7, "name": "链路A", "overall": "fail", "active_site": "主",
             "error": "",
             "checks": [
                 {"item": "主库可达", "status": "ok", "message": "正常"},
                 {"item": "备库复制", "status": "fail", "message": "IO=Connecting"},
             ]},
            {"id": 8, "name": "链路B", "overall": "ok", "checks": [
                {"item": "DNS 解析", "status": "ok", "message": "正常"}]},
        ]}, raising=False)
    evs = tl._collect_inspection(tl._since(30), 50)
    assert len(evs) == 2                      # 1 条汇总 + 1 条失败项
    bad = evs[1]
    assert bad["title"] == "巡检项 链路A · 备库复制"
    assert bad["severity"] == "error"
    assert bad["link_id"] == 7                # 有链路归属，可按链路过滤
    assert bad["summary"] == "IO=Connecting"


def test_inspection_without_detail_is_honest(monkeypatch):
    """拿不到 detail 时只出汇总，不得编造失败项。"""
    from core import dr_inspection
    monkeypatch.setattr(dr_inspection, "list_inspections",
                        lambda limit=50: [{"id": 3, "overall": "ok",
                                           "links_total": 1, "ok_count": 1,
                                           "warn_count": 0, "fail_count": 0,
                                           "summary": "s", "triggered_by": "manual",
                                           "created_at": _ts(0)}], raising=False)
    monkeypatch.setattr(dr_inspection, "get_inspection",
                        lambda rid: None, raising=False)
    evs = tl._collect_inspection(tl._since(30), 50)
    assert len(evs) == 1
    assert evs[0]["kind"] == "inspection"
