# -*- coding: utf-8 -*-
"""备份策略重构回归（2026-11-13）。

旧版策略只有 RPO/RTO 两个数字，backup_strategy 是一段手写 JSON，
与任务的频次/周期/保留完全脱节（策略改了任务不动）。本文件锁定新契约：

1. 旧版 frequency（PT1H/P1D/PT15M）能自动换算成 interval；
2. 结构化调度能翻译成任务字段（cron / interval / 组合全量+增量）；
3. 保留策略 → retention_days / retention_count / retention_lock_days；
4. 人话摘要与下次执行时间可预览；
5. 策略 → 任务字段的整体拼装（policy_task_fields）。
"""
from core import policy_schedule as ps


def test_legacy_frequency_maps_to_interval():
    s = ps.normalize_schedule({"type": "full", "mode": "logical", "frequency": "PT1H"})
    assert s["type"] == "interval"
    assert s["interval_minutes"] == 60
    assert ps.normalize_schedule({"frequency": "PT15M"})["interval_minutes"] == 15
    assert ps.normalize_schedule({"frequency": "P1D"})["interval_minutes"] == 1440
    assert ps.normalize_schedule({"frequency": "P1W"})["interval_minutes"] == 10080


def test_cron_schedule_passthrough():
    s = ps.normalize_schedule({"type": "cron", "cron_expr": "30 3 * * *"})
    fields = ps.schedule_to_task_fields(s)
    assert fields["schedule_type"] == "cron"
    assert fields["cron_expr"] == "30 3 * * *"
    assert fields["mixed_backup"] == 0


def test_mixed_schedule_splits_incremental_and_full():
    s = ps.normalize_schedule({
        "type": "interval", "interval_minutes": 360,
        "full_mode": "mixed", "full_type": "week", "full_days": [6], "full_time": "02:00",
    })
    f = ps.schedule_to_task_fields(s)
    assert f["mixed_backup"] == 1
    assert f["schedule_type"] == "interval" and f["interval_minutes"] == 360
    assert f["full_schedule_type"] == "week"
    assert f["full_schedule_days"] == "6"


def test_mixed_full_by_cron_generates_week_cron():
    s = ps.normalize_schedule({
        "type": "cron", "cron_expr": "0 */6 * * *",
        "full_mode": "mixed", "full_type": "cron", "full_days": [6], "full_time": "02:00",
    })
    f = ps.schedule_to_task_fields(s)
    # UI 的 0=周一 → cron 的 0=周日；周日 02:00 = "0 2 * * 0"
    assert f["full_schedule_expr"] == "0 2 * * 0"


def test_backup_mode_only_when_explicit():
    assert "backup_mode" not in ps.schedule_to_task_fields({"type": "interval"})
    assert ps.schedule_to_task_fields(
        {"type": "interval", "backup_mode": "physical"})["backup_mode"] == "physical"


def test_retention_mapping():
    """days/count 是表列；lock_days 存在 extra_options，必须分开返回。"""
    pol = {"retention": {"days": 30, "count": 10, "lock_days": 7}}
    assert ps.retention_to_task_fields(pol) == {"retention_days": 30, "retention_count": 10}
    assert ps.policy_extra_fields(pol) == {"retention_lock_days": 7}
    assert ps.retention_to_task_fields({"retention": {"days": 0}}) == {}
    # 显式 0 必须下发（用于清除任务上已有的锁）
    assert ps.policy_extra_fields({"retention": {"lock_days": 0}}) == {"retention_lock_days": 0}
    assert ps.policy_extra_fields({"retention": {}}) == {}


def test_describe_and_next_runs():
    sched = {"type": "interval", "interval_minutes": 360,
             "full_mode": "mixed", "full_days": [6], "full_time": "02:00"}
    assert ps.describe_schedule(sched) == "每 6 小时增量 + 周日 02:00 全量"
    runs = ps.next_fire_times(sched, 3)
    assert len(runs) >= 3, runs
    cron_runs = ps.next_fire_times({"type": "cron", "cron_expr": "0 2 * * *"}, 2)
    assert len(cron_runs) == 2


def test_policy_task_fields_combines_schedule_and_retention():
    pol = {
        "backup_strategy": {"type": "interval", "interval_minutes": 720,
                            "full_mode": "mixed", "full_days": [0], "full_time": "03:00"},
        "retention": {"days": 60, "count": 20},
    }
    f = ps.policy_task_fields(pol)
    assert f["interval_minutes"] == 720 and f["mixed_backup"] == 1
    assert f["full_schedule_days"] == "0"
    assert f["retention_days"] == 60 and f["retention_count"] == 20


def test_disabled_schedule_does_not_touch_task():
    assert ps.schedule_to_task_fields({"enabled": False, "type": "interval"}) == {}
