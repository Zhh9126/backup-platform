# -*- coding: utf-8 -*-
"""FastCDC 可变长分块（对标 PB 级方案 §7.1 的 Gear-hash 分块器）。

为什么需要它
------------
平台此前的「块级去重账本」用**固定 4MB 分块**：产物一旦被 gzip/zstd 重新
压缩，头部 mtime 变化会让其后所有字节整体移位，固定边界全部错位，跨轮
命中率趋近 0（2026-10-26 实测教训）。FastCDC 用内容定义边界（Gear 滚动
哈希 + 归一化掩码），插入/删除数据只影响局部块，能显著提升跨轮复用率。

实现取舍
--------
- 纯 Python（离线交付无新增原生依赖），吞吐约 10~30 MB/s；用于去重测算
  与中小产物分块，不参与备份主链路的实时切块。
- 掩码/阈值与 FastCDC 论文一致：min < normal < max，前段用严格掩码
  （更难切）、后段用宽松掩码（更易切），使块长集中在中位值附近。
- 指纹用 SHA-256 截断 16 字节（与既有 dedup 账本口径一致，避免引入新依赖）。
"""
import hashlib
import os

MASK64 = (1 << 64) - 1

DEFAULT_MIN_SIZE = 2 * 1024 * 1024      # 2 MB
DEFAULT_AVG_SIZE = 8 * 1024 * 1024      # 8 MB
DEFAULT_MAX_SIZE = 32 * 1024 * 1024     # 32 MB


def _build_gear_table():
    """splitmix64 生成 256 项 Gear 表（与方案 Rust 骨架同款常量）。"""
    table = []
    state = 0x853C49E6748FEA9B
    for _ in range(256):
        state = (state + 0x9E3779B97F4A7C15) & MASK64
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        table.append((z ^ (z >> 31)) & MASK64)
    return tuple(table)


GEAR = _build_gear_table()


def _mask_for(avg_size: int, harder_bits: int = 0) -> int:
    """按目标平均块长生成掩码：位数 = log2(avg) + harder_bits。"""
    bits = max(1, int(avg_size).bit_length() - 1)
    bits = min(bits + harder_bits, 48)
    return (1 << bits) - 1


def cut_points(data, min_size: int = DEFAULT_MIN_SIZE,
               avg_size: int = DEFAULT_AVG_SIZE,
               max_size: int = DEFAULT_MAX_SIZE):
    """返回分块边界 [(offset, length), ...]。

    ``min_size <= avg_size <= max_size`` 越界会被自动收敛；数据量小于
    min_size 时整块返回（不产生碎片）。
    """
    n = len(data)
    if n <= 0:
        return []
    min_size = max(64, int(min_size))
    avg_size = max(min_size, int(avg_size))
    max_size = max(avg_size, int(max_size))
    if n <= min_size:
        return [(0, n)]

    mask_s = _mask_for(avg_size, 2)   # 严格：min 之后不易切
    mask_l = _mask_for(avg_size, 0)   # 宽松：normal 之后更易切
    normal = min(avg_size, max_size)

    out = []
    pos = 0
    while pos < n:
        remain = n - pos
        if remain <= min_size:
            out.append((pos, remain))
            break
        end = min(pos + max_size, n)
        mid = min(pos + normal, end)
        cut = end
        h = 0
        i = pos + min_size
        while i < mid:
            h = ((h << 1) + GEAR[data[i]]) & MASK64
            if (h & mask_s) == 0:
                cut = i + 1
                break
            i += 1
        if cut == end:
            while i < end:
                h = ((h << 1) + GEAR[data[i]]) & MASK64
                if (h & mask_l) == 0:
                    cut = i + 1
                    break
                i += 1
        out.append((pos, cut - pos))
        pos = cut
    return out


def fingerprint(block: bytes) -> str:
    """块指纹：SHA-256 截断 16 字节（与既有去重账本口径一致）。"""
    return hashlib.sha256(block).hexdigest()[:32]


def chunk_bytes(data, min_size: int = DEFAULT_MIN_SIZE,
                avg_size: int = DEFAULT_AVG_SIZE,
                max_size: int = DEFAULT_MAX_SIZE):
    """对内存数据分块，返回 [{"offset","length","fp"}]。"""
    return [{"offset": off, "length": ln, "fp": fingerprint(data[off:off + ln])}
            for off, ln in cut_points(data, min_size, avg_size, max_size)]


def chunk_file(path: str, max_bytes: int = 256 * 1024 * 1024,
               **kw):
    """对文件分块（一次性读入，超过 max_bytes 只取前 max_bytes 字节）。

    纯 Python 实现，超大文件请用 ``estimate_reuse`` 的抽样模式。
    """
    with open(path, "rb") as f:
        data = f.read(max_bytes)
    chunks = chunk_bytes(data, **kw)
    return {"size": os.path.getsize(path), "scanned": len(data),
            "chunks": chunks, "truncated": os.path.getsize(path) > len(data)}


def fixed_chunks(data, block_size: int):
    """固定长度分块（作为对照基线）。"""
    return [fingerprint(data[o:o + block_size])
            for o in range(0, len(data), block_size)]


ORACLE_BLOCK_SIZE = 8 * 1024          # Oracle 默认数据块（DB_BLOCK_SIZE，常见 8KB）
SECTOR_ALIGN_SIZES = (4096, 8192, 16384)


