# -*- coding: utf-8 -*-
"""FastCDC 可变长分块单测（对标 PB 级方案 §7.1）。

核心断言：
  - 边界由内容决定，同一数据分块结果稳定（可复现）；
  - 头部插入/变化只影响局部块 —— 这正是固定 4MB 分块的失效场景
    （2026-10-26 实测：gzip 重打包后固定块命中率≈0）；
  - min/avg/max 约束与空数据边界。
"""
import os
import random

import pytest

from core import fastcdc

MIN, AVG, MAX = 4096, 16384, 65536


def _rand(n, seed=7):
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(n))


def test_empty_and_small_data():
    assert fastcdc.cut_points(b"") == []
    assert fastcdc.cut_points(b"abc", MIN, AVG, MAX) == [(0, 3)]


def test_chunks_cover_data_exactly():
    data = _rand(300 * 1024)
    parts = fastcdc.cut_points(data, MIN, AVG, MAX)
    assert sum(ln for _o, ln in parts) == len(data)
    off = 0
    for o, ln in parts:
        assert o == off
        off += ln


def test_chunk_sizes_respect_limits():
    data = _rand(400 * 1024, seed=11)
    parts = fastcdc.cut_points(data, MIN, AVG, MAX)
    assert all(MIN <= ln <= MAX for _o, ln in parts[:-1])
    # 末块允许小于 MIN


def test_deterministic_across_runs():
    data = _rand(200 * 1024, seed=13)
    a = fastcdc.cut_points(data, MIN, AVG, MAX)
    b = fastcdc.cut_points(bytes(data), MIN, AVG, MAX)
    assert a == b


def test_local_change_keeps_most_fingerprints():
    """头部变长插入（等价 gzip 重打包）只破坏局部块。"""
    body = _rand(512 * 1024, seed=21)
    a = b"HDR-v1\n" + body
    b = b"HDR-version-2-longer\n" + body

    ca = {c["fp"] for c in fastcdc.chunk_bytes(a, MIN, AVG, MAX)}
    cb = {c["fp"] for c in fastcdc.chunk_bytes(b, MIN, AVG, MAX)}
    reuse = len(ca & cb) / len(ca)
    assert reuse > 0.7, f"可变长块复用率过低: {reuse:.2f}"


def test_fixed_blocks_fail_on_shifted_content():
    """对照组：同样的数据用固定块，边界整体错位 → 复用率极低。"""
    body = _rand(512 * 1024, seed=21)
    a = b"HDR-v1\n" + body
    b = b"HDR-version-2-longer\n" + body
    fa = set(fastcdc.fixed_chunks(a, MIN))
    fb = set(fastcdc.fixed_chunks(b, MIN))
    assert len(fa & fb) / len(fa) < 0.3


def test_estimate_reuse_reports_both(tmp_path):
    body = _rand(256 * 1024, seed=31)
    pa = tmp_path / "a.bin"
    pb = tmp_path / "b.bin"
    pa.write_bytes(b"HEADER-1\n" + body)
    pb.write_bytes(b"HEADER-2-different\n" + body)

    rep = fastcdc.estimate_reuse(str(pa), str(pb), block_size=MIN,
                                 min_size=MIN, avg_size=AVG, max_size=MAX)
    assert rep["fixed"]["reuse"] <= rep["cdc"]["reuse"]
    assert rep["cdc"]["reuse"] > 0.6
    assert rep["scanned_bytes"] > 0
    assert rep["cdc"]["avg_chunk_kb"] > 0


def test_chunk_file(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(_rand(100 * 1024, seed=41))
    rep = fastcdc.chunk_file(str(p), min_size=MIN, avg_size=AVG, max_size=MAX)
    assert rep["size"] == 100 * 1024
    assert rep["truncated"] is False
    assert rep["chunks"] and all("fp" in c for c in rep["chunks"])


# ---------------------------------------------------------------------------
# Oracle 8KB 块对齐分块（方案 §7.3，块级备份产物）
# ---------------------------------------------------------------------------

def test_align_cut_points_exact_and_tail():
    pts = fastcdc.align_cut_points(8 * 1024 * 3 + 100, 8192)
    assert [ln for _o, ln in pts] == [8192, 8192, 8192, 100]
    assert pts[0][0] == 0 and pts[-1][0] == 8 * 1024 * 3


def test_align_cut_points_empty():
    assert fastcdc.align_cut_points(0, 8192) == []


def test_block_level_update_reuses_almost_everything():
    """块级产物只改 1 个 8KB 块 → 块对齐复用率应≈99%，优于 CDC 与固定块。"""
    block = fastcdc.ORACLE_BLOCK_SIZE
    base = bytearray(_rand(block * 200, seed=71))     # 200 个数据库块
    a = bytes(base)
    base[block * 100:block * 101] = _rand(block, seed=73)   # 只改中间一个块
    b = bytes(base)

    rep = fastcdc.compare_cutters(a, b, block_size=64 * 1024,
                                  min_size=16 * 1024, avg_size=64 * 1024,
                                  max_size=256 * 1024)
    assert rep["aligned"]["chunks"] == 200
    assert rep["aligned"]["reuse"] >= 0.99            # 199/200
    assert rep["aligned"]["reuse"] >= rep["fixed"]["reuse"]
    assert rep["aligned"]["reuse"] >= rep["cdc"]["reuse"]


def test_aligned_chunks_cover_all_bytes():
    data = _rand(50 * 1024 + 7, seed=79)
    chunks = fastcdc.chunk_bytes_aligned(data)
    assert sum(c["length"] for c in chunks) == len(data)
    assert [c["offset"] for c in chunks] == [c["offset"] for c in chunks]
