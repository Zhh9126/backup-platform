# -*- coding: utf-8 -*-
"""7 个自治 Agent：离线诊断 → 白名单修复 → 回验闭环回归（本地工作区，不入库）。"""
import os
import tempfile
from datetime import datetime, timedelta

_TMP = tempfile.mkdtemp(prefix="ai_autonomous_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

import pytest  # noqa: E402

from core import db  # noqa: E402

db.init_schema()

from core.ai_agent import autonomous as au  # noqa: E402
from core.ai_agent.skills import SKILLS  # noqa: E402
from core.ai_agent.tools import create_default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_runs():
    o = au.get_orchestrator()
    o._runs.clear()
    db.execute("DELETE FROM backup_records")
    db.execute("DELETE FROM system_logs WHERE source='ai_agent'")
    yield


def _zombie():
    old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    return db.execute(
        "INSERT INTO backup_records (task_id, db_type, backup_type, started_at, "
        "status, size_bytes, is_simulated, message) "
        "VALUES (?, 'mysql', 'FULL', ?, 'running', 0, 0, 'e2e')",
        (1, old))


def _zombie_finding(run):
    return next(f for f in run.findings
                if f.action == "clear_zombie_running")


def test_seven_agents_registered():
    keys = [a["key"] for a in au.list_agents()]
    assert len(keys) == 7
    assert set(keys) == {
        "backup_job", "record_integrity", "storage_capacity",
        "realtime_link", "restore_readiness", "disaster_readiness",
        "security_anomaly"}
    assert all(a["scope"] for a in au.list_agents())


def test_natural_language_tools_and_skills_registered():
    reg = create_default_registry()
    for name in ("autonomous_diagnose", "autonomous_plan",
                 "autonomous_execute", "autonomous_verify"):
        t = reg.get(name)
        assert t is not None and t.executor is not None
    skill_names = {s.name for s in SKILLS}
    assert "autonomous_diagnose" in skill_names
    assert "autonomous_execute_confirmed" in skill_names
    confirm = next(s for s in SKILLS
                   if s.name == "autonomous_execute_confirmed")
    assert confirm.args.get("approved") == "true"
    assert confirm.args.get("dry_run") == "false"
    preview = next(s for s in SKILLS if s.name == "autonomous_execute")
    assert preview.args.get("dry_run") == "true"


def test_sweep_is_offline_and_agents_isolated():
    _zombie()
    run = au.get_orchestrator().sweep(use_llm=False)
    d = run.to_dict()
    assert len(d["agents"]) == 7
    assert all(a["status"] == "ok" for a in d["agents"]), d["agents"]
    assert d["explanation_source"] == "local_rules"
    assert "本地规则与真实元数据" in d["explanation"]
    assert _zombie_finding(run).auto_fixable is True


def test_llm_unavailable_degrades_without_fabrication():
    """模型未启用时照常出结论，且明确标注降级，绝不伪装成 AI 分析。"""
    _zombie()
    run = au.get_orchestrator().sweep(use_llm=True)
    assert run.explanation_source in ("local_rules", "llm_degraded")
    assert run.explanation  # 规则结论仍在
    if run.explanation_source == "llm_degraded" or run.llm_note:
        assert "降级" in run.llm_note or "未启用" in run.llm_note


def test_action_whitelist_rejects_unknown_action():
    """红线：模型生成的任意动作必须被拒绝，不能落到命令/SQL 执行。"""
    run = au.get_orchestrator().sweep(use_llm=False)
    bad = au._finding("record_integrity", "evil", "high", "伪造发现", "x", {},
                      action="rm_rf_everything", auto_fixable=True,
                      target_key="record:999")
    run.findings.append(bad)
    res = au.get_orchestrator().execute(
        run.id, [bad.id], approved=True, dry_run=False)
    item = res["results"][0]
    assert item["status"] == "rejected" and item["ok"] is False


def test_dry_run_has_no_side_effect():
    rid = _zombie()
    run = au.get_orchestrator().sweep(use_llm=False)
    f = _zombie_finding(run)
    res = au.get_orchestrator().execute(
        run.id, [f.id], approved=False, dry_run=True)
    assert res["results"][0]["status"] == "dry_run"
    row = db.query_one("SELECT status FROM backup_records WHERE id=?", (rid,))
    assert row["status"] == "running", "预演不得改动任何数据"


def test_execute_then_recheck_resolves_and_audits():
    rid = _zombie()
    run = au.get_orchestrator().sweep(use_llm=False)
    f = _zombie_finding(run)
    res = au.get_orchestrator().execute(
        run.id, [f.id], approved=True, dry_run=False)
    item = res["results"][0]
    assert item["ok"] is True and item["status"] == "executed"
    assert item["recheck"]["status"] == "resolved", item
    row = db.query_one("SELECT status, message FROM backup_records WHERE id=?",
                       (rid,))
    assert row["status"] == "failed"
    assert "自治修复" in str(row["message"] or "")
    logs = db.query("SELECT source FROM system_logs WHERE source='ai_agent'")
    assert logs, "修复动作必须落审计日志"


def test_high_risk_action_requires_explicit_approval():
    run = au.get_orchestrator().sweep(use_llm=False)
    f = au._finding("backup_job", "enable", "medium", "任务被停用", "x", {},
                    action="enable_task", action_args={"task_id": 1},
                    auto_fixable=False, requires_confirm=True,
                    target_key="task:1")
    run.findings.append(f)
    res = au.get_orchestrator().execute(
        run.id, [f.id], approved=False, dry_run=False)
    assert res["results"][0]["status"] == "needs_confirm"


def test_verify_tool_reports_resolved_and_persist():
    from core.ai_agent.tools import _autonomous_verify_executor
    rid = _zombie()
    run = au.get_orchestrator().sweep(use_llm=False)
    f = _zombie_finding(run)
    au.get_orchestrator().execute(run.id, [f.id], approved=True, dry_run=False)
    out = _autonomous_verify_executor({"run_id": run.id}, {})
    assert out["ok"] is True
    assert f.id in out["resolved"], out
    assert rid not in (out["new"] or [])


def test_chat_formatter_never_returns_literal_null():
    """本地快速通道对新工具必须有可读回答，不能输出字符串 "null"。"""
    from core.ai_agent.agent import AIAgent
    from core.ai_agent.tools import _autonomous_diagnose_executor
    a = AIAgent.__new__(AIAgent)
    res = _autonomous_diagnose_executor({}, {})
    text = a._format_tool_result_to_answer("autonomous_diagnose", res)
    assert text and text != "null"
    assert "自治" in text and "结论" in text
    plan_text = a._format_tool_result_to_answer(
        "autonomous_plan", {"ok": True, "run_id": "r1", "plan": []})
    assert "修复计划" in plan_text


def test_actions_are_only_platform_wrappers():
    """白名单内每个动作都必须有平台封装函数，且元数据完整。"""
    assert set(au.ACTIONS) == {
        "retry_failed_backup", "clear_zombie_running", "run_backup_precheck",
        "run_inspection_quick", "enable_task", "trigger_restore_verify"}
    for name, spec in au.ACTIONS.items():
        assert callable(spec["executor"]), name
        assert spec["risk"] in ("low", "medium", "high")
