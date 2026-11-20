# -*- coding: utf-8 -*-
"""分块压缩内容寻址存储（对标 PB 级方案 §3.1 Container + §4 增量复制）。

为什么需要它（上一轮实测暴露的真问题）
--------------------------------------
备份产物通常是**整包压缩**（.tar.gz / .sql.zst）。整包压缩有两个硬伤：
  1. 局部数据变化 → 压缩流大范围重编码，跨版本可复用性极差；
  2. 单点损坏 → 整包不可用。
实测（2026-11-19）：同数据两次 gzip 打包（仅头部 mtime 4 字节不同），
固定块复用 96.3%、FastCDC 80%；一旦源数据有真实变化，两者都急剧下降。

本模块把「分块」和「压缩」顺序倒过来：先按内容切块（FastCDC），再**逐块
独立压缩**，块按内容哈希寻址。效果：
  - 局部变化只产生少量新块（其余块命中复用）；
  - 单块损坏只影响该块，可精确定位；
  - 跨版本增量可只传/只存新块（对齐方案 §4「密文块直通」的落地前提）。

布局
----
``<store_root>/blobs/<hash[:2]>/<hash>``   独立压缩块（默认 zstd，回退 zlib）
``<store_root>/manifests/<id>.json``       有序块清单（可重建原文件）

边界（如实）
------------
- 纯 Python 实现，吞吐约 10~30 MB/s（FastCDC 逐字节 + 逐块压缩）；
  用于存储层与增量测算，不阻塞既有备份主链路（默认不启用）。
- 块内压缩不做跨块字典，压缩率略低于整包（换来的是可复用性），
  实际取舍由 ``estimate_vs_whole()`` 量化后由运维决定。
"""
import hashlib
import json
import re
import tempfile
import os
import shutil
import time
import uuid
import zlib

from core import fastcdc
from core.store_lock import coordinated

DEFAULT_SUBDIR = "chunk_store"
DEFAULT_CHUNK = {"min_size": 1024 * 1024, "avg_size": 4 * 1024 * 1024,
                 "max_size": 16 * 1024 * 1024}


