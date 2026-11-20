# -*- coding: utf-8 -*-
"""跨站点块级按需传输（对标 PB 级方案 §4.1 / §4.2）。

目标
----
跨中心复制时不要"重传整包"，而是先问对端「这些块你有没有」，
只传缺失块；连续缺失块再合并成区间下发（等价方案的 RANGE_CLONE，
方向相反：我们传变化区间，未变区间只用一条指令跳过）。

协议雏形（两步）
----------------
1. **指纹探路**：源端把目标版本的块指纹按 ``BATCH``（默认 16384）分批
   发给对端，对端返回**位图**（1=已有，0=缺失）。
   一次性发百万指纹会撑爆请求体，分批 + 位图是关键（方案称"宏批次"）。
2. **区间传输**：把缺失块按在清单中的顺序合并成 ``[start, count]`` 区间，
   逐区间读取块并发送（已压缩/加密的块**直通**，全程不解压——对齐 §4.3）。

本模块只做**计划与位图计算**（纯逻辑，可在两端独立验证），实际发送由
调用方完成——这样单测可以完全覆盖协议正确性，不依赖网络。
"""
from core import chunk_store

BATCH = 16384          # 单次探路指纹数（方案：约 512KB 请求）
RANGE_MERGE_MIN = 1    # 连续缺失块 >= 该值才合并成区间


def have_bitmap(have_fps, wanted_fps):
    """对端已有的块集合 → 缺失位图。

    返回 dict：``bitmap`` 为 "0101..." 字符串（1=对端已有，0=需要传），
    ``missing_fps`` 为缺失块指纹列表。位图比指纹列表小得多（每块 1 bit），
    高延迟链路上能显著降低响应体积。
    """
    have = set(have_fps or ())
    bits = []
    missing = []
    for fp in (wanted_fps or []):
        hit = fp in have
        bits.append("1" if hit else "0")
        if not hit:
            missing.append(fp)
    return {"bitmap": "".join(bits), "missing_fps": missing,
            "wanted": len(bits), "missing": len(missing),
            "hit_ratio": round((len(bits) - len(missing)) / len(bits), 4) if bits else 0}


def batch_fingerprints(fps, batch: int = BATCH):
    """把指纹列表切成探路批次（方案：单批 16384 个 ≈ 512KB）。"""
    fps = list(fps or [])
    batch = max(1, int(batch or BATCH))
    return [fps[i:i + batch] for i in range(0, len(fps), batch)]


def merge_missing_ranges(ordered_fps, missing_fps):
    """把缺失块合并成「按清单顺序的连续区间」。

    ``ordered_fps`` 是目标版本清单里的块顺序（list），``missing_fps`` 是缺失
    集合。返回 ``[{"start": i, "count": n, "fps": [...]}]``：同一区间内的块
    是连续读取的，可以一次 IO / 一个 TCP 段发完，不必逐块建连。
    """
    missing = set(missing_fps or ())
    ranges = []
    cur = None
    for idx, fp in enumerate(ordered_fps or []):
        if fp in missing:
            if cur and cur["start"] + cur["count"] == idx:
                cur["count"] += 1
                cur["fps"].append(fp)
            else:
                cur = {"start": idx, "count": 1, "fps": [fp]}
                ranges.append(cur)
        else:
            cur = None
    return ranges


def plan_transfer(source_manifest_id: str, target_manifest_id: str,
                  store_root: str = "", batch: int = BATCH) -> dict:
    """生成「源 → 目标」的增量传输计划。

    两个 manifest 都来自本机块存储（用于验证/演练）；真实跨站点场景下，
    目标端只需返回 ``have_bitmap`` 的位图，本函数负责把缺失块合并成区间。
    """
    src = chunk_store.load_manifest(source_manifest_id, store_root)
    tgt = chunk_store.load_manifest(target_manifest_id, store_root)

    src_fps = [c["fp"] for c in src["chunks"]]
    tgt_fps = [c["fp"] for c in tgt["chunks"]]
    by_fp = {c["fp"]: c for c in src["chunks"]}

    # 目标端"已有"= 源端也有的块（内容寻址下同指纹即同内容）
    res = have_bitmap(set(src_fps), tgt_fps)
    miss = res["missing_fps"]
    ranges = merge_missing_ranges(tgt_fps, miss)

    transfer_bytes = sum(by_fp[fp]["comp_size"] for fp in miss if fp in by_fp)
    total_bytes = sum(c["comp_size"] for c in tgt["chunks"])
    batches = len(batch_fingerprints(tgt_fps, batch))

    return {
        "ok": True,
        "source_manifest": source_manifest_id,
        "target_manifest": target_manifest_id,
        "chunks_total": len(tgt_fps),
        "chunks_reusable": len(tgt_fps) - len(miss),
        "chunks_missing": len(miss),
        "reuse_ratio": round((len(tgt_fps) - len(miss)) / len(tgt_fps), 4) if tgt_fps else 0,
        "target_total_comp_bytes": total_bytes,
        "transfer_comp_bytes": transfer_bytes,
        "saved_pct": round((1 - transfer_bytes / total_bytes) * 100, 1) if total_bytes else 0,
        "probe_batches": batches,
        "probe_batch_size": batch,
        "bitmap": res["bitmap"][:4096],       # 预览；完整位图由 have_bitmap 返回
        "ranges": ranges,
        "missing_fps": miss,
        "note": ("缺失块按清单顺序合并为区间：区间内连续读取、块直通（不解压/不解密）；"
                 "未变块只用位图标记，不产生数据传输"),
    }


def apply_bitmap(target_fps, bitmap: str, source_chunk_map: dict) -> dict:
    """根据对端返回的位图生成区间计划（真实跨站点路径）。

    ``source_chunk_map``: {fp: chunk_meta}，用于估算传输体积。
    """
    bits = str(bitmap or "")
    target_fps = list(target_fps or [])
    if len(bits) != len(target_fps):
        raise ValueError("位图长度与块数不符（%d vs %d）" % (len(bits), len(target_fps)))
    missing = [fp for fp, b in zip(target_fps, bits) if b != "1"]
    ranges = merge_missing_ranges(target_fps, missing)
    transfer = sum((source_chunk_map.get(fp) or {}).get("comp_size", 0) for fp in missing)
    total = sum((source_chunk_map.get(fp) or {}).get("comp_size", 0) for fp in target_fps)
    return {
        "chunks_total": len(target_fps), "chunks_missing": len(missing),
        "ranges": ranges, "missing_fps": missing,
        "transfer_comp_bytes": transfer, "target_total_comp_bytes": total,
        "saved_pct": round((1 - transfer / total) * 100, 1) if total else 0,
    }
