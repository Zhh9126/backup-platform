# -*- coding: utf-8 -*-
"""P1-1 自动容灾切换监控回归（探测函数 mock，验证状态机与分支）。"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dr_mon_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "bk")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import core.db as db
db.init_schema()

from core import dr_failover, dr_monitor, models


def _make_link(auto: dict, name="mon-link") -> int:
    cfg = {
        "primary_conn": {"host": "10.9.9.9", "port": 3306,
                         "user": "root", "password": "enc:x"},
        "standby_conn": {"host": "10.9.9.10", "port": 3306,
                         "user": "root", "password": "enc:y"},
    }
    if auto is not None:
        cfg["auto"] = auto
    lid = models.create_disaster_link({
        "name": name, "status": "active", "enabled": 1})
    # failover_config 走 update 白名单路径（与 API 行为一致）
    models.update_disaster_link(lid, {
        "failover_config": json.dumps(cfg, ensure_ascii=False)})
    return lid


def _snap(reachable=True, error=""):
    return {"link_id": 1, "link_name": "x", "checked_at": db.now_iso(),
            "primary": {"reachable": reachable, "error": error,
                        "host": "10.9.9.9", "port": 3306},
            "standby": None, "dns": None}


def _advance_clock():
    """回拨所有链路的 last_check_ts，绕过探测间隔节流（测试专用）。"""
    st = dr_monitor.state_view()
    for k in st:
        st[k]["last_check_ts"] = 0
    dr_monitor._save_state(st)


def setup_function(function):
    dr_monitor.reset_state()
    db.execute("DELETE FROM disaster_links")


def test_auto_policy_defaults_and_clamp():
    p = dr_failover.auto_policy({})
    assert p["enabled"] is False and p["require_manual_confirm"] is True
    p2 = dr_failover.auto_policy({"auto": {"enabled": True,
                                           "check_interval_sec": 1,
                                           "consecutive_failures": 0}})
    assert p2["enabled"] is True
    assert p2["check_interval_sec"] >= 10      # 下限钳制
    assert p2["consecutive_failures"] >= 1


def test_health_snapshot_structure(monkeypatch):
    lid = _make_link(None)
    monkeypatch.setattr(dr_failover, "_probe_mysql",
                        lambda *a, **k: {"reachable": True, "error": ""})
    monkeypatch.setattr(dr_failover, "_replica_status",
                        lambda *a, **k: {"reachable": True, "is_replica": True,
                                         "io": "Yes", "sql": "Yes", "behind": 0})
    monkeypatch.setattr(dr_failover, "_resolve_domain",
                        lambda domain: "10.9.9.9")
    # dns 配置注入
    link = models.get_disaster_link(lid)
    cfg = dr_failover._load_config(link)
    cfg["dns"] = {"domain": "dr.test", "provider": "dnsmasq"}
    db.execute("UPDATE disaster_links SET failover_config=? WHERE id=?",
               (json.dumps(cfg), lid))
    snap = dr_failover.health_snapshot(lid)
    assert snap["primary"]["reachable"] is True
    assert snap["standby"]["io"] == "Yes" and snap["standby"]["behind"] == 0
    assert snap["dns"]["in_sync"] is True


def test_monitor_counts_then_alerts(monkeypatch):
    lid = _make_link({"enabled": True, "check_interval_sec": 10,
                      "consecutive_failures": 2,
                      "require_manual_confirm": True})
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(False, "connection refused"))
    _advance_clock()
    out1 = dr_monitor.check_once()
    assert out1[0]["action"].startswith("counting")
    _advance_clock()
    out2 = dr_monitor.check_once()
    assert out2[0]["action"] == "alerted"
    st = dr_monitor.state_view()[str(lid)]
    assert st["fails"] == 2 and st["alerted"] is True
    # 第三轮不重复告警（去重）
    _advance_clock()
    out3 = dr_monitor.check_once()
    assert out3[0]["action"] == "alerted"
    # 系统日志落库
    row = db.query_one("SELECT level, source FROM system_logs "
                       "WHERE source='dr_monitor' ORDER BY id DESC LIMIT 1")
    assert row and row["level"] == "WARNING"


def test_monitor_auto_failover(monkeypatch):
    lid = _make_link({"enabled": True, "check_interval_sec": 10,
                      "consecutive_failures": 2,
                      "require_manual_confirm": False})
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(False, "down"))
    monkeypatch.setattr(dr_failover, "run_failover",
                        lambda x, force=False: {"success": True,
                                                "rto_sec": 12.5, "to": "10.9.9.10"})
    dr_monitor.check_once()
    _advance_clock()
    out = dr_monitor.check_once()
    assert out[0]["action"] == "auto_failover"
    assert out[0]["result"]["success"] is True and out[0]["result"]["rto_sec"] == 12.5
    st = dr_monitor.state_view()[str(lid)]
    assert st.get("auto_running") is False and st.get("last_failover_at")


def test_monitor_auto_failover_failure_recorded(monkeypatch):
    lid = _make_link({"enabled": True, "check_interval_sec": 10,
                      "consecutive_failures": 1,
                      "require_manual_confirm": False})
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(False, "down"))
    monkeypatch.setattr(dr_failover, "run_failover",
                        lambda x, force=False: (_ for _ in ()).throw(
                            dr_failover.FailoverError("备库不可达")))
    _advance_clock()
    out = dr_monitor.check_once()
    assert out[0]["action"] == "auto_failover_failed"
    assert out[0]["result"]["retry_in_sec"] == 60
    st = dr_monitor.state_view()[str(lid)]
    assert "备库不可达" in (st.get("auto_failover_error") or "")
    assert st["failover_fail_count"] == 1
    # 退避期内不再重试，避免每轮探测都尝试切换并刷 ERROR
    _advance_clock()
    out2 = dr_monitor.check_once()
    assert out2[0]["action"] == "backoff"
    assert out2[0]["result"]["retry_in_sec"] > 0
    # 模拟退避到期：再次真实尝试，退避翻倍
    st2 = dr_monitor.state_view()
    st2[str(lid)]["failover_backoff_until"] = 0
    dr_monitor._save_state(st2)
    _advance_clock()
    out3 = dr_monitor.check_once()
    assert out3[0]["action"] == "auto_failover_failed"
    assert out3[0]["result"]["retry_in_sec"] == 120
    assert dr_monitor.state_view()[str(lid)]["failover_fail_count"] == 2


def test_monitor_recovery_clears_backoff(monkeypatch):
    """主库恢复后 reset_state 应清空退避，故障再来时重新按 60s 起算。"""
    lid = _make_link({"enabled": True, "check_interval_sec": 10,
                      "consecutive_failures": 1,
                      "require_manual_confirm": False})
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(False, "down"))
    monkeypatch.setattr(dr_failover, "run_failover",
                        lambda x, force=False: (_ for _ in ()).throw(
                            dr_failover.FailoverError("备库不可达")))
    _advance_clock()
    assert dr_monitor.check_once()[0]["action"] == "auto_failover_failed"
    assert dr_monitor.state_view()[str(lid)].get("failover_backoff_until")
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda x: _snap(True))
    _advance_clock()
    assert dr_monitor.check_once()[0]["action"] == "ok"
    st = dr_monitor.state_view()[str(lid)]
    assert not st.get("failover_backoff_until")
    assert not st.get("failover_fail_count")


def test_monitor_recovery_resets(monkeypatch):
    lid = _make_link({"enabled": True, "check_interval_sec": 10,
                      "consecutive_failures": 2,
                      "require_manual_confirm": True})
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(False, "down"))
    dr_monitor.check_once()
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: _snap(True))
    _advance_clock()
    out = dr_monitor.check_once()
    assert out[0]["action"] == "ok"
    st = dr_monitor.state_view()[str(lid)]
    assert st["fails"] == 0 and not st.get("alerted")


def test_monitor_disabled_links_skipped(monkeypatch):
    _make_link(None, "no-auto")
    _make_link({"enabled": False}, "auto-off")
    monkeypatch.setattr(dr_failover, "health_snapshot",
                        lambda x: (_ for _ in ()).throw(AssertionError("不应探测")))
    assert dr_monitor.check_once() == []
