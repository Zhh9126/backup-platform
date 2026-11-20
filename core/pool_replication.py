# -*- coding: utf-8 -*-
"""池复制两地三中心编排（P1-3，对标鼎甲池复制 / DBackup 池链）：

概念：把既有 storage_targets（MinIO/S3/local/tape 后端全复用）组织成
「站点点位链」：

    生产中心池(A) → 同城灾备池(B) → 异地灾备池(C)

- serial 模式：A 成功才复制 B，B 成功才复制 C（级联语义，链路中断即停）
- parallel 模式：A/B/C 扇出并行（互不依赖）
- 每站复制结果落 pool_replica_logs（副本分布视图数据源）
- 异地取回：任一站点副本可 get_file 拉回平台恢复区（后端能力既有）

与 TierReplicate 的关系：TierReplicate 是按「层级」的存量自动复制，
保持不动；池复制是按「站点链」的编排，备份成功后两者并行触发，互不影响。
"""
import json
import os
import threading
import time
import tempfile
import uuid
from typing import Optional

import core.db as db
import config

SITE_ROLES = ("production", "same_city", "remote")
_ROLE_CN = {"production": "生产中心", "same_city": "同城灾备", "remote": "异地灾备"}


class PoolError(ValueError):
    """池复制错误（消息可直接透出）。"""


# ---------------------------------------------------------------- 链 CRUD #

def _now():
    return db.now_iso()


def normalize_sites(sites) -> list:
    """站点点位归一化：seq 连续、target 必须存在且启用。"""
    if isinstance(sites, str):
        try:
            sites = json.loads(sites or "[]")
        except Exception:
            raise PoolError("sites 不是合法 JSON")
    if not isinstance(sites, list) or not sites:
        raise PoolError("链至少需要一个站点点位")
    out = []
    for i, s in enumerate(sites, 1):
        if not isinstance(s, dict):
            raise PoolError("站点点位格式错误")
        tid = s.get("target_id")
        row = db.query_one("SELECT id, name, type, enabled FROM storage_targets WHERE id=?",
                           (tid,))
        if not row:
            raise PoolError(f"站点 {i} 的存储目标不存在: {tid}")
        if not row.get("enabled"):
            raise PoolError(f"站点 {i} 的存储目标已停用: {tid}")
        out.append({
            "seq": i,
            "name": (s.get("name") or "").strip() or row["name"],
            "site_role": s.get("site_role") if s.get("site_role") in SITE_ROLES
            else SITE_ROLES[min(i - 1, 2)],
            "target_id": int(tid),
            "target_name": row["name"],
            "target_type": row["type"],
        })
    return out


def create_chain(data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise PoolError("链名称必填")
    sites = normalize_sites(data.get("sites"))
    now = _now()
    mode = data.get("mode") if data.get("mode") in ("serial", "parallel") else "serial"
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    return db.execute(
        "INSERT INTO pool_chains(name, sites, mode, policy, enabled, remark, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, json.dumps(sites, ensure_ascii=False), mode,
         json.dumps(policy, ensure_ascii=False),
         1 if data.get("enabled", True) else 0,
         data.get("remark") or "", now, now))


