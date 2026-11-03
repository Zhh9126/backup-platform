# -*- coding: utf-8 -*-
"""P1-2 Oracle 事务级精准还原回归：jsonl 解析 / 事务聚合 / 回滚计划 / 预览闸。"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="oracle_txn_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "bk")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest

from core.oracle_txn import (OracleTxnError, load_events, rollback_plan,
                             transactions, txn_events)


@pytest.fixture(scope="module")
def app():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture(scope="module")
def client(app):
    with app.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123"})
        assert r.status_code in (200, 302)
        yield c

# 模拟 LogMiner 段（与 oracle_logminer._read_contents 字段一致）
EV = [
    {"scn": "100", "ts": "2026-11-03 10:00:01", "owner": "BP", "table": "T1",
     "op": "INSERT",
     "redo": "INSERT INTO \"BP\".\"T1\" VALUES ('1','订单-一')",
     "undo": "DELETE FROM \"BP\".\"T1\" WHERE \"ID\"='1'", "xid": "5.12.321"},
    {"scn": "105", "ts": "2026-11-03 10:00:02", "owner": "BP", "table": "T1",
     "op": "UPDATE",
     "redo": "UPDATE \"BP\".\"T1\" SET \"NAME\"='改' WHERE \"ID\"='1'",
     "undo": "UPDATE \"BP\".\"T1\" SET \"NAME\"='订单-一' WHERE \"ID\"='1'",
     "xid": "5.12.321"},
    {"scn": "110", "ts": "2026-11-03 10:00:03", "owner": "BP", "table": "T2",
     "op": "DELETE",
     "redo": "DELETE FROM \"BP\".\"T2\" WHERE \"ID\"='9'",
     "undo": "INSERT INTO \"BP\".\"T2\" VALUES ('9','x')", "xid": "5.12.321"},
    {"scn": "120", "ts": "2026-11-03 10:00:05", "owner": "BP", "table": "T3",
     "op": "DDL",
     "redo": "ALTER TABLE \"BP\".\"T3\" ADD (C1 NUMBER)", "undo": "",
     "xid": "6.1.1"},
    {"scn": "130", "ts": "2026-11-03 10:00:08", "owner": "BP", "table": "T1",
     "op": "INSERT",
     "redo": "INSERT INTO \"BP\".\"T1\" VALUES ('2','b')",
     "undo": "DELETE FROM \"BP\".\"T1\" WHERE \"ID\"='2'", "xid": "7.2.9"},
]


@pytest.fixture()
def seg_file(tmp_path):
    p = os.path.join(str(tmp_path), "oracle_logminer_1_90_130.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for ev in EV:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return p


def test_load_events_skips_bad_lines(seg_file):
    with open(seg_file, "a", encoding="utf-8") as f:
        f.write("not-json\n\n")
    evs = load_events(seg_file)
    assert len(evs) == 5


def test_transactions_grouped_by_xid(seg_file):
    txns = transactions(load_events(seg_file))
    # 3 个 xid：5.12.321（DML×3）/ 6.1.1（DDL）/ 7.2.9（单插）
    assert len(txns) == 3
    # SCN 降序：最新事务在前
    assert txns[0]["xid"] == "7.2.9"
    t = [x for x in txns if x["xid"] == "5.12.321"][0]
    assert t["n_insert"] == 1 and t["n_update"] == 1 and t["n_delete"] == 1
    assert t["first_scn"] == 100 and t["last_scn"] == 110
    assert "BP.T1" in t["tables"] and "BP.T2" in t["tables"]
    assert t["n_ops"] == 3


def test_legacy_segment_without_xid(seg_file):
    evs = load_events(seg_file)
    for e in evs:
        e.pop("xid")
    txns = transactions(evs)
    assert len(txns) == 1 and txns[0]["xid"] == "-"
    assert txns[0]["n_ops"] == 5


def test_rollback_plan_reverse_order(seg_file):
    plan = rollback_plan(load_events(seg_file), "5.12.321")
    assert plan["n_undo"] == 3 and plan["n_skip"] == 0
    # SCN 降序：110(T2 DELETE) → 105(UPDATE) → 100(INSERT)
    scns = [s["scn"] for s in plan["statements"]]
    assert scns == ["110", "105", "100"]
    assert plan["statements"][0]["undo_sql"].startswith("INSERT INTO")
    assert plan["statements"][2]["undo_sql"].startswith("DELETE FROM")


def test_rollback_plan_marks_undo_less_as_skipped(seg_file):
    plan = rollback_plan(load_events(seg_file), "6.1.1")
    assert plan["n_undo"] == 0 and plan["n_skip"] == 1
    assert plan["skipped"][0]["op"] == "DDL"
    assert "不可按行撤销" in plan["skipped"][0]["reason"]


def test_rollback_plan_unknown_txn(seg_file):
    with pytest.raises(OracleTxnError, match="不存在事务"):
        rollback_plan(load_events(seg_file), "99.1.1")


def test_rollback_preview_gate(client):
    """执行（preview=false）必须 confirm + 验证码双闸。"""
    _enable(client)
    try:
        r = client.post("/api/oracle-txn/rollback", json={
            "task_id": 1, "segment": "x.jsonl", "xid": "1.1.1",
            "preview": False, "confirm": True})
        assert r.status_code == 403
        assert r.get_json()["verify_code_required"] is True
        # 预览不受闸限制（只读）
        r = client.post("/api/oracle-txn/rollback", json={
            "task_id": 999999, "segment": "x.jsonl", "xid": "1.1.1",
            "preview": True})
        assert r.status_code == 400  # 任务不存在→400 而非 500
    finally:
        _enable(client, False)


def _enable(client, on=True):
    r = client.post("/api/op-guard/config", json={"verify_code_enabled": on})
    assert r.status_code == 200
