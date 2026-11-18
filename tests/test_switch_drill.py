# -*- coding: utf-8 -*-
"""切换演练（switch drill）单元测试：追平判据 / 超时保护 / 编排闭环。

纯 mock 设计——不写元数据库、不连真实数据库（与 test_restore_precheck_gate
同思路，避免并发污染）。
"""
import time as _time

import pytest

from core import dr_failover as df


# ---------------------------------------------------------------------------
# GTID 追平判据
# ---------------------------------------------------------------------------

def test_gtid_missing_detects_divergence():
    """旧主有、备库没有的事务必须被检出（分叉保护语义复用）。"""
    p = df._parse_gtid_set("uuid-a:1-5,uuid-b:1-3")
    s = df._parse_gtid_set("uuid-a:1-5")
    missing = df.gtid_missing(p, s)
    assert missing, "uuid-b 区间缺失必须检出"
    # 备库 ⊇ 主库 → 追平
    s2 = df._parse_gtid_set("uuid-a:1-6,uuid-b:1-3")
    assert df.gtid_missing(p, s2) == []


def test_wait_catchup_ok(monkeypatch):
    """备库已覆盖主库事务集 → 立即返回 caught_up。"""
    sets = {"p": "uuid-a:1-5", "s": "uuid-a:1-9"}
    monkeypatch.setattr(df, "_fetch_gtid_executed",
                        lambda c: sets["p"] if c is PRIMARY else sets["s"])
    r = df._wait_standby_catchup(PRIMARY, STANDBY, timeout=5)
    assert r.get("caught_up") is True


def test_wait_catchup_timeout_blocks_promotion(monkeypatch):
    """备库追不平 → 超时抛错（追不平绝不提升的 RPO 保护）。"""
    sets = {"p": "uuid-a:1-5,uuid-b:1-2", "s": "uuid-a:1-9"}
    monkeypatch.setattr(df, "_fetch_gtid_executed",
                        lambda c: sets["p"] if c is PRIMARY else sets["s"])
    monkeypatch.setattr(df.time, "sleep", lambda *_: None)
    with pytest.raises(df.FailoverError) as ei:
        df._wait_standby_catchup(PRIMARY, STANDBY, timeout=0)
    assert "追平" in str(ei.value)


# ---------------------------------------------------------------------------
# 写入探针（幂等建库/写/读语义，monkeypatch 连接）
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, args=None):
        self.store.append((sql, args))

    def fetchall(self):
        return [(self.store.get("marker"),)] if False else []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def close(self):
        pass


def test_probe_write_read_marker(monkeypatch):
    """写入探针写 marker，读探针按 marker 命中。"""
    store = {"marker": None}
    monkeypatch.setattr(df, "_conn_of", lambda cfg: _FakeConn(store))

    class _Cur2(_FakeCursor):
        def execute(self, sql, args=None):
            if sql.startswith("INSERT"):
                store["marker"] = args[1]
            elif sql.startswith("SELECT marker"):
                store["_rows"] = [(store["marker"],)]
            elif sql.startswith("TRUNCATE"):
                store["marker"] = None
            store.setdefault("_log", []).append(sql)

        def fetchall(self):
            return store.get("_rows") or []

    monkeypatch.setattr(df, "_FakeCursorCls", _Cur2, raising=False)
    # _write_probe 用 conn.cursor() 的 execute 带参数——直接替换 cursor 工厂
    monkeypatch.setattr(_FakeConn, "cursor", lambda self: _Cur2(self.store))

    r = df._write_probe({}, 37, "drill-l37-123")
    assert r == {"written": True, "marker": "drill-l37-123"}
    rr = df._read_probe({}, "drill-l37-123")
    assert rr["found"] is True


# ---------------------------------------------------------------------------
# 演练编排闭环（run_switch_drill）
# ---------------------------------------------------------------------------

_LINK = {"id": 37, "name": "E2E-D17-容灾切换-MySQL170/171",
         "primary_site": "170-主库", "dr_site": "171-备库",
         "failover_config": "{}", "failover_history": "[]"}


@pytest.fixture(autouse=True)
def _patch_link_and_lock(monkeypatch):
    monkeypatch.setattr(df.db, "query_one", lambda *a, **k: dict(_LINK))
    monkeypatch.setattr(df.db, "execute", lambda *a, **k: None)


def test_switch_drill_happy_path(monkeypatch):
    captured = {}

    def fake_switch_planned(self):
        captured["switch"] = True
        return {"direction": "switch_planned", "steps": [{"name": "s", "ok": True}],
                "rto_sec": 12.0, "at": "t"}

    def fake_probe_write(self, marker):
        captured["marker"] = marker
        return {"written": True}

    def fake_rejoin(self):
        captured["rejoin"] = True
        return {}

    def fake_switch_back(self, probe_marker=""):
        captured["switchback_marker"] = probe_marker
        return {"direction": "switch_back", "steps": [{"name": "b", "ok": True}],
                "rto_sec": 15.0, "at": "t"}

    monkeypatch.setattr(df.FailoverEngine, "switch_planned", fake_switch_planned)
    monkeypatch.setattr(df.FailoverEngine, "switch_probe_write", fake_probe_write)
    monkeypatch.setattr(df.FailoverEngine, "switch_rejoin", fake_rejoin)
    monkeypatch.setattr(df.FailoverEngine, "switch_back_planned", fake_switch_back)

    r = df.run_switch_drill(37)
    assert r["ok"] is True
    assert r["rto_switch_sec"] == 12.0 and r["rto_switchback_sec"] == 15.0
    assert r["total_sec"] == 27.0
    assert captured.get("switch") and captured.get("rejoin")
    assert captured.get("marker", "").startswith("drill-l37-")
    # 回切必须带着同一个 marker 做闭环核对
    assert captured.get("switchback_marker") == captured.get("marker")


def test_switch_drill_failure_reports_steps_and_snapshot(monkeypatch):
    """任一步失败：ok=False + 已执行步骤 + 健康快照（供人工恢复）。"""
    def boom(self):
        raise df.FailoverError("备库追平超时（60s）")

    monkeypatch.setattr(df.FailoverEngine, "switch_planned", boom)
    monkeypatch.setattr(df, "health_snapshot", lambda lid: {"link_id": lid})

    r = df.run_switch_drill(37)
    assert r["ok"] is False
    assert "追平" in r["error"]
    assert r["snapshot"] == {"link_id": 37}


def test_switch_planned_refuses_unreachable_primary(monkeypatch):
    """主库不可达不属于计划性切换——必须引导走故障切换（failover force）。"""
    eng = df.FailoverEngine(dict(_LINK))
    monkeypatch.setattr(df, "_probe_mysql",
                        lambda *a, **k: {"reachable": False, "error": "refused"})
    with pytest.raises(df.FailoverError) as ei:
        eng.switch_planned()
    assert "failover" in str(ei.value) or "故障切换" in str(ei.value)


PRIMARY = {"host": "192.168.220.170", "port": 3306, "user": "root", "password": "x"}
STANDBY = {"host": "192.168.220.171", "port": 3306, "user": "root", "password": "x"}
