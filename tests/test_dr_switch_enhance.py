# -*- coding: utf-8 -*-
"""容灾一键切换增强回归（2026-11-20）。

覆盖本轮新增的四项能力：
1. **切换可中止**：作业不在 running / 不在本进程内 → 如实拒绝，不伪造中止成功；
   引擎侧在**步骤边界**响应中止信号（当前步骤跑完后停止后续步骤）。
2. **切换事件外发**：开始 / 成功 / 失败 / 中止各有对应事件，通知失败不影响切换结果。
3. **RTO 统计**：次数 / 成功率 / 均值与极值 / SLO 达标率 / 趋势序列口径正确。
4. **预检增强**：GTID 位点核对量化 RPO 缺口与回切追平量；探测不到时如实留空。
"""
import json
import threading

import pytest

from core import dr_failover
from core import dr_orchestrator as orch


def _patch_webhooks(monkeypatch, fake):
    """替换 webhooks 模块，且不受测试导入顺序影响。

    编排器用 `from core import webhooks` 取模块——该写法**优先读 core 包的属性**。
    若 core.webhooks 已被同批次其它测试导入过（包上已留下属性），
    单独替换 sys.modules 会被绕过，导致本用例在组合运行时假失败。
    """
    import core
    monkeypatch.setitem(__import__("sys").modules, "core.webhooks", fake)
    monkeypatch.setattr(core, "webhooks", fake, raising=False)


# ---------------------------------------------------------------------
# 1. 中止
# ---------------------------------------------------------------------

def test_cancel_run_rejects_unknown_job():
    res = orch.cancel_run(999999)
    assert res["ok"] is False
    assert res["error"], "未知作业必须给出原因，不能伪造中止成功"


def test_cancel_run_rejects_non_running_status(monkeypatch):
    monkeypatch.setattr(orch, "get_run",
                        lambda rid: {"id": rid, "status": "success"})
    res = orch.cancel_run(1)
    assert res["ok"] is False
    assert "无法中止" in res["error"]


def test_cancel_run_rejects_job_not_in_process(monkeypatch):
    """进程内没有中止信号（如平台重启过）→ 如实拒绝，不假装已中止。"""
    monkeypatch.setattr(orch, "get_run",
                        lambda rid: {"id": rid, "status": "running"})
    monkeypatch.setattr(orch, "_CANCEL_EVENTS", {}, raising=False)
    res = orch.cancel_run(1)
    assert res["ok"] is False
    assert "无法中止" in res["error"]


def test_cancel_run_sets_event(monkeypatch):
    ev = threading.Event()
    monkeypatch.setattr(orch, "get_run",
                        lambda rid: {"id": rid, "status": "running"})
    monkeypatch.setattr(orch, "_CANCEL_EVENTS", {7: ev}, raising=False)
    monkeypatch.setattr(orch.db, "execute", lambda *a, **k: 0)
    monkeypatch.setattr(orch.db, "add_log", lambda *a, **k: None)
    res = orch.cancel_run(7)
    assert res["ok"] is True
    assert ev.is_set(), "中止信号必须置位"


def test_engine_stops_at_step_boundary():
    """引擎在步骤边界响应中止：当前步骤跑完后即停止，后续步骤不再执行。

    语义：中止请求在步骤 1 执行期间到达 → 步骤 1 正常完成并落账，随后引擎
    抛出中止异常（该异常携带「已被用户中止」），步骤 2 不会被执行。
    """
    eng = dr_failover.FailoverEngine.__new__(dr_failover.FailoverEngine)
    eng.steps = []
    eng.on_step = None
    ev = threading.Event()
    eng.cancel_event = ev

    calls = []

    def _s1_and_cancel():
        calls.append("s1")
        ev.set()
        return {"ok": True}

    def _s2():
        calls.append("s2")
        return {"ok": True}

    assert eng.cancel_requested() is False
    with pytest.raises(dr_failover.FailoverError) as ei:
        eng._step("步骤1", _s1_and_cancel)
    assert "中止" in str(ei.value)
    # 步骤 1 已成功落账（ok=True），后续步骤不再执行
    assert len(eng.steps) == 1 and eng.steps[0]["ok"] is True
    assert calls == ["s1"], "中止后不应再执行后续步骤"


def test_engine_cancel_event_absent_is_false():
    eng = dr_failover.FailoverEngine.__new__(dr_failover.FailoverEngine)
    eng.cancel_event = None
    assert eng.cancel_requested() is False


# ---------------------------------------------------------------------
# 2. 事件外发
# ---------------------------------------------------------------------

def test_emit_switch_event_emits_webhook(monkeypatch):
    seen = {}

    class _FakeWebhooks:
        @staticmethod
        def emit_async(event, payload):
            seen["event"] = event
            seen["payload"] = payload

    _patch_webhooks(monkeypatch, _FakeWebhooks)
    orch._emit_switch_event("dr.switch.success",
                             {"id": 3, "name": "170-171"}, "failover", "full",
                             run_id=9, operator="admin", rto=15.2,
                             message="切换完成")
    assert seen["event"] == "dr.switch.success"
    assert seen["payload"]["link_id"] == 3
    assert seen["payload"]["rto_sec"] == 15.2
    assert seen["payload"]["operator"] == "admin"


