# -*- coding: utf-8 -*-
"""多表并行迁移自测（P1，2026-11）。

覆盖：
- _task_to_config 并行度钳制（1-8）
- 并行迁移引擎配置实例隔离：每 worker 独立 SyncEngine，串表免疫
- 并行聚合结果结构与串行一致（含失败表如实记录不阻断）
- 多表 full 同步路径走并行；incremental 保持串行
- sync_tasks 表契约：parallel_tables / type_mapping 列存在

与 test_sync_reliability.py 同款隔离：元数据库指向临时目录。
"""
import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="parallel_mig_")
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
from core.sync.engine import SyncEngine, _task_to_config  # noqa: E402
from core.sync.plugins import registry          # noqa: E402
from core.sync.plugins.base import (            # noqa: E402
    BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader, SyncConfig,
)


# ---------------------------------------------------------------------------
# Fake 插件：内存读/写，验证并行实例隔离与聚合
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, reader):
        self._reader = reader

    def close(self):
        pass

    def cursor(self):
        # 真实游标：持有读取位置，read_batch 每次从上次进度继续
        return _ReadCursor(_FakeReader._store.get(
            self._reader.config.source_table, []))

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeReader(SourceReader):
    """按 config.source_table 从共享内存字典读数据。"""

    def connect(self):
        return _FakeConn(self)

    def list_tables(self):
        return sorted(_FakeReader._store.keys())

    def list_columns(self, table):
        return [ColumnMeta(name="id", type="INTEGER", is_primary=True),
                ColumnMeta(name="v", type="VARCHAR(100)")]

    def read_batch(self, cursor):
        return cursor.next_batch()


class _ReadCursor:
    def __init__(self, rows):
        self._rows = rows
        self._i = 0

    def next_batch(self):
        chunk = self._rows[self._i:self._i + 2]
        self._i += len(chunk)
        return ReadResult(records=list(chunk), columns=["id", "v"],
                          has_more=self._i < len(self._rows))

    def close(self):
        pass


class _FakeWriter(SinkWriter):
    """按 config.target_table 写共享内存字典（记录线程与表名）。"""

    def connect(self):
        return _FakeConn(None)

    def prepare_table(self, conn, columns):
        _FakeWriter.writes.setdefault(self.config.target_table, [])

    def write_batch(self, conn, records, columns):
        _FakeWriter.writes.setdefault(self.config.target_table, []).extend(records)
        time.sleep(0.01)  # 模拟写入耗时，让并行有意义
        return len(records)


class _FakePlugin(BasePlugin):
    db_type = "fakep"

    def type_to_java(self, col):
        return col.type or "STRING"

    def java_to_db(self, type_str):
        return type_str or "TEXT"

    def quote_identifier(self, name):
        return name

    def create_reader(self, config):
        return _FakeReader(config, self)

    def create_writer(self, config):
        return _FakeWriter(config, self)


_FakeReader._store = {}
_FakeWriter.writes = {}
registry.register("fakep", _FakePlugin)


def _cfg(tables=None, **kw) -> SyncConfig:
    c = SyncConfig(
        task_id=0, task_name="t",
        src_db_type="fakep", src_host="h", src_port=1,
        src_username="u", src_password="p", src_db_name="s",
        tgt_db_type="fakep", tgt_host="h", tgt_port=1,
        tgt_username="u", tgt_password="p", tgt_db_name="d",
        sync_mode="full", save_mode="append",
        full_db_migrate=True, skip_precheck=True,
        **kw)
    if tables:
        c.source_tables_list = tables
    return c


# ---------------------------------------------------------------------------
# 1. 并行度钳制
# ---------------------------------------------------------------------------
def test_task_to_config_parallel_clamp():
    base = {"id": -1, "name": "t"}
    assert _task_to_config({**base, "parallel_tables": 0}).parallel_tables == 1
    assert _task_to_config({**base, "parallel_tables": 99}).parallel_tables == 8
    assert _task_to_config({**base, "parallel_tables": 4}).parallel_tables == 4
    assert _task_to_config({**base, "parallel_tables": None}).parallel_tables == 1


# ---------------------------------------------------------------------------
# 2. 并行迁移：实例隔离 + 聚合
# ---------------------------------------------------------------------------
def test_parallel_migration_isolates_config():
    """4 表并行：每 worker 独立 config，写入不串表。"""
    _FakeReader._store = {f"t{i}": [[i, f"v{i}-{j}"] for j in range(3)]
                          for i in range(1, 5)}
    _FakeWriter.writes = {}
    eng = SyncEngine(_cfg(parallel_tables=4))
    res = eng.run()
    assert res["success"] is True, res.get("message")
    assert res["parallel"] == 4
    assert res["total_write"] == 12
    # 每张表数据落在自己名下（串表会互相覆盖/混行）
    for i in range(1, 5):
        rows = _FakeWriter.writes.get(f"t{i}", [])
        assert len(rows) == 3, f"表 t{i} 行数不对: {len(rows)}"
        assert all(r[0] == i for r in rows), f"表 t{i} 数据串表: {rows}"


