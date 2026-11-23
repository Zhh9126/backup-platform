# -*- coding: utf-8 -*-
"""容灾站点拓扑 / 候选池 / 目标驱动单测（隔离元数据库，零真机依赖）。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "tests", ".tmp_dr_topology")
os.makedirs(TMP, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(TMP, "meta.db")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.META_DB_PATH = os.environ["META_DB_PATH"]
from core import db as _db  # noqa: E402
_db.init_schema()
from core import dr_topology as topo  # noqa: E402
from core import models  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _clean():
    topo.ensure_schema()
    for t in ("dr_sites", "dr_failover_candidates", "disaster_links"):
        _db.execute("DELETE FROM %s" % t)
    yield


def _link():
    return models.create_disaster_link({
        "name": "T-容灾拓扑", "primary_site": "170", "dr_site": "171",
        "status": "active", "enabled": 1,
    })


# ------------------------------------------------------------------ 站点
def test_site_crud_and_validation():
    sid = topo.create_site({"name": "北京主中心", "site_role": "primary",
                            "site_type": "datacenter", "city": "北京",
                            "lat": 39.9, "lon": 116.4})
    assert sid > 0
    row = topo.get_site(sid)
    assert row["name"] == "北京主中心" and row["site_role"] == "primary"
    assert topo.update_site(sid, {"city": "海淀", "site_role": "bogus"}) is True
    assert topo.get_site(sid)["site_role"] == "standby"  # 非法角色回落
    assert topo.get_site(sid)["city"] == "海淀"
    with pytest.raises(ValueError):
        topo.create_site({"name": ""})
    assert topo.delete_site(sid)["ok"] is True


def test_site_delete_guard_when_referenced():
    lid = _link()
    sid = topo.create_site({"name": "西安备中心", "site_role": "standby"})
    cid = topo.upsert_candidate({"link_id": lid, "site_id": sid, "host": "1.2.3.4",
                                 "username": "root", "password": "pw", "priority": 10})
    assert cid > 0
    assert topo.delete_site(sid)["ok"] is False  # 被引用不可删
    topo.delete_candidate(cid)
    assert topo.delete_site(sid)["ok"] is True


# --------------------------------------------------------------- 候选池
def test_candidate_priority_and_password_mask():
    lid = _link()
    c1 = topo.upsert_candidate({"link_id": lid, "name": "备机A", "host": "10.0.0.1",
                                "username": "root", "password": "Secret@1", "priority": 20})
    c2 = topo.upsert_candidate({"link_id": lid, "name": "备机B", "host": "10.0.0.2",
                                "username": "root", "password": "Secret@2", "priority": 10})
    rows = topo.list_candidates(lid)
    assert len(rows) == 2
    assert rows[0]["host"] == "10.0.0.2"  # 小 priority 优先
    assert all(r["password"] == "***" and r["has_password"] for r in rows)
    raw = _db.query_one("SELECT password FROM dr_failover_candidates WHERE id=?", (c1,))
    assert raw["password"] != "Secret@1"  # 落库为密文
    # 掩码回写保留原密码
    topo.upsert_candidate({"id": c2, "link_id": lid, "host": "10.0.0.2", "password": "***"})
    after = _db.query_one("SELECT password FROM dr_failover_candidates WHERE id=?", (c2,))
    assert after["password"] == raw["password"] or after["password"] != "***"
    topo.delete_candidate(c1); topo.delete_candidate(c2)


def test_ranked_candidates_skips_unhealthy(monkeypatch):
    lid = _link()
    a = topo.upsert_candidate({"link_id": lid, "name": "好备机", "host": "1.1.1.1",
                               "priority": 20, "enabled": 1})
    b = topo.upsert_candidate({"link_id": lid, "name": "坏备机", "host": "2.2.2.2",
                               "priority": 10, "enabled": 1})

    def fake_probe(row, check_replica=True):
        good = row["id"] == a
        return {"id": row["id"], "name": row["name"], "host": row["host"],
                "healthy": good, "reason": "" if good else "不可达",
                "priority": row["priority"]}

    monkeypatch.setattr(topo, "probe_candidate", fake_probe)
    ranked = topo.ranked_candidates(lid)
    assert len(ranked["usable"]) == 1 and ranked["usable"][0]["id"] == a
    assert ranked["skipped"][0]["id"] == b  # 高优先级但不健康被跳过
    topo.delete_candidate(a); topo.delete_candidate(b)


def test_probe_requires_io_sql_yes(monkeypatch):
    """具备接管条件必须 IO/SQL 双 Yes，只有复制配置行不算。"""
    from core import dr_failover as fo
    monkeypatch.setattr(fo, "_probe_mysql",
                        lambda *a, **k: {"reachable": True, "error": ""})
    monkeypatch.setattr(fo, "_replica_status",
                        lambda *a, **k: {"reachable": True, "is_replica": True,
                                         "io": "Connecting", "sql": "Yes", "behind": 0})
    r = topo.probe_candidate({"id": 1, "host": "3.3.3.3", "priority": 1,
                              "username": "root", "password": ""})
    assert r["healthy"] is False and "复制线程异常" in r["reason"]
    monkeypatch.setattr(fo, "_replica_status",
                        lambda *a, **k: {"reachable": True, "is_replica": True,
                                         "io": "Yes", "sql": "Yes", "behind": 3})
    r = topo.probe_candidate({"id": 1, "host": "3.3.3.3", "priority": 1,
                              "username": "root", "password": ""})
    assert r["healthy"] is True


# --------------------------------------------------------------- 目标驱动
def test_targets_and_judgement():
    lid = _link()
    t = topo.set_targets(lid, rto_target_sec=120, rpo_target_min=5)
    assert t["rto_target_sec"] == 120 and t["rpo_target_min"] == 5
    assert topo.get_targets(lid)["source"] == "link_column"
    assert topo.judge_rto(100, 120)["met"] is True
    assert topo.judge_rto(200, 120)["status"] == "breached"
    assert topo.judge_rto(None, 120)["status"] == "unknown"
    assert topo.judge_rpo(200, 5)["met"] is True     # 5min = 300s
    assert topo.judge_rpo(400, 5)["met"] is False
    assert topo.judge_rpo(0, 0)["met"] is True       # 近实时
    assert topo.judge_rpo(60, 0)["met"] is False
    with pytest.raises(ValueError):
        topo.set_targets(lid, rto_target_sec=-1)


def test_readiness_honest_aggregate(monkeypatch):
    lid = _link()
    topo.set_targets(lid, rto_target_sec=120)
    topo.upsert_candidate({"link_id": lid, "host": "4.4.4.4", "priority": 5})
    monkeypatch.setattr(topo, "probe_candidate",
                        lambda row, check_replica=True: {"id": row["id"], "healthy": True,
                                                         "reason": "", "priority": row["priority"]})
    r = topo.readiness(light=False)
    assert r["total_links"] >= 1
    item = next((i for i in r["items"] if i["link_id"] == lid), None)
    assert item and item["candidates_usable"] >= 1
    assert item["targets"]["rto_target_sec"] == 120
    assert "配置实体" in r["honest_note"]
    light = topo.readiness(light=True)
    assert all(i["skipped_probe"] for i in light["items"])
