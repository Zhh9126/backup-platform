# -*- coding: utf-8 -*-
"""迁移计划可靠性自测（对标 influxdb2tsfile 取长补短，2026-10）。

覆盖：
- 断点续跑：manifest → skip_tables 提取；失败表清表决策
- manifest 构建：engine 结果 → 逐表状态/行数/错误明细；跳过表保留上次记录
- verify manifest 对照：行数映射大小写归一
- 全库迁移 skip_tables：真实 SyncEngine 循环跳过已成功表
- dry-run API 契约：路由注册 + 计划不存在 404

与 test_sync_reliability.py 同款隔离：元数据库指向临时目录。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mig_manifest_")
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

from core.db_migrate import DbMigrationEngine   # noqa: E402

ENG = DbMigrationEngine()


# ---------------------------------------------------------------------------
# 1. manifest 构建（migrate 阶段产物）
# ---------------------------------------------------------------------------
def _fake_engine_res(tables, skipped=None, success=True):
    return {
        "success": success, "message": "ok",
        "total_read": sum(t.get("total_read", 0) for t in tables),
        "total_write": sum(t.get("total_write", 0) for t in tables),
        "errors": 0, "tables": tables,
        "failed_tables": [t["table"] for t in tables if not t.get("success")],
        "skipped_tables": skipped or [],
    }


def test_manifest_built_from_engine_result(monkeypatch):
    import core.db_migrate as dm
    plan = {"id": 1, "name": "t", "src_db_type": "mysql", "src_host": "127.0.0.1",
            "src_port": 3306, "src_username": "u", "src_password": "p",
            "src_db_name": "s", "tgt_db_type": "postgresql", "tgt_host": "127.0.0.1",
            "tgt_port": 5432, "tgt_username": "u", "tgt_password": "p",
            "tgt_db_name": "t"}
    res = _fake_engine_res([
        {"table": "orders", "success": True, "total_read": 10, "total_write": 10,
         "errors": 0},
        {"table": "users", "success": False, "total_read": 5, "total_write": 3,
         "errors": 2, "error_details": ["[users] too long"]},
    ])
    # 直接测 manifest 逻辑：monkeypatch 引擎层入口
    import core.sync.engine as eng_mod
    monkeypatch.setattr(eng_mod, "run_sync_task_with_task",
                        lambda task, progress_callback=None: res)
    out = ENG._phase_migrate(plan, with_structure=False)
    mf = {m["source_table"]: m for m in out["manifest"]}
    assert mf["orders"]["status"] == "ok"
    assert mf["orders"]["rows_written"] == 10
    assert mf["users"]["status"] == "failed"
    assert mf["users"]["rows_written"] == 3
    assert mf["users"]["error_detail"] == ["[users] too long"]
    assert out["resume"] is False
    # synthetic 任务应带 skip_tables 字段（即使为空）
    assert out["skipped_tables"] == []


def test_resume_skips_previous_ok_and_keeps_records(monkeypatch):
    import core.sync.engine as eng_mod
    plan = {"id": 2, "name": "t2", "src_db_type": "mysql", "src_host": "127.0.0.1",
            "src_port": 3306, "src_username": "u", "src_password": "p",
            "src_db_name": "s", "tgt_db_type": "mysql", "tgt_host": "127.0.0.1",
            "tgt_port": 3306, "tgt_username": "u", "tgt_password": "p",
            "tgt_db_name": "t"}
    prev = [
        {"source_table": "orders", "status": "ok", "rows_written": 10},
        {"source_table": "users", "status": "failed", "rows_written": 3},
    ]
    captured = {}

    def fake_run(task, progress_callback=None):
        captured["skip_tables"] = task.get("skip_tables")
        return _fake_engine_res([
            {"table": "users", "success": True, "total_read": 5,
             "total_write": 5, "errors": 0}])
    monkeypatch.setattr(eng_mod, "run_sync_task_with_task", fake_run)
    out = ENG._phase_migrate(plan, with_structure=False, prev_manifest=prev)
    # orders 上次成功 → 跳过；users 失败 → 重迁
    assert captured["skip_tables"] == ["orders"]
    assert out["resume"] is True
    mf = {m["source_table"]: m for m in out["manifest"]}
    # users 重新迁成功
    assert mf["users"]["status"] == "ok" and mf["users"]["rows_written"] == 5
    # orders 保留上次成功记录（resumed 标记）
    assert mf["orders"]["status"] == "ok" and mf["orders"]["resumed"] is True


def test_clear_target_table_unsupported_type_honest():
    # 不支持自动清表的类型 → 如实返回原因（不静默成功，杜绝部分行重复）
    plan = {"tgt_db_type": "sqlserver", "tgt_host": "127.0.0.1", "tgt_port": 1433,
            "tgt_username": "u", "tgt_password": "p", "tgt_db_name": "t"}
    ok, msg = ENG._clear_target_table(plan, "orders")
    assert ok is False and "不支持" in msg


# ---------------------------------------------------------------------------
# 2. verify manifest 对照
# ---------------------------------------------------------------------------
def test_manifest_rows_map_case_insensitive():
    manifest = [
        {"source_table": "Orders", "status": "ok", "rows_written": 42},
        {"source_table": "users", "status": "failed", "rows_written": 3},
        {"source_table": "items", "status": "ok", "rows_written": 0},
    ]
    m = ENG._manifest_rows_map(manifest)
    assert m["orders"] == 42
    assert "users" not in m          # 失败表不参与对照
    assert m["items"] == 0


# ---------------------------------------------------------------------------
# 3. 全库迁移 skip_tables（真实 SyncEngine 循环）
# ---------------------------------------------------------------------------
def test_full_migration_skips_tables(monkeypatch):
    from core.sync.engine import SyncEngine
    from core.sync.plugins.base import SyncConfig
    cfg = SyncConfig(task_id=1, src_db_type="mysql", tgt_db_type="postgresql",
                     full_db_migrate=True, skip_tables=["t1", "t3"])
    eng = SyncEngine(cfg)
    monkeypatch.setattr(eng, "_get_tables_to_sync",
                        lambda: ["t1", "t2", "t3", "t4"])
    calls = []

    def fake_single(src, dst, cb=None):
        calls.append(src)
        return {"success": True, "total_read": 1, "total_write": 1, "errors": 0}
    monkeypatch.setattr(eng, "_run_single_table", fake_single)
    res = eng._run_full_migration()
    # 只执行 t2/t4，跳过 t1/t3（断点续跑语义）
    assert calls == ["t2", "t4"]
    assert res["skipped_tables"] == ["t1", "t3"]
    assert res["success"] is True


# ---------------------------------------------------------------------------
# 4. dry-run API 契约
# ---------------------------------------------------------------------------
def test_dry_run_route_registered():
    import app as app_mod
    rules = {r.rule for r in app_mod.app.url_map.iter_rules()}
    assert "/api/migration-plans/<int:plan_id>/dry-run" in rules
    assert "/api/db-migrate/<int:plan_id>/dry-run" in rules


def test_dry_run_plan_not_found():
    try:
        ENG.dry_run(999999)
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert "不存在" in str(e)
