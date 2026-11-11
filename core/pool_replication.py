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


def _replicate_one(chain: dict, site: dict, backup_path: str,
                   object_key: str, policy: dict) -> dict:
    """复制到单站点（带重试），返回站点结果。"""
    log = db.get_logger("pool")
    started = time.time()
    max_retries = int(policy.get("max_retries", 3))
    retry_interval = int(policy.get("retry_interval", 30))
    target = _target_by_id(site["target_id"])
    ok, err = False, ""
    from core.storage_backends import get_backend
    for attempt in range(1, max_retries + 1):
        try:
            backend = get_backend(target["type"], target, log)
            ok = bool(backend.save_file(backup_path, object_key))
            err = "" if ok else "后端返回失败"
        except Exception as e:
            ok, err = False, str(e)[:200]
        if ok:
            break
        if attempt < max_retries:
            log.info("[pool] 链「%s」站点%d(%s) 第 %d/%d 次失败，%ds 后重试",
                     chain.get("name"), site["seq"], site["target_name"],
                     attempt, max_retries, retry_interval)
            time.sleep(retry_interval)
    return {
        "seq": site["seq"], "target_id": site["target_id"],
        "target_name": site["target_name"], "target_type": site["target_type"],
        "site_role": site["site_role"],
        "site_role_cn": _ROLE_CN.get(site["site_role"]),
        "status": "success" if ok else "failed",
        "object_key": object_key if ok else "",
        "size_bytes": os.path.getsize(backup_path) if os.path.isfile(backup_path) else 0,
        "error": err,
        "duration_sec": round(time.time() - started, 2),
    }


def _log_site(chain_id: int, record_id: int, r: dict) -> None:
    db.execute(
        "INSERT INTO pool_replica_logs(chain_id, record_id, seq, target_id, "
        "target_name, status, object_key, size_bytes, error, duration_sec, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (chain_id, record_id, r["seq"], r["target_id"], r["target_name"],
         r["status"], r["object_key"], r["size_bytes"], r["error"],
         r["duration_sec"], _now()))


def run_chain(chain_id: int, record_id: int, backup_path: str,
              object_key: str = None) -> dict:
    """执行一条池复制作业链（同步；异步入口见 replicate_record_async）。

    serial：前一站点失败即中断，后续标 skipped；
    parallel：全部站点并行执行。
    """
    chain = get_chain(chain_id)
    if not chain:
        raise PoolError(f"复制作业链不存在: {chain_id}")
    if not backup_path or not os.path.exists(backup_path):
        raise PoolError(f"备份产物不存在: {backup_path}")
    rec = db.query_one("SELECT id FROM backup_records WHERE id=?", (record_id,))
    if not rec:
        raise PoolError(f"备份记录不存在: {record_id}")
    if not object_key:
        task_id = db.query_one(
            "SELECT task_id FROM backup_records WHERE id=?", (record_id,))
        task = db.query_one("SELECT db_type, name FROM backup_tasks WHERE id=?",
                            (task_id["task_id"],)) if task_id else None
        ts = time.strftime("%Y%m%d_%H%M%S")
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(sites)) as pool:
            futs = {pool.submit(_replicate_one, chain, s, backup_path,
                                object_key, policy): s for s in sites}
            for fut, s in futs.items():
                r = fut.result()
                results.append(r)
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
                r = _replicate_one(chain, s, backup_path, object_key, policy)
                if r["status"] != "success":
                    stopped = True
            results.append(r)
            _log_site(chain_id, record_id, r)
    ok = all(r["status"] == "success" for r in results)
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
        "SELECT l.*, c.name AS chain_name, c.mode AS chain_mode "
        "FROM pool_replica_logs l JOIN pool_chains c ON c.id=l.chain_id "
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
            "fetchable": l["status"] == "success",
        })
    return {"record_id": record_id, "storage_tier": rec.get("storage_tier"),
            "local_backup_path": rec.get("backup_path"),
            "replicas": replicas,
            "n_ok": sum(1 for r in replicas if r["status"] == "success")}


def fetch_back(record_id: int, log_id: int, dest_dir: str = None) -> dict:
    """异地取回：从池站点把副本拉回平台恢复区。"""
    row = db.query_one(
        "SELECT l.*, c.name AS chain_name FROM pool_replica_logs l "
        "JOIN pool_chains c ON c.id=l.chain_id WHERE l.id=? AND l.record_id=?",
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
    started = time.time()
    data = backend.get_file(row["object_key"], dest)
    ok = data is not None or os.path.isfile(dest)
    if ok and not os.path.isfile(dest) and data:
        with open(dest, "wb") as f:
            f.write(data)
    size = os.path.getsize(dest) if os.path.isfile(dest) else 0
    db.add_log("INFO", "pool",
               "异地取回: record=%s 链「%s」站点%s → %s (%s bytes)" % (
                   record_id, row["chain_name"], row["seq"], dest, size))
    return {"ok": ok, "dest": dest, "size_bytes": size,
            "target_name": row["target_name"],
            "object_key": row["object_key"],
            "duration_sec": round(time.time() - started, 2)}
