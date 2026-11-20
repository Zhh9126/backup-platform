# -*- coding: utf-8 -*-
"""Append-Only 容器封箱存储（对标 PB 级方案 §3.1 Sealed Container）。

问题
----
`chunk_store` 是"一块一个文件"的内容寻址布局：块平均 512KB~8MB，一个 1TB
备份会产生上百万个小文件，HDD 上全是随机写、inode 压力大、rsync/备份目录
遍历都极慢。

方案做法（本模块）
------------------
把块**追加**写进固定上限（默认 64MB）的容器文件，块的位置记在索引里：
- 写：只顺序追加，HDD 全程顺序 IO；写满即"封箱"（改名 .sealed）后开新箱；
- 读：按 (容器, 偏移, 长度) 定位，连续块可一次预读；
- 复用：索引 fp→位置，同内容块只写一次（与 chunk_store 同一套指纹口径）。

布局
----
``containers/<cid>.bin``        容器数据（追加写，写满封箱）
``containers/index.json``       块索引 {fp: {cid, offset, size, comp, raw_size}}
``manifests/<id>.json``         清单（每块带 container / container_offset）

边界
----
- 单进程追加（线程锁保护）；容器不就地压缩整理，空间回收靠整箱判断
  （``gc`` 只清索引条目并报告容器占用，不搬移数据）。
- 纯 Python 实现，吞吐与 chunk_store 同量级（瓶颈在 FastCDC + 压缩）。
"""
import json
import hashlib
import io
import re
import os
import threading
import time
import uuid

from core import chunk_store, fastcdc

DEFAULT_CONTAINER_MAX = 64 * 1024 * 1024     # 64MB 封箱
_LOCK = threading.RLock()

_INDEX_NAME = "index.json"


def _root(store_root: str = "") -> str:
    base = chunk_store._root(store_root)     # noqa: SLF001 - 同一存储根
    return os.path.join(base, "containers")


def _index_path(root: str) -> str:
    return os.path.join(root, _INDEX_NAME)


def _load_index(root: str) -> dict:
    p = _index_path(root)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            index = json.load(f)
            if not isinstance(index, dict) or any(
                    not isinstance(loc, dict) or not all(k in loc for k in
                        ("cid", "offset", "size", "comp", "raw_size"))
                    for loc in index.values()):
                raise ValueError("容器索引结构无效")
            return index
    except (OSError, ValueError) as exc:
        raise ValueError("容器索引损坏，停止修改并保留数据") from exc


def _save_index(root: str, idx: dict):
    os.makedirs(root, exist_ok=True)
    chunk_store._atomic_write(_index_path(root), json.dumps(idx).encode("utf-8"))


def _sorted_containers(root: str):
    """全部容器 ID（含已封箱的 .sealed），按名称升序。"""
    if not os.path.isdir(root):
        return []
    out = []
    for fn in os.listdir(root):
        if fn.endswith((".bin", ".sealed")):
            out.append(fn.rsplit(".", 1)[0])
    return sorted(set(out))


def _active_container(root: str):
    """当前可追加的活动容器（只认 .bin；封箱后不再写入）。"""
    if not os.path.isdir(root):
        return None
    bins = sorted(fn[:-4] for fn in os.listdir(root) if fn.endswith(".bin"))
    return bins[-1] if bins else None


def _new_container(root: str) -> str:
    cid = "%s_%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6])
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, cid + ".bin"), "wb"):
        pass
    return cid


def _append_blob(root: str, blob: bytes, container_max: int) -> tuple:
    """把块追加到活动容器，返回 (cid, offset)。写满则封箱开新箱。"""
    cid = _active_container(root)
    cpath = os.path.join(root, cid + ".bin") if cid else None
    if not cid or not os.path.isfile(cpath) or os.path.getsize(cpath) + len(blob) > container_max:
        if cid and os.path.isfile(cpath):
            # 封箱：改名标记（读取按 index 定位，.bin/.sealed 都能读）
            try:
                os.replace(cpath, os.path.join(root, cid + ".sealed"))
                cpath = None
            except OSError:
                pass
        cid = _new_container(root)
        cpath = os.path.join(root, cid + ".bin")
    offset = os.path.getsize(cpath)
    with open(cpath, "ab") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    return cid, offset


def _read_blob(root: str, cid: str, offset: int, size: int) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", cid or ""):
        raise ValueError("非法容器标识")
    if int(offset) < 0 or int(size) <= 0:
        raise ValueError("容器块边界无效")
    for ext in (".bin", ".sealed"):
        p = os.path.join(root, cid + ext)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                f.seek(int(offset))
                data = f.read(int(size))
            if len(data) != int(size):
                raise ValueError("容器块长度不符（%s@%d 期望 %d 实际 %d）"
                                 % (cid, offset, size, len(data)))
            return data
    raise FileNotFoundError("容器不存在: %s" % cid)


