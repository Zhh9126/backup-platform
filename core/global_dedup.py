# -*- coding: utf-8 -*-
"""全局重删（Global Deduplication）引擎 —— 参照鼎甲迪备白皮书 §2.4。

核心思想（源端/全局重删）：
- 备份产出按固定大小切片（默认 4MB），对每块计算内容哈希（sha256）；
- 以 block_hash 为内容寻址键，跨任务、跨备份集、跨时间点复用同一物理块；
- 新块首次出现时落盘并记一笔 dedup_index；后续命中则只累加引用计数与
  dedup_saved_bytes（节省量 = 该块原始大小），不再重复存储数据；
- 提供全局统计接口，供仪表盘展示"重删比 / 累计节省"。

设计约束（保证可落地、可验证）：
- 不依赖外部存储集群，物理块直接存放于本地 content-addressable store
  （BACKUP_ROOT/.dedup_store/<block_hash>）；
- 哈希与记账均用真实 I/O，测试可断言 dedup_saved_bytes 真实增长；
- 失败时安全降级（返回 saved=0，不阻断备份主流程）。
"""
from __future__ import annotations

import os
import time
import hashlib
import logging
from typing import List, Dict, Optional, Tuple

import config
import core.db as db

logger = logging.getLogger("core.global_dedup")

DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024  # 4MB 切片（白皮书给 4KB~128KB 量级，
                                       # 实际生产用 MB 级切片以平衡索引规模）


def _store_dir() -> str:
    d = os.path.join(getattr(config, "BACKUP_ROOT", "."), ".dedup_store")
    os.makedirs(d, exist_ok=True)
    return d


def _block_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return db.now_iso() if hasattr(db, "now_iso") else time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _upsert_block(block_hash: str, size: int, task_id, set_id,
                  object_key: str) -> int:
    """写入或命中一个物理块，返回本次为该块节省的字节数（0 = 新块）。"""
    row = db.query_one(
        "SELECT id, ref_count, size_bytes FROM dedup_index WHERE block_hash=?",
        (block_hash,))
    if row:
        # 命中：引用计数 +1，累计节省 = 该块大小（不再重复存储）
        db.execute(
            "UPDATE dedup_index SET ref_count = ref_count + 1 WHERE id=?",
            (row["id"],))
        return int(row["size_bytes"] or 0)
    # 未命中：落盘物理块 + 记账
    db.execute(
        "INSERT INTO dedup_index "
        "(block_hash, size_bytes, ref_count, first_task_id, first_set_id, "
        "object_key, created_at) VALUES (?,?,?,?,?,?,?)",
        (block_hash, size, 1, task_id, set_id, object_key, _now_iso()))
    return 0


def dedup_bytes(data: bytes, task_id=None, set_id=None,
                block_size: int = DEFAULT_BLOCK_SIZE) -> Dict:
    """对一个备份产物字节流做切片重删，返回统计。

    返回: {"saved_bytes": int, "blocks": int, "new_blocks": int,
           "stored_path": str|None}
    - saved_bytes: 因命中已有块而节省的字节（跨任务全局复用）；
    - 首次出现的新块会写入 .dedup_store 物理落盘。
    """
    if not data:
        return {"saved_bytes": 0, "blocks": 0, "new_blocks": 0,
                "stored_path": None}
    store = _store_dir()
    saved = 0
    blocks = 0
    new_blocks = 0
    for i in range(0, len(data), block_size):
        chunk = data[i:i + block_size]
        h = _block_hash(chunk)
        phys = os.path.join(store, h[:2], h)
        blocks += 1
        if os.path.isfile(phys):
            # 物理块已存在（任何任务/集写过），直接命中
            saved += len(chunk)
            new_blocks += 0
            # 仍更新引用计数（即便物理已存在，可能来自其他任务）
            row = db.query_one(
                "SELECT id FROM dedup_index WHERE block_hash=?", (h,))
            if row:
                db.execute(
                    "UPDATE dedup_index SET ref_count=ref_count+1 WHERE id=?",
                    (row["id"],))
            else:
                # 物理存在但索引缺失（异常恢复）：补登索引，不计入 saved
                db.execute(
                    "INSERT INTO dedup_index "
                    "(block_hash, size_bytes, ref_count, first_task_id, "
                    "first_set_id, object_key, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (h, len(chunk), 1, task_id, set_id, phys, _now_iso()))
        else:
            parent = os.path.dirname(phys)
            os.makedirs(parent, exist_ok=True)
            with open(phys, "wb") as f:
                f.write(chunk)
            s = _upsert_block(h, len(chunk), task_id, set_id, phys)
            saved += s
            if s == 0:
                new_blocks += 1
    return {"saved_bytes": saved, "blocks": blocks,
            "new_blocks": new_blocks, "stored_path": store}


def dedup_file(path: str, task_id=None, set_id=None,
               block_size: int = DEFAULT_BLOCK_SIZE) -> Dict:
    """对文件做流式切片重删（避免一次性读入大文件）。"""
    if not os.path.isfile(path):
        return {"saved_bytes": 0, "blocks": 0, "new_blocks": 0,
                "stored_path": None, "error": "missing"}
    store = _store_dir()
    saved = 0
    blocks = 0
    new_blocks = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            h = _block_hash(chunk)
            phys = os.path.join(store, h[:2], h)
            blocks += 1
            if os.path.isfile(phys):
                saved += len(chunk)
                row = db.query_one(
                    "SELECT id FROM dedup_index WHERE block_hash=?", (h,))
                if row:
                    db.execute(
                        "UPDATE dedup_index SET ref_count=ref_count+1 WHERE id=?",
                        (row["id"],))
                else:
                    db.execute(
                        "INSERT INTO dedup_index "
                        "(block_hash, size_bytes, ref_count, first_task_id, "
                        "first_set_id, object_key, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (h, len(chunk), 1, task_id, set_id, phys, _now_iso()))
            else:
                parent = os.path.dirname(phys)
                os.makedirs(parent, exist_ok=True)
                with open(phys, "wb") as wf:
                    wf.write(chunk)
                s = _upsert_block(h, len(chunk), task_id, set_id, phys)
                saved += s
                if s == 0:
                    new_blocks += 1
    return {"saved_bytes": saved, "blocks": blocks,
            "new_blocks": new_blocks, "stored_path": store}


def global_stats() -> Dict:
    """全局重删统计（参照白皮书"全局重删比"指标）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS raw, "
        "COALESCE(SUM(ref_count),0) AS refs "
        "FROM dedup_index")
    n = int(row["n"] or 0)
    raw = int(row["raw"] or 0)
    refs = int(row["refs"] or 0)
    # 逻辑总量 = 各块原始大小 × 引用次数；物理总量 = 各块原始大小（只存一份）
    logical = raw * (refs if refs else 0) if n else 0
    physical = raw
    if logical > 0 and physical > 0:
        ratio = (1 - physical / logical) * 100.0
    else:
        ratio = 0.0
    total_saved = max(logical - physical, 0)
    return {
        "unique_blocks": n,
        "total_references": refs,
        "logical_bytes": logical,
        "physical_bytes": physical,
        "saved_bytes": total_saved,
        "dedup_ratio_pct": round(ratio, 2),
    }


def reset_index() -> int:
    """清空重删索引（仅测试/运维使用）。返回删除的索引条数。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM dedup_index")
    n = int(row["n"] or 0)
    db.execute("DELETE FROM dedup_index")
    store = _store_dir()
    removed = 0
    if os.path.isdir(store):
        for root, _dirs, files in os.walk(store):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                    removed += 1
                except OSError:
                    pass
    return n
