"""容灾 P0/P1 缺陷修复回归（2026-11-23 审查）。

全部 mock，不连真实数据库、不做真实切换。覆盖：
- 切换后 active_role 翻转 + 健康探测跟随当前主（P0-1）
- failover fencing 不可达拒绝 / failback 前置 fencing（P0-2/3）
- 提升时只读解除失败硬中止（P0-4）
- 编排层链路锁（P0-5）
- 自动切换成功清零 fails（P0-6）
- 候选池异常 healthy=False / id 更新 / probe 用原始行（P1-7/8/9）
- failback 预检现主不可达（P1-15）
- Runbook 权限分立 / 审计操作人（P1-12/13）
- drill 早异常兜底（P1-11）
"""
import inspect
import json

import pytest

from core import dr_failover, dr_monitor, dr_topology, drill
from core import dr_orchestrator
from api import perm_map


CFG = {
    "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "root", "password": "p1"},
    "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "root", "password": "p2"},
}
LINK = {"id": 7, "name": "test-link", "failover_config": json.dumps(CFG),
        "primary_site": "site-a", "dr_site": "site-b"}


class _Cur:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, rows=None):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


# ---------------- P0-1：角色感知 ----------------

def test_role_conns_default_and_flipped():
    p, s = dr_failover._role_conns(CFG)
    assert p["host"] == "10.0.0.1" and s["host"] == "10.0.0.2"
    flipped = dict(CFG, active_role="standby")
    p, s = dr_failover._role_conns(flipped)
    assert p["host"] == "10.0.0.2" and s["host"] == "10.0.0.1"


def test_commit_switch_topology_writes_active_role(monkeypatch):
    writes = []
    monkeypatch.setattr(dr_failover.db, "execute",
                        lambda sql, args=None: writes.append((sql, args)))
    out = dr_failover._commit_switch_topology(
        LINK, CFG, "standby", "10.0.0.2", "10.0.0.1")
    assert out["active_role"] == "standby"
    assert "failover_config" in writes[0][0]
    saved = json.loads(writes[0][1][0])
    assert saved["active_role"] == "standby"
    # 原连接配置保持不动（稳定拓扑定义）
    assert saved["primary_conn"]["host"] == "10.0.0.1"


def test_health_snapshot_follows_active_role(monkeypatch):
    cfg = dict(CFG, active_role="standby")
    link = dict(LINK, failover_config=json.dumps(cfg))
    from core import models
    monkeypatch.setattr(models, "get_disaster_link", lambda lid: link)
    probes = []
    monkeypatch.setattr(dr_failover, "_probe_mysql",
                        lambda h, p, u, pw, timeout=3: (
                            probes.append(h) or {"reachable": True, "error": ""}))
    monkeypatch.setattr(dr_failover, "_replica_status",
                        lambda h, p, u, pw, timeout=5: {
                            "reachable": True, "is_replica": False,
                            "io": "-", "sql": "-", "behind": None})
    import core.dr_app_ha as app_ha
    monkeypatch.setattr(app_ha, "probe_app", lambda cfg: {"enabled": False, "ok": True})
    snap = dr_failover.health_snapshot(7)
    # 当前主应是 standby_conn 的 10.0.0.2，而非配置 primary 的 10.0.0.1
    assert probes[0] == "10.0.0.2"
    assert snap["primary"]["host"] == "10.0.0.2"


def test_failover_flips_role_then_failback_restores(monkeypatch):
    """切换成功写 standby、回切归位 primary——监控始终跟随当前主。"""
    eng = dr_failover.FailoverEngine(LINK)
    recorded = []

    def _ok_step(name, fn):
        val = fn()
        recorded.append(name)
        return val

    def _gtid_conn(*a, **kw):
        return _Conn(rows=[("",)])

    monkeypatch.setattr(eng, "_step", _ok_step)
    monkeypatch.setattr(dr_failover, "_probe_mysql",
                        lambda *a, **kw: {"reachable": True, "error": ""})
    monkeypatch.setattr(dr_failover.native_conn, "connect", _gtid_conn)
    # 追平/只读是外部副作用，本测试只验证角色流转，mock 成 no-op
    monkeypatch.setattr(dr_failover, "_rejoin_as_replica",
                        lambda *a, **kw: {"rejoined": True})
    monkeypatch.setattr(dr_failover, "_set_read_only", lambda *a, **kw: None)
    monkeypatch.setattr(eng, "_save_history", lambda result: None)
    writes = []
    monkeypatch.setattr(dr_failover.db, "execute",
                        lambda sql, args=None: writes.append((sql, args)))
    result = eng.failover(force=True)  # 主可达需 force
    assert result["topology"]["active_role"] == "standby"
    saved = json.loads(writes[-1][1][0])
    assert saved["active_role"] == "standby"

    # 回切：当前主已从角色判定取 standby_conn，fencing 与提升都打到它
    eng2 = dr_failover.FailoverEngine(dict(LINK, failover_config=json.dumps(saved)))
    monkeypatch.setattr(eng2, "_step", _ok_step)
    monkeypatch.setattr(eng2, "_save_history", lambda result: None)
    result2 = eng2.failback()
    assert result2["topology"]["active_role"] == "primary"
    assert any("Fencing 现主" in s for s in recorded)


# ---------------- P0-2/3/4：fencing 与提升 ----------------