def test_emit_switch_event_notification_failure_is_isolated(monkeypatch):
    """通知失败不得抛出（旁路语义：绝不改变切换结果）。"""
    class _Boom:
        @staticmethod
        def emit_async(event, payload):
            raise RuntimeError("webhook down")

    _patch_webhooks(monkeypatch, _Boom)
    orch._emit_switch_event("dr.switch.failure",
                             {"id": 1, "name": "x"}, "failover", "full",
                             run_id=1, message="boom")


# ---------------------------------------------------------------------
# 3. RTO 统计
# ---------------------------------------------------------------------

def _mk_run(monkeypatch, rows):
    monkeypatch.setattr(orch.db, "query",
                        lambda sql, params=(): list(rows))
    monkeypatch.setattr(orch.db, "execute", lambda *a, **k: 0)


def test_stats_aggregates_correctly(monkeypatch):
    rows = [
        {"id": 1, "status": "success", "rto_sec": 10.0, "direction": "failover",
         "created_at": "2026-11-19T10:00:00+08:00", "link_name": "A"},
        {"id": 2, "status": "success", "rto_sec": 30.0, "direction": "failback",
         "created_at": "2026-11-19T12:00:00+08:00", "link_name": "A"},
        {"id": 3, "status": "failed", "rto_sec": 0, "direction": "failover",
         "created_at": "2026-11-19T13:00:00+08:00", "link_name": "A"},
        {"id": 4, "status": "cancelled", "rto_sec": 0, "direction": "failover",
         "created_at": "2026-11-19T14:00:00+08:00", "link_name": "A"},
    ]
    _mk_run(monkeypatch, rows)
    st = orch.stats(slo_rto_sec=20)
    assert st["total"] == 4
    assert st["success"] == 2
    assert st["failed"] == 1
    assert st["cancelled"] == 1
    assert st["success_rate"] == 50.0
    assert st["avg_rto"] == 20.0
    assert st["max_rto"] == 30.0
    assert st["min_rto"] == 10.0
    # 2 次成功里只有 10s 那次达标 → 达标率 50%
    assert st["slo_hit_rate"] == 50.0
    assert len(st["series"]) == 4
    assert st["series"][0]["id"] == 1


def test_stats_empty_is_honest(monkeypatch):
    _mk_run(monkeypatch, [])
    st = orch.stats()
    assert st["total"] == 0
    assert st["avg_rto"] == 0.0
    assert st["slo_hit_rate"] == 0.0
    assert st["series"] == []


def test_stats_default_slo_used_when_not_given(monkeypatch):
    _mk_run(monkeypatch, [])
    monkeypatch.setattr(orch, "_slo_target", lambda lid=None: orch.DEFAULT_SLO_RTO_SEC)
    st = orch.stats()
    assert st["slo_rto_sec"] == orch.DEFAULT_SLO_RTO_SEC


# ---------------------------------------------------------------------
# 4. 预检增强（GTID 位点）
# ---------------------------------------------------------------------

def test_replication_position_quantifies_gap(monkeypatch):
    """主库有备库没有的段 → primary_missing_on_standby 非空（RPO 缺口）。"""
    monkeypatch.setattr(dr_failover, "_load_config", lambda link: {
        "primary_conn": {"host": "1.1.1.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "2.2.2.2", "port": 3306, "user": "u", "password": "p"},
    })
    monkeypatch.setattr(dr_failover, "_decrypt_conn", lambda c: dict(c or {}))
    texts = {"1.1.1.1": "uuid-a:1-10", "2.2.2.2": "uuid-a:1-5"}
    monkeypatch.setattr(dr_failover, "_fetch_gtid_executed",
                        lambda cfg: texts.get(cfg.get("host"), ""))
    pos = orch._replication_position({"id": 1})
    assert pos["gtid_enabled"] is True
    assert len(pos["primary_missing_on_standby"]) == 1   # 6-10 段未到备库
    assert pos["standby_missing_on_primary"] == []


def test_replication_position_absent_when_no_gtid(monkeypatch):
    """未开 GTID → 返回空 dict（如实无量化信息，不伪造 RPO=0）。"""
    monkeypatch.setattr(dr_failover, "_load_config", lambda link: {
        "primary_conn": {"host": "h1"}, "standby_conn": {"host": "h2"},
    })
    monkeypatch.setattr(dr_failover, "_decrypt_conn", lambda c: dict(c or {}))
    monkeypatch.setattr(dr_failover, "_fetch_gtid_executed", lambda cfg: "")
    assert orch._replication_position({"id": 1}) == {}


def test_precheck_warns_rpo_gap(monkeypatch):
    """failover 方向：主库未到备库的段数 → 预检给出量化风险提示。"""
    snap = {"primary": {"host": "h1", "port": 3306, "reachable": False},
            "standby": {"host": "h2", "port": 3306, "reachable": True,
                        "is_replica": True, "behind": 0},
            "dns": {}}
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)
    monkeypatch.setattr(orch.models, "get_disaster_link",
                        lambda lid: {"id": lid, "name": "A", "failover_config": "{}"})
    monkeypatch.setattr(orch, "_replication_position", lambda link, s=None: {
        "gtid_enabled": True, "primary_uuids": 1, "standby_uuids": 1,
        "primary_missing_on_standby": [("u", 6, 10)],
        "standby_missing_on_primary": [],
    })
    res = orch.precheck(1, direction="failover", force=True)
    assert res["ok"] is True
    assert any("RPO 缺口" in w for w in res["warnings"]), res["warnings"]
    assert res["replication_position"]["primary_missing_on_standby"]
