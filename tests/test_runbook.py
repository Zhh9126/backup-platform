# -*- coding: utf-8 -*-
"""Runbook 恢复编排单元测试。

隔离元数据库（tmp 目录）+ 少量 mock（步骤处理器 / 时间），
覆盖：步骤规范化 / CRUD / 审批闸 / 停止幂等 / 执行引擎成功与失败路径。
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="rb_test_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest

from core import db  # noqa: E402  需在 env 设置之后导入
from core import runbook as rb  # noqa: E402


# ---------------------------------------------------------------------------
# 步骤规范化
# ---------------------------------------------------------------------------

def test_norm_steps_assigns_seq_and_defaults():
    steps = rb._norm_steps([
        {"type": "run_backup", "params": {"task_id": 1}},
        {"type": "wait", "params": {"seconds": 5}},
    ])
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["name"] == "执行备份"
    assert steps[0]["require_confirm"] is False
    assert steps[1]["params"]["seconds"] == 5


def test_norm_steps_rejects_unknown_type_and_bad_input():
    with pytest.raises(ValueError, match="不支持"):
        rb._norm_steps([{"type": "no_such"}])
    with pytest.raises(ValueError, match="JSON"):
        rb._norm_steps("not-a-list")
    with pytest.raises(ValueError, match="数组"):
        rb._norm_steps('{"a": 1}')  # 合法 JSON 但不是数组
    with pytest.raises(ValueError, match="对象"):
        rb._norm_steps(["step"])
    with pytest.raises(ValueError, match="JSON"):
        rb._norm_steps("{bad json")


def test_norm_steps_rejects_over_limit():
    with pytest.raises(ValueError, match="上限"):
        rb._norm_steps([{"type": "wait"}] * (rb.MAX_STEPS + 1))


def test_row_to_dict_parses_json_fields():
    row = {"steps": '[{"type":"wait"}]', "steps_log": "[]", "approved_seqs": "[1]"}
    d = rb._row_to_dict(row)
    assert d["steps"] == [{"type": "wait"}]
    assert d["approved_seqs"] == [1]
    assert rb._row_to_dict(None) is None


# ---------------------------------------------------------------------------
# CRUD（隔离元数据库）
# ---------------------------------------------------------------------------

def test_create_get_update_delete_runbook():
    created = rb.create_runbook({
        "name": "t-crud",
        "description": "单测剧本",
        "steps": [{"type": "wait", "params": {"seconds": 0}}],
    }, operator="tester")
    assert created["id"] > 0 and created["name"] == "t-crud"
    assert len(created["steps"]) == 1

    got = rb.get_runbook(created["id"])
    assert got["description"] == "单测剧本"

    updated = rb.update_runbook(created["id"], {"name": "t-crud-2", "enabled": 0})
    assert updated["name"] == "t-crud-2"
    assert not updated["enabled"]

    with pytest.raises(ValueError):
        rb.update_runbook(created["id"], {"steps": [{"type": "bad"}]})

    assert rb.delete_runbook(created["id"]) is True
    assert rb.get_runbook(created["id"]) is None
    assert rb.delete_runbook(created["id"]) is False


# ---------------------------------------------------------------------------
# 审批 / 停止
# ---------------------------------------------------------------------------

def _mk_runbook(steps, enabled=1):
    rb._ensure_tables()
    rid = db.execute(
        "INSERT INTO runbooks(name, description, steps, enabled, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        ("t-run", "", json.dumps(steps, ensure_ascii=False), enabled,
         db.now_iso(), db.now_iso()))
    return int(rid)


def _mk_run(runbook_id, status="running"):
    rb._ensure_tables()
    run_id = db.execute(
        "INSERT INTO runbook_runs(runbook_id, runbook_name, status, created_at)"
        " VALUES(?,?,?,?)",
        (runbook_id, "t-run", status, db.now_iso()))
    return int(run_id)


def test_wait_for_approval_stops_when_run_missing_or_stopped(monkeypatch):
    import time as _t
    monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
    # 运行记录不存在 → 视为已停止（停止语义：DB 置 stopped 或进程内标记）
    assert rb._wait_for_approval(999999, 1, deadline=_t.time() + 1) == "stopped"
    rid = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                        "params": {}, "require_confirm": True,
                        "continue_on_fail": False}])
    run_id = _mk_run(rid, status="stopped")
    assert rb._wait_for_approval(run_id, 1, deadline=_t.time() + 1) == "stopped"


def test_wait_for_approval_approved_via_db_record(monkeypatch):
    import time as _t
    monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
    rid = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                        "params": {}, "require_confirm": True,
                        "continue_on_fail": False}])
    run_id = _mk_run(rid)
    rb._update_run(run_id, approved_seqs=json.dumps([1]))
    assert rb._wait_for_approval(run_id, 1, deadline=_t.time() + 1) == "approved"


def test_approve_run_and_invalid_seq():
    rid = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                        "params": {}, "require_confirm": True,
                        "continue_on_fail": False}])
    run_id = _mk_run(rid)
    out = rb.approve_run(run_id, 1, operator="tester")
    assert 1 in (out.get("approved_seqs") or [])
    # 幂等重复审批
    rb.approve_run(run_id, 1)
    assert rb.get_run(run_id)["approved_seqs"] == [1]
    with pytest.raises(ValueError):
        rb.approve_run(run_id, 0)


def test_stop_run_idempotent_and_terminal():
    rid = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                        "params": {}, "require_confirm": False,
                        "continue_on_fail": False}])
    run_id = _mk_run(rid, status="running")
    out = rb.stop_run(run_id, operator="tester")
    assert out["status"] == "stopped"
    # 终态再停：幂等返回，不抛错
    out2 = rb.stop_run(run_id)
    assert out2["status"] == "stopped"
    # 不存在的运行
    with pytest.raises(ValueError):
        rb.stop_run(999999)


def test_run_runbook_rejects_missing_or_disabled_or_concurrent():
    with pytest.raises(ValueError, match="不存在"):
        rb.run_runbook(999999)
    rid = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                        "params": {}, "require_confirm": False,
                        "continue_on_fail": False}], enabled=0)
    with pytest.raises(ValueError, match="停用"):
        rb.run_runbook(rid)
    rid2 = _mk_runbook([])
    with pytest.raises(ValueError, match="步骤"):
        rb.run_runbook(rid2)
    # 并发护栏：进程内标记已占用（用启用的剧本）
    rid3 = _mk_runbook([{"seq": 1, "type": "wait", "name": "w",
                         "params": {}, "require_confirm": False,
                         "continue_on_fail": False}], enabled=1)
    rb._RUNNING.add(rid3)
    try:
        with pytest.raises(ValueError, match="执行中"):
            rb.run_runbook(rid3)
    finally:
        rb._RUNNING.discard(rid3)


# ---------------------------------------------------------------------------
# 执行引擎（同步调用 _execute_run，mock 步骤处理器）
# ---------------------------------------------------------------------------

def _run_sync(run_id, runbook_id, steps):
    rb._execute_run(run_id, runbook_id, steps, operator="tester")


def test_execute_run_success_and_rto():
    rid = _mk_runbook([{"seq": 1, "type": "notify", "name": "n1",
                        "params": {"title": "t", "message": "m"},
                        "require_confirm": False, "continue_on_fail": False}])
    run_id = _mk_run(rid)
    _run_sync(run_id, rid, json.loads(db.query_one(
        "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"]))
    run = rb.get_run(run_id)
    assert run["status"] == "success"
    assert run["rto_sec"] is not None
    log = run["steps_log"]
    assert log[0]["status"] == "ok"
    assert "通知已广播" in log[0]["message"]


def test_execute_run_fail_stops_pipeline():
    def boom(params, operator):
        raise ValueError("步骤炸了")

    rid = _mk_runbook([
        {"seq": 1, "type": "notify", "name": "bad", "params": {},
         "require_confirm": False, "continue_on_fail": False},
        {"seq": 2, "type": "wait", "name": "never", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False},
    ])
    run_id = _mk_run(rid)
    old_notify = rb._STEP_HANDLERS["notify"]
    rb._STEP_HANDLERS["notify"] = boom
    try:
        steps = json.loads(db.query_one(
            "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"])
        _run_sync(run_id, rid, steps)
    finally:
        rb._STEP_HANDLERS["notify"] = old_notify
    run = rb.get_run(run_id)
    assert run["status"] == "failed"
    assert "步骤炸了" in run["error"]
    log = run["steps_log"]
    assert log[0]["status"] == "fail"
    assert len(log) == 1  # 失败即中止，第 2 步未执行


def test_execute_run_continue_on_fail():
    calls = []

    def ok_after_fail(params, operator):
        calls.append(1)
        return {"message": "ok"}

    rid = _mk_runbook([
        {"seq": 1, "type": "notify", "name": "bad", "params": {},
         "require_confirm": False, "continue_on_fail": True},
        {"seq": 2, "type": "wait", "name": "w", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False},
    ])
    run_id = _mk_run(rid)
    old_notify = rb._STEP_HANDLERS["notify"]
    rb._STEP_HANDLERS["notify"] = lambda p, o: (_ for _ in ()).throw(
        RuntimeError("可忽略失败"))
    try:
        steps = json.loads(db.query_one(
            "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"])
        _run_sync(run_id, rid, steps)
    finally:
        rb._STEP_HANDLERS["notify"] = old_notify
    run = rb.get_run(run_id)
    assert run["status"] == "success"
    assert run["steps_log"][0]["status"] == "fail_cont"
    assert len(run["steps_log"]) == 2


def test_execute_run_approval_gate_flow(monkeypatch):
    """require_confirm 步骤：预置审批放行 → 执行通过。"""
    monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
    rid = _mk_runbook([
        {"seq": 1, "type": "notify", "name": "n", "params": {},
         "require_confirm": True, "continue_on_fail": False},
    ])
    run_id = _mk_run(rid)
    rb._update_run(run_id, approved_seqs=json.dumps([1]))  # 预先放行
    steps = json.loads(db.query_one(
        "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"])
    _run_sync(run_id, rid, steps)
    run = rb.get_run(run_id)
    assert run["status"] == "success"
    assert run["steps_log"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# DAG 依赖（PB 级方案 §5.2：入度推进）
# ---------------------------------------------------------------------------

def test_norm_steps_keeps_depends_on():
    steps = rb._norm_steps([
        {"type": "wait", "params": {"seconds": 0}},
        {"type": "wait", "params": {"seconds": 0}, "depends_on": [1]},
    ])
    assert steps[1]["depends_on"] == [1]
    assert steps[0]["depends_on"] == []


def test_norm_steps_rejects_self_dependency():
    with pytest.raises(ValueError, match="依赖自己"):
        rb._norm_steps([{"type": "wait", "depends_on": [1]}])


def test_norm_steps_rejects_missing_dependency():
    with pytest.raises(ValueError, match="不存在"):
        rb._norm_steps([{"type": "wait", "depends_on": [5]}])


def test_norm_steps_rejects_cycle():
    with pytest.raises(ValueError, match="环"):
        rb._norm_steps([
            {"type": "wait", "depends_on": [2]},
            {"type": "wait", "depends_on": [1]},
        ])


def test_topo_order_respects_dependencies():
    steps = rb._norm_steps([
        {"type": "wait", "params": {"seconds": 0}},                    # 1
        {"type": "wait", "params": {"seconds": 0}, "depends_on": [1]},  # 2
        {"type": "wait", "params": {"seconds": 0}, "depends_on": [1]},  # 3
        {"type": "wait", "params": {"seconds": 0}, "depends_on": [2, 3]},  # 4
    ])
    order = [s["seq"] for s in rb.topo_order(steps)]
    assert order[0] == 1 and order[-1] == 4
    assert order.index(2) < order.index(4) and order.index(3) < order.index(4)
    assert rb.has_dag(steps) is True


def test_has_dag_false_for_legacy_steps():
    steps = rb._norm_steps([{"type": "wait"}, {"type": "wait"}])
    assert rb.has_dag(steps) is False


def test_execute_dag_skips_downstream_and_continues_sibling(monkeypatch):
    """1 失败 → 依赖它的 2 被跳过；3 无依赖照常执行；整体判失败。"""
    monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
    rid = _mk_runbook([
        {"seq": 1, "type": "notify", "name": "bad", "params": {},
         "require_confirm": False, "continue_on_fail": False, "depends_on": []},
        {"seq": 2, "type": "wait", "name": "child", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": [1]},
        {"seq": 3, "type": "wait", "name": "sibling", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": []},
    ])
    run_id = _mk_run(rid)
    old = rb._STEP_HANDLERS["notify"]
    rb._STEP_HANDLERS["notify"] = lambda p, o: (_ for _ in ()).throw(
        RuntimeError("上游炸了"))
    try:
        steps = json.loads(db.query_one(
            "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"])
        _run_sync(run_id, rid, steps)
    finally:
        rb._STEP_HANDLERS["notify"] = old
    run = rb.get_run(run_id)
    log = {e["seq"]: e for e in run["steps_log"]}
    assert log[1]["status"] == "fail"
    assert log[2]["status"] == "skipped"
    assert "上游步骤" in log[2]["message"]
    assert log[3]["status"] == "ok"          # 兄弟分支不受影响
    assert run["status"] == "failed"


def test_execute_dag_all_success(monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
    rid = _mk_runbook([
        {"seq": 1, "type": "wait", "name": "a", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": []},
        {"seq": 2, "type": "wait", "name": "b", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": [1]},
        {"seq": 3, "type": "wait", "name": "c", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": [1]},
        {"seq": 4, "type": "wait", "name": "d", "params": {"seconds": 0},
         "require_confirm": False, "continue_on_fail": False, "depends_on": [2, 3]},
    ])
    run_id = _mk_run(rid)
    steps = json.loads(db.query_one(
        "SELECT steps FROM runbooks WHERE id=?", (rid,))["steps"])
    _run_sync(run_id, rid, steps)
    run = rb.get_run(run_id)
    assert run["status"] == "success"
    assert [e["status"] for e in run["steps_log"]] == ["ok"] * 4
