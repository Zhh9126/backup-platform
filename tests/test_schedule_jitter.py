# -*- coding: utf-8 -*-
"""确定性调度抖动单测（对标 PB 级方案 §5.1）。

语义要求：
  1. 默认关闭 → 触发时刻与用户配置完全一致（零行为变化）；
  2. 开启后偏移 = CRC32(任务标识) % 窗口，**确定性**（重启后同一任务落同一槽）；
  3. 只对「分钟位是单一数字」的 cron 生效；通配/列表/范围/跨小时保持原样；
  4. 调度注册与「下次备份时间」展示用同一套偏移，避免界面与实际不一致。
"""
import pytest

from core import scheduler


@pytest.fixture
def cfg(monkeypatch):
    """可控的 system_config 读实现。"""
    store = {}

    def _get(key, default=None):
        return store.get(key, default)

    monkeypatch.setattr(scheduler.db, "get_system_config", _get)
    return store


def test_jitter_disabled_by_default(cfg):
    assert scheduler._schedule_jitter_minutes({"id": 1, "name": "t"}) == 0


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE"])
def test_jitter_enabled_flags(cfg, flag):
    cfg["schedule_jitter_enabled"] = flag
    cfg["schedule_jitter_seconds"] = 300
    assert scheduler._schedule_jitter_minutes({"id": 7, "name": "ord"}) >= 0


def test_jitter_window_bounds(cfg):
    cfg["schedule_jitter_enabled"] = "1"
    cfg["schedule_jitter_seconds"] = 300      # 5 个分钟槽
    vals = [scheduler._schedule_jitter_minutes({"id": i, "name": "t%d" % i})
            for i in range(200)]
    assert max(vals) <= 5
    assert len(set(vals)) >= 4                # 确实被打散，不是恒 0


def test_jitter_is_deterministic(cfg):
    cfg["schedule_jitter_enabled"] = "1"
    cfg["schedule_jitter_seconds"] = 300
    task = {"id": 42, "name": "财务库"}
    assert (scheduler._schedule_jitter_minutes(task)
            == scheduler._schedule_jitter_minutes(dict(task)))


def test_zero_window_means_no_jitter(cfg):
    cfg["schedule_jitter_enabled"] = "1"
    cfg["schedule_jitter_seconds"] = 0
    assert scheduler._schedule_jitter_minutes({"id": 1, "name": "t"}) == 0


def test_apply_jitter_simple_cron():
    assert scheduler._apply_jitter_to_cron("0 2 * * *", 3) == "3 2 * * *"
    assert scheduler._apply_jitter_to_cron("0 2 * * *", 0) == "0 2 * * *"


@pytest.mark.parametrize("expr", [
    "*/5 * * * *",      # 通配步长：加了会改变触发次数
    "0,30 * * * *",     # 列表
    "0-10 * * * *",     # 范围
    "58 * * * *",       # 跨小时（58+3 > 59）→ 不挤到下一小时
])
def test_apply_jitter_keeps_special_expr(expr):
    assert scheduler._apply_jitter_to_cron(expr, 3) == expr


def test_make_trigger_applies_jitter(cfg):
    from apscheduler.triggers.cron import CronTrigger
    cfg["schedule_jitter_enabled"] = "1"
    cfg["schedule_jitter_seconds"] = 300
    task = {"id": 9, "name": "x", "schedule_type": "cron", "cron_expr": "0 3 * * *"}
    jit = scheduler._schedule_jitter_minutes(task)
    trig = scheduler._make_trigger(task, jitter_minutes=jit)
    assert isinstance(trig, CronTrigger)
    _m = CronTrigger.FIELD_NAMES.index("minute")
    assert str(trig.fields[_m]) == str(jit)    # 分钟字段被偏移到 jit


def test_make_trigger_untouched_when_disabled(cfg):
    from apscheduler.triggers.cron import CronTrigger
    task = {"id": 9, "name": "x", "schedule_type": "cron", "cron_expr": "0 3 * * *"}
    trig = scheduler._make_trigger(task, jitter_minutes=scheduler._schedule_jitter_minutes(task))
    _m = CronTrigger.FIELD_NAMES.index("minute")
    assert str(trig.fields[_m]) == "0"
