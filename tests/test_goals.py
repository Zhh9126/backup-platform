# -*- coding: utf-8 -*-
"""P0-2 目标驱动收口单测：RPO→CDP / 备份间隔 / 异地副本 三规则。"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="goals_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
os.environ.setdefault("LD_LIBRARY_PATH",
                      os.path.join(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__))), "lib"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core.goals import evaluate_task_goals, protection_gaps  # noqa: E402
import core.models as models  # noqa: E402


def _mk(name, level=None, extra_task=None, extra_extra=None):
    task = {
        "name": name, "db_type": "mysql", "host": "127.0.0.1", "port": 3307,
        "username": "root", "password": "x", "db_name": "demo",
        "backup_type": "full", "schedule_type": "cron",
        "cron_expr": "0 2 * * *", "enabled": 1,
        "extra_options": json.dumps(extra_extra or {}),
    }
    task.update(extra_task or {})
    tid = models.create_task(task)
    if level:
        # protection_level 是派生列（仅 policy_id 绑定时填充），测试直接置列
        _db.execute("UPDATE backup_tasks SET protection_level=? WHERE id=?",
                    (level, tid))
    return models.get_task(tid, include_secret=True)


def test_rpo_near_realtime_no_cdp_is_fail():
    # core 策略 RPO≈0（近实时）且未开 rt → fail 级缺口
    t = _mk("g-core", level="core")
    gaps = evaluate_task_goals(t)
    fail = next((g for g in gaps if g["code"] == "rpo_no_cdp"), None)
    assert fail and fail["level"] == "fail", gaps


def test_rpo_near_realtime_with_cdp_no_gap():
    t = _mk("g-core2", level="core", extra_task={"rt_enabled": 1})
    if not t.get("rt_enabled"):
        # rt_enabled 不在 create_task 白名单（由 rt 配置接口管理），直置列
        _db.execute("UPDATE backup_tasks SET rt_enabled=1 WHERE id=?",
                    (t["id"],))
        t = models.get_task(t["id"], include_secret=True)
    gaps = evaluate_task_goals(t)
    assert not any(g["code"] == "rpo_no_cdp" for g in gaps), gaps


def test_interval_gt_rpo_warned():
    # 每天 02:00（1440 分钟间隔）绑 important 策略（RPO 15 分钟）→ 告警
    t = _mk("g-interval", level="important")
    gaps = evaluate_task_goals(t)
    assert any(g["code"] == "interval_gt_rpo" for g in gaps), gaps


def test_cron_every_15min_ok_for_240min_rpo():
    # */15 → 15 分钟间隔；general RPO 240 分钟 → 无 interval 告警
    t = _mk("g-cron15", level="general", extra_task={"cron_expr": "*/15 * * * *"})
    gaps = evaluate_task_goals(t)
    assert not any(g["code"] == "interval_gt_rpo" for g in gaps), gaps


def test_cross_site_without_offsite_warned():
    # core 策略 cross_site=true 且 storage=local → 告警
    t = _mk("g-cross", level="core", extra_task={"storage_backend": "local"})
    gaps = evaluate_task_goals(t)
    assert any(g["code"] == "cross_site_no_offsite" for g in gaps), gaps


def test_cross_site_with_remote_ok():
    t = _mk("g-cross2", level="core", extra_task={"storage_backend": "s3"})
    gaps = evaluate_task_goals(t)
    assert not any(g["code"] == "cross_site_no_offsite" for g in gaps), gaps


def test_protection_gaps_aggregate():
    _mk("g-agg1", level="core")
    agg = protection_gaps()
    assert agg["total"] >= 1
    assert agg["fail_count"] >= 1
    assert all("task_id" in g and "message" in g for g in agg["gaps"])