def auto_chunk_opts(size: int) -> dict:
    """按文件大小自适应分块参数。

    固定 min/avg/max 对低熵数据（WAL、重复页、日志）会导致 Gear 哈希长时间
    不命中而退化成"一整块"，跨版本复用立刻失效。这里让平均块长随体积缩放
    （size/8，夹在 512KB~8MB），保证大产物也能切出足够多的块。
    """
    size = max(1, int(size))
    avg = max(512 * 1024, min(8 * 1024 * 1024, size // 8))
    return {"min_size": max(128 * 1024, avg // 4),
            "avg_size": avg,
            "max_size": avg * 4}


def _root(store_root: str = "") -> str:
    if store_root:
        return os.path.join(store_root, DEFAULT_SUBDIR)
    try:
        import config
        base = getattr(config, "BACKUP_ROOT", "") or os.getcwd()
    except Exception:
        base = os.getcwd()
    return os.path.join(base, DEFAULT_SUBDIR)


def _compress(data: bytes):
    """块压缩：优先 zstd（离线包已含 zstandard），回退 zlib。"""
    try:
        import zstandard as zstd
        c = zstd.ZstdCompressor(level=3)
        return "zstd", c.compress(data)
    except Exception:
        return "zlib", zlib.compress(data, 6)


def _codec(blob):
    return "zstd" if blob.startswith(b"\x28\xb5\x2f\xfd") else "zlib"


def _decompress(kind: str, blob: bytes) -> bytes:
    # 已有块的真实编码优先，兼容旧清单与发布期间压缩算法切换。
    kind = _codec(blob)
    try:
        if kind == "zstd":
            import zstandard as zstd
            return zstd.ZstdDecompressor().decompress(blob)
        return zlib.decompress(blob)
    except Exception as exc:
        raise ValueError("块解压失败") from exc


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str, buf: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _blob_path(root: str, fp: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", fp or ""):
        raise ValueError("非法块指纹")
    return os.path.join(root, "blobs", fp[:2], fp)


def _atomic_write(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%s" % uuid.uuid4().hex
    try:
        with open(tmp, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        descriptor = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def source_blocks(path, opts, max_bytes):
    before = os.stat(path)
    if max_bytes and before.st_size > int(max_bytes):
        raise ValueError("文件超过单次分块上限（%d 字节）" % max_bytes)
    with open(path, "rb") as stream:
        yield from fastcdc.iter_file_chunks(stream, **opts)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("入库期间源文件发生变化，清单未发布")


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("chunks"), list):
        raise ValueError("块清单结构无效")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("source_sha256") or "")):
        raise ValueError("清单缺少有效的整体摘要")
    offset = 0
    for chunk in manifest["chunks"]:
        if not isinstance(chunk, dict):
            raise ValueError("清单包含无效块")
        _blob_path("", chunk.get("fp"))
        if chunk.get("offset") != offset or not isinstance(chunk.get("raw_size"), int) or chunk["raw_size"] <= 0:
            raise ValueError("清单块边界无效")
        offset += chunk["raw_size"]
    if offset != manifest.get("source_size"):
        raise ValueError("清单总长度不符")
    return manifest


def has_block(fp: str, store_root: str = "") -> bool:
    """本端是否已存在该块（供跨站点「指纹探路」应答使用）。"""
    path = _blob_path(_root(store_root), str(fp))
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as stream:
            return _sha_bytes(_decompress("", stream.read())) == fp
    except (OSError, ValueError):
        return False


@coordinated
def store_file(path: str, store_root: str = "", chunk_opts: dict = None,
               max_bytes: int = 0) -> dict:
    """把一个文件按 CDC 分块、逐块压缩后写入内容寻址存储。

    返回 manifest 摘要（含新增/复用块数）。已存在的块直接复用（不重复写盘）。
    """
    root = _root(store_root)
    size = os.path.getsize(path)
    # 未显式指定时按体积自适应，避免低熵大文件被切成"一整块"
    opts = auto_chunk_opts(size) if not chunk_opts else dict(auto_chunk_opts(size))
    if chunk_opts:
        opts.update(chunk_opts)
    source_hash = hashlib.sha256()
    chunks = []
    new_blocks = reused = 0
    raw_total = comp_total = 0
    t0 = time.time()
    for off, block in source_blocks(path, opts, max_bytes):
        ln = len(block)
        source_hash.update(block)
        fp = _sha_bytes(block)
        bp = _blob_path(root, fp)
        valid = False
        if os.path.isfile(bp):
            try:
                with open(bp, "rb") as existing:
                    blob = existing.read()
                kind = _codec(blob)
                valid = _sha_bytes(_decompress(kind, blob)) == fp
            except Exception:
                valid = False
        if valid:
            reused += 1
        else:
            kind, blob = _compress(block)
            _atomic_write(bp, blob)
            new_blocks += 1
        raw_total += ln
        comp_total += len(blob)
        chunks.append({"fp": fp, "offset": off, "raw_size": ln,
                       "comp_size": len(blob), "comp": kind})

    if raw_total != size:
        raise ValueError("入库期间源文件长度发生变化，清单未发布")
    mid = "%s_%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    manifest = {
        "id": mid,
        "source_name": os.path.basename(path),
        "source_size": size,
        "source_sha256": source_hash.hexdigest(),
        "chunk_opts": opts,
        "chunks": chunks,
        "raw_total": raw_total,
        "comp_total": comp_total,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    mp = os.path.join(root, "manifests", mid + ".json")
    _atomic_write(mp, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    return {
        "ok": True, "manifest_id": mid, "chunks": len(chunks),
        "new_blocks": new_blocks, "reused_blocks": reused,
        "raw_bytes": raw_total, "comp_bytes": comp_total,
        "ratio": round(comp_total / raw_total, 3) if raw_total else 0,
        "manifest_path": mp,
    }


def load_manifest(manifest_id: str, store_root: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(manifest_id)):
        raise ValueError("非法清单标识")
    mp = os.path.join(_root(store_root), "manifests", manifest_id + ".json")
    with open(mp, "r", encoding="utf-8") as f:
        return validate_manifest(json.load(f))


@coordinated
def restore_file(manifest_id: str, out_path: str, store_root: str = "",
                 verify: bool = True) -> dict:
    man = load_manifest(manifest_id, store_root)
    root = _root(store_root)
    return restore_manifest(man, manifest_id, out_path,
                            lambda chunk: open(_blob_path(root, chunk["fp"]), "rb"),
                            verify)


def restore_manifest(man, manifest_id, out_path, reader, verify=True):
    """逐块校验后原子发布，失败时保留目标原文件。reader 返回上下文管理的字节流。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aidbm-restore-", dir=os.path.dirname(os.path.abspath(out_path)))
    digest = hashlib.sha256()
    written = 0
    missing, corrupt = [], []
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in man["chunks"]:
                try:
                    with reader(chunk) as stream:
                        raw = _decompress(chunk.get("comp") or "zlib", stream.read())
                    if len(raw) != chunk["raw_size"] or _sha_bytes(raw) != chunk["fp"]:
                        raise ValueError("块长度或 SHA256 不匹配")
                    out.write(raw)
                    digest.update(raw)
                    written += len(raw)
                except FileNotFoundError:
                    missing.append(chunk["fp"])
                except Exception as exc:
                    corrupt.append({"fp": chunk["fp"], "error": str(exc)[:120]})
            out.flush()
            os.fsync(out.fileno())
        sha_ok = digest.hexdigest() == man["source_sha256"]
        ok = not missing and not corrupt and written == man["source_size"] and (sha_ok or not verify)
        if ok:
            os.replace(temporary, out_path)
        return {"ok": bool(ok), "manifest_id": manifest_id, "out_path": out_path,
                "bytes": written, "expected_bytes": man["source_size"],
                "sha256_match": sha_ok, "missing_blocks": missing,
                "corrupt_blocks": corrupt, "verified": verify}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def diff_manifests(a_id: str, b_id: str, store_root: str = "") -> dict:
    """两版 manifest 的块差异（跨版本增量可只传 b 独有的块）。"""
    a = load_manifest(a_id, store_root)
    b = load_manifest(b_id, store_root)
    sa = [c["fp"] for c in a["chunks"]]
    sb = [c["fp"] for c in b["chunks"]]
    set_a, set_b = set(sa), set(sb)
    only_b = set_b - set_a
    only_a = set_a - set_b
    common = set_a & set_b
    by_fp = {c["fp"]: c for c in b["chunks"]}
    transfer_bytes = sum(by_fp[fp]["comp_size"] for fp in only_b)
    return {
        "a": a_id, "b": b_id,
        "chunks_a": len(sa), "chunks_b": len(sb),
        "common": len(common), "only_in_a": len(only_a), "only_in_b": len(only_b),
        "reuse_ratio": round(len(common) / len(set_b), 4) if set_b else 0,
        "b_total_comp_bytes": b["comp_total"],
        "incremental_comp_bytes": transfer_bytes,
        "note": "跨版本增量只需传 only_in_b 的块（密文/压缩块可直通，无需解压）",
    }


@coordinated
def stats(store_root: str = "") -> dict:
    root = _root(store_root)
    blobs_dir = os.path.join(root, "blobs")
    count = 0
    total = 0
    if os.path.isdir(blobs_dir):
        for sub in os.listdir(blobs_dir):
            p = os.path.join(blobs_dir, sub)
            if not os.path.isdir(p):
                continue
            for fn in os.listdir(p):
                fp = os.path.join(p, fn)
                if os.path.isfile(fp):
                    count += 1
                    total += os.path.getsize(fp)
    mans = 0
    mdir = os.path.join(root, "manifests")
    if os.path.isdir(mdir):
        mans = len([f for f in os.listdir(mdir) if f.endswith(".json")])
    return {"store_root": root, "blocks": count, "manifests": mans,
            "physical_bytes": total}


def estimate_vs_whole(path: str, store_root: str = "",
                      chunk_opts: dict = None) -> dict:
    """量化「整包压缩」与「分块压缩」的体积差异（同一次输入）。

    分块压缩率通常略低于整包（块内无跨块字典），换来的是跨版本可复用性。
    """
    opts = dict(DEFAULT_CHUNK)
    opts.update(chunk_opts or {})
    with open(path, "rb") as f:
        data = f.read()
    whole_kind, whole_blob = _compress(data)
    cuts = fastcdc.cut_points(data, opts["min_size"], opts["avg_size"],
                             opts["max_size"])
    per_block = 0
    for off, ln in cuts:
        _, blob = _compress(data[off:off + ln])
        per_block += len(blob)
    return {
        "raw_bytes": len(data),
        "whole_comp": {"kind": whole_kind, "bytes": len(whole_blob),
                       "ratio": round(len(whole_blob) / max(1, len(data)), 3)},
        "chunked_comp": {"chunks": len(cuts), "bytes": per_block,
                         "ratio": round(per_block / max(1, len(data)), 3)},
        "overhead_pct": round((per_block - len(whole_blob)) /
                              max(1, len(whole_blob)) * 100, 1),
    }


def list_manifests(store_root: str = "", limit: int = 50) -> list:
    mdir = os.path.join(_root(store_root), "manifests")
    if not os.path.isdir(mdir):
        return []
    out = []
    for fn in sorted(os.listdir(mdir), reverse=True)[:limit]:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(mdir, fn), "r", encoding="utf-8") as f:
                m = json.load(f)
            out.append({"id": m.get("id"), "source_name": m.get("source_name"),
                        "source_size": m.get("source_size"),
                        "chunks": len(m.get("chunks") or []),
                        "comp_total": m.get("comp_total"),
                        "created_at": m.get("created_at")})
        except (OSError, ValueError):
            continue
    return out


@coordinated
def gc(store_root: str = "", dry_run: bool = True,
       min_age_hours: float = 0) -> dict:
    """回收「没有任何 manifest 引用」的块。

    内容寻址存储只增不减会把磁盘吃满，必须有回收：先全量扫描 manifest 收集
    引用集，再比对 blobs 目录。默认 ``dry_run=True``（只报数不动盘），运维确认
    后再执行实删。存在损坏清单时停止删除，保留所有可能仍被引用的块。

    ``min_age_hours``：只回收「最后修改时间早于该宽限期」的孤儿块。定时任务
    用它兜底（默认 168h），避免删除"正在写但清单还没落盘"的块。
    """
    root = _root(store_root)
    mdir = os.path.join(root, "manifests")
    bdir = os.path.join(root, "blobs")

    referenced = set()
    bad_manifests = 0
    manifests = 0
    if os.path.isdir(mdir):
        for fn in os.listdir(mdir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(mdir, fn), "r", encoding="utf-8") as f:
                    m = json.load(f)
                validate_manifest(m)
                for c in m.get("chunks") or []:
                    fp = c.get("fp")
                    if fp:
                        referenced.add(fp)
                manifests += 1
            except (OSError, ValueError, TypeError, AttributeError):
                bad_manifests += 1

    scanned = 0
    unreferenced = []
    reclaimed = 0
    if os.path.isdir(bdir):
        for sub in os.listdir(bdir):
            p = os.path.join(bdir, sub)
            if not os.path.isdir(p):
                continue
            for fn in os.listdir(p):
                fp = os.path.join(p, fn)
                if not os.path.isfile(fp):
                    continue
                scanned += 1
                if fn not in referenced:
                    if min_age_hours:
                        try:
                            if (time.time() - os.path.getmtime(fp)) < \
                                    float(min_age_hours) * 3600:
                                continue    # 宽限期内：可能是正在写入的块
                        except OSError:
                            continue
                    try:
                        reclaimed += os.path.getsize(fp)
                    except OSError:
                        pass
                    unreferenced.append(fp)

    deleted = 0
    reclaimed_actual = 0
    if not dry_run and not bad_manifests:
        for fp in unreferenced:
            try:
                amount = os.path.getsize(fp)
                os.remove(fp)
                deleted += 1
                reclaimed_actual += amount
            except OSError:
                pass
        # 清掉空的二级目录
        if os.path.isdir(bdir):
            for sub in os.listdir(bdir):
                sp = os.path.join(bdir, sub)
                try:
                    if os.path.isdir(sp) and not os.listdir(sp):
                        os.rmdir(sp)
                except OSError:
                    pass

    return {
        "ok": not bad_manifests, "dry_run": bool(dry_run),
        "blocked": bool(bad_manifests),
        "manifests": manifests, "bad_manifests": bad_manifests,
        "blocks_scanned": scanned, "referenced_blocks": len(referenced),
        "unreferenced_blocks": len(unreferenced),
        "reclaimable_bytes": reclaimed, "reclaimed_bytes": reclaimed_actual,
        "deleted_blocks": deleted,
        "store_root": root,
    }


@coordinated
def purge(store_root: str = "") -> dict:
    """清空分块存储（运维动作，测试/回收用）。"""
    root = _root(store_root)
    deleted = 0
    for sub in ("blobs", "manifests"):
        p = os.path.join(root, sub)
        if os.path.isdir(p):
            for name in os.listdir(p):
                fp = os.path.join(p, name)
                try:
                    if os.path.isdir(fp):
                        shutil.rmtree(fp)
                    else:
                        os.remove(fp)
                    deleted += 1
                except OSError:
                    pass
    return {"ok": True, "deleted": deleted, "store_root": root}
