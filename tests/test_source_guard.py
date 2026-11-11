# -*- coding: utf-8 -*-
"""源端资源感知 / 三层 CPU 保护 / 离线负载周期学习回归。

真实 170/171 SSH 采样由 /root/e2e/e2e_source_guard_170171.py 覆盖；
本文件用注入样本验证阈值决策、fail-open、并发降级、窗口统计与全链路契约。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="source_guard_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import core.db as db  # noqa: E402
db.init_schema()

from core import models, source_guard as sg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK = {"id": 1, "host": "192.0.2.10", "db_type": "mysql", "name": "unit"}


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _sample(pct):
    return {"host": "192.0.2.10", "cpu_pct": pct, "load1": 1.0,
            "cores": 2, "source": "ssh", "ts": "2026-11-10 10:00:00",
            "error": ""}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sg._ensure_tables()  # 样本表是懒创建的，fixture 清理前先确保存在
    db.execute("DELETE FROM source_load_samples")
    sg._throttle_until = 0.0
    sg._throttle_parallel = 0
    yield


def test_parse_probe_dual_sample():
    text = ("cpu  100 0 100 1000 0 0 0 0\n"
            "cpu  200 0 200 1100 0 0 0 0\n"
            "0.50 0.40 0.30 1/100 123\n2")
    r = sg._parse_probe(text)
    # delta total=300, idle=100 → 66.7%
    assert 66.0 <= r["cpu_pct"] <= 67.5
    assert r["load1"] == 0.5 and r["cores"] == 2
    assert r["estimated"] is False


def test_parse_probe_loadavg_fallback():
    r = sg._parse_probe("garbage\n0.80 0.40 0.30 1/100 123\n2")
    assert r["cpu_pct"] == 40.0
    assert r["estimated"] is True


def test_disabled_by_default():
    v = sg.evaluate(TASK)
    assert v["level"] == "disabled"


def test_zero_config_and_invalid_values():
    cfg = sg.get_config()
    assert cfg["source_guard_enabled"] == "0"
    assert cfg["source_guard_block_pct"] == "70"
    with pytest.raises(ValueError):
        sg.set_config({"source_guard_block_pct": "abc"})
    with pytest.raises(ValueError):
        sg.set_config({"source_guard_block_pct": "150"})


def test_block_above_70(monkeypatch):
    monkeypatch.setattr(sg, "get_config", lambda: dict(sg.DEFAULTS, source_guard_enabled="1"))
    v = sg.evaluate(TASK, force=True, sample=_sample(85.0))
    assert v["level"] == "block" and "拒绝" in v["message"]


def test_throttle_between_50_and_70(monkeypatch):
    monkeypatch.setattr(sg, "get_config", lambda: dict(sg.DEFAULTS, source_guard_enabled="1"))
    v = sg.evaluate(TASK, force=True, sample=_sample(60.0))
    assert v["level"] == "throttle"
    assert v["throttle_kbps"] == 51200 and v["parallel"] == 2
    assert sg.current_parallel_cap(8) == 2
    assert sg.current_parallel_cap(1) == 1


def test_ok_below_50(monkeypatch):
    monkeypatch.setattr(sg, "get_config", lambda: dict(sg.DEFAULTS, source_guard_enabled="1"))
    v = sg.evaluate(TASK, force=True, sample=_sample(20.0))
    assert v["level"] == "ok"
    assert sg.current_parallel_cap(8) == 8


def test_sample_failure_fails_open(monkeypatch):
    monkeypatch.setattr(sg, "get_config", lambda: dict(sg.DEFAULTS, source_guard_enabled="1"))
    v = sg.evaluate(TASK, force=True,
                    sample={"host": "h", "cpu_pct": None, "error": "SSH 采样失败"})
    assert v["level"] == "unknown" and v["fail_open"] is True
    assert "放行备份" in v["message"]


def test_remote_failure_never_uses_local_cpu(monkeypatch):
    """远端采样失败必须返回 error，不能拿平台机 CPU 冒充源端。"""
    monkeypatch.setattr("core.remote_dump.resolve_ssh_host",
                        lambda task: {"host_key": "root@h:22", "hostname": "h"})
    monkeypatch.setattr("core.remote_dump._connect_isolated",
                        lambda ssh: (_ for _ in ()).throw(OSError("no route")))
    r = sg.sample_cpu(TASK)
    assert r["cpu_pct"] is None and r["source"] == "unavailable"
    assert "SSH 采样失败" in r["error"]


def test_guard_before_backup_writes_failed_record(monkeypatch):
    tid = models.create_task({
        "name": "sg-block-test", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "root", "password": "x", "db_name": "d",
        "backup_type": "FULL", "backup_mode": "logical", "enabled": 0,
    })
    task = models.get_task(tid, include_secret=True)
    monkeypatch.setattr(sg, "evaluate", lambda t: {
        "level": "block", "cpu_pct": 90.0,
        "message": "源端 CPU 90% > 拒绝阈值 70%，为保护生产系统拒绝本次备份"})
    out = sg.guard_before_backup(task, operator="unit")
    assert out["blocked"] is True
    assert out["record"]["status"] == "failed"
    row = db.query_one("SELECT status, message FROM backup_records WHERE id=?",
                       (out["record"]["id"],))
    assert row["status"] == "failed" and "拒绝" in row["message"]


def test_recommend_window_insufficient_samples():
    r = sg.recommend_window()
    assert r["ok"] is False and "样本不足" in r["reason"]


def test_recommend_window_picks_lowest_cpu_hour():
    now = "2026-11-10 10:00:00"
    for hour, pct in ((3, 12.0), (3, 14.0), (14, 80.0), (14, 90.0), (20, 40.0)):
        db.execute(
            "INSERT INTO source_load_samples "
            "(task_id, host, ts, hour, cpu_pct, load1, source) VALUES (?,?,?,?,?,?,?)",
            (1, "h", now, hour, pct, 0.5, "ssh"))
    r = sg.recommend_window(min_samples=5)
    assert r["ok"] is True
    assert r["best_hour"] == 3 and r["cron"] == "0 3 * * *"
    assert r["samples"] == 5 and "离线" in r["method"]


def test_apply_window_rejects_when_samples_insufficient():
    r = sg.apply_window(999999)
    assert r["ok"] is False and "样本不足" in r["reason"]


def test_full_chain_contract():
    sched = _src("core/scheduler.py")
    assert "guard_before_backup" in sched
    assert "current_parallel_cap" in sched
    assert 'task["bandwidth_limit"] = guard["throttle_kbps"]' in sched

    api = _src("api/tasks.py")
    assert "check_api_guard" in api and '"/tasks/<int:task_id>/retry"' in api
    assert "source_guard_config" in _src("api/source_guard.py")
    assert "source_guard" in _src("api/__init__.py")

    html = _src("templates/settings.html")
    assert 'id="sg_enabled"' in html and 'id="sgApplyBtn"' in html
    assert "/api/source-guard/window" in html