@chunk_store.coordinated
def store_file(path: str, store_root: str = "", chunk_opts: dict = None,
               container_max: int = DEFAULT_CONTAINER_MAX,
               max_bytes: int = 0) -> dict:
    """按 CDC 分块 → 逐块压缩 → 追加进容器 → 写清单。返回存储摘要。"""
    root = _root(store_root)
    size = os.path.getsize(path)
    opts = chunk_store.auto_chunk_opts(size)
    if chunk_opts:
        opts.update(chunk_opts)
    source_hash = hashlib.sha256()
    with _LOCK:
        idx = _load_index(root)
        chunks = []
        new_blocks = reused = 0
        raw_total = comp_total = 0
        t0 = time.time()
        for off, block in chunk_store.source_blocks(path, opts, max_bytes):
            ln = len(block)
            source_hash.update(block)
            fp = chunk_store._sha_bytes(block)
            loc = idx.get(fp)
            if loc:
                try:
                    saved = _read_blob(root, loc["cid"], loc["offset"], loc["size"])
                    if chunk_store._sha_bytes(chunk_store._decompress(loc["comp"], saved)) != fp:
                        loc = None
                except (OSError, ValueError, KeyError):
                    loc = None
            if loc:
                reused += 1
            else:
                kind, blob = chunk_store._compress(block)   # noqa: SLF001
                cid, coff = _append_blob(root, blob, container_max)
                loc = {"cid": cid, "offset": coff, "size": len(blob),
                       "comp": kind, "raw_size": ln}
                idx[fp] = loc
                new_blocks += 1
                comp_total += len(blob)
            raw_total += ln
            chunks.append({"fp": fp, "offset": off, "raw_size": ln,
                           "comp_size": loc["size"], "comp": loc["comp"],
                           "container": loc["cid"],
                           "container_offset": loc["offset"]})
        # 复用块的压缩体积要计入总量（用于压缩比展示）
        comp_total = sum(c["comp_size"] for c in chunks)
        _save_index(root, idx)

    if raw_total != size:
        raise ValueError("入库期间源文件长度发生变化，清单未发布")
    mid = "%s_%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    manifest = {
        "id": mid, "source_name": os.path.basename(path), "source_size": size,
        "source_sha256": source_hash.hexdigest(), "chunk_opts": opts, "chunks": chunks,
        "raw_total": raw_total, "comp_total": comp_total,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 2),
        "storage_mode": "container", "container_max": container_max,
    }
    mp = os.path.join(chunk_store._root(store_root), "manifests", mid + ".json")  # noqa: SLF001
    chunk_store._atomic_write(mp, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))  # noqa: SLF001
    return {"ok": True, "manifest_id": mid, "chunks": len(chunks),
            "new_blocks": new_blocks, "reused_blocks": reused,
            "raw_bytes": raw_total, "comp_bytes": comp_total,
            "ratio": round(comp_total / raw_total, 3) if raw_total else 0,
            "containers": len([c for c in _sorted_containers(root)]),
            "manifest_path": mp}


@chunk_store.coordinated
def restore_file(manifest_id: str, out_path: str, store_root: str = "",
                 verify: bool = True) -> dict:
    man = chunk_store.load_manifest(manifest_id, store_root)
    root = _root(store_root)
    try:
        index = _load_index(root)
    except ValueError:
        index = {}
    def read(chunk):
        location = index.get(chunk["fp"])
        if location:
            return io.BytesIO(_read_blob(root, location["cid"], location["offset"], location["size"]))
        return io.BytesIO(_read_blob(root, chunk.get("container") or "",
                                    chunk.get("container_offset") or 0,
                                    chunk.get("comp_size") or 0))
    result = chunk_store.restore_manifest(man, manifest_id, out_path, read, verify)
    result["storage_mode"] = man.get("storage_mode") or "container"
    return result


@chunk_store.coordinated
def stats(store_root: str = "") -> dict:
    root = _root(store_root)
    idx = _load_index(root)
    total = 0
    containers = 0
    for cid in _sorted_containers(root):
        for ext in (".bin", ".sealed"):
            p = os.path.join(root, cid + ext)
            if os.path.isfile(p):
                total += os.path.getsize(p)
                containers += 1
                break
    return {"store_root": root, "blocks_indexed": len(idx),
            "containers": containers, "physical_bytes": total}


@chunk_store.coordinated
def gc(store_root: str = "", dry_run: bool = True) -> dict:
    """清理索引中「没有任何清单引用」的块条目。

    容器数据不做搬移（Append-Only 的代价）：只有当一个容器内的所有块条目
    都被清理后才可能整箱回收——这里只报告可回收箱数，不擅自删文件。
    """
    from core import chunk_store as cs
    root = _root(store_root)
    mdir = os.path.join(cs._root(store_root), "manifests")    # noqa: SLF001
    referenced = set()
    manifests = 0
    bad_manifests = 0
    live_containers = set()
    if os.path.isdir(mdir):
        for fn in os.listdir(mdir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(mdir, fn), "r", encoding="utf-8") as f:
                    m = json.load(f)
                cs.validate_manifest(m)
                for c in m.get("chunks") or []:
                    if c.get("container") and c.get("fp"):
                        referenced.add(c["fp"])
                        live_containers.add(c["container"])
                manifests += 1
            except (OSError, ValueError, TypeError, AttributeError):
                bad_manifests += 1
    idx = _load_index(root)
    live_containers.update(loc["cid"] for fp, loc in idx.items() if fp in referenced)
    orphan = [fp for fp in idx if fp not in referenced]
    by_container = {}
    for fp in orphan:
        by_container.setdefault(idx[fp]["cid"], 0)
        by_container[idx[fp]["cid"]] += idx[fp]["size"]
    reclaimable = sum(by_container.values())

    deleted = 0
    if not dry_run and orphan and not bad_manifests:
        for fp in orphan:
            idx.pop(fp, None)
            deleted += 1
        _save_index(root, idx)

    return {"ok": not bad_manifests, "dry_run": bool(dry_run), "manifests": manifests,
            "bad_manifests": bad_manifests, "blocked": bool(bad_manifests),
            "blocks_indexed": len(idx) + (deleted if not dry_run else 0),
            "orphan_blocks": len(orphan), "deleted_entries": deleted,
            "reclaimable_bytes": reclaimable,
            "orphan_containers": len(set(_sorted_containers(root)) - live_containers),
            "note": "容器数据 Append-Only，不就地搬移；整箱全孤儿时可按 "
                    "orphan_containers 人工回收"}