def align_cut_points(size: int, block: int = ORACLE_BLOCK_SIZE):
    """按「数据库块」对齐切分（固定边界 = 数据库块边界）。

    适用对象：**块级**备份产物（RMAN 备份片、数据文件拷贝、卷级镜像）。
    数据库更新一行只会改写 1~2 个块，块对齐后跨版本复用率接近「未变块占比」，
    而内容定义分块（FastCDC）因为边界漂移会连带影响相邻块。

    对逻辑导出（expdp dmp / mysqldump SQL）不适用——它们不是块结构，
    仍应使用 FastCDC。
    """
    size = max(0, int(size))
    block = max(512, int(block))
    out = []
    off = 0
    while off < size:
        ln = min(block, size - off)
        out.append((off, ln))
        off += ln
    return out


def chunk_bytes_aligned(data, block: int = ORACLE_BLOCK_SIZE):
    """块对齐分块 + 指纹（返回结构与 chunk_bytes 一致）。"""
    return [{"offset": off, "length": ln, "fp": fingerprint(data[off:off + ln])}
            for off, ln in align_cut_points(len(data), block)]


def compare_cutters(data_a: bytes, data_b: bytes,
                    block_size: int = 4 * 1024 * 1024,
                    min_size: int = None,
                    avg_size: int = None,
                    max_size: int = None) -> dict:
    """三种切分方式在两版数据间的「可复用块占比」对比。

    用于判断某类产物该用哪种分块：块级备份（RMAN/数据文件）→ aligned；
    逻辑导出/压缩包 → cdc。

    ``avg_size`` 缺省时按数据大小自适应（size/8，夹在 256KB~4MB）——否则小产物
    会被整块吞掉（1 块 → 复用率恒 0），对比结果对 CDC 不公平（2026-11-20 实测）。
    """
    n = max(len(data_a or b""), len(data_b or b""))
    if not avg_size:
        avg_size = max(256 * 1024, min(4 * 1024 * 1024, max(1, n) // 8))
    if not min_size:
        min_size = max(64 * 1024, avg_size // 4)
    if not max_size:
        max_size = avg_size * 4

    def _reuse(fps_a, fps_b):
        sa, sb = set(fps_a), set(fps_b)
        return round(len(sa & sb) / len(sa), 4) if sa else 0.0

    fixed_a = fixed_chunks(data_a, block_size)
    fixed_b = fixed_chunks(data_b, block_size)

    cdc_a = [c["fp"] for c in chunk_bytes(data_a, min_size, avg_size, max_size)]
    cdc_b = [c["fp"] for c in chunk_bytes(data_b, min_size, avg_size, max_size)]

    al_a = [c["fp"] for c in chunk_bytes_aligned(data_a)]
    al_b = [c["fp"] for c in chunk_bytes_aligned(data_b)]

    return {
        "bytes": {"a": len(data_a), "b": len(data_b)},
        "fixed": {"block_size": block_size, "chunks": len(fixed_a),
                  "reuse": _reuse(fixed_a, fixed_b)},
        "cdc": {"avg_size": avg_size, "chunks": len(cdc_a),
                "reuse": _reuse(cdc_a, cdc_b)},
        "aligned": {"block_size": ORACLE_BLOCK_SIZE, "chunks": len(al_a),
                    "reuse": _reuse(al_a, al_b)},
        "note": ("块级备份产物（RMAN 备份片/数据文件）应优先 aligned；"
                 "逻辑导出与压缩包用 cdc。真实收益需用真机 RMAN 备份片验证。"),
    }


def estimate_reuse(path_a: str, path_b: str, block_size: int = 4 * 1024 * 1024,
                   max_bytes: int = 128 * 1024 * 1024, **kw):
    """对比「固定块」与「FastCDC 可变长块」在两次产物间的可复用比例。

    典型场景：同一源数据两次备份，产物被 gzip 重新打包（头部时间戳变化），
    固定块边界整体错位 → 复用率≈0；可变长块只损失被改动的局部。

    返回 dict：fixed_reuse / cdc_reuse（0~1）、两侧块数、扫描字节数。
    """
    with open(path_a, "rb") as f:
        a = f.read(max_bytes)
    with open(path_b, "rb") as f:
        b = f.read(max_bytes)

    fa = set(fixed_chunks(a, block_size))
    fb = set(fixed_chunks(b, block_size))
    fixed_reuse = (len(fa & fb) / len(fa)) if fa else 0.0

    ca = chunk_bytes(a, **kw)
    cb = chunk_bytes(b, **kw)
    sa = {c["fp"] for c in ca}
    sb = {c["fp"] for c in cb}
    cdc_reuse = (len(sa & sb) / len(sa)) if sa else 0.0

    return {
        "scanned_bytes": min(len(a), len(b)),
        "fixed": {"chunks_a": len(fa), "chunks_b": len(fb),
                  "reuse": round(fixed_reuse, 4)},
        "cdc": {"chunks_a": len(sa), "chunks_b": len(sb),
                "reuse": round(cdc_reuse, 4),
                "avg_chunk_kb": round(
                    (sum(c["length"] for c in ca) / len(ca) / 1024), 1) if ca else 0},
    }


def iter_file_chunks(stream, min_size=DEFAULT_MIN_SIZE, avg_size=DEFAULT_AVG_SIZE,
                     max_size=DEFAULT_MAX_SIZE):
    """有界窗口分块，与 cut_points 的整文件边界保持一致。"""
    min_size = max(64, int(min_size))
    avg_size = max(min_size, int(avg_size))
    max_size = max(avg_size, int(max_size))
    buffer = b""
    offset = 0
    while True:
        wanted = max_size - len(buffer)
        data = stream.read(wanted)
        buffer += data
        if not buffer:
            return
        final = len(data) < wanted
        cuts = cut_points(buffer, min_size, avg_size, max_size)
        selected = cuts if final or len(cuts) == 1 else cuts[:-1]
        consumed = 0
        for start, length in selected:
            yield offset + start, buffer[start:start + length]
            consumed = start + length
        offset += consumed
        buffer = buffer[consumed:]
        if final:
            return