def update_chain(chain_id: int, data: dict) -> bool:
    row = db.query_one("SELECT * FROM pool_chains WHERE id=?", (chain_id,))
    if not row:
        raise PoolError(f"复制作业链不存在: {chain_id}")
    sets, params = [], []
    if "name" in data and (data["name"] or "").strip():
        sets.append("name=?"); params.append(data["name"].strip())
    if "sites" in data:
        sets.append("sites=?")
        params.append(json.dumps(normalize_sites(data["sites"]), ensure_ascii=False))
    if data.get("mode") in ("serial", "parallel"):
        sets.append("mode=?"); params.append(data["mode"])
    if isinstance(data.get("policy"), dict):
        sets.append("policy=?")
        params.append(json.dumps(data["policy"], ensure_ascii=False))
    if "enabled" in data:
        sets.append("enabled=?")
        params.append(1 if data["enabled"] else 0)
    if "remark" in data:
        sets.append("remark=?"); params.append(data["remark"] or "")
    if not sets:
        return False
    sets.append("updated_at=?"); params.append(_now())
    params.append(chain_id)
    db.execute("UPDATE pool_chains SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_chain(chain_id: int) -> None:
    db.execute("DELETE FROM pool_chains WHERE id=?", (chain_id,))


def get_chain(chain_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM pool_chains WHERE id=?", (chain_id,))
    return _decorate_chain(dict(row)) if row else None


def list_chains() -> list:
    return [_decorate_chain(dict(r))
            for r in db.query("SELECT * FROM pool_chains ORDER BY id DESC") or []]


def _decorate_chain(row: dict) -> dict:
    try:
        row["sites"] = json.loads(row.get("sites") or "[]")
    except Exception:
        row["sites"] = []
    try:
        row["policy"] = json.loads(row.get("policy") or "{}")
    except Exception:
        row["policy"] = {}
    for s in row["sites"]:
        s["site_role_cn"] = _ROLE_CN.get(s.get("site_role"), s.get("site_role"))
    try:
        row["last_result"] = json.loads(row.get("last_result") or "null")
    except Exception:
        row["last_result"] = None
    return row


# ---------------------------------------------------------------- 复制执行 #

def _target_by_id(target_id: int) -> dict:
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (target_id,))
    if not row:
        raise PoolError(f"存储目标不存在: {target_id}")
    t = dict(row)
    if t.get("secret_key"):
        t["secret_key"] = db.decrypt_secret(t["secret_key"])
    if t.get("extra_options"):
        try:
            t["extra_options"] = json.loads(t["extra_options"])
        except Exception:
            pass
    return t


def _download_verified(backend, object_key, dest, checksum, expected_size):
    data = backend.get_file(object_key, dest)
    if isinstance(data, bytes):
        with open(dest, "wb") as stream:
            stream.write(data)
    elif data is None or data is False:
        raise PoolError("副本读取失败，未发布恢复文件")
    if not os.path.isfile(dest) or os.path.getsize(dest) != expected_size:
        raise PoolError("副本长度校验失败，未发布恢复文件")
    if db.sha256_file(dest) != checksum:
        raise PoolError("副本 SHA256 校验失败，未发布恢复文件")
    with open(dest, "rb") as stream:
        os.fsync(stream.fileno())


def _replicate_one(chain, site, backup_path, object_key, policy, source=None):
    """Read-back verification and an independent sidecar are required for a usable replica."""
    log = db.get_logger("pool")
    started = time.monotonic()
    max_retries = max(1, min(101, int(policy.get("max_retries", 3))))
    retry_interval = max(0, float(policy.get("retry_interval", 30)))
    err, ok, manifest_key = "", False, object_key + ".aidbm-replica.json"
    checksum = source["checksum"] if source else db.sha256_file(backup_path)
    size = source["size_bytes"] if source else os.path.getsize(backup_path)
    from core.storage_backends import get_backend
    from core.network_retry import retry_wait
    from core.engines.file import _is_transient
    for attempt in range(1, max_retries + 1):
        try:
            target = _target_by_id(site["target_id"])
            if not target.get("enabled"):
                raise PoolError("存储目标已停用")
            backend = get_backend(target["type"], target, log)
            if not backend.save_file(backup_path, object_key):
                raise OSError("复制后端返回失败")
            with tempfile.TemporaryDirectory(prefix="aidbm-replica-") as temporary:
                _download_verified(backend, object_key, os.path.join(temporary, "payload"), checksum, size)
                if db.sha256_file(backup_path) != checksum:
                    raise PoolError("复制期间源备份发生变化，副本未登记")
                manifest = dict(source or {})
                manifest.update(schema=1, kind="aidbm-replica", checksum=checksum,
                                size_bytes=size, object_key=object_key, verified_at=_now())
                manifest_path = os.path.join(temporary, "manifest.json")
                from core.chunk_store import _atomic_write
                _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
                if not backend.save_file(manifest_path, manifest_key):
                    raise OSError("副本清单保存失败")
                _download_verified(backend, manifest_key, os.path.join(temporary, "manifest-check"),
                                   db.sha256_file(manifest_path), os.path.getsize(manifest_path))
            ok = True
            break
        except Exception as exc:
            err = str(exc)[:300]
            if attempt >= max_retries or not _is_transient(exc):
                break
            log.warning("[pool] 站点 %s 第 %s/%s 次失败，退避重试: %s",
                        site["seq"], attempt, max_retries, err)
            retry_wait(attempt, retry_interval)
    return {
        "seq": site["seq"], "target_id": site["target_id"],
        "target_name": site["target_name"], "target_type": site["target_type"],
        "site_role": site["site_role"], "site_role_cn": _ROLE_CN.get(site["site_role"]),
        "status": "success" if ok else "failed", "object_key": object_key if ok else "",
        "size_bytes": size, "checksum": checksum if ok else "",
        "integrity_status": "verified" if ok else "failed",
        "verified_at": _now() if ok else None, "manifest_key": manifest_key if ok else "",
        "error": "" if ok else err, "attempts": attempt,
        "duration_sec": round(time.monotonic() - started, 2),
    }


def _log_site(chain_id, record_id, r):
    db.execute(
        "INSERT INTO pool_replica_logs(chain_id, record_id, seq, target_id, target_name, "
        "status, object_key, size_bytes, error, duration_sec, created_at, checksum, "
        "verified_at, manifest_key, integrity_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chain_id, record_id, r["seq"], r["target_id"], r["target_name"], r["status"],
         r["object_key"], r["size_bytes"], r["error"], r["duration_sec"], _now(),
         r.get("checksum"), r.get("verified_at"), r.get("manifest_key"),
         r.get("integrity_status", "unknown")))


