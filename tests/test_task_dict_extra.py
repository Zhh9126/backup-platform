# -*- coding: utf-8 -*-
"""回归：POST/PUT /api/tasks 传 dict 型 extra_options 不再 500（dict→JSON 归一）。"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="task_dict_extra_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ.setdefault("LD_LIBRARY_PATH",
                      os.path.join(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__))), "lib"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

import pytest  # noqa: E402
import core.models as models  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_shared_tasks():
    """隔离共享元库残留：本文件只验证 dict→JSON 归一，不关心存量任务数。

    config.META_DB_PATH 是运行时读的模块属性，pytest 同进程收集多个测试模块
    时后导入者的 env 不会改写已固化的 config；其它模块留下的备份任务会触发
    社区版 5 个配额上限，导致这里无故 QuotaExceededError。
    """
    try:
        _db.execute("DELETE FROM backup_tasks")
    except Exception:
        pass
    yield


def test_create_task_with_dict_extra_options():
    tid = models.create_task({
        "name": "t-dict-eo",
        "db_type": "mysql",
        "host": "127.0.0.1",
        "port": 3307,
        "username": "root",
        "password": "x",
        "db_name": "demo",
        "backup_type": "logical",
        "schedule_type": "cron",
        "cron_expr": "0 2 * * *",
        "extra_options": {"backup_dir": "/tmp/x", "env_vars": {"A": "1"}},
    })
    t = models.get_task(tid, include_secret=True)
    assert isinstance(t["extra_options"], str)
    assert "backup_dir" in t["extra_options"]


def test_update_task_with_dict_extra_options():
    tid = models.create_task({
        "name": "t-dict-eo2", "db_type": "mysql", "host": "127.0.0.1",
        "backup_type": "logical", "schedule_type": "cron",
    })
    ok = models.update_task(tid, {"extra_options": {"retention_lock_days": 3}})
    assert ok is not False
    t = models.get_task(tid, include_secret=True)
    assert isinstance(t["extra_options"], str)
    assert "retention_lock_days" in t["extra_options"]
