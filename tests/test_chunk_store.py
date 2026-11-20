# -*- coding: utf-8 -*-
"""分块压缩内容寻址存储单测（PB 级方案 §3.1 / §4）。

核心承诺：
  - 存进去能原样取出来（逐字节 sha256 一致）；
  - 相同内容二次存储不重复写块（内容寻址）；
  - 局部修改后只新增少量块（跨版本增量）；
  - 块被篡改/丢失时如实失败，不伪装成功。
"""
import os
import random

import pytest

from core import chunk_store

OPTS = {"min_size": 256 * 1024, "avg_size": 1024 * 1024, "max_size": 4 * 1024 * 1024}


def _rand(n, seed=5):
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(n))


@pytest.fixture
def store(tmp_path):
    return str(tmp_path)


def _mkfile(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_store_and_restore_roundtrip(store, tmp_path):
    data = _rand(4 * 1024 * 1024, seed=11)
    src = _mkfile(tmp_path, "src.bin", data)
    rep = chunk_store.store_file(src, store, chunk_opts=OPTS)
    assert rep["ok"] and rep["chunks"] >= 2
    assert rep["new_blocks"] == rep["chunks"]
    out = str(tmp_path / "out.bin")
    r = chunk_store.restore_file(rep["manifest_id"], out, store)
    assert r["ok"] and r["sha256_match"]
    assert open(out, "rb").read() == data


def test_second_store_reuses_all_blocks(store, tmp_path):
    data = _rand(3 * 1024 * 1024, seed=13)
    src = _mkfile(tmp_path, "a.bin", data)
    r1 = chunk_store.store_file(src, store, chunk_opts=OPTS)
    r2 = chunk_store.store_file(src, store, chunk_opts=OPTS)
    assert r1["new_blocks"] > 0
    assert r2["new_blocks"] == 0          # 内容寻址：完全复用
    assert r2["reused_blocks"] == r2["chunks"]


def test_local_edit_only_adds_few_blocks(store, tmp_path):
    base = bytearray(_rand(6 * 1024 * 1024, seed=17))
    src = _mkfile(tmp_path, "v1.bin", bytes(base))
    r1 = chunk_store.store_file(src, store, chunk_opts=OPTS)

    # 在中间改 4KB（模拟局部变化）
    mid = len(base) // 2
    base[mid:mid + 4096] = _rand(4096, seed=19)
    src2 = _mkfile(tmp_path, "v2.bin", bytes(base))
    r2 = chunk_store.store_file(src2, store, chunk_opts=OPTS)

    assert r2["new_blocks"] < r1["chunks"], "局部修改不应导致全量重写"
    assert r2["reused_blocks"] > 0
    d = chunk_store.diff_manifests(r1["manifest_id"], r2["manifest_id"], store)
    assert d["reuse_ratio"] > 0.5
    assert d["incremental_comp_bytes"] < r2["comp_bytes"]


def test_restore_of_v2_is_correct(store, tmp_path):
    base = bytearray(_rand(2 * 1024 * 1024, seed=23))
    src = _mkfile(tmp_path, "v1.bin", bytes(base))
    chunk_store.store_file(src, store, chunk_opts=OPTS)
    base[100:200] = b"\x00" * 100
    src2 = _mkfile(tmp_path, "v2.bin", bytes(base))
    r2 = chunk_store.store_file(src2, store, chunk_opts=OPTS)
    out = str(tmp_path / "out2.bin")
    res = chunk_store.restore_file(r2["manifest_id"], out, store)
    assert res["ok"] and open(out, "rb").read() == bytes(base)


def test_corrupted_block_detected(store, tmp_path):
    data = _rand(1024 * 1024, seed=29)
    src = _mkfile(tmp_path, "c.bin", data)
    rep = chunk_store.store_file(src, store, chunk_opts=OPTS)
    man = chunk_store.load_manifest(rep["manifest_id"], store)
    fp = man["chunks"][0]["fp"]
    blob = os.path.join(store, "chunk_store", "blobs", fp[:2], fp)
    with open(blob, "wb") as f:
        f.write(b"corrupted")
    res = chunk_store.restore_file(rep["manifest_id"], str(tmp_path / "x.bin"), store)
    assert res["ok"] is False
    assert (res["sha256_match"] is False) or res["missing_blocks"]


def test_missing_block_reported(store, tmp_path):
    data = _rand(1024 * 1024, seed=31)
    src = _mkfile(tmp_path, "d.bin", data)
    rep = chunk_store.store_file(src, store, chunk_opts=OPTS)
    man = chunk_store.load_manifest(rep["manifest_id"], store)
    fp = man["chunks"][0]["fp"]
    os.remove(os.path.join(store, "chunk_store", "blobs", fp[:2], fp))
    res = chunk_store.restore_file(rep["manifest_id"], str(tmp_path / "y.bin"), store)
    assert res["ok"] is False and res["missing_blocks"]


def test_stats_and_list(store, tmp_path):
    src = _mkfile(tmp_path, "s.bin", _rand(1024 * 1024, seed=37))
    rep = chunk_store.store_file(src, store, chunk_opts=OPTS)
    st = chunk_store.stats(store)
    assert st["blocks"] == rep["new_blocks"]
    assert st["manifests"] == 1
    items = chunk_store.list_manifests(store)
    assert items and items[0]["id"] == rep["manifest_id"]


def test_estimate_vs_whole(store, tmp_path):
    src = _mkfile(tmp_path, "e.bin", _rand(2 * 1024 * 1024, seed=41))
    est = chunk_store.estimate_vs_whole(src, store, chunk_opts=OPTS)
    assert est["raw_bytes"] == 2 * 1024 * 1024
    assert est["whole_comp"]["bytes"] > 0
    assert est["chunked_comp"]["chunks"] >= 1
    # 分块压缩有少量额外开销（块头），但不会离谱
    assert est["overhead_pct"] > -100


def test_auto_chunk_opts_scales_with_size():
    small = chunk_store.auto_chunk_opts(1024 * 1024)
    big = chunk_store.auto_chunk_opts(1024 * 1024 * 1024)
    assert 512 * 1024 <= small["avg_size"] <= 8 * 1024 * 1024
    assert big["avg_size"] > small["avg_size"]
    assert big["avg_size"] <= 8 * 1024 * 1024
    assert small["min_size"] < small["avg_size"] < small["max_size"]


def test_low_entropy_file_still_splits(store, tmp_path):
    """低熵（全零/重复页）数据不能让 Gear 直接退化成"一整块"。"""
    data = b"\x00" * (12 * 1024 * 1024)
    src = _mkfile(tmp_path, "zeros.bin", data)
    rep = chunk_store.store_file(src, store)      # 走自适应参数
    assert rep["chunks"] > 1, "低熵大文件应被切成多块，否则跨版本复用失效"
    out = str(tmp_path / "zeros_out.bin")
    res = chunk_store.restore_file(rep["manifest_id"], out, store)
    assert res["ok"] and res["sha256_match"]


def test_gc_keeps_referenced_blocks(store, tmp_path):
    """GC 只能回收无引用的块：另一份清单引用的块必须保留且可还原。"""
    data_a = _rand(2 * 1024 * 1024, seed=47)
    data_b = _rand(2 * 1024 * 1024, seed=53)
    pa = _mkfile(tmp_path, "ga.bin", data_a)
    pb = _mkfile(tmp_path, "gb.bin", data_b)
    ra = chunk_store.store_file(pa, store, chunk_opts=OPTS)
    rb = chunk_store.store_file(pb, store, chunk_opts=OPTS)

    # 删掉 A 的清单 → A 的块变成孤儿（B 的块仍被引用）
    mpath = os.path.join(store, "chunk_store", "manifests", ra["manifest_id"] + ".json")
    os.remove(mpath)

    dry = chunk_store.gc(store, dry_run=True)
    assert dry["unreferenced_blocks"] > 0
    assert dry["deleted_blocks"] == 0                     # dry_run 不动盘

    real = chunk_store.gc(store, dry_run=False)
    assert real["deleted_blocks"] == dry["unreferenced_blocks"]

    # B 仍完整可还原
    out = str(tmp_path / "gb_out.bin")
    res = chunk_store.restore_file(rb["manifest_id"], out, store)
    assert res["ok"] and open(out, "rb").read() == data_b


def test_gc_min_age_grace_protects_fresh_blocks(store, tmp_path):
    """定时回收带宽限期：刚写入的孤儿块（可能清单还没落盘）不能被删。"""
    data = _rand(1024 * 1024, seed=61)
    p = _mkfile(tmp_path, "fresh.bin", data)
    rep = chunk_store.store_file(p, store, chunk_opts=OPTS)
    mpath = os.path.join(store, "chunk_store", "manifests", rep["manifest_id"] + ".json")
    os.remove(mpath)                      # 变成孤儿

    guarded = chunk_store.gc(store, dry_run=False, min_age_hours=24)
    assert guarded["deleted_blocks"] == 0
    assert chunk_store.stats(store)["blocks"] == rep["chunks"]

    forced = chunk_store.gc(store, dry_run=False, min_age_hours=0)
    assert forced["deleted_blocks"] == rep["chunks"]


def test_gc_bad_manifest_does_not_delete_blocks(store, tmp_path):
    data = _rand(1024 * 1024, seed=59)
    p = _mkfile(tmp_path, "bad.bin", data)
    rep = chunk_store.store_file(p, store, chunk_opts=OPTS)
    # 写坏一个 manifest（不影响引用计数逻辑：坏文件跳过，块仍被正确清单引用）
    mdir = os.path.join(store, "chunk_store", "manifests")
    with open(os.path.join(mdir, "broken.json"), "w") as f:
        f.write("{not json")
    res = chunk_store.gc(store, dry_run=True)
    assert res["bad_manifests"] >= 1
    assert res["unreferenced_blocks"] == 0
    out = str(tmp_path / "ok_out.bin")
    assert chunk_store.restore_file(rep["manifest_id"], out, store)["ok"]


def test_purge(store, tmp_path):
    src = _mkfile(tmp_path, "p.bin", _rand(512 * 1024, seed=43))
    chunk_store.store_file(src, store, chunk_opts=OPTS)
    r = chunk_store.purge(store)
    assert r["ok"] and r["deleted"] >= 1
    assert chunk_store.stats(store)["blocks"] == 0