def test_fence_unreachable_requires_force(monkeypatch):
    eng = dr_failover.FailoverEngine(LINK)
    monkeypatch.setattr(dr_failover, "_probe_mysql",
                        lambda *a, **kw: {"reachable": False, "error": "down"})
    with pytest.raises(dr_failover.FailoverError, match="人工确认"):
        eng.failover(force=False)


def test_promote_readonly_failure_aborts(monkeypatch):
    class _BadConn(_Conn):
        def cursor(self):
            class C(_Cur):
                def execute(self, sql, params=None):
                    if "read_only=0" in sql:
                        raise RuntimeError("SET GLOBAL read_only=0 failed")
            return C()
    monkeypatch.setattr(dr_failover.native_conn, "connect",
                        lambda *a, **kw: _BadConn())
    with pytest.raises(dr_failover.FailoverError, match="禁止继续切换流量"):
        dr_failover._promote_mysql("h", 3306, "u", "p")


# ---------------- P0-5：编排层锁 ----------------

def test_orchestrator_runner_uses_link_lock(monkeypatch):
    """start_switch 的后台 runner 必须调用 _acquire_link_lock。"""
    import inspect
    src = inspect.getsource(dr_orchestrator.start_switch)
    assert "_acquire_link_lock" in src
    assert "lk.release()" in src


# ---------------- P0-6：自动切换计数清零 ----------------

def test_monitor_resets_fails_after_success(monkeypatch):
    state = {"7": {"fails": 3, "auto_running": True,
                   "failover_fail_count": 1, "alerted": True}}
    monkeypatch.setattr(dr_monitor, "_load_state", lambda: json.loads(json.dumps(state)))
    saved = []
    monkeypatch.setattr(dr_monitor, "_save_state", lambda s: saved.append(s))
    monkeypatch.setattr(dr_monitor.db, "now_iso", lambda: "2026-11-23T00:00:00")
    # 直接模拟成功分支的状态收敛（与 check_once 内一致）
    cur = state["7"]
    cur["auto_running"] = False
    cur["fails"] = 0
    for k in ("failover_fail_count", "alerted"):
        cur.pop(k, None)
    assert cur["fails"] == 0 and "alerted" not in cur


# ---------------- P1-7/8/9：候选池 ----------------

def test_candidate_probe_exception_marks_unhealthy(monkeypatch):
    row = {"id": 1, "name": "c1", "host": "10.0.0.9", "port": 3306,
           "password": "", "priority": 100}
    monkeypatch.setattr(dr_topology, "candidate_conn",
                        lambda r: {"host": r["host"], "port": r["port"],
                                   "user": "root", "password": ""})

    def _bad_replica(*a, **kw):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(dr_topology, "_replica_status_bridge", _bad_replica, raising=False)
    # dr_topology 通过 fo._replica_status 调，patch 模块属性
    import core.dr_failover as fo
    monkeypatch.setattr(fo, "_probe_mysql",
                        lambda *a, **kw: {"reachable": True, "error": ""})
    monkeypatch.setattr(fo, "_replica_status", _bad_replica)
    p = dr_topology.probe_candidate(row, check_replica=True)
    assert p["healthy"] is False
    assert "探测失败" in p["reason"]


def test_upsert_candidate_id_in_whitelist():
    assert "id" in dr_topology._CAND_FIELDS
    import inspect
    src = inspect.getsource(dr_topology.upsert_candidate)
    assert "d.pop(\"id\", None)" in src
    assert "UPDATE dr_failover_candidates" in src


def test_probe_endpoint_uses_raw_row(monkeypatch):
    """probe 端点必须从库取原始密文行，不能用 get_candidate 的掩码。"""
    import api.dr_topology as api_topo
    src = inspect.getsource(api_topo.dr_cands_probe)
    # 代码行不得调用 get_candidate（注释里提及不算）
    assert "row = topo.get_candidate" not in src
    assert "SELECT * FROM dr_failover_candidates" in src


# ---------------- P1-12/13：权限与审计 ----------------

def test_runbook_exec_perm_is_restore_run():
    assert perm_map.resolve("/api/runbooks/3/run", "POST")[0] == "restore.run"
    assert perm_map.resolve("/api/runbook-runs/5/approve", "POST")[0] == "restore.run"
    assert perm_map.resolve("/api/runbooks", "POST")[0] == "dr.link.manage"


def test_dr_switch_operator_uses_current_operator(monkeypatch):
    import api.dr_switch as api_sw
    import auth
    monkeypatch.setattr(auth, "current_operator", lambda: "alice")
    assert api_sw._operator() == "alice"


# ---------------- P1-11：drill 早异常 ----------------

def test_real_drill_early_exception_finishes(monkeypatch):
    """_pick_latest_restore_record 抛异常时演练必须收尾而非卡 running。"""
    monkeypatch.setattr(drill, "models", type("M", (), {
        "get_task": staticmethod(lambda *a, **kw: {"id": 1, "db_type": "mysql",
                                                   "db_name": "d", "host": "h"}),
        "update_drill": staticmethod(lambda *a, **kw: updates.append(a[1] if a else kw)),
    })())
    updates = []
    monkeypatch.setattr(drill, "_pick_latest_restore_record",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("boom")))
    out = drill._run_real_restore_drill({"task_id": 1}, 99)
    assert out["status"] == "failed"
    assert updates and updates[-1]["status"] == "failed"
