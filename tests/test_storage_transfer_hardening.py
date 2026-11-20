"""Storage integrity, streaming and transfer retry regression tests."""
import errno
import io
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor

import pytest
from core import chunk_store as cs, container_store as ct, fastcdc, remote_dump as rd
from core.engines.file import _is_transient

OPTS = dict(min_size=256, avg_size=1024, max_size=4096)

@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 100_000])
def test_streaming_preserves_boundaries(size):
    data = random.Random(42).randbytes(size)
    actual = list(fastcdc.iter_file_chunks(io.BytesIO(data), **OPTS))
    expected = [(off, data[off:off+n]) for off, n in fastcdc.cut_points(data, **OPTS)]
    assert actual == expected

@pytest.mark.parametrize("backend", [cs, ct])
def test_bad_block_repair_and_atomic_restore(tmp_path, backend):
    source = tmp_path / "source"
    source.write_bytes(random.Random(10).randbytes(50_000))
    rep = backend.store_file(str(source), str(tmp_path), chunk_opts=OPTS, max_bytes=0)
    man = cs.load_manifest(rep["manifest_id"], str(tmp_path))
    chunk = man["chunks"][0]
    if backend is cs:
        damaged = cs._blob_path(cs._root(str(tmp_path)), chunk["fp"])
        with open(damaged, "r+b") as stream:
            stream.write(b"bad!")
    else:
        root = ct._root(str(tmp_path))
        damaged = next(os.path.join(root, chunk["container"] + ext)
                       for ext in (".bin", ".sealed")
                       if os.path.exists(os.path.join(root, chunk["container"] + ext)))
        with open(damaged, "r+b") as stream:
            stream.seek(chunk["container_offset"])
            stream.write(b"bad!")
    target = tmp_path / "target"
    target.write_bytes(b"keep existing")
    assert not backend.restore_file(rep["manifest_id"], str(target), str(tmp_path), verify=False)["ok"]
    assert target.read_bytes() == b"keep existing"
    repaired = backend.store_file(str(source), str(tmp_path), chunk_opts=OPTS, max_bytes=0)
    assert repaired["new_blocks"] >= 1
    assert backend.restore_file(rep["manifest_id"], str(target), str(tmp_path))["ok"]
    assert target.read_bytes() == source.read_bytes()
    assert not list(tmp_path.glob(".aidbm-restore-*"))

@pytest.mark.parametrize("backend", [cs, ct])
def test_gc_fail_closed(tmp_path, backend):
    source = tmp_path / "source"
    source.write_bytes(b"storage" * 1000)
    rep = backend.store_file(str(source), str(tmp_path), chunk_opts=OPTS)
    manifest = os.path.join(cs._root(str(tmp_path)), "manifests", rep["manifest_id"] + ".json")
    with open(manifest, "w") as stream:
        stream.write("{bad")
    before = backend.stats(str(tmp_path))
    report = backend.gc(str(tmp_path), dry_run=False)
    assert not report["ok"] and report["blocked"] and report["bad_manifests"] == 1
    after = backend.stats(str(tmp_path))
    assert before == after

def test_mixed_compression_reuse(tmp_path, monkeypatch):
    import zlib
    source = tmp_path / "source"
    source.write_bytes(b"reusable" * 1000)
    with monkeypatch.context() as patch:
        patch.setattr(cs, "_compress", lambda raw: ("zlib", zlib.compress(raw)))
        old = cs.store_file(str(source), str(tmp_path), chunk_opts=OPTS)
    new = cs.store_file(str(source), str(tmp_path), chunk_opts=OPTS)
    assert new["new_blocks"] == 0
    for item in (old, new):
        assert cs.restore_file(item["manifest_id"], str(tmp_path / "out"), str(tmp_path))["ok"]

def _write_worker(args):
    path, root, container = args
    backend = ct if container else cs
    rep = backend.store_file(path, root, chunk_opts=OPTS)
    backend.gc(root, dry_run=False)
    return rep["manifest_id"]

@pytest.mark.parametrize("container", [False, True])
def test_process_writes_and_gc(tmp_path, container):
    source = tmp_path / "source"
    source.write_bytes(random.Random(7).randbytes(25000))
    backend = ct if container else cs
    with ProcessPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(_write_worker, [(str(source), str(tmp_path), container)] * 8))
    for mid in ids:
        out = tmp_path / ("out-" + mid)
        assert backend.restore_file(mid, str(out), str(tmp_path))["ok"]
        assert out.read_bytes() == source.read_bytes()

@pytest.mark.parametrize("code", [errno.ENOENT, errno.EACCES, errno.ENOSPC, errno.EINVAL])
def test_permanent_errors_not_retried(code):
    assert not _is_transient(OSError(code, os.strerror(code)))

class Client:
    def open_sftp(self):
        raise EOFError("disconnected")