def run_chain(chain_id: int, record_id: int, backup_path: str,
              object_key: str = None) -> dict:
    from core.operation_lease import operation_lease
    try:
        with operation_lease("pool-chain:%s:%s" % (chain_id, record_id)) as ensure_owned:
            return _run_chain(chain_id, record_id, backup_path, object_key, ensure_owned)
    except ValueError as exc:
        raise PoolError(str(exc)) from exc


def _run_chain(chain_id, record_id, backup_path, object_key, ensure_owned):
    """执行一条池复制作业链（同步；异步入口见 replicate_record_async）。

    serial：前一站点失败即中断，后续标 skipped；
    parallel：全部站点并行执行。
    """
    chain = get_chain(chain_id)
    if not chain:
        raise PoolError(f"复制作业链不存在: {chain_id}")
    if not backup_path or not os.path.isfile(backup_path):
        raise PoolError(f"备份产物不存在: {backup_path}")
    rec = db.query_one("SELECT * FROM backup_records WHERE id=?", (record_id,))
    if not rec:
        raise PoolError(f"备份记录不存在: {record_id}")
    if rec.get("status") != "success":
        raise PoolError("仅真实成功备份可登记容灾副本")
    digest = db.sha256_file(backup_path)
    expected = rec.get("checksum") or ""
    if expected and db.normalize_sha256(expected) != digest:
        raise PoolError("源备份 SHA256 与记录不一致")
    source = {k: rec.get(k) for k in ("task_id", "db_type", "backup_type", "backup_mode",
                                     "started_at", "finished_at", "parent_record_id")}
    source.update(record_id=record_id, checksum=digest,
                  size_bytes=os.path.getsize(backup_path), source_name=os.path.basename(backup_path))
    source_task = db.query_one("SELECT name,db_type,backup_mode,db_name FROM backup_tasks WHERE id=?",
                               (rec.get("task_id"),)) or {}
    source.update(task_name=source_task.get("name"), database_name=source_task.get("db_name"))
    for key in ("db_type", "backup_mode"):
        source[key] = source.get(key) or source_task.get(key)

    if not object_key:
        task_id = db.query_one(
            "SELECT task_id FROM backup_records WHERE id=?", (record_id,))
        task = db.query_one("SELECT db_type, name FROM backup_tasks WHERE id=?",
                            (task_id["task_id"],)) if task_id else None
        ts = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
        object_key = "pool/%s/%s_%s/%s__%s" % (
            task["db_type"] if task else "unknown",
            task_id["task_id"] if task_id else 0,
            task["name"] if task else "task", ts,
            os.path.basename(backup_path))
    policy = chain.get("policy") or {}
    sites = chain.get("sites") or []
    results = []
    if chain.get("mode") == "parallel":
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(sites)))) as pool:
            futs = {pool.submit(_replicate_one, chain, s, backup_path,
                                object_key, policy, source): s for s in sites}
            for fut, s in futs.items():
                r = fut.result()
                results.append(r)
                ensure_owned()
                _log_site(chain_id, record_id, r)
    else:
        stopped = False
        for s in sites:
            if stopped:
                r = {"seq": s["seq"], "target_id": s["target_id"],
                     "target_name": s["target_name"],
                     "target_type": s.get("target_type"),
                     "site_role": s["site_role"],
                     "site_role_cn": _ROLE_CN.get(s["site_role"]),
                     "status": "skipped",
                     "object_key": "", "size_bytes": 0,
                     "error": "上游站点复制失败，级联中断", "duration_sec": 0}
            else:
                r = _replicate_one(chain, s, backup_path, object_key, policy, source)
                if r["status"] != "success":
                    stopped = True
            results.append(r)
            ensure_owned()
            _log_site(chain_id, record_id, r)
    ok = bool(results) and all(r["status"] == "success" for r in results)
    ensure_owned()
    db.execute(
        "UPDATE pool_chains SET last_run_at=?, last_result=?, updated_at=? "
        "WHERE id=?",
        (_now(), json.dumps({"record_id": record_id, "ok": ok,
                             "sites": [{"seq": r["seq"], "status": r["status"]}
                                        for r in results]}, ensure_ascii=False),
         _now(), chain_id))
    db.get_logger("pool").info(
        "[pool] 链「%s」record=%s 完成: %s",
        chain["name"], record_id,
        " ".join("S%d=%s" % (r["seq"], r["status"]) for r in results))
    return {"chain_id": chain_id, "chain_name": chain["name"],
            "record_id": record_id, "ok": ok, "sites": results,
            "object_key": object_key}


