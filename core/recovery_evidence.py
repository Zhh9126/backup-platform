"""Evidence for artifact checks and actual reads from a restored isolated clone."""
import os
from datetime import datetime

import config
from core import db, models


def check_artifact(record):
    """Independently enforce recorded SHA256, even when an engine only checks headers."""
    path = record.get("backup_path") or record.get("output_path") or ""
    if record.get("status") == "simulated" or path.endswith(".sim"):
        raise ValueError("仿真产物不能作为恢复验证证据")
    if not path or not os.path.exists(path):
        raise ValueError("备份产物不存在")
    expected = record.get("checksum") or ""
    evidence = {"record_id": record["id"], "artifact_present": True,
                "artifact_checksum_verified": False}
    if os.path.isfile(path):
        if os.path.getsize(path) <= 0:
            raise ValueError("备份产物为空文件")
        evidence["size_bytes"] = os.path.getsize(path)
        if expected:
            digest = db.normalize_sha256(expected)
            if not digest or db.sha256_file(path) != digest:
                raise ValueError("备份产物 SHA256 校验失败")
            evidence.update(artifact_checksum_verified=True, checksum=digest)
    return evidence


def prove_clone(record, task, clone_request_id=None):
    """Only restored copies of this exact record qualify; production connectivity never does."""
    explicit = clone_request_id is not None
    if not explicit:
        row = db.query_one(
            "SELECT id FROM clone_requests WHERE source_record_id=? AND status='ready' "
            "AND purpose IN ('test','dev','staging') ORDER BY id DESC LIMIT 1", (record["id"],))
        if not row:
            return {"runtime_verified": False, "reason": "没有该备份的就绪隔离副本"}
        clone_request_id = row["id"]
    req = models.get_clone_request(int(clone_request_id))
    if not req or req.get("status") != "ready" or req.get("source_record_id") != record["id"]:
        raise ValueError("隔离副本未就绪或不属于本次备份记录")
    if req.get("purpose") not in ("test", "dev", "staging"):
        raise ValueError("仅测试、开发和预发布副本可用于隔离恢复验证")
    if config.DEMO_MODE != "off" or "DEMO" in str(req.get("note") or "").upper():
        raise ValueError("仿真副本不能作为实启验证证据")
    from core.clone_service import clone_service
    (mode, _), _ = clone_service._parse_mode(req)
    if mode != "logical" or "[snapshot_parent=" in str(req.get("note") or ""):
        raise ValueError("当前副本不是从本次备份产物独立恢复，不能作为恢复证据")
    if req.get("expires_at"):
        expires = datetime.fromisoformat(req["expires_at"])
        now = datetime.now(expires.tzinfo)
        if expires <= now:
            raise ValueError("隔离副本已过期")
    vdb = models.get_vdb(req.get("vdb_instance_id")) or {}
    if not vdb:
        raise ValueError("隔离副本元数据不存在")
    if vdb.get("source_record_id") != record["id"] or vdb.get("db_type") != task.get("db_type"):
        raise ValueError("隔离实例与备份来源不一致")
    source_db = task.get("db_name") or task.get("database") or ""
    target_db = vdb.get("database_name") or ""
    # Requiring a different database name also protects against DNS aliases of production.
    if not target_db or target_db.casefold() == source_db.casefold():
        raise ValueError("恢复验证目标未与生产数据库隔离")
    if "DEMO" in str(vdb.get("note") or "").upper():
        raise ValueError("仿真实例不能作为实启验证证据")
    result = clone_service.verify_clone(int(clone_request_id))
    if not result.get("ok") or result.get("simulated"):
        raise ValueError("隔离副本查询失败: " + str(result.get("message") or "")[:300])
    # Current Chroma verifier only checks heartbeat, which is weaker than a data query.
    runtime_verified = vdb.get("db_type") != "chroma"
    return {"runtime_verified": runtime_verified, "isolated": True,
            "instance_running": True, "query_succeeded": runtime_verified,
            "clone_request_id": int(clone_request_id), "vdb_instance_id": vdb["id"],
            "record_id": record["id"], "verified_at": db.now_iso(),
            "reason": "隔离恢复副本查询通过" if runtime_verified else "仅心跳通过，尚未验证数据查询"}
