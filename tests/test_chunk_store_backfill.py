# -*- coding: utf-8 -*-
"""分块存储「产物缺失回填」单测。

场景：备份产物被保留策略清理（文件没了），但块存储清单与块仍在 ——
恢复入口应能自动回填出等价文件，让恢复/校验无损继续；块缺失时必须如实
返回原记录（由后续逻辑报"产物不存在"），不能伪装成功。
"""
import hashlib
import os

import pytest

import config
from core import chunk_store, scheduler

OPTS = {"min_size": 256 * 1024, "avg_size": 512 * 1024, "max_size": 2 * 1024 * 1024}


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path), raising=False)
    return tmp_path


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def _make_artifact(tmp_path, size=1024 * 1024):
    p = tmp_path / "artifact.sql.zst"
    p.write_bytes(os.urandom(size))
    return str(p)


def test_no_manifest_returns_same(rooted, tmp_path):
    rec = {"id": 1, "backup_path": str(tmp_path / "gone.bin")}
    out = scheduler._materialize_from_chunk_store(rec)
    assert out is rec
    assert "chunk_restored" not in out


def test_existing_artifact_not_touched(rooted, tmp_path):
    src = _make_artifact(tmp_path)
    rep = chunk_store.store_file(src, "", chunk_opts=OPTS)
    rec = {"id": 2, "backup_path": src, "chunk_manifest_id": rep["manifest_id"]}
    out = scheduler._materialize_from_chunk_store(rec)
    assert out["backup_path"] == src           # 产物还在，不折腾
    assert "chunk_restored" not in out


def test_missing_artifact_is_restored(rooted, tmp_path):
    src = _make_artifact(tmp_path)
    rep = chunk_store.store_file(src, "", chunk_opts=OPTS)
    want = _sha(src)
    os.remove(src)                              # 模拟被清理

    rec = {"id": 3, "backup_path": src, "chunk_manifest_id": rep["manifest_id"]}
    out = scheduler._materialize_from_chunk_store(rec)
    assert out.get("chunk_restored") is True
    assert os.path.isfile(out["backup_path"])
    assert _sha(out["backup_path"]) == want     # 逐字节等价


def test_corrupt_store_falls_back_honestly(rooted, tmp_path):
    src = _make_artifact(tmp_path)
    rep = chunk_store.store_file(src, "", chunk_opts=OPTS)
    os.remove(src)
    # 删除一个块 → 回填必然失败
    man = chunk_store.load_manifest(rep["manifest_id"], "")
    fp = man["chunks"][0]["fp"]
    blob = os.path.join(str(tmp_path), "chunk_store", "blobs", fp[:2], fp)
    os.remove(blob)

    rec = {"id": 4, "backup_path": src, "chunk_manifest_id": rep["manifest_id"]}
    out = scheduler._materialize_from_chunk_store(rec)
    assert out["backup_path"] == src            # 不伪造可用路径
    assert "chunk_restored" not in out
