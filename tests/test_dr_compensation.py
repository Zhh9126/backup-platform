# -*- coding: utf-8 -*-
"""失败切换补偿建议回归（2026-11-20）。

覆盖 core/dr_compensation.py 的判定口径：
1. 按**真实步骤名**判定卡点；
2. 「DNS 牵引未完成」→ 给出可一键执行的补偿（scope=dns_only）；
3. 链路未配 DNS → 如实降级为人工（不假装可自动执行）；
4. 认不出的步骤 → 只给人工判定，**不编造**动作；
5. GTID 分叉 / 追平失败 → 如实说明是保护行为，建议人工而非自动绕过。
"""
from core import dr_compensation as dc


def _link(with_dns=True):
    if with_dns:
        return {"id": 1, "name": "L", "failover_config":
                '{"dns": {"domain": "dr-mysql.test", "provider": "dnsmasq"}}'}
    return {"id": 1, "name": "L", "failover_config": "{}"}


def _run(status="failed", steps=None, direction="failover", rid=7):
    return {"id": rid, "status": status, "direction": direction,
            "steps": steps or []}


def _step(name, ok=True):
    return {"name": name, "ok": ok, "ms": 10, "detail": {}}


def test_dns_step_failure_gives_executable_compensation():
    run = _run(steps=[_step("主库故障探测"), _step("Fencing 旧主写入冻结"),
                      _step("备库提升为主库"),
                      _step("DNS 切换（dr-mysql.test → 2.2.2.2）", ok=False)])
    res = dc.analyze(run, _link())
    assert res["failed_step"].startswith("DNS 切换")
    assert "仅 DNS 牵引未完成" in res["diagnosis"]
    acts = res["actions"]
    assert acts and acts[0]["executable"] is True
    assert acts[0]["scope"] == "dns_only"
    assert acts[0]["direction"] == "failover"


def test_dns_compaction_degrades_without_dns_config():
    run = _run(steps=[_step("备库提升为主库"),
                      _step("DNS 切换（x → y）", ok=False)])
    res = dc.analyze(run, _link(with_dns=False))
    acts = res["actions"]
    assert acts and acts[0]["executable"] is False
    assert acts[0]["id"] == "manual"


def test_probe_failure_means_no_change_made():
    run = _run(steps=[_step("主库故障探测", ok=False)])
    res = dc.analyze(run, _link())
    assert "尚未对任何实例做变更" in res["diagnosis"]
    assert res["actions"][0]["id"] == "manual"
    assert res["actions"][0]["executable"] is False


def test_gtid_divergence_is_protection_not_autofix():
    run = _run(direction="failback",
               steps=[_step("GTID 分叉检测", ok=False)])
    res = dc.analyze(run, _link())
    assert "保护行为" in res["diagnosis"]
    assert res["actions"][0]["executable"] is False


def test_catchup_failure_suggests_manual_fix():
    run = _run(direction="failback",
               steps=[_step("GTID 分叉检测"),
                      _step("旧主追平现主增量", ok=False)])
    res = dc.analyze(run, _link())
    assert "防止数据缺失" in res["diagnosis"]
    assert res["actions"][0]["id"] == "manual"


def test_unknown_step_no_fabricated_action():
    run = _run(steps=[_step("某个从未见过的步骤", ok=False)])
    res = dc.analyze(run, _link())
    assert "未识别" in res["diagnosis"]
    assert len(res["actions"]) == 1
    assert res["actions"][0]["executable"] is False


def test_cancelled_after_promotion_offers_dns_compensation():
    run = _run(status="cancelled",
               steps=[_step("主库故障探测"), _step("备库提升为主库")])
    res = dc.analyze(run, _link())
    assert "被中止" in res["diagnosis"]
    assert res["actions"][0]["executable"] is True
    assert res["actions"][0]["scope"] == "dns_only"


def test_cancelled_before_promotion_is_manual():
    run = _run(status="cancelled", steps=[_step("主库故障探测")])
    res = dc.analyze(run, _link())
    assert res["actions"][0]["executable"] is False


def test_success_needs_no_compensation():
    res = dc.analyze(_run(status="success", steps=[_step("新主可达验证")]), _link())
    assert "无需补偿" in res["diagnosis"]
    assert res["actions"][0]["executable"] is False


def test_analyze_shape():
    res = dc.analyze(_run(steps=[_step("主库故障探测", ok=False)]), _link())
    for k in ("run_id", "status", "direction", "failed_step",
              "last_ok_step", "diagnosis", "actions", "generated_at"):
        assert k in res
