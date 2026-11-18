# -*- coding: utf-8 -*-
"""仪表盘「正在执行 / 即将执行」回归（2026-11-13）。

用户诉求：仪表盘那块卡片要能看到**正在执行**与**即将执行的计划**，
而不是永远一句"暂无计划任务"。

真机暴露的三个问题，这里逐条锁死：
1. 原实现只读 APScheduler jobs，而平台上多数任务 ``schedule_type=manual``
   不注册调度器 → 永远为空；现在合并「任务自身调度」与「策略托管调度」推算。
2. 调度器里还有 heartbeat / cleanup 等系统维护作业，混进"备份计划"会误导
   → 只保留能映射到真实备份任务的 job。
3. 时间格式两种（``...T22:16:12+08:00`` 与 ``... 22:29:12``），直接用 UTC now
   相减会得到 -28800 秒的负时长 → 统一按本地时间口径计算。
"""
from core import db
from api import system as sys_api


def test_iso_normalizes_space_and_timezone():
    assert sys_api._iso("2026-11-14 10:29").startswith("2026-11-14T10:29")
    # 无时区的时间要补上本地偏移，否则与带时区的时间混排会错序
    assert sys_api._iso("2026-11-14 10:29")[-6:] in ("+08:00", "-05:00", "+00:00")
    assert sys_api._iso("2026-11-14T10:29:00+08:00") == "2026-11-14T10:29:00+08:00"
    assert sys_api._iso(None) == ""


def test_elapsed_sec_accepts_both_formats_and_never_negative():
    import datetime as _dt
    now = _dt.datetime.now()
    for val in (now.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00",
                now.strftime("%Y-%m-%d %H:%M:%S")):
        sec = sys_api._elapsed_sec(val)
        assert sec is not None and 0 <= sec < 120, (val, sec)
    assert sys_api._elapsed_sec("") is None
    assert sys_api._elapsed_sec("not-a-time") is None


def test_live_shape():
    live = sys_api._dashboard_live()
    for k in ("running", "next_runs", "running_count", "rt_running", "generated_at"):
        assert k in live
    assert live["running_count"] == len(live["running"])
    for r in live["next_runs"]:
        assert {"task_id", "name", "next_at", "source"} <= set(r)
        assert r["next_at"], "next_at 不能为空"
    # next_at 必须可排序（ISO 风格，不允许空格分隔）
    assert all(" " not in str(r["next_at"]) for r in live["next_runs"])


def test_system_jobs_are_filtered(monkeypatch):
    """heartbeat / cleanup 等系统作业不得进入"备份计划"。"""
    class _S:
        @staticmethod
        def scheduler_status():
            return {"jobs": [
                {"id": "runtime_heartbeat", "name": "runtime_heartbeat",
                 "next_run": "2026-11-13T23:59:00+08:00"},
                {"id": "backup_cleanup", "name": "backup_cleanup",
                 "next_run": "2026-11-14T03:10:00+08:00"},
                {"id": "backup_task_99999999", "name": "不存在的任务",
                 "next_run": "2026-11-14T04:00:00+08:00"},
            ]}
    monkeypatch.setattr(sys_api, "scheduler", _S)
    live = sys_api._dashboard_live()
    names = [r["name"] for r in live["next_runs"]]
    assert "runtime_heartbeat" not in names
    assert "backup_cleanup" not in names
    assert "不存在的任务" not in names


def test_running_rows_reflect_db_status():
    """DB 里 status='running' 的记录必须出现在 running 段。"""
    tid_row = db.query_one("SELECT id FROM backup_tasks ORDER BY id LIMIT 1")
    if not tid_row:
        import pytest
        pytest.skip("无任务可关联")
    rid = db.execute(
        "INSERT INTO backup_records (task_id, backup_type, status, started_at, "
        "backup_path, size_bytes, message) VALUES (?,?,?,?,?,?,?)",
        (tid_row["id"], "full", "running", db.now_iso(), "", 0, "单元测试"))
    try:
        live = sys_api._dashboard_live()
        assert any(r["record_id"] == rid for r in live["running"]), live["running"]
        rec = [r for r in live["running"] if r["record_id"] == rid][0]
        assert rec["elapsed_sec"] is not None and rec["elapsed_sec"] >= 0
    finally:
        db.execute("DELETE FROM backup_records WHERE id=?", (rid,))
