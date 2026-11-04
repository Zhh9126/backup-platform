# -*- coding: utf-8 -*-
"""P2 池自测（2026-11）。

覆盖：
- 时间段限速：rate_limit_window 窗口内限速 / 窗口外全速 / 跨零点环绕 /
  窗口非法退化 / 带宽=0 不受影响
- 报表中心聚合：统计字段齐全、口径正确（空库也能出结构化报告）
"""
import os
import sys
import tempfile
from datetime import datetime

_TMP = tempfile.mkdtemp(prefix="p2_pool_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.environ["SCHEDULER_ENABLED"] = "0"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()                                # noqa: E402

from core.engines.base import BackupEngine      # noqa: E402
from core.report_center import build_summary    # noqa: E402


class _MinEngine(BackupEngine):
    """最小化引擎（只测限速判断，不触发任何 IO）。"""


def _eng(bw=1024, extra=None):
    return _MinEngine({"id": 0, "name": "t", "bandwidth_limit": bw,
                       "extra_options": extra}, _TMP)


def test_bandwidth_no_window_full_time():
    # 未配置窗口：全程限速
    assert _eng().bandwidth_limit == 1024


def test_bandwidth_zero_untouched():
    assert _eng(bw=0, extra={"rate_limit_window": {"start": "00:00", "end": "23:59"}}).bandwidth_limit == 0


def test_bandwidth_in_window():
    # 窗口覆盖当前时刻 → 限速生效
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    # start=cur, end=cur+30（防跨零点回绕）
    s = cur
    e = (cur + 30) % (24 * 60)
    win = {"start": f"{s // 60:02d}:{s % 60:02d}", "end": f"{e // 60:02d}:{e % 60:02d}"}
    assert _eng(extra={"rate_limit_window": win}).bandwidth_limit == 1024


def test_bandwidth_out_of_window():
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    # 窗口 = (cur-20, cur-5)，当前时刻在窗口外
    s = (cur - 20) % (24 * 60)
    e = (cur - 5) % (24 * 60)
    win = {"start": f"{s // 60:02d}:{s % 60:02d}", "end": f"{e // 60:02d}:{e % 60:02d}"}
    if s != e:  # 避免窗口退化为空时语义歧义
        eng = _eng(extra={"rate_limit_window": win})
        assert eng.bandwidth_limit == 0


def test_bandwidth_invalid_window_falls_back():
    assert _eng(extra={"rate_limit_window": {"start": "abc", "end": "!!!"}}).bandwidth_limit == 1024
    assert _eng(extra={"rate_limit_window": "garbage"}).bandwidth_limit == 1024
    assert _eng(extra={"rate_limit_window": {}}).bandwidth_limit == 1024


# ---------------------------------------------------------------------------
# 报表中心
# ---------------------------------------------------------------------------
def test_report_summary_structure_on_empty_db():
    s = build_summary(days=7)
    # 结构齐全（空库也应返回可渲染的结构化数据）
    for k in ("window_days", "backup", "restore", "verify", "by_db_type",
              "top_failed_tasks", "generated_at"):
        assert k in s, f"缺字段 {k}"
    assert isinstance(s["by_db_type"], list)
    assert isinstance(s["top_failed_tasks"], list)
    assert "total" in s["backup"] and "success" in s["backup"]


def test_report_summary_counts_records():
    now = db.now_iso()
    db.execute(
        "INSERT INTO backup_records (task_id, db_type, backup_type,"
        " status, size_bytes, started_at) VALUES (?,?,?,?,?,?)",
        (9001, "mysql", "FULL", "success", 1024 * 1024, now))
    db.execute(
        "INSERT INTO backup_records (task_id, db_type, backup_type,"
        " status, size_bytes, started_at, message) VALUES (?,?,?,?,?,?,?)",
        (9002, "postgresql", "FULL", "failed", 0, now, "boom"))
    db.execute(
        "INSERT INTO backup_records (task_id, db_type, backup_type,"
        " status, size_bytes, started_at) VALUES (?,?,?,?,?,?)",
        (9002, "postgresql", "FULL", "failed", 0, now))
    s = build_summary(days=7)
    assert s["backup"]["total"] >= 3
    assert s["backup"]["failed"] >= 2
    assert s["backup"]["success"] >= 1
    # 按库型聚合含 mysql 与 postgresql
    types = {r.get("db_type"): r for r in s["by_db_type"]}
    assert "mysql" in types and "postgresql" in types
    # 失败任务 Top 含 9002
    assert any(t.get("task_id") == 9002 for t in s["top_failed_tasks"])
