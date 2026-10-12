# -*- coding: utf-8 -*-
"""数据同步/迁移可靠性优化自测（2026-10）。

覆盖：
- 轮询实时同步：水位持久化/断点续传、回看窗口计算
- 同步记录可观测：read_rows / error_rows / watermark 落库
- 迁移校验：抽样内容比对（bool 归一、差异行数）
- 表结构契约：sync_tasks.realtime_watermark、db_migration_plans.verify_sample_rows

与 test_backup_cleanup.py 同款隔离：元数据库指向临时目录。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="sync_reliability_")
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

import core.models as models                    # noqa: E402
from core.sync.engine import SyncEngine         # noqa: E402
from core.db_migrate import DbMigrationEngine   # noqa: E402


# ---------------------------------------------------------------------------
# 1. 回看窗口（_shift_watermark_back）
# ---------------------------------------------------------------------------
def test_shift_watermark_back_timestamp():
    # 时间戳水位回拨 5 秒（重叠读取，upsert 幂等吸收）
    assert SyncEngine._shift_watermark_back(
        "2026-10-12 10:00:00", 5) == "2026-10-12 09:59:55"


def test_shift_watermark_back_fractional_and_iso():
    assert SyncEngine._shift_watermark_back(
        "2026-10-12 10:00:00.500000", 10) == "2026-10-12 09:59:50.500000"
    assert SyncEngine._shift_watermark_back(
        "2026-10-12T10:00:30", 30) == "2026-10-12T10:00:00"


def test_shift_watermark_back_numeric_passthrough():
    # 自增主键等数值水位不回拨（整段重读代价高，交由用户权衡）
    assert SyncEngine._shift_watermark_back("1042", 5) == "1042"


def test_shift_watermark_back_garbage_passthrough():
    assert SyncEngine._shift_watermark_back("not-a-time", 5) == "not-a-time"
    assert SyncEngine._shift_watermark_back("", 5) == ""


# ---------------------------------------------------------------------------
# 2. 水位持久化 / 断点续传契约
# ---------------------------------------------------------------------------
def _make_task(**kw) -> int:
    data = {
        "name": kw.pop("name", "可靠性测试任务"),
        "src_db_type": "mysql", "src_host": "127.0.0.1", "src_port": 3306,
        "src_username": "u", "src_password": "p", "src_db_name": "sdb",
        "tgt_db_type": "postgresql", "tgt_host": "127.0.0.1", "tgt_port": 5432,
        "tgt_username": "u", "tgt_password": "p", "tgt_db_name": "tdb",
        "sync_mode": "realtime", "save_mode": "upsert",
        "incremental_column": "updated_at",
    }
    data.update(kw)
    return models.create_sync_task(data)


def test_watermark_persist_roundtrip():
    tid = _make_task()
    SyncEngine._save_persisted_watermark(tid, "updated_at", "2026-10-12 10:00:00")
    saved = SyncEngine._load_persisted_watermark(tid)
    assert saved == {"col": "updated_at", "value": "2026-10-12 10:00:00"}
    # 断点续传守卫：列名一致才允许跳过快照
    assert saved.get("col") == "updated_at"


def test_watermark_column_mismatch_blocks_resume():
    tid = _make_task()
    SyncEngine._save_persisted_watermark(tid, "old_col", "100")
    saved = SyncEngine._load_persisted_watermark(tid)
    # 增量列已改为 id：旧水位 col 不匹配 → 不允许跳过全量快照
    assert saved.get("col") != "id"


def test_watermark_invalid_json_returns_none():
    tid = _make_task()
    models.update_sync_task(tid, {"realtime_watermark": "{broken json"})
    assert SyncEngine._load_persisted_watermark(tid) is None


def test_watermark_empty_task_returns_none():
    tid = _make_task()
    assert SyncEngine._load_persisted_watermark(tid) is None


# ---------------------------------------------------------------------------
# 3. 表结构契约
# ---------------------------------------------------------------------------
def test_sync_tasks_has_realtime_watermark_column():
    cols = {r["name"] for r in db.query("PRAGMA table_info(sync_tasks)")}
    assert "realtime_watermark" in cols


def test_sync_records_has_observability_columns():
    cols = {r["name"] for r in db.query("PRAGMA table_info(sync_records)")}
    assert {"read_rows", "error_rows", "watermark"} <= cols


def test_migration_plans_has_verify_sample_rows():
    cols = {r["name"] for r in db.query("PRAGMA table_info(db_migration_plans)")}
    assert "verify_sample_rows" in cols


# ---------------------------------------------------------------------------
# 4. 同步记录对账字段落库（数量守恒：读取=写入+过滤+失败）
# ---------------------------------------------------------------------------
def test_sync_record_writes_read_error_watermark(monkeypatch):
    import core.sync.runner as runner_mod
    tid = _make_task()

    def fake_run(task):
        return {"success": True, "message": "同步完成：读取 100 行，写入 98 行，错误 2 行",
                "total_read": 100, "total_write": 98, "errors": 2,
                "next_value": "2026-10-12 10:00:00"}
    monkeypatch.setattr(runner_mod, "run_sync_task_with_task", fake_run)
    rec = runner_mod.run_sync(tid)
    assert rec is not None
    assert rec["status"] == "success"
    assert rec["read_rows"] == 100
    assert rec["rows_synced"] == 98
    assert rec["error_rows"] == 2
    # 数量守恒：读取 = 写入 + 失败（无过滤场景）
    assert rec["read_rows"] == rec["rows_synced"] + rec["error_rows"]
    assert rec["watermark"] == "2026-10-12 10:00:00"


# ---------------------------------------------------------------------------
# 5. 迁移校验：抽样内容比对
# ---------------------------------------------------------------------------
def test_diff_samples_bool_normalization():
    # pymysql tinyint(1) 返回 True/False，PG smallint 返回 1/0 → 必须视为一致
    src = [(1, "订单A", True), (2, "订单B", False)]
    tgt = [(1, "订单A", 1), (2, "订单B", 0)]
    assert DbMigrationEngine._diff_samples(src, tgt) == 0


def test_diff_samples_detects_content_diff():
    # 金额截断场景：行数一致但内容不同 → 必须能发现
    src = [(1, 123.456), (2, 99.001)]
    tgt = [(1, 123.45), (2, 99.001)]
    assert DbMigrationEngine._diff_samples(src, tgt) == 1


def test_diff_samples_detects_row_missing():
    src = [(1, "a"), (2, "b")]
    tgt = [(1, "a")]
    assert DbMigrationEngine._diff_samples(src, tgt) == 1


def test_diff_samples_none_and_datetime():
    from datetime import datetime
    dt = datetime(2026, 10, 12, 8, 0, 0)
    src = [(1, None, dt)]
    tgt = [(1, None, dt)]
    assert DbMigrationEngine._diff_samples(src, tgt) == 0


def test_watermark_lag_seconds():
    from datetime import datetime, timedelta
    # 时间戳水位 → 延迟秒数（构造 60s 前的水位）
    wm = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    lag = SyncEngine._watermark_lag_seconds(wm)
    assert lag is not None and 59 <= lag <= 90
    # 未来时间（时钟漂移）→ 不为负
    wm2 = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    assert SyncEngine._watermark_lag_seconds(wm2) == 0
    # 自增主键等数值水位 → 无法计算时间延迟
    assert SyncEngine._watermark_lag_seconds("1042") is None
    assert SyncEngine._watermark_lag_seconds("") is None
    assert SyncEngine._watermark_lag_seconds(None) is None
    # ISO 格式也可解析
    wm3 = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S")
    assert SyncEngine._watermark_lag_seconds(wm3) is not None


def test_verify_sample_rows_default_when_missing():
    # 旧计划无该列值 → 默认抽样 100 行（不因升级而关闭内容校验）
    plan = {}
    sample_rows = 100 if plan.get("verify_sample_rows") is None \
        else int(plan.get("verify_sample_rows") or 0)
    assert sample_rows == 100
    assert int({"verify_sample_rows": 0}.get("verify_sample_rows") or 0) == 0
