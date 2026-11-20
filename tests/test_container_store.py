# -*- coding: utf-8 -*-
"""Append-Only 容器封箱存储单测（PB 级方案 §3.1）。

关键承诺：
  - 容器内存块能原样取回（sha256 一致）；
  - 写满即封箱、新块进新容器（不越界写）；
  - 同内容块只写一次（跨清单复用）；
  - 容器缺失/损坏如实失败；
  - GC 只清索引条目、不动容器数据（Append-Only 代价如实报告）。
"""
import os
import random

import pytest

from core import chunk_store, container_store

OPTS = {"min_size": 256 * 1024, "avg_size": 512 * 1024, "max_size": 2 * 1024 * 1024}


def _rand(n, seed=7):
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(n))


@pytest.fixture
def store(tmp_path):
    return str(tmp_path)


def _mk(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_container_roundtrip(store, tmp_path):
    data = _rand(3 * 1024 * 1024, seed=11)
    src = _mk(tmp_path, "a.bin", data)
    rep = container_store.store_file(src, store, chunk_opts=OPTS)
    assert rep["ok"] and rep["chunks"] >= 2
    assert rep["containers"] >= 1

    out = str(tmp_path / "out.bin")
    res = container_store.restore_file(rep["manifest_id"], out, store)
    assert res["ok"] and res["sha256_match"]
    assert open(out, "rb").read() == data


def test_rollover_to_multiple_containers(store, tmp_path):
    """容器上限设小 → 必须封箱开新箱，且所有块仍可读回。"""
    data = _rand(2 * 1024 * 1024, seed=13)
    src = _mk(tmp_path, "b.bin", data)
    rep = container_store.store_file(src, store, chunk_opts=OPTS,
                                     container_max=256 * 1024)
    assert rep["containers"] >= 2
    out = str(tmp_path / "out.bin")
    res = container_store.restore_file(rep["manifest_id"], out, store)
    assert res["ok"] and open(out, "rb").read() == data


def test_same_content_stored_once(store, tmp_path):
    data = _rand(2 * 1024 * 1024, seed=17)
    src = _mk(tmp_path, "c.bin", data)
    r1 = container_store.store_file(src, store, chunk_opts=OPTS)
    before = container_store.stats(store)["physical_bytes"]
    r2 = container_store.store_file(src, store, chunk_opts=OPTS)
    after = container_store.stats(store)["physical_bytes"]
    assert r2["new_blocks"] == 0
    assert r2["reused_blocks"] == r2["chunks"]
    assert after == before                    # 容器体积未增长


def test_local_edit_only_appends_few_blocks(store, tmp_path):
    base = bytearray(_rand(4 * 1024 * 1024, seed=19))
    src = _mk(tmp_path, "v1.bin", bytes(base))
    r1 = container_store.store_file(src, store, chunk_opts=OPTS)
    base[1024 * 1024:1024 * 1024 + 4096] = b"\x00" * 4096
    src2 = _mk(tmp_path, "v2.bin", bytes(base))
    r2 = container_store.store_file(src2, store, chunk_opts=OPTS)
    assert r2["new_blocks"] < r2["chunks"]
    out = str(tmp_path / "v2out.bin")
    res = container_store.restore_file(r2["manifest_id"], out, store)
    assert res["ok"] and open(out, "rb").read() == bytes(base)


def test_missing_container_reported(store, tmp_path):
    data = _rand(1024 * 1024, seed=23)
    src = _mk(tmp_path, "d.bin", data)
    rep = container_store.store_file(src, store, chunk_opts=OPTS)
    root = container_store._root(store)
    for fn in os.listdir(root):
        if fn.endswith((".bin", ".sealed")):
            os.remove(os.path.join(root, fn))
    res = container_store.restore_file(rep["manifest_id"], str(tmp_path / "x.bin"), store)
    assert res["ok"] is False and res["missing_blocks"]


def test_stats(store, tmp_path):
    src = _mk(tmp_path, "s.bin", _rand(1024 * 1024, seed=29))
    rep = container_store.store_file(src, store, chunk_opts=OPTS)
    st = container_store.stats(store)
    assert st["blocks_indexed"] == rep["chunks"]
    assert st["containers"] >= 1
    assert st["physical_bytes"] > 0


def test_gc_reports_without_deleting(store, tmp_path):
    data = _rand(2 * 1024 * 1024, seed=31)
    src = _mk(tmp_path, "g.bin", data)
    rep = container_store.store_file(src, store, chunk_opts=OPTS)
    # 删掉清单 → 所有块变孤儿
    mp = os.path.join(chunk_store._root(store), "manifests",     # noqa: SLF001
                      rep["manifest_id"] + ".json")
    os.remove(mp)
    dry = container_store.gc(store, dry_run=True)
    assert dry["orphan_blocks"] == rep["chunks"]
    assert dry["deleted_entries"] == 0
    assert dry["reclaimable_bytes"] > 0

    real = container_store.gc(store, dry_run=False)
    assert real["deleted_entries"] == rep["chunks"]
    # 容器文件本身仍在（Append-Only 不搬移），如实报告
    assert container_store.stats(store)["physical_bytes"] > 0
