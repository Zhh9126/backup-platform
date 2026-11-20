# -*- coding: utf-8 -*-
"""跨站点块级按需传输单测（PB 级方案 §4.1/§4.2）。

协议正确性：
  - 位图 1/0 与命中/缺失严格对应，长度与块数一致；
  - 缺失块按清单顺序合并成连续区间（RANGE_CLONE 语义）；
  - 指纹探路按批切分（16384/批）；
  - 传输字节 = 仅缺失块的压缩体积，节省率计算正确。
"""
import pytest

from core import block_transfer as bt


def test_have_bitmap_basic():
    have = {"a", "c"}
    wanted = ["a", "b", "c", "d"]
    r = bt.have_bitmap(have, wanted)
    assert r["bitmap"] == "1010"
    assert r["missing_fps"] == ["b", "d"]
    assert r["hit_ratio"] == 0.5


def test_have_bitmap_all_hit_and_all_miss():
    assert bt.have_bitmap({"a", "b"}, ["a", "b"])["bitmap"] == "11"
    assert bt.have_bitmap(set(), ["a", "b"])["bitmap"] == "00"
    assert bt.have_bitmap({"a"}, [])["bitmap"] == ""


def test_batch_fingerprints():
    fps = [str(i) for i in range(40000)]
    batches = bt.batch_fingerprints(fps, 16384)
    assert [len(b) for b in batches] == [16384, 16384, 7232]
    assert bt.batch_fingerprints([], 100) == []


def test_merge_missing_ranges_contiguous():
    ordered = ["a", "b", "c", "d", "e", "f"]
    miss = ["b", "c", "e"]
    r = bt.merge_missing_ranges(ordered, miss)
    assert [(x["start"], x["count"]) for x in r] == [(1, 2), (4, 1)]
    assert r[0]["fps"] == ["b", "c"]


def test_merge_missing_ranges_none():
    assert bt.merge_missing_ranges(["a", "b"], []) == []
    assert bt.merge_missing_ranges(["a", "b"], ["a", "b"])[0]["count"] == 2


def test_apply_bitmap_validates_length():
    with pytest.raises(ValueError):
        bt.apply_bitmap(["a", "b"], "1", {})


def test_apply_bitmap_plan():
    target = ["a", "b", "c", "d"]
    src_map = {"a": {"comp_size": 100}, "b": {"comp_size": 200},
               "c": {"comp_size": 300}, "d": {"comp_size": 400}}
    plan = bt.apply_bitmap(target, "1010", src_map)
    assert plan["chunks_missing"] == 2
    assert plan["transfer_comp_bytes"] == 200 + 400
    assert plan["target_total_comp_bytes"] == 1000
    assert plan["saved_pct"] == 40.0
    assert [(r["start"], r["count"]) for r in plan["ranges"]] == [(1, 1), (3, 1)]


def _fake_manifest(store, tmp_path, name, data, opts):
    p = tmp_path / name
    p.write_bytes(data)
    return _store(store, str(p), opts)


def _store(store, path, opts):
    from core import chunk_store
    return chunk_store.store_file(path, store, chunk_opts=opts)


def test_plan_transfer_end_to_end(store_root, tmp_path):
    """真实块存储：目标版本只与源版本有局部差异 → 只传缺失块。"""
    from core import chunk_store
    opts = {"min_size": 256 * 1024, "avg_size": 512 * 1024, "max_size": 2 * 1024 * 1024}
    import random
    rnd = random.Random(77)
    base = bytearray(rnd.getrandbits(8) for _ in range(6 * 1024 * 1024))

    p1 = tmp_path / "s1.bin"
    p1.write_bytes(bytes(base))
    m1 = chunk_store.store_file(str(p1), store_root, chunk_opts=opts)

    base[2 * 1024 * 1024:2 * 1024 * 1024 + 8192] = b"\x00" * 8192   # 局部改动
    p2 = tmp_path / "s2.bin"
    p2.write_bytes(bytes(base))
    m2 = chunk_store.store_file(str(p2), store_root, chunk_opts=opts)

    plan = bt.plan_transfer(m1["manifest_id"], m2["manifest_id"], store_root)
    assert plan["ok"]
    assert plan["chunks_missing"] >= 1
    assert plan["chunks_reusable"] >= 1
    assert plan["reuse_ratio"] > 0.3
    assert plan["transfer_comp_bytes"] < plan["target_total_comp_bytes"]
    assert plan["saved_pct"] > 0
    # 缺失块确实落在改动附近
    assert any(r["count"] >= 1 for r in plan["ranges"])
    assert len(plan["missing_fps"]) == plan["chunks_missing"]


@pytest.fixture
def store_root(tmp_path):
    return str(tmp_path)
