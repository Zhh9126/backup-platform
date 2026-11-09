# -*- coding: utf-8 -*-
"""对象存储备份的断点（stage / checkpoint）管理。

备份中断（网络断连、进程重启、用户点「停止」）时，已下载到 stage 的字节
就是最有价值的成果，不能像旧实现那样 ``rmtree`` 掉。这里把「哪些对象可以
安全复用」的判断收敛成纯函数 + 断点文件，便于单测，也让引擎只负责编排。

两层续传的分工：

* **对象内**：``download_to_file(resume=True)`` 用 HTTP Range 从 ``*.aidbm_part``
  已有字节继续追加（见 client.py）；
* **对象间**：``.done.jsonl`` 记录已完成对象，重跑时逐个校验后跳过。

正确性护栏（宁可重下，绝不复用错字节）：
1. scope 指纹覆盖 task_id / provider / endpoint / bucket / prefix /
   include_versions / backup_type —— 换桶、换前缀、全量改增量一律整体作废；
2. 每个候选对象还要比对 ETag / Size / LastModified / VersionId 四元组，
   源侧在此期间被改写就只重下这一个对象；
3. 落盘文件必须重新核对 size 与 sha256，防止上一次被截断的残件混入产物；
4. checkpoint 有 TTL，陈旧断点自动作废（默认 72 小时）。

为什么完成清单用追加式 JSONL 而不是全量重写 JSON：大桶可能有十万级对象，
每完成一个对象全量 dump 一次是 O(n²)；追加一行是 O(1)，进程被强杀最多丢
最后一行（对应对象会重下，不会污染产物）。坏行（写一半）解析时跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

STAGE_DIR = ".stage_resume"
CHECKPOINT_NAME = ".checkpoint.json"
DONE_NAME = ".done.jsonl"
PART_SUFFIX = ".aidbm_part"
CHECKPOINT_VERSION = 1
DEFAULT_TTL_HOURS = 72


def stage_path(output_dir: str) -> str:
    """每个任务的固定断点目录（输出目录本身已按 task_id 隔离）。"""
    return os.path.join(output_dir, STAGE_DIR)


def checkpoint_path(stage: str) -> str:
    return os.path.join(stage, CHECKPOINT_NAME)


def done_path(stage: str) -> str:
    return os.path.join(stage, DONE_NAME)


def part_path(dest: str) -> str:
    return dest + PART_SUFFIX


def scope_digest(task_id, task_name: str, cfg: dict, backup_type: str) -> str:
    """作业范围指纹：范围一变，所有断点整体失效，避免串用别的桶/前缀的数据。"""
    parts = [
        str(task_id), str(task_name or ""),
        str(cfg.get("provider") or ""), str(cfg.get("endpoint") or ""),
        str(cfg.get("region") or ""), str(cfg.get("bucket") or ""),
        str(cfg.get("prefix") or ""),
        "versions=1" if cfg.get("include_versions") else "versions=0",
        str(backup_type),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def new_checkpoint(task_id, task_name: str, cfg: dict, backup_type: str,
                   plan_total: int) -> dict:
    now = time.time()
    return {
        "version": CHECKPOINT_VERSION,
        "task_id": task_id,
        "task_name": task_name or "",
        "scope": scope_digest(task_id, task_name, cfg, backup_type),
        "backup_type": backup_type,
        "created_at": now,
        "updated_at": now,
        "plan_total": int(plan_total),
    }


def save_meta(stage: str, cp: dict) -> None:
    """原子写断点元数据（小文件，仅在创建/计划变化时写）。"""
    os.makedirs(stage, exist_ok=True)
    cp["updated_at"] = time.time()
    tmp = checkpoint_path(stage) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False)
    os.replace(tmp, checkpoint_path(stage))


def append_done(stage: str, unit: str, entry: dict) -> None:
    """记录一个已完成对象：单行追加 + flush，O(1)。主线程串行调用无竞态。"""
    with open(done_path(stage), "a", encoding="utf-8") as f:
        f.write(json.dumps({"u": unit, "e": entry}, ensure_ascii=False) + "\n")
        f.flush()


def load_done(stage: str) -> dict:
    """重建「unit → manifest 条目」映射；坏行跳过，后写覆盖先写。"""
    path = done_path(stage)
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                unit, entry = row.get("u"), row.get("e")
                if isinstance(unit, str) and isinstance(entry, dict):
                    out[unit] = entry
    except OSError:
        return {}
    return out


def load_valid(stage: str, scope: str, ttl_hours: float) -> tuple[dict, dict]:
    """返回 (可复用 done 映射, checkpoint 元数据)。任何一项不符返回空。"""
    path = checkpoint_path(stage)
    if not os.path.isfile(path):
        return {}, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cp = json.load(f)
    except Exception:
        return {}, {}
    if cp.get("version") != CHECKPOINT_VERSION or cp.get("scope") != scope:
        return {}, {}
    created = float(cp.get("created_at") or 0)
    if ttl_hours > 0 and created > 0 and (time.time() - created) > ttl_hours * 3600:
        return {}, {}
    return load_done(stage), cp


def meta_matches(entry: dict, meta: dict) -> bool:
    """源对象四元组未变，才允许复用已下载字节。"""
    try:
        a = int(entry.get("size") if entry.get("size") is not None else -2)
        b = int(meta.get("size") if meta.get("size") is not None else -2)
    except (TypeError, ValueError):
        return False
    return (str(entry.get("etag") or "") == str(meta.get("etag") or "")
            and a == b
            and str(entry.get("last_modified") or "") == str(meta.get("last_modified") or "")
            and str(entry.get("version_id") or "") == str(meta.get("version_id") or ""))


def reusable_entry(stage: str, entry, meta: dict) -> dict | None:
    """校验条目对应的落盘文件；可用则返回可直接进 manifest 的条目副本。"""
    if not isinstance(entry, dict) or not meta_matches(entry, meta):
        return None
    rel = entry.get("path") or ""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    fp = os.path.join(stage, rel)
    if not os.path.isfile(fp):
        return None
    try:
        if os.path.getsize(fp) != int(entry.get("size")):
            return None
        if _sha256(fp) != entry.get("sha256"):
            return None
    except (OSError, ValueError):
        return None
    return dict(entry)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def clear(stage: str) -> None:
    shutil.rmtree(stage, ignore_errors=True)


def status(output_dir: str, scope: str = "", ttl_hours: float = DEFAULT_TTL_HOURS) -> dict:
    """给 API / 前端看的断点现状（只读，不修改任何文件）。"""
    stage = stage_path(output_dir)
    out = {
        "has_checkpoint": False, "stage_exists": os.path.isdir(stage),
        "stage": stage, "done_count": 0, "plan_total": 0,
        "done_bytes": 0, "part_count": 0, "part_bytes": 0,
        "created_at": None, "updated_at": None, "age_hours": None,
        "ttl_hours": ttl_hours, "expired": False, "valid": False,
        "scope_matched": False,
    }
    if not out["stage_exists"]:
        return out
    for root, _dirs, files in os.walk(stage):
        for name in files:
            fp = os.path.join(root, name)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if name.endswith(PART_SUFFIX):
                out["part_count"] += 1
                out["part_bytes"] += sz
    done = load_done(stage)
    out["done_count"] = len(done)
    out["done_bytes"] = sum(int(v.get("size") or 0) for v in done.values())
    cp_path = checkpoint_path(stage)
    if not os.path.isfile(cp_path):
        return out
    out["has_checkpoint"] = True
    try:
        with open(cp_path, "r", encoding="utf-8") as f:
            cp = json.load(f)
    except Exception:
        return out
    out["created_at"] = cp.get("created_at")
    out["updated_at"] = cp.get("updated_at")
    out["plan_total"] = int(cp.get("plan_total") or 0)
    created = float(cp.get("created_at") or 0)
    if created:
        out["age_hours"] = round((time.time() - created) / 3600, 2)
        out["expired"] = bool(ttl_hours > 0
                              and (time.time() - created) > ttl_hours * 3600)
    out["scope_matched"] = bool(scope and cp.get("scope") == scope)
    out["valid"] = bool(cp.get("version") == CHECKPOINT_VERSION
                        and (not scope or cp.get("scope") == scope)
                        and not out["expired"])
    return out