def test_parallel_migration_failure_isolated():
    """单表失败如实记录，不阻断其他表。"""
    _FakeReader._store = {"ok1": [[1, "a"]], "ok2": [[2, "b"]],
                          "bad": [[9, "x"]]}  # bad 表写入时抛错（注入失败）
    _FakeWriter.writes = {}

    orig_write = _FakeWriter.write_batch

    def write_or_boom(self, conn, records, columns):
        if self.config.target_table == "bad":
            raise RuntimeError("模拟整表写入失败")
        return orig_write(self, conn, records, columns)

    _FakeWriter.write_batch = write_or_boom
    try:
        eng = SyncEngine(_cfg(parallel_tables=3))
        res = eng.run()
        assert res["success"] is False
        assert "bad" in res["failed_tables"]
        assert res["total_write"] == 2  # ok1/ok2 各 1 行
        assert len(_FakeWriter.writes.get("ok1", [])) == 1
        assert len(_FakeWriter.writes.get("ok2", [])) == 1
    finally:
        _FakeWriter.write_batch = orig_write


def test_parallel_config_never_shared():
    """并行运行期间，主 config 的 source_table 不被 worker 改写。"""
    _FakeReader._store = {f"x{i}": [[i, "v"]] for i in range(1, 5)}
    _FakeWriter.writes = {}
    cfg = _cfg(parallel_tables=2)
    eng = SyncEngine(cfg)
    seen = []
    orig_single = SyncEngine._run_single_table

    def spy(self, src, dst, progress_callback=None):
        seen.append((src, self.config.source_table))
        return orig_single(self, src, dst, progress_callback)

    SyncEngine._run_single_table = spy
    try:
        eng.run()
    finally:
        SyncEngine._run_single_table = orig_single
    # 每个 worker 的 config.source_table 与传入表一致（隔离生效的硬证据）
    for src, cfg_table in seen:
        assert src == cfg_table, f"worker 内 config 被串改: {src} != {cfg_table}"
    assert cfg.source_table == ""  # 主 config 未被污染


# ---------------------------------------------------------------------------
# 3. 多表 full 同步走并行 / incremental 保持串行
# ---------------------------------------------------------------------------
def test_multi_table_full_sync_uses_parallel():
    _FakeReader._store = {f"s{i}": [[i, "v"]] for i in range(1, 4)}
    _FakeWriter.writes = {}
    c = _cfg(tables=["s1", "s2", "s3"], parallel_tables=2)
    c.full_db_migrate = False
    eng = SyncEngine(c)
    res = eng.run()
    assert res["success"] is True
    assert res.get("parallel") == 2
    assert res["total_write"] == 3
    assert len(res["per_table"]) == 3


def test_incremental_sync_stays_serial():
    """incremental 模式不走并行（水位回写共享任务行）。"""
    _FakeReader._store = {"a": [[1, "v"]], "b": [[2, "v"]]}
    _FakeWriter.writes = {}
    c = _cfg(tables=["a", "b"], parallel_tables=4)
    c.full_db_migrate = False
    c.sync_mode = "incremental"
    c.incremental_column = "id"
    eng = SyncEngine(c)
    res = eng.run()
    assert res["success"] is True
    assert "parallel" not in res  # 串行路径无 parallel 键


# ---------------------------------------------------------------------------
# 4. 表结构契约：sync_tasks 列
# ---------------------------------------------------------------------------
def test_sync_tasks_columns_contract():
    cols = {r["name"] for r in db.query("PRAGMA table_info(sync_tasks)")}
    assert "parallel_tables" in cols
    assert "type_mapping" in cols


def test_sync_task_roundtrip_parallel_and_type_mapping():
    tid = models.create_sync_task({
        "name": "并行契约", "src_db_type": "mysql", "src_host": "127.0.0.1",
        "src_port": 3306, "src_username": "u", "src_password": "p",
        "src_db_name": "sdb", "tgt_db_type": "postgresql", "tgt_host": "127.0.0.1",
        "tgt_port": 5432, "tgt_username": "u", "tgt_password": "p",
        "tgt_db_name": "tdb", "sync_mode": "full", "save_mode": "append",
        "parallel_tables": 4, "type_mapping": {"decimal": "NUMBER(10,2)"},
    })
    row = models.get_sync_task(tid)
    assert row["parallel_tables"] == 4
    assert row["type_mapping"] == {"decimal": "NUMBER(10,2)"}
