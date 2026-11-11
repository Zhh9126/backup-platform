# -*- coding: utf-8 -*-
"""迁移 / 同步（容灾数据底座）P0 回归。

现场问题（2026-11-12 排查结论）：
- 同步：各插件 read_batch 每轮重新 execute 同一条 SQL，而引擎循环内
  从不推进 cfg.incremental_value → 表行数 ≥ batch_size 时无限重读首页、
  重复写、状态卡 running。
- 同步：轮询水位取源表全局 MAX，读取期间新写入被越过 → 静默丢数。
- 同步：binlog 位点零持久化、停止恒定 success、手动 run 无并发护栏。
- 迁移：模块级单例共享 _obj_plan 导致并发串库；run_plan 先读状态再起
  线程，双请求可并发跑两份全量；例程/触发器只生成 DDL 却显示 ✅。
- 权限：migration/db-migrate 的 verify/dry-run 因动作词被判只读豁免。

这里用隔离 Fake 复现真实分页/状态行为，不连生产库。
"""
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.sync.plugins.base import ReadResult  # noqa: E402


class _Conn:
    def cursor(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _cfg(**kw):
    base = dict(
        task_id=999, src_db_type="mysql", tgt_db_type="mysql",
        source_table="t", target_table="t", sync_mode="incremental",
        incremental_column="id", incremental_value="0", batch_size=3,
        save_mode="upsert", error_threshold=0, source_where=None,
        column_mapping=[], tgt_schema="", src_schema="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _PagingReader:
    """按 cfg.incremental_value 切片，模拟插件每轮重新拼 SQL 的行为。"""

    def __init__(self, cfg, rows, stuck=False):
        self.config, self.rows, self.stuck = cfg, rows, stuck
        self.calls = 0

    def connect(self):
        return _Conn()

    def list_columns(self, table):
        return ["id", "v"]

    def read_batch(self, cursor):
        self.calls += 1
        if self.stuck:
            page = self.rows[:self.config.batch_size]
        else:
            lower = int(self.config.incremental_value or 0)
            page = [r for r in self.rows if r[0] > lower][:self.config.batch_size]
        return ReadResult(
            records=[list(r) for r in page], columns=["id", "v"],
            has_more=len(page) == self.config.batch_size,
            next_value=page[-1][0] if page else None)

    def close(self, conn=None, cur=None):
        pass


class _RecordingWriter:
    def __init__(self):
        self.rows = []

    def connect(self):
        return _Conn()

    def prepare_table(self, conn, cols):
        pass

    def write_batch(self, conn, records, columns):
        self.rows.extend([list(r) for r in records])
        return len(records)

    def close(self, conn=None):
        pass


def _engine(cfg, rows, stuck=False, monkeypatch=None):
    from core.sync.engine import SyncEngine
    eng = SyncEngine.__new__(SyncEngine)
    eng.config = cfg
    eng.reader = _PagingReader(cfg, rows, stuck)
    eng.writer = _RecordingWriter()
    eng._disable_constraints = lambda conn: None
    eng._enable_constraints = lambda conn: None
    if monkeypatch:
        import core.models as models
        monkeypatch.setattr(models, "update_sync_task",
                            lambda *a, **kw: None)
    return eng


# --------------------------------------------------------------------------
# 同步：分批翻页
# --------------------------------------------------------------------------

def test_paging_reads_every_row_exactly_once(monkeypatch):
    """10 行 / batch=3：4 批读完，目标端无重复行，位点推进到 10。"""
    cfg = _cfg()
    eng = _engine(cfg, [[i, "v%d" % i] for i in range(1, 11)],
                  monkeypatch=monkeypatch)
    res = eng._run_single_table("t", "t")
    assert res["success"] is True, res
    assert res["total_read"] == 10
    assert [r[0] for r in eng.writer.rows] == list(range(1, 11))
    assert cfg.incremental_value == "10"
    assert eng.reader.calls == 4          # 10 行 / batch=3 → 4 批，末批 has_more=False


def test_paging_guard_aborts_when_position_does_not_advance(monkeypatch):
    """分页位点不推进时明确失败，绝不无限重复写首批。"""
    cfg = _cfg()
    eng = _engine(cfg, [[i, "v%d" % i] for i in range(1, 11)],
                  stuck=True, monkeypatch=monkeypatch)
    res = eng._run_single_table("t", "t")
    assert res["success"] is False
    assert "分批读取未推进" in res["message"]
    assert eng.reader.calls == 2          # 第二轮即中止
    assert len(eng.writer.rows) == cfg.batch_size


def test_value_newer_handles_numeric_and_timestamp():
    from core.sync.engine import _value_newer
    assert _value_newer(10, 9) is True
    assert _value_newer(9, 10) is False       # 不能按字符串误判
    assert _value_newer("2026-11-12 10:00:00", "2026-11-12 09:00:00") is True
    assert _value_newer(None, 1) is False
    assert _value_newer(1, None) is True


def test_commit_failure_is_not_swallowed(monkeypatch):
    """目标端 commit 失败必须报失败，不能显示写入成功却无数据。"""
    cfg = _cfg()
    eng = _engine(cfg, [[1, "a"], [2, "b"]], monkeypatch=monkeypatch)

    def _boom():
        raise RuntimeError("disk full")

    eng.writer.connect = lambda: _ConnNoCommit(_boom)
    res = eng._run_single_table("t", "t")
    assert res["success"] is False
    assert "目标端提交失败" in res["message"]


class _ConnNoCommit(_Conn):
    def __init__(self, boom):
        self._boom = boom

    def commit(self):
        self._boom()


# --------------------------------------------------------------------------
# 同步：API 护栏 / 停止语义 / 权限
# --------------------------------------------------------------------------

def test_run_claim_is_exclusive():
    from api.sync import _claim_run, _release_run
    assert _claim_run(4242) is True
    assert _claim_run(4242) is False       # 重复触发被挡住
    _release_run(4242)
    assert _claim_run(4242) is True
    _release_run(4242)


def test_stop_runner_timeout_reports_failure():
    from core.sync import realtime_runners as rr
    # 没有运行线程时 stop_runner 不得伪造 success=True
    rr.clear_runner(4343)
    res = rr.stop_runner(4343, timeout=0.01)
    assert res.get("success") is False
    assert "超时" in res.get("message", "")


def test_migration_and_sync_write_endpoints_require_permission():
    from api.perm_map import resolve
    cases = [
        ("/api/migration/1/verify", "dr.migration.manage"),
        ("/api/db-migrate/1/dry-run", "dr.migration.manage"),
        ("/api/migration-plans/1/verify", "dr.migration.manage"),
        ("/api/migration-protection-plans/1/dry-run", "dr.migration.manage"),
        ("/api/sync-tasks/1/validate", "dr.sync.manage"),
        ("/api/sync-tasks/1/verify", "dr.sync.manage"),
    ]
    for path, expected in cases:
        perm, reason = resolve(path, "POST")
        assert perm == expected, (path, perm, reason)
        assert reason == "force_check", (path, reason)


def test_flink_config_does_not_leak_password():
    src = open(os.path.join(ROOT, "core/sync/engine.py"),
               encoding="utf-8").read()
    assert "f\"  'password' = '{cfg.src_password}'" not in src
    assert "请在 Flink 侧填入源库口令" in src


# --------------------------------------------------------------------------
# 迁移：原子 CAS / 取消 / 对象诚实化
# --------------------------------------------------------------------------

def test_execute_rowcount_returns_real_affected_rows(tmp_path, monkeypatch):
    import core.db as db
    path = str(tmp_path / "cas.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER, status TEXT)")
    c.execute("INSERT INTO t VALUES (1, 'created')")
    c.commit()
    c.close()
    monkeypatch.setattr(db, "get_conn", lambda: sqlite3.connect(path))
    sql = ("UPDATE t SET status='checking' WHERE id=1 "
           "AND status='created'")
    assert db.execute_rowcount(sql) == 1
    assert db.execute_rowcount(sql) == 0   # 第二次 CAS 抢不到


def test_run_plan_cas_rejects_concurrent_trigger(monkeypatch):
    from core import db_migrate as dm
    eng = dm.DbMigrationEngine()
    monkeypatch.setattr(eng, "get_plan", lambda pid, hide_secret=False: {
        "id": pid, "status": "created", "phases_json": {}})
    monkeypatch.setattr(dm.db, "execute_rowcount",
                        lambda *a, **kw: 0)
    out = eng.run_plan(8181)
    assert out["ok"] is False
    assert "正在执行" in out["message"]


def test_run_plan_uses_isolated_engine_per_plan(monkeypatch):
    """每个计划独立引擎实例，避免 _obj_plan 并发串库。"""
    from core import db_migrate as dm
    eng = dm.DbMigrationEngine()
    old = {"id": 8182, "status": "failed",
           "phases_json": {"migrate": {"manifest": [{"table": "t"}]}}}
    monkeypatch.setattr(eng, "get_plan", lambda pid, hide_secret=False: old)
    monkeypatch.setattr(dm.db, "execute_rowcount", lambda *a, **kw: 1)
    captured = {}

    class _Thread:
        def __init__(self, target=None, args=(), kwargs=None, **kw):
            captured["target_self"] = getattr(target, "__self__", None)
            captured["kwargs"] = kwargs or {}

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(dm.threading, "Thread", _Thread)
    out = eng.run_plan(8182)
    assert out["ok"] is True
    assert captured.get("started") is True
    assert captured["target_self"] is not eng          # 独立实例
    assert captured["kwargs"]["prev_manifest"] == [{"table": "t"}]


def test_cancel_helpers():
    from core import db_migrate as dm
    dm.clear_cancel(9191)
    assert dm.is_cancelled(9191) is False
    dm.request_cancel(9191)
    assert dm.is_cancelled(9191) is True
    dm.clear_cancel(9191)
    assert dm.is_cancelled(9191) is False


def test_objects_are_exported_not_reported_as_migrated(monkeypatch):
    """例程/触发器只生成 DDL、从未执行，不能再显示 ✅ 已迁移。"""
    from core import db_migrate as dm
    eng = dm.DbMigrationEngine()
    plan = {
        "id": 9192, "src_db_type": "mysql", "tgt_db_type": "mysql",
        "src_db_name": "src", "tgt_db_name": "tgt",
        "extra_options": {"migrate_objects": {
            "routines": True, "triggers": True}},
    }
    monkeypatch.setattr(eng, "_obj_conn", lambda p, side: _Conn())
    monkeypatch.setattr(eng, "_mig_routines",
                        lambda *a: [{"object": "f1", "kind": "function",
                                     "sql": "CREATE FUNCTION f1..."}])
    monkeypatch.setattr(eng, "_mig_triggers",
                        lambda *a: [{"object": "tr1", "kind": "trigger",
                                     "sql": "CREATE TRIGGER tr1..."}])
    monkeypatch.setattr(dm.db, "add_log", lambda *a, **kw: None)
    out = eng._phase_objects(plan)
    assert out["routines"] == []
    assert out["triggers"] == []
    assert len(out["routines_export"]) == 2
    assert "未在目标端执行" in out["note"]


def test_api_contract_for_cancel_and_source_guards():
    src = open(os.path.join(ROOT, "api/migration.py"),
               encoding="utf-8").read()
    assert '/db-migrate/<int:plan_id>/cancel' in src
    runner = open(os.path.join(ROOT, "core/sync/realtime_runners.py"),
                  encoding="utf-8").read()
    assert '"success": False, "timeout": True' in runner
    sync_api = open(os.path.join(ROOT, "api/sync.py"),
                    encoding="utf-8").read()
    assert "_claim_run" in sync_api and "_release_run" in sync_api
    assert "sync task %s crashed" in sync_api
    engine_src = open(os.path.join(ROOT, "core/sync/engine.py"),
                      encoding="utf-8").read()
    assert "cfg.incremental_value = str(result.next_value)" in engine_src
    assert "'col' == \"binlog\"" not in engine_src   # 不是死字符串
    assert 'saved or {}).get("col") == "binlog"' in engine_src
    assert "batch_max" in engine_src
