# -*- coding: utf-8 -*-
"""容灾巡检 + 切换 scope 单元测试。

隔离元数据库 + mock 探测（不连真实数据库）：
- 巡检配置（cron 校验）
- 单链路巡检三场景（健康/异常/未配置）
- DNS 服务巡检（一致/不一致/读取失败）
- 巡检主入口落库与保留策略
- 编排层 scope：_active_site 判定 / dns_only 预检放宽
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dri_test_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest

from core import db  # noqa: E402  需在 env 设置之后导入
from core import dr_inspection as dri  # noqa: E402
from core import dr_orchestrator as orch  # noqa: E402
from core import models  # noqa: E402


# ---------------------------------------------------------------- 配置
def test_config_default_and_save():
    cfg = dri.get_config()
    assert cfg["enabled"] is True
    assert cfg["cron"] == dri.DEFAULT_CRON
    saved = dri.save_config(False, "30 7 * * *")
    assert saved["enabled"] is False
    assert saved["cron"] == "30 7 * * *"
    with pytest.raises(ValueError, match="5 段"):
        dri.save_config(True, "0 8 * *")
    with pytest.raises(ValueError, match="5 段"):
        dri.save_config(True, "bad cron")
    dri.save_config(True, dri.DEFAULT_CRON)


# ---------------------------------------------------------------- 单链路巡检
def _mk_link(name, config="", **kw):
    data = {"name": name, "primary_site": "140", "dr_site": "129",
            "enabled": 1, "failover_config": config}
    data.update(kw)
    rid = models.create_disaster_link(data)
    return models.get_disaster_link(rid)


def test_inspect_link_without_config_is_warn():
    lk = _mk_link("无配置链路")
    out = dri.inspect_link(lk)
    assert out["overall"] == "warn"
    assert out["checks"][0]["status"] == "warn"
    assert "failover-config" in out["checks"][0]["message"]


def test_inspect_link_healthy(monkeypatch):
    lk = _mk_link("健康链路", json.dumps({
        "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "u", "password": "p"},
    }))
    snap = {
        "primary": {"reachable": True, "host": "10.0.0.1", "port": 3306},
        "standby": {"reachable": True, "host": "10.0.0.2", "port": 3306,
                    "is_replica": True, "io": "Yes", "sql": "Yes", "behind": 0},
        "dns": {"domain": "dr.test", "resolved_ip": "10.0.0.1",
                "expected_ip": "10.0.0.1", "in_sync": True},
        "app": None,
    }
    from core import dr_failover
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)
    out = dri.inspect_link(lk)
    assert out["overall"] == "ok"
    assert out["active_site"] == "primary"
    statuses = [c["status"] for c in out["checks"]]
    assert "fail" not in statuses and "warn" not in statuses


def test_inspect_link_replication_lag_is_warn(monkeypatch):
    lk = _mk_link("延迟链路", json.dumps({
        "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "u", "password": "p"},
    }))
    snap = {
        "primary": {"reachable": True, "host": "10.0.0.1", "port": 3306},
        "standby": {"reachable": True, "host": "10.0.0.2", "port": 3306,
                    "is_replica": True, "io": "Yes", "sql": "Yes", "behind": 120},
        "dns": None, "app": None,
    }
    from core import dr_failover
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)
    out = dri.inspect_link(lk)
    assert out["overall"] == "fail"  # 延迟 >60s 判 fail（RPO 风险）
    lag = [c for c in out["checks"] if "延迟" in c["item"]][0]
    assert lag["status"] == "fail" and "120" in lag["message"]


def test_inspect_link_snapshot_error_is_fail(monkeypatch):
    lk = _mk_link("探活失败链路", json.dumps({
        "primary_conn": {"host": "10.0.0.9", "port": 3306, "user": "u", "password": "p"},
    }))
    from core import dr_failover

    def boom(lid):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(dr_failover, "health_snapshot", boom)
    out = dri.inspect_link(lk)
    assert out["overall"] == "fail"
    assert "connection refused" in out["checks"][0]["message"]


# ---------------------------------------------------------------- DNS 服务级
def test_dns_service_consistent(monkeypatch):
    from core import dr_dns
    monkeypatch.setattr(dr_dns, "list_records",
                        lambda: [{"domain": "a.test", "ip": "1.1.1.1"}])
    monkeypatch.setattr(dr_dns, "resolve_domain", lambda d: "1.1.1.1")
    out = dri.inspect_dns_service()
    assert out["ok"] is True
    assert out["records"][0]["consistent"] is True


def test_dns_service_inconsistent(monkeypatch):
    from core import dr_dns
    monkeypatch.setattr(dr_dns, "list_records",
                        lambda: [{"domain": "a.test", "ip": "1.1.1.1"}])
    monkeypatch.setattr(dr_dns, "resolve_domain", lambda d: "2.2.2.2")
    out = dri.inspect_dns_service()
    assert out["ok"] is False
    assert out["errors"] and "不一致" in out["errors"][0]


# ---------------------------------------------------------------- 主入口
def test_run_inspection_empty_links_is_warn(monkeypatch):
    monkeypatch.setattr(models, "list_disaster_links", lambda: [])
    insp = dri.run_inspection(triggered_by="test")
    assert insp["overall"] == "warn"
    assert insp["links_total"] == 0
    got = dri.get_inspection(insp["id"])
    assert got["detail"] == []
    assert dri.list_inspections()[0]["id"] == insp["id"]


def test_run_inspection_fail_when_link_fail(monkeypatch):
    lk = _mk_link("失败链路", json.dumps({
        "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "u", "password": "p"},
    }))
    snap = {
        "primary": {"reachable": False, "host": "10.0.0.1", "port": 3306,
                    "error": "refused"},
        "standby": {"reachable": True, "host": "10.0.0.2", "port": 3306,
                    "is_replica": True, "io": "Yes", "sql": "Yes", "behind": 0},
        "dns": None, "app": None,
    }
    from core import dr_failover
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)
    monkeypatch.setattr(models, "list_disaster_links", lambda: [dict(lk)])
    insp = dri.run_inspection(triggered_by="test")
    assert insp["overall"] == "fail"
    assert insp["fail_count"] == 1
    detail = insp["detail"][0]
    assert any(c["status"] == "fail" for c in detail["checks"])


def test_inspection_delete():
    insp = dri.run_inspection(triggered_by="test")
    assert dri.delete_inspection(insp["id"]) is True
    assert dri.get_inspection(insp["id"]) == {}
    assert dri.delete_inspection(insp["id"]) is False


# ---------------------------------------------------------------- 编排层 scope
def test_active_site_by_dns_and_reachability():
    assert orch._active_site(
        {"dns": {"resolved_ip": "10.0.0.2", "in_sync": False}},
        "10.0.0.1", "10.0.0.2") == "standby"
    assert orch._active_site(
        {"dns": {"resolved_ip": "10.0.0.1", "in_sync": True}},
        "10.0.0.1", "10.0.0.2") == "primary"
    # DNS 指向未知 IP → unknown（不伪造）
    assert orch._active_site(
        {"dns": {"resolved_ip": "9.9.9.9"}},
        "10.0.0.1", "10.0.0.2") == "unknown"
    # 无 DNS：主可达 → primary；主不可达 → unknown
    assert orch._active_site(
        {"primary": {"reachable": True}}, "10.0.0.1", "10.0.0.2") == "primary"
    assert orch._active_site(
        {"primary": {"reachable": False}}, "10.0.0.1", "10.0.0.2") == "unknown"


def test_parse_region():
    assert orch._parse_region('{"city":"成都","lat":30.5,"lon":104}') == \
        {"city": "成都", "lat": 30.5, "lon": 104}
    assert orch._parse_region("bad json") == {}
    assert orch._parse_region(None) == {}


def test_precheck_dns_only_requires_dns_config(monkeypatch):
    lk = _mk_link("DNS 预检链路", json.dumps({
        "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "u", "password": "p"},
    }))
    snap = {
        "primary": {"reachable": True, "host": "10.0.0.1", "port": 3306},
        "standby": {"reachable": True, "host": "10.0.0.2", "port": 3306,
                    "is_replica": True, "io": "Yes", "sql": "Yes", "behind": 0},
        "dns": None, "app": None,
    }
    from core import dr_failover
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)
    # 未配置 DNS → 阻塞
    res = orch.precheck(lk["id"], direction="failover", scope="dns_only")
    assert res["ready"] is False
    assert any("未配置 DNS" in r for r in res["risks"])
    # 有 DNS → 放行（不检查数据库复制角色）
    snap["dns"] = {"domain": "dr.test", "resolved_ip": "10.0.0.1",
                   "expected_ip": "10.0.0.1", "in_sync": True}
    res = orch.precheck(lk["id"], direction="failover", scope="dns_only")
    assert res["ready"] is True


def test_start_switch_scope_recorded(monkeypatch):
    """dns_only 作业：mock 引擎方法，验证 run 记录 scope 与执行路径。"""
    lk = _mk_link("scope 链路", json.dumps({
        "primary_conn": {"host": "10.0.0.1", "port": 3306, "user": "u", "password": "p"},
        "standby_conn": {"host": "10.0.0.2", "port": 3306, "user": "u", "password": "p"},
        "dns": {"domain": "dr.test", "provider": "dnsmasq",
                "conf_path": "/tmp/nonexistent.conf", "container": "x"},
    }))
    snap = {
        "primary": {"reachable": True, "host": "10.0.0.1", "port": 3306},
        "standby": {"reachable": True, "host": "10.0.0.2", "port": 3306,
                    "is_replica": True, "io": "Yes", "sql": "Yes", "behind": 0},
        "dns": {"domain": "dr.test", "resolved_ip": "10.0.0.1",
                "expected_ip": "10.0.0.1", "in_sync": True},
        "app": None,
    }
    from core import dr_failover
    monkeypatch.setattr(dr_failover, "health_snapshot", lambda lid: snap)

    class FakeEngine:
        def __init__(self, link):
            self.steps = []
            self.on_step = None

        def switch_dns_only(self, direction):
            self.steps.append({"name": "DNS 切换", "ok": True, "ms": 5,
                               "detail": {"resolved": "10.0.0.2"}})
            return {"direction": direction, "scope": "dns_only",
                    "domain": "dr.test", "to": "10.0.0.2",
                    "steps": self.steps, "rto_sec": 0.1, "at": "t"}

    monkeypatch.setattr(dr_failover, "FailoverEngine", FakeEngine)
    res = orch.start_switch(lk["id"], direction="failover", scope="dns_only",
                            operator="t")
    assert res["ok"] is True
    import time as _t
    run = {}
    for _ in range(50):  # 后台线程执行，轮询等终态（最多 5s）
        run = orch.get_run(res["run_id"])
        if run.get("status") in ("success", "failed"):
            break
        _t.sleep(0.1)
    assert run["status"] == "success"
    assert run["scope"] == "dns_only"
    assert run["status"] == "success"
    assert run["steps"][0]["name"] == "DNS 切换"
    assert "仅DNS" in run["message"]