def replicate_record_async(record_id: int, backup_path: str) -> None:
    """备份成功后异步触发所有启用链（不阻塞备份主流程）。"""
    def _worker():
        try:
            for chain in list_chains():
                if not chain.get("enabled"):
                    continue
                try:
                    run_chain(chain["id"], record_id, backup_path)
                except Exception as e:
                    db.get_logger("pool").warning(
                        "[pool] 链「%s」record=%s 执行失败: %s",
                        chain.get("name"), record_id, e)
        except Exception as e:
            db.get_logger("pool").warning("[pool] 异步复制异常: %s", e)
    threading.Thread(target=_worker, daemon=True,
                     name=f"pool-repl-{record_id}").start()


# ---------------------------------------------------------------- 视图/取回 #

def replica_view(record_id: int) -> dict:
    """副本分布视图：一条备份记录在哪些池/站点有副本。"""
    rec = db.query_one(
        "SELECT id, storage_tier, backup_path FROM backup_records WHERE id=?",
        (record_id,))
    if not rec:
        raise PoolError(f"备份记录不存在: {record_id}")
    logs = db.query(
        "SELECT l.*, COALESCE(c.name,'已删除复制链') AS chain_name, c.mode AS chain_mode "
        "FROM pool_replica_logs l LEFT JOIN pool_chains c ON c.id=l.chain_id "
        "WHERE l.record_id=? ORDER BY l.chain_id, l.seq", (record_id,)) or []
    replicas = []
    for l in logs:
        replicas.append({
            "id": l["id"],
            "chain_id": l["chain_id"], "chain_name": l["chain_name"],
            "mode": l["chain_mode"],
            "seq": l["seq"], "target_id": l["target_id"],
            "target_name": l["target_name"],
            "status": l["status"], "object_key": l["object_key"],
            "size_bytes": l["size_bytes"], "error": l["error"],
            "duration_sec": l["duration_sec"], "created_at": l["created_at"],
            "integrity_status": l.get("integrity_status") or "unknown",
            "checksum": l.get("checksum") or "", "verified_at": l.get("verified_at"),
            "manifest_key": l.get("manifest_key") or "",
            "fetchable": l["status"] == "success",
            "recoverable": l["status"] == "success" and l.get("integrity_status") == "verified",
        })
    return {"record_id": record_id, "storage_tier": rec.get("storage_tier"),
            "local_backup_path": rec.get("backup_path"),
            "replicas": replicas,
            "n_ok": len({(r["target_id"], r["object_key"]) for r in replicas if r["recoverable"]})}


