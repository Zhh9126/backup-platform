# -*- coding: utf-8 -*-
"""补偿建议 → 恢复剧本 回归（2026-11-20 第二轮）。

覆盖两处新增能力：
1. ``core/dr_compensation.build_runbook``：把补偿建议编排成**可审批的剧本**——
   第一步必须是播报事实的 notify；可自动执行的补偿走 ``dr_compensate`` 步骤
   且必须带审批闸；平台无能力的动作只能编成「人工处置确认点」，
   **绝不生成假的自动修复步骤**。
2. ``core/runbook`` 的 ``dr_compensate`` 步骤处理器：只执行平台确有能力的
   补偿（仅 DNS 牵引），无能力时**如实报错**而不是假装成功。
"""
import pytest

from core import dr_compensation as dc
from core import runbook


def _link(with_dns=True):
    if with_dns:
        return {"id": 1, "name": "L", "failover_config":
                '{"dns": {"domain": "dr-mysql.test", "provider": "dnsmasq"}}'}
    return {"id": 1, "name": "L", "failover_config": "{}"}


def _run(status="failed", steps=None, direction="failover", rid=7, link_id=1):
    return {"id": rid, "status": status, "direction": direction,
            "link_id": link_id, "link_name": "L", "steps": steps or []}


def _step(name, ok=True):
    return {"name": name, "ok": ok, "ms": 10, "detail": {}}


def _patch_link(monkeypatch):
    """补偿判定依赖链路的 DNS 配置（决定能否自动执行），这里给一条配好 DNS 的链路。"""
    from core import models
    monkeypatch.setattr(models, "get_disaster_link",
                        lambda lid: _link(), raising=False)


# ---------------------------------------------------------------------
# 1. build_runbook 编排
# ---------------------------------------------------------------------

def test_runbook_first_step_always_notifies_facts():
    """第一步必须先广播事实——人要先知道发生了什么。"""
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    plan = dc.build_runbook(run, _link())
    first = plan["steps"][0]
    assert first["type"] == "notify"
    assert first["require_confirm"] is False      # 播报不需要等审批
    assert "诊断" in first["params"]["message"]
    assert plan["diagnosis"]


def test_runbook_executable_action_becomes_compensate_step_with_gate():
    """可自动执行的补偿 → dr_compensate 步骤，且必须带审批闸。"""
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    plan = dc.build_runbook(run, _link())
    comp = [s for s in plan["steps"] if s["type"] == "dr_compensate"]
    assert len(comp) == 1
    assert comp[0]["require_confirm"] is True     # 会改流量入口 → 人工放行
    assert comp[0]["params"]["run_id"] == run["id"]
    assert comp[0]["params"]["scope"] == "dns_only"
    assert plan["executable"] is True


def test_runbook_manual_action_is_a_human_confirmation_not_autofix():
    """平台无能力的动作 → 人工处置确认点，不得生成假的自动修复步骤。"""
    run = _run(steps=[_step("GTID 分叉检测", ok=False)])
    plan = dc.build_runbook(run, _link())
    assert not [s for s in plan["steps"] if s["type"] == "dr_compensate"]
    manual = [s for s in plan["steps"] if s["type"] == "notify"][1:]
    assert manual, "人工动作应编成 notify 步骤"
    assert manual[0]["require_confirm"] is True   # 人工确认已处理
    assert plan["executable"] is False
    assert plan["warnings"], "需人工处理的动作必须列进 warnings"


def test_runbook_steps_are_accepted_by_norm_steps():
    """生成的步骤必须能被 Runbook 的校验器接受（否则落库即失败）。"""
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    plan = dc.build_runbook(run, _link())
    norm = runbook._norm_steps(plan["steps"])
    assert len(norm) == len(plan["steps"])
    assert norm[0]["type"] == "notify"


def test_runbook_generation_never_executes_anything(monkeypatch):
    """生成剧本是纯编排——不得触发任何切换。"""
    called = []
    from core import dr_orchestrator
    monkeypatch.setattr(dr_orchestrator, "start_switch",
                        lambda *a, **k: called.append(1) or {"ok": True}, raising=False)
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    dc.build_runbook(run, _link())
    assert called == []


# ---------------------------------------------------------------------
# 2. dr_compensate 步骤处理器
# ---------------------------------------------------------------------

def test_compensate_step_rejects_missing_run_id():
    with pytest.raises(ValueError):
        runbook._step_dr_compensate({}, "admin")


def test_compensate_step_errors_when_no_executable_action(monkeypatch):
    """没有平台可自动执行的补偿 → 如实报错，绝不假装成功。"""
    from core import dr_orchestrator
    run = _run(steps=[_step("GTID 分叉检测", ok=False)])
    monkeypatch.setattr(dr_orchestrator, "get_run", lambda rid: run, raising=False)
    monkeypatch.setattr(dr_orchestrator, "ensure_schema", lambda: None, raising=False)
    with pytest.raises(ValueError) as e:
        runbook._step_dr_compensate({"run_id": 7}, "admin")
    assert "没有平台可自动执行" in str(e.value)


def test_compensate_step_executes_dns_only_scope(monkeypatch):
    """可执行补偿 → 走既有切换编排的 dns_only 通道。"""
    from core import dr_orchestrator
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    _patch_link(monkeypatch)
    seen = {}

    def _fake_start(link_id, direction="failover", mode="execute", force=False,
                    operator="", scope="full"):
        seen.update({"link_id": link_id, "direction": direction,
                     "scope": scope, "operator": operator})
        return {"ok": True, "run_id": 99, "status": "running"}

    monkeypatch.setattr(dr_orchestrator, "get_run",
                        lambda rid: run if rid == 7 else
                        {"id": 99, "status": "success", "rto_sec": 1.2,
                         "message": "完成"}, raising=False)
    monkeypatch.setattr(dr_orchestrator, "ensure_schema", lambda: None, raising=False)
    monkeypatch.setattr(dr_orchestrator, "start_switch", _fake_start, raising=False)
    res = runbook._step_dr_compensate({"run_id": 7}, "tester")
    assert seen["scope"] == "dns_only"
    assert seen["link_id"] == 1
    assert seen["operator"] == "tester"
    assert res["compensation_run_id"] == 99


def test_compensate_step_reports_failed_compensation_job(monkeypatch):
    """补偿作业失败 → 步骤判失败（不得报成功）。"""
    from core import dr_orchestrator
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    _patch_link(monkeypatch)
    monkeypatch.setattr(dr_orchestrator, "get_run",
                        lambda rid: run if rid == 7 else
                        {"id": 99, "status": "failed", "rto_sec": 0,
                         "message": "DNS 服务不可达"}, raising=False)
    monkeypatch.setattr(dr_orchestrator, "ensure_schema", lambda: None, raising=False)
    monkeypatch.setattr(dr_orchestrator, "start_switch",
                        lambda *a, **k: {"ok": True, "run_id": 99}, raising=False)
    with pytest.raises(ValueError) as e:
        runbook._step_dr_compensate({"run_id": 7, "timeout": 5}, "admin")
    assert "未成功" in str(e.value)


def test_step_type_registered():
    """新类型必须注册到 STEP_TYPES 与处理器表。"""
    assert "dr_compensate" in runbook.STEP_TYPES
    assert runbook._STEP_HANDLERS["dr_compensate"] is runbook._step_dr_compensate
    assert runbook.STEP_TYPE_LABELS["dr_compensate"] == "失败补偿"
