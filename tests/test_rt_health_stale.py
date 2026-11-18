# -*- coding: utf-8 -*-
"""实时保护健康灯回归（2026-11-14）。

修复点：从未产出恢复点（last_rp_at 为空）的任务此前一律只判到 yellow（顶格），
实测有任务 106 天没有任何恢复点仍显示黄色，掩盖了「捕获在跑却从未入账」的故障。
现在按 RPO 与 target 的倍数关系给出 green / yellow / red。
"""
from core.rt_backup.types import RtHealth


def _mk(**kw):
    base = dict(task_id=1, task_name="t", capture_kind="db-log", engine="mysql",
                daemon_status="running", rpo_target_sec=300)
    base.update(kw)
    return RtHealth(**base)


def test_no_recovery_point_fresh_is_green():
    """刚起步：尚未产出恢复点但 RPO 未超阈值 → 绿。"""
    h = _mk(rpo_actual_sec=100, last_rp_at="")
    assert h.compute_health() == "green"


def test_no_recovery_point_over_target_is_yellow():
    h = _mk(rpo_actual_sec=500, last_rp_at="")
    assert h.compute_health() == "yellow"


def test_no_recovery_point_way_over_is_red():
    """核心修复：从未入账且 RPO 远超 2×target 必须判红，不能只标黄。"""
    h = _mk(rpo_actual_sec=9164269, last_rp_at="")
    assert h.compute_health() == "red"


def test_with_recovery_point_thresholds():
    assert _mk(rpo_actual_sec=100, last_rp_at="2026-11-14 10:00:00").compute_health() == "green"
    assert _mk(rpo_actual_sec=500, last_rp_at="2026-11-14 10:00:00").compute_health() == "yellow"
    assert _mk(rpo_actual_sec=1000, last_rp_at="2026-11-14 10:00:00").compute_health() == "red"


def test_failed_daemon_is_red_regardless():
    h = _mk(daemon_status="failed", rpo_actual_sec=0, last_rp_at="2026-11-14 10:00:00")
    assert h.compute_health() == "red"


def test_stopped_daemon_is_unknown():
    h = _mk(daemon_status="stopped", rpo_actual_sec=0)
    assert h.compute_health() == "unknown"