def fetch_back(record_id: int, log_id: int, dest_dir: str = None) -> dict:
    """异地取回：从池站点把副本拉回平台恢复区。"""
    row = db.query_one(
        "SELECT l.*, COALESCE(c.name,'已删除复制链') AS chain_name FROM pool_replica_logs l "
        "LEFT JOIN pool_chains c ON c.id=l.chain_id WHERE l.id=? AND l.record_id=?",
        (log_id, record_id))
    if not row:
        raise PoolError("副本记录不存在")
    if row["status"] != "success" or not row["object_key"]:
        raise PoolError("该站点副本不可取回（状态 %s）" % row["status"])
    target = _target_by_id(row["target_id"])
    from core.storage_backends import get_backend
    log = db.get_logger("pool")
    backend = get_backend(target["type"], target, log)
    dest_dir = dest_dir or os.path.join(config.BACKUP_ROOT, "pool_fetched",
                                        str(record_id))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(row["object_key"]))
    started = time.monotonic()
    expected = db.normalize_sha256(row.get("checksum") or "")
    if not expected:
        record = db.query_one("SELECT checksum FROM backup_records WHERE id=?", (record_id,)) or {}
        expected = db.normalize_sha256(record.get("checksum") or "")
    if not expected:
        raise PoolError("历史副本没有可信摘要，无法确认可恢复性；请重新复制生成校验清单")
    descriptor, temporary = tempfile.mkstemp(prefix=".aidbm-fetch-", dir=dest_dir)
    os.close(descriptor)
    try:
        _download_verified(backend, row["object_key"], temporary, expected, int(row["size_bytes"]))
        if row.get("manifest_key"):
            with tempfile.TemporaryDirectory(prefix=".aidbm-manifest-", dir=dest_dir) as work:
                path = os.path.join(work, "manifest.json")
                result = backend.get_file(row["manifest_key"], path)
                if isinstance(result, bytes):
                    with open(path, "wb") as stream:
                        stream.write(result)
                elif result is None or result is False:
                    raise PoolError("副本清单读取失败")
                with open(path, encoding="utf-8") as stream:
                    manifest = json.load(stream)
                if (manifest.get("schema") != 1 or manifest.get("record_id") != record_id
                        or manifest.get("object_key") != row["object_key"]
                        or manifest.get("checksum") != expected
                        or manifest.get("size_bytes") != row["size_bytes"]):
                    raise PoolError("副本清单与登记记录不一致")
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    db.add_log("INFO", "pool", "异地副本取回并校验: record=%s target=%s" % (record_id, row["target_name"]))
    return {"ok": True, "verified": True, "checksum": expected, "dest": dest,
            "size_bytes": os.path.getsize(dest), "target_name": row["target_name"],
            "object_key": row["object_key"], "duration_sec": round(time.monotonic() - started, 2)}


