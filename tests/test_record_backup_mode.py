# -*- coding: utf-8 -*-
"""备份记录「备份方式」列（物理/逻辑/自定义/文件…）单测。

背景（2026-11-20 用户反馈）：备份记录页只有「备份类型」列显示的是**数据库类型**
（MySQL/Oracle…），看不出这条记录到底是物理备份还是逻辑备份。

口径：
- 新记录：创建时把任务生效的备份方式落库（backup_records.backup_mode）；
- 历史记录：该列为空 → 按任务当前配置推断，并标 backup_mode_inferred=True
  （不假装是产物事实，前端 title 会说明）。
"""
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="rec_mode_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

import core.models as models  # noqa: E402
import config  # noqa: E402


def _mk_task(**over):
    task = {
        "name": "t-mode", "db_type": "mysql", "host": "127.0.0.1", "port": 3307,
        "username": "root", "password": "x", "db_name": "demo",
        "backup_type": "full", "schedule_type": "cron",
        "cron_expr": "0 2 * * *", "enabled": 1,
    }
    task.update(over)
    return models.create_task(task)


def test_effective_mode_from_task_config():
    assert models.effective_backup_mode({"backup_mode": "physical"}) == "physical"
    assert models.effective_backup_mode({"backup_mode": "logical"}) == "logical"
    assert models.effective_backup_mode({"backup_mode": "custom"}) == "custom"
    assert models.effective_backup_mode({"backup_mode": "hot_copy"}) == "hot_copy"
    # 大小写/空白容错
    assert models.effective_backup_mode({"backup_mode": " PHYSICAL "}) == "physical"


def test_effective_mode_defaults_by_db_type():
    # 数据库类默认逻辑备份（与 base.BackupEngine.backup_mode 一致）
    assert models.effective_backup_mode({"db_type": "mysql"}) == "logical"
    assert models.effective_backup_mode({"db_type": "oracle"}) == "logical"
    # 非数据库负载没有逻辑/物理之分
    assert models.effective_backup_mode({"db_type": "file"}) == "file"
    assert models.effective_backup_mode({"db_type": "object_storage"}) == "object"
    assert models.effective_backup_mode({"db_type": "vm"}) == "vm"
    assert models.effective_backup_mode({"db_type": "synology"}) == "nas"


def test_mode_display_names_are_complete():
    for mode in ("logical", "physical", "custom", "hot_copy",
                 "file", "object", "vm", "nas"):
        assert mode in config.BACKUP_MODE_DISPLAY_NAMES, mode
        assert config.BACKUP_MODE_DISPLAY_NAMES[mode].strip()


def test_new_record_stores_mode_and_lists_it():
    tid = _mk_task(backup_mode="physical")
    task = models.get_task(tid, include_secret=True)
    rid = models.create_record({
        "task_id": tid, "db_type": "mysql", "backup_type": "full",
        "backup_mode": models.effective_backup_mode(task),
        "started_at": _db.now_iso(), "status": "running",
    })
    rec = models.get_record(rid)
    assert rec["backup_mode"] == "physical"

    row = [r for r in models.list_records(task_id=tid) if r["id"] == rid][0]
    assert row["backup_mode"] == "physical"
    assert row["backup_mode_display"] == "物理备份"
    assert row["backup_mode_inferred"] is False
    # 「数据库类型」与「备份方式」是两列，不能互相顶替
    assert row["db_type_display"] == config.DB_DISPLAY_NAMES["mysql"]


def test_legacy_record_infers_mode_from_task():
    """历史记录 backup_mode 为空 → 按任务当前配置推断并标 inferred。"""
    tid = _mk_task(backup_mode="physical")
    rid = models.create_record({
        "task_id": tid, "db_type": "mysql", "backup_type": "full",
        "started_at": _db.now_iso(), "status": "success",
    })
    assert models.get_record(rid)["backup_mode"] in (None, "")

    row = [r for r in models.list_records(task_id=tid) if r["id"] == rid][0]
    assert row["backup_mode"] == "physical"
    assert row["backup_mode_display"] == "物理备份"
    assert row["backup_mode_inferred"] is True


def test_file_task_record_shows_file_backup():
    tid = _mk_task(name="t-file", db_type="file", db_name="/tmp")
    task = models.get_task(tid, include_secret=True)
    rid = models.create_record({
        "task_id": tid, "db_type": "file", "backup_type": "full",
        "backup_mode": models.effective_backup_mode(task),
        "started_at": _db.now_iso(), "status": "success",
    })
    row = [r for r in models.list_records(task_id=tid) if r["id"] == rid][0]
    assert row["backup_mode"] == "file"
    assert row["backup_mode_display"] == "文件备份"
