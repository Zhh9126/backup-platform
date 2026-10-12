# -*- coding: utf-8 -*-
"""MySQL→达梦 DM8 信创迁移兼容性自测（对标"7 个致命暗坑"文章，2026-10）。

覆盖：
- 应用 SQL 兼容清单（坑三 CONCAT NULL / 坑四函数对照 / 坑六分页自增 /
  坑七配套：模式映射、隔离级别、统计信息、连接串）
- 保留字交叉比对（坑五）：表名 + 源端列名
- 迁移后统计信息收集 SP_TAB_STAT_INIT（坑七配套）
- run_compat_advisory 达梦目标集成（探测全失败也必须给出清单——清单是
  静态知识，不依赖探测成功）

与 test_sync_reliability.py 同款隔离：元数据库指向临时目录。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="dm_compat_")
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

from core.sync.compat_advisor import (          # noqa: E402
    app_sql_checklist_dm8, dm_reserved_word_hits, run_compat_advisory,
)
from core.sync.plugins.base import SyncConfig   # noqa: E402
from core.db_migrate import DbMigrationEngine   # noqa: E402

ENG = DbMigrationEngine()


class _DeadCursor:
    """所有 execute 都抛错的游标：模拟探测全部失败（老版本/权限不足）。"""

    def execute(self, *a, **k):
        raise RuntimeError("simulated probe failure")

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _DeadConn:
    def cursor(self):
        return _DeadCursor()

    def close(self):
        pass


def _cfg(src="mysql", tgt="dameng") -> SyncConfig:
    return SyncConfig(task_id=1, src_db_type=src, tgt_db_type=tgt)


# ---------------------------------------------------------------------------
# 1. 应用 SQL 兼容清单（静态知识，坑三/四/六/七配套）
# ---------------------------------------------------------------------------
def test_checklist_covers_pits_for_mysql_source():
    items = app_sql_checklist_dm8(_cfg())
    codes = {i["code"] for i in items}
    assert len(items) == 6
    assert all(i["level"] == "info" for i in items)
    # 坑三：CONCAT NULL + 双引号字符串
    assert "app_sql_concat_null" in codes
    # 坑四：函数对照（IFNULL/NVL、LISTAGG、TO_CHAR 等）
    assert "app_sql_functions" in codes
    # 坑六 + 除零 + FORCE INDEX
    assert "app_sql_division_paging" in codes
    assert "app_sql_identity_index" in codes
    # 坑七配套：模式映射规范 + 隔离级别
    assert "app_sql_schema_session" in codes
    # 坑七配套：统计信息 + 连接串
    assert "app_sql_stats_conn" in codes


def test_checklist_skipped_for_non_mysql_source():
    # 源为 Oracle/PG 时不输出 MySQL 习惯的改造清单
    assert app_sql_checklist_dm8(_cfg(src="oracle")) == []
    assert app_sql_checklist_dm8(_cfg(src="postgresql")) == []


def test_checklist_survives_total_probe_failure():
    # 探测全部失败（连 V$RESERVED_WORDS 都不可读）时清单必须照常给出：
    # 清单是静态知识，不依赖目标库探测成功——这正是"迁移工具不报错"的
    # 问题的兜底提示
    out = run_compat_advisory(_cfg(), _DeadConn(), tables=["orders"])
    codes = {i["code"] for i in out}
    assert "app_sql_concat_null" in codes
    assert "app_sql_functions" in codes


# ---------------------------------------------------------------------------
# 2. 保留字交叉比对（坑五）
# ---------------------------------------------------------------------------
def test_reserved_word_hits_table_and_columns():
    # _DeadConn → 退回兜底清单；ORDER/STATUS/USER/LEVEL 等在其中
    hits = dm_reserved_word_hits(_DeadConn(),
                                 ["orders", "ORDER", "user_info",
                                  "status", "section"])
    assert hits == {"ORDER", "STATUS", "SECTION"}
    # 普通列名不误报
    assert dm_reserved_word_hits(_DeadConn(), ["amount", "created_at"]) == set()


# ---------------------------------------------------------------------------
# 3. 迁移后统计信息收集（坑七配套）
# ---------------------------------------------------------------------------
class _StatCursor:
    calls = []

    def execute(self, sql, *a, **k):
        _StatCursor.calls.append(sql)

    def close(self):
        pass


class _StatConn:
    def cursor(self):
        return _StatCursor()

    def close(self):
        pass


def test_collect_dm_stats_calls_per_table(monkeypatch):
    import core.db_migrate as dm
    _StatCursor.calls = []
    plan = {"tgt_host": "127.0.0.1", "tgt_port": 5236,
            "tgt_username": "BIZUSER", "tgt_password": "p",
            "tgt_db_name": "BIZDB"}
    monkeypatch.setattr(dm, "_stats_connect",
                        lambda *a, **k: _StatConn())
    res = ENG._collect_dm_table_stats(plan, ["orders", "order_items"])
    assert res["executed"] is True
    assert res["schema"] == "BIZUSER"
    assert res["collected"] == ["ORDERS", "ORDER_ITEMS"]
    assert res["failed"] == []
    assert _StatCursor.calls == [
        "CALL SP_TAB_STAT_INIT('BIZUSER', 'ORDERS')",
        "CALL SP_TAB_STAT_INIT('BIZUSER', 'ORDER_ITEMS')",
    ]


def test_collect_dm_stats_single_table_failure_not_blocking(monkeypatch):
    import core.db_migrate as dm

    class _HalfCursor:
        n = 0

        def execute(self, sql, *a, **k):
            _HalfCursor.n += 1
            if _HalfCursor.n == 1:
                raise RuntimeError("lock timeout")

        def close(self):
            pass

    class _HalfConn:
        def cursor(self):
            return _HalfCursor()

        def close(self):
            pass

    plan = {"tgt_host": "h", "tgt_port": 5236, "tgt_username": "u",
            "tgt_password": "p", "tgt_db_name": "d"}
    monkeypatch.setattr(dm, "_stats_connect", lambda *a, **k: _HalfConn())
    res = ENG._collect_dm_table_stats(plan, ["t1", "t2"])
    # 单表失败不阻断：t1 进 failed，t2 照常收集
    assert res["collected"] == ["T2"]
    assert len(res["failed"]) == 1 and res["failed"][0]["table"] == "T1"


def test_collect_dm_stats_no_schema_or_tables():
    res = ENG._collect_dm_table_stats({"tgt_username": ""}, ["t"])
    assert res["executed"] is False
    res2 = ENG._collect_dm_table_stats({"tgt_username": "u"}, [])
    assert res2["executed"] is False


# ---------------------------------------------------------------------------
# 4. 迁移阶段集成：达梦目标成功后 result 带 stats_init
# ---------------------------------------------------------------------------
def test_phase_migrate_includes_stats_init_for_dm(monkeypatch):
    import core.sync.engine as eng_mod
    plan = {"id": 3, "name": "t", "src_db_type": "mysql", "src_host": "127.0.0.1",
            "src_port": 3306, "src_username": "u", "src_password": "p",
            "src_db_name": "s", "tgt_db_type": "dameng", "tgt_host": "127.0.0.1",
            "tgt_port": 5236, "tgt_username": "BIZ", "tgt_password": "p",
            "tgt_db_name": "BIZ"}
    res = {"success": True, "message": "ok", "total_read": 1, "total_write": 1,
           "errors": 0,
           "tables": [{"table": "t1", "success": True, "total_read": 1,
                       "total_write": 1, "errors": 0}]}
    monkeypatch.setattr(eng_mod, "run_sync_task_with_task",
                        lambda task, progress_callback=None: res)
    monkeypatch.setattr(ENG, "_collect_dm_table_stats",
                        lambda p, ts: {"executed": True, "collected": ["T1"],
                                       "failed": []})
    out = ENG._phase_migrate(plan, with_structure=True)
    assert out["stats_init"] == {"executed": True, "collected": ["T1"],
                                 "failed": []}


def test_phase_migrate_skips_stats_for_non_dm(monkeypatch):
    import core.sync.engine as eng_mod
    plan = {"id": 4, "name": "t", "src_db_type": "mysql", "src_host": "127.0.0.1",
            "src_port": 3306, "src_username": "u", "src_password": "p",
            "src_db_name": "s", "tgt_db_type": "postgresql",
            "tgt_host": "127.0.0.1", "tgt_port": 5432, "tgt_username": "u",
            "tgt_password": "p", "tgt_db_name": "t"}
    res = {"success": True, "message": "ok", "total_read": 1, "total_write": 1,
           "errors": 0,
           "tables": [{"table": "t1", "success": True, "total_read": 1,
                       "total_write": 1, "errors": 0}]}
    monkeypatch.setattr(eng_mod, "run_sync_task_with_task",
                        lambda task, progress_callback=None: res)
    out = ENG._phase_migrate(plan, with_structure=True)
    assert out["stats_init"] is None