def test_pull_uses_configured_retry_for_connect(tmp_path, monkeypatch):
    calls, waits = [], []
    def refresh(*args, **kwargs):
        calls.append(1)
        raise EOFError("connect interrupted")
    monkeypatch.setattr(rd, "_sftp_session_refresh", refresh)
    monkeypatch.setattr(rd, "_retry_wait", lambda attempt, interval: waits.append((attempt, interval)))
    task = {"id": 22, "extra_options": {"retry_max": 4, "retry_interval": 7}}
    with pytest.raises(EOFError):
        rd.sftp_pull_resumable(Client(), "/source", str(tmp_path / "out"), task=task)
    assert len(calls) == 5
    assert waits == [(1, 7), (2, 7), (3, 7), (4, 7)]

def test_pull_zero_retry(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(rd, "_sftp_session_refresh", lambda client, **kwargs: client)
    monkeypatch.setattr(rd, "_retry_wait", lambda *args: waits.append(args))
    with pytest.raises(EOFError):
        rd.sftp_pull_resumable(Client(), "/source", str(tmp_path / "out"),
                              task={"extra_options": {"retry_max": 0}})
    assert not waits

def test_retry_wait_cancelled():
    from core import cancel
    key = "storage-retry-test"
    cancel.register(key)
    token = cancel.attach(key)
    cancel.cancel(key)
    try:
        with pytest.raises(cancel.CancelledError):
            rd._retry_wait(1, 60)
    finally:
        cancel.detach(token)
        cancel.finish(key)

def test_index_corruption_preserved(tmp_path):
    root = ct._root(str(tmp_path))
    os.makedirs(root)
    with open(ct._index_path(root), "w") as stream:
        stream.write("[]")
    source = tmp_path / "source"
    source.write_bytes(b"test")
    with pytest.raises(ValueError, match="索引"):
        ct.store_file(str(source), str(tmp_path))
    assert open(ct._index_path(root)).read() == "[]"

def test_have_block_does_not_advertise_corruption(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"abc" * 1000)
    rep = cs.store_file(str(source), str(tmp_path), chunk_opts=OPTS)
    chunk = cs.load_manifest(rep["manifest_id"], str(tmp_path))["chunks"][0]
    assert cs.has_block(chunk["fp"], str(tmp_path))
    with open(cs._blob_path(cs._root(str(tmp_path)), chunk["fp"]), "wb") as stream:
        stream.write(b"corrupt")
    assert not cs.has_block(chunk["fp"], str(tmp_path))

def test_transfer_plan_counts_missing_bytes(tmp_path):
    from core import block_transfer
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"A" * 2000)
    two.write_bytes(b"B" * 2000)
    old = cs.store_file(str(one), str(tmp_path), chunk_opts=OPTS)
    new = cs.store_file(str(two), str(tmp_path), chunk_opts=OPTS)
    plan = block_transfer.plan_transfer(old["manifest_id"], new["manifest_id"], str(tmp_path))
    assert plan["transfer_comp_bytes"] == plan["target_total_comp_bytes"] > 0
    assert plan["saved_pct"] == 0

def test_global_dedup_stats_and_repair(tmp_path, monkeypatch):
    import sqlite3
    import config
    from core import global_dedup as gd
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE dedup_index(id INTEGER PRIMARY KEY, block_hash TEXT, size_bytes INTEGER, ref_count INTEGER, first_task_id INTEGER, first_set_id INTEGER, object_key TEXT, created_at TEXT)")
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path))
    monkeypatch.setattr(gd.db, "query_one", lambda sql, args=(): conn.execute(sql, args).fetchone())
    monkeypatch.setattr(gd.db, "execute", lambda sql, args=(): conn.execute(sql, args))
    try:
        gd.dedup_bytes(b"ABCDEFGH", block_size=5)
        gd.dedup_bytes(b"ABCDE", block_size=5)
        stats = gd.global_stats()
        assert stats["logical_bytes"] == 13 and stats["physical_bytes"] == 8
        assert stats["saved_bytes"] == 5
        fp = gd._block_hash(b"ABCDE")
        physical = tmp_path / ".dedup_store" / fp[:2] / fp
        physical.write_bytes(b"wrong")
        report = gd.dedup_bytes(b"ABCDE", block_size=5)
        assert report["new_blocks"] == 1 and report["saved_bytes"] == 0
        assert physical.read_bytes() == b"ABCDE"
    finally:
        conn.close()

def test_source_dedup_failure_preserves_both_ends(tmp_path, monkeypatch):
    import hashlib
    import config
    from core import dedup_source as ds
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"wrong")
    destination.write_bytes(b"previous backup")
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path))
    good_hash = hashlib.sha256(b"right").hexdigest()
    monkeypatch.setattr(ds, "remote_fingerprint", lambda *args: {
        "size": 5, "hashes": [good_hash], "sha256": good_hash})
    cleanup = []
    class SFTP:
        def open(self, path, mode):
            return open(path, mode)
        def close(self):
            pass
    class Client:
        def open_sftp(self):
            return SFTP()
    with pytest.raises(RuntimeError, match="哈希"):
        ds.pull_assemble_dedup(Client(), str(source), str(destination),
                               exec_fn=lambda cmd: cleanup.append(cmd), block_size=5)
    assert source.read_bytes() == b"wrong" and destination.read_bytes() == b"previous backup"
    assert cleanup == [] and not list(tmp_path.glob(".aidbm-assemble-*"))