def import_replica(target_id, manifest_key, task_id, expected_checksum=""):
    """Register a full replica using only its independent sidecar and a local task mapping."""
    import shutil
    from core import models
    from core.operation_lease import operation_lease
    from core.storage_backends import get_backend
    task = models.get_task(int(task_id))
    if not task:
        raise PoolError("请选择本平台已有的备份任务")
    suffix = ".aidbm-replica.json"
    if not isinstance(manifest_key, str) or not manifest_key.endswith(suffix):
        raise PoolError("请选择 aidbm-replica.json 副本清单")
    payload_key = manifest_key[:-len(suffix)]
    if not payload_key or payload_key.startswith("/") or ".." in payload_key.split("/"):
        raise PoolError("副本清单对象路径无效")
    target = _target_by_id(int(target_id))
    if not target.get("enabled"):
        raise PoolError("存储目标已停用")
    backend = get_backend(target["type"], target, db.get_logger("pool"))
    root = os.path.join(config.BACKUP_ROOT, "pool_imported", str(task_id))
    os.makedirs(root, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aidbm-import-", dir=root) as temporary:
        local_manifest = os.path.join(temporary, "manifest.json")
        data = backend.get_file(manifest_key, local_manifest)
        if isinstance(data, bytes):
            with open(local_manifest, "wb") as stream:
                stream.write(data)
        elif data is None or data is False:
            raise PoolError("副本清单读取失败")
        if not os.path.isfile(local_manifest) or os.path.getsize(local_manifest) > 1024 * 1024:
            raise PoolError("副本清单过大或不存在")
        try:
            with open(local_manifest, encoding="utf-8") as stream:
                manifest = json.load(stream)
            if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                    or manifest.get("kind") != "aidbm-replica"
                    or manifest.get("object_key") != payload_key):
                raise ValueError("unsupported manifest")
            checksum = db.normalize_sha256(manifest.get("checksum"))
            size = manifest.get("size_bytes")
            if not checksum or type(size) is not int or size <= 0:
                raise ValueError("invalid checksum or size")
        except (OSError, ValueError, TypeError) as exc:
            raise PoolError("副本清单格式或内容无效") from exc
        if expected_checksum and db.normalize_sha256(expected_checksum) != checksum:
            raise PoolError("副本清单摘要与指定的可信摘要不一致")
        if manifest.get("db_type") != task.get("db_type"):
            raise PoolError("副本数据库类型与所选任务不一致")
        if (manifest.get("backup_mode") and task.get("backup_mode")
                and manifest["backup_mode"] != task["backup_mode"]):
            raise PoolError("副本备份方式与所选任务不一致")
        if str(manifest.get("backup_type") or "").lower() not in ("full", "snapshot"):
            raise PoolError("增量或日志副本需先导入完整恢复链，不能单独登记为可恢复备份")
        try:
            with operation_lease("pool-import:%s:%s" % (task_id, checksum)) as ensure_owned:
                directory = os.path.join(root, checksum)
                dest = os.path.join(directory, os.path.basename(payload_key))
                existing = db.query_one(
                    "SELECT id FROM backup_records WHERE task_id=? AND backup_path=? AND status='success'",
                    (int(task_id), dest))
                if existing and os.path.isfile(dest) and db.sha256_file(dest) == checksum:
                    return {"ok": True, "record_id": existing["id"], "dest": dest,
                            "checksum": checksum, "reused": True, "verified": True}
                if shutil.disk_usage(root).free < size:
                    raise PoolError("导入目录可用空间不足")
                staging = os.path.join(temporary, "payload")
                _download_verified(backend, payload_key, staging, checksum, size)
                ensure_owned()
                # Preserve source time: importing an old copy must not reset RPO freshness.
                from datetime import datetime
                source_time = manifest.get("started_at") or ""
                try:
                    datetime.fromisoformat(source_time)
                except (ValueError, TypeError):
                    raise PoolError("副本清单缺少有效的原始备份时间")
                os.makedirs(directory, exist_ok=True)
                os.replace(staging, dest)
                record_id = models.create_record({
                    "task_id": int(task_id), "db_type": task["db_type"],
                    "backup_type": str(manifest["backup_type"]).lower(), "status": "success",
                    "backup_path": dest, "size_bytes": size, "checksum": checksum,
                    "started_at": source_time, "finished_at": source_time, "duration_sec": 0,
                    "message": "从独立副本清单导入；内容摘要已校验，尚未完成数据库实启恢复验证",
                })
                db.add_log("INFO", "pool", "独立副本导入: target=%s task=%s record=%s" %
                           (target_id, task_id, record_id))
                return {"ok": True, "record_id": record_id, "dest": dest,
                        "checksum": checksum, "reused": False, "verified": True,
                        "runtime_verified": False}
        except ValueError as exc:
            raise PoolError(str(exc)) from exc
