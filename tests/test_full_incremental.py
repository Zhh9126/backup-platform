# -*- coding: utf-8 -*-
"""全量+增量追平内核单测（纯内存，不依赖外部数据库）。

覆盖交接水位、按已写行推进、位点回调、配置恢复、防死循环与失败如实。
真机 E2E 见 /root/e2e/e2e_full_incremental_1122.py。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="fullinc_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
config.META_DB_PATH = os.environ["META_DB_PATH"]

from core.sync.catchup import run_catchup, snapshot_watermark  # noqa: E402
from core.sync.engine import _norm_mode, _SYNC_MODE_ALIASES  # noqa: E402
from core.sync.plugins.base import ReadResult, SyncConfig  # noqa: E402


class _FakeReader:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def read_batch(self, cur):
        self.calls.append(cur)
        if not self.batches:
            return ReadResult(has_more=False)
        return self.batches.pop(0)

    def close(self, cursor=None, conn=None):
        pass


class _FakeWriter:
    def __init__(self, written=None, fail=False):
        self.written = written
        self.fail = fail
        self.received = []

    def write_batch(self, conn, records, columns):
        if self.fail:
            raise RuntimeError("simulated write failure")
        self.received.append([list(r) for r in records])
        if self.written is None:
            return len(records)
        if isinstance(self.written, list):
            return self.written[min(len(self.received) - 1, len(self.written) - 1)]
        return self.written

    def close(self, cursor=None, conn=None):
        pass


class _FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return object()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _cfg():
    return SyncConfig(src_db_type="mysql", tgt_db_type="mysql",
                      incremental_column="id", batch_size=3,
                      source_table="src", target_table="dst",
                      sync_mode="full_incremental", save_mode="overwrite")


def test_sync_mode_aliases():
    assert _norm_mode("全量+增量", _SYNC_MODE_ALIASES, "full") == "full_incremental"
    assert _norm_mode("full_and_incremental", _SYNC_MODE_ALIASES, "full") == "full_incremental"


def test_catchup_drains_batches_and_advances_watermark_by_written_rows():
    cfg = _cfg()
    reader = _FakeReader([
        ReadResult(records=[[1], [2], [3]], columns=["id"], has_more=True),
        ReadResult(records=[[4], [5]], columns=["id"], has_more=False),
    ])
    writer = _FakeWriter()
    src, tgt = _FakeConn(), _FakeConn()
    checkpoints = []
    res = run_catchup(cfg, reader, writer, src, tgt, "0", "src", "dst",
                      on_watermark=lambda c, v: checkpoints.append((c, v)))
    assert res["success"] is True
    assert res["caught_up"] is True
    assert res["rounds"] == 2
    assert res["rows_written"] == 5
    assert res["watermark"] == "5"
    assert checkpoints == [("id", "3"), ("id", "5")]
    # 追平结束后配置必须原样恢复（不得污染调用方）
    assert cfg.sync_mode == "full_incremental"
    assert cfg.save_mode == "overwrite"
    assert cfg.incremental_value == ""
    assert tgt.commits == 2


def test_catchup_never_advances_beyond_written_rows():
    cfg = _cfg()
    # 第二批写入只成功 1/3 行：水位只能推进到已写记录的最大值
    reader = _FakeReader([
        ReadResult(records=[[1], [2], [3]], columns=["id"], has_more=True),
        ReadResult(records=[[4], [5], [6]], columns=["id"], has_more=True),
        ReadResult(records=[], columns=["id"], has_more=False),
    ])
    writer = _FakeWriter(written=[3, 1])
    checkpoints = []
    res = run_catchup(cfg, reader, writer, _FakeConn(), _FakeConn(), "0", "src",
                      on_watermark=lambda c, v: checkpoints.append(v))
    assert res["rows_written"] == 4          # 3 + 部分写入的 1 行
    assert checkpoints == ["3"]              # 第二批部分写入，水位不动
    assert any("水位不推进" in w for w in res["warnings"])


def test_catchup_stops_when_watermark_not_advancing():
    cfg = _cfg()
    reader = _FakeReader([
        ReadResult(records=[[1], [2], [3]], columns=["id"], has_more=True),
        ReadResult(records=[[1], [2], [3]], columns=["id"], has_more=True),
    ])
    res = run_catchup(cfg, reader, _FakeWriter(), _FakeConn(), _FakeConn(),
                      "0", "src")
    assert res["rounds"] == 1
    assert any("位点未推进" in w for w in res["warnings"])
    assert len(reader.calls) == 2   # 检测到不推进后立刻终止


def test_catchup_write_failure_rolls_back_and_reports():
    cfg = _cfg()
    reader = _FakeReader([
        ReadResult(records=[[1], [2]], columns=["id"], has_more=False),
    ])
    tgt = _FakeConn()
    res = run_catchup(cfg, reader, _FakeWriter(fail=True), _FakeConn(), tgt,
                      "0", "src")
    assert res["success"] is False
    assert tgt.rollbacks == 1
    assert any("追平写入失败" in w for w in res["warnings"])


def test_catchup_requires_incremental_column():
    cfg = SyncConfig(src_db_type="mysql", tgt_db_type="mysql")
    res = run_catchup(cfg, _FakeReader([]), _FakeWriter(), _FakeConn(),
                      _FakeConn(), "", "src")
    assert res["success"] is False
    assert "incremental_column" in res["message"]


def test_catchup_restores_config_on_failure():
    cfg = _cfg()
    reader = _FakeReader([
        ReadResult(records=[[1]], columns=["id"], has_more=False),
    ])
    run_catchup(cfg, reader, _FakeWriter(fail=True), _FakeConn(), _FakeConn(),
                "0", "src")
    assert cfg.sync_mode == "full_incremental"
    assert cfg.source_table == "src"
    assert cfg.target_table == "dst"


def test_config_limits_present():
    assert config.SYNC_CATCHUP_MAX_ROUNDS > 0
    assert config.SYNC_CATCHUP_MAX_SECONDS > 0
