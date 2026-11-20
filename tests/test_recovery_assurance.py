"""Recovery evidence, verified off-site copies and PITR bounds inspired by the supplied zBackup README."""
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import config
from core import db, models, pool_replication as pool, restore_verify as verify
from core.engines.base import BackupResult
from core.rt_backup.journal import RecoveryJournal
from core.rt_backup.pitr import PITRRestore, normalize_ts
from core.rt_backup.types import RecoveryPoint, RP_DB_FULL, RP_DB_LOG


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "DEMO_MODE", "off")
    os.makedirs(config.BACKUP_ROOT)
    db.init_schema(backend="sqlite")
    task_id = db.execute(
        "INSERT INTO backup_tasks(name,db_type,host,port,db_name,username,enabled,created_at) "
        "VALUES ('assurance','postgresql','127.0.0.1',5432,'production','user',0,?)", (db.now_iso(),))
    source = tmp_path / "backup.dump"
    source.write_bytes(b"PGDMP" + b"backup-content" * 100)
    record_id = models.create_record({"task_id": task_id, "db_type": "postgresql", "backup_type": "FULL",
                                     "status": "success", "backup_path": str(source),
                                     "size_bytes": source.stat().st_size,
                                     "started_at": db.now_iso(), "checksum": db.sha256_file(str(source))})
    policy_id = models.create_restore_verify_policy({"task_id": task_id, "name": "assurance",
                                                      "schedule_type": "manual"})
    return SimpleNamespace(root=tmp_path, source=source, task_id=task_id,
                           record_id=record_id, policy_id=policy_id)


def stub_engine(monkeypatch):
    monkeypatch.setattr(verify, "get_engine", lambda *args: SimpleNamespace(
        verify_record=lambda record, options=None: BackupResult(success=True, verified=True, message="header OK")))


def make_clone(env, name="clone_1", mode="logical", record_id=None, kind="postgresql"):
    source_record = record_id or env.record_id
    vdb = db.execute(
        "INSERT INTO vdb_instances(name,source_record_id,task_id,db_type,host,port,database_name,status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, source_record, env.task_id, kind, "127.0.0.1", 5432, name, "ready"))
    return models.create_clone_request({"source_record_id": source_record, "target_env": "test",
                                        "purpose": "test", "status": "ready", "vdb_instance_id": vdb,
                                        "note": "[clone_mode=%s]" % mode})


def test_real_engine_header_check_is_partial(env):
    result = verify.run_restore_verify_policy(env.policy_id)
    assert result["status"] == "partial" and not result["success"]
    assert result["evidence"]["artifact_checksum_verified"]
    report = models.get_restore_test_report(result["report_id"])
    assert report["verification_level"] == "artifact"
    assert models.get_restore_verify_stats()["partial_count"] == 1
    assert models.get_restore_verify_stats()["success_count"] == 0


def test_engine_verified_boolean_is_not_runtime_evidence(env, monkeypatch):
    stub_engine(monkeypatch)
    assert verify.run_restore_verify_policy(env.policy_id)["status"] == "partial"


@pytest.mark.parametrize("prefix", ["", "sha256:"])
def test_corrupt_artifact_rejected_before_engine(env, monkeypatch, prefix):
    db.execute("UPDATE backup_records SET checksum=? WHERE id=?", (prefix + "a" * 64, env.record_id))
    monkeypatch.setattr(verify, "get_engine", lambda *args: pytest.fail("engine must not run"))
    assert verify.run_restore_verify_policy(env.policy_id)["status"] == "failed"


def test_simulated_record_is_not_selected(env):
    models.create_record({"task_id": env.task_id, "status": "simulated", "backup_path": "/simulated"})
    result = verify.run_restore_verify_policy(env.policy_id)
    assert result["status"] == "partial" and result["evidence"]["record_id"] == env.record_id


def test_legacy_verify_signature_without_options(env, monkeypatch):
    monkeypatch.setattr(verify, "get_engine", lambda *args: SimpleNamespace(
        verify_record=lambda record: BackupResult(success=True, message="structure OK")))
    assert verify.run_restore_verify_policy(env.policy_id)["status"] == "partial"


def test_only_matching_isolated_clone_can_pass(env, monkeypatch):
    stub_engine(monkeypatch)
    clone_id = make_clone(env)
    from core.clone_service import clone_service
    calls = []
    monkeypatch.setattr(clone_service, "verify_clone", lambda cid: calls.append(cid) or {"ok": True})
    result = verify.run_restore_verify_policy(env.policy_id)
    assert result["success"] and result["status"] == "success"
    assert result["evidence"]["query_succeeded"] and result["evidence"]["isolated"]
    assert calls == [clone_id]
    assert models.get_restore_verify_stats()["success_count"] == 1


@pytest.mark.parametrize("mode,name", [("logical", "production"), ("template", "clone_1"),
                                      ("schema", "clone_1"), ("snapshot", "clone_1")])
def test_nonisolated_or_live_clone_not_recovery_proof(env, monkeypatch, mode, name):
    stub_engine(monkeypatch)
    clone_id = make_clone(env, name=name, mode=mode)
    from core.clone_service import clone_service
    monkeypatch.setattr(clone_service, "verify_clone", lambda cid: pytest.fail("must reject before connection"))
    result = verify.run_restore_verify_policy(env.policy_id, clone_request_id=clone_id)
    assert result["status"] == "failed" and not result["success"]


def test_expired_clone_not_runtime_proof(env, monkeypatch):
    stub_engine(monkeypatch)
    clone_id = make_clone(env)
    db.execute("UPDATE clone_requests SET expires_at=? WHERE id=?",
               ((datetime.now() - timedelta(days=1)).isoformat(), clone_id))
    assert verify.run_restore_verify_policy(env.policy_id, clone_request_id=clone_id)["status"] == "failed"


def test_failed_runtime_probe_stays_partial_in_auto_mode(env, monkeypatch):
    stub_engine(monkeypatch)
    make_clone(env)
    from core.clone_service import clone_service
    monkeypatch.setattr(clone_service, "verify_clone", lambda cid: {"ok": False, "message": "unreachable"})
    result = verify.run_restore_verify_policy(env.policy_id)
    assert result["status"] == "partial" and "查询失败" in result["message"]


def test_policy_concurrent_runs_are_exclusive(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def check(record, options=None):
        entered.set()
        assert release.wait(5)
        return BackupResult(success=True)
    monkeypatch.setattr(verify, "get_engine", lambda *args: SimpleNamespace(verify_record=check))
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(verify.run_restore_verify_policy, env.policy_id)
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match="正在执行"):
                verify.run_restore_verify_policy(env.policy_id)
        finally:
            release.set()
        assert first.result()["status"] == "partial"


def test_legacy_report_migration_is_idempotent(env):
    report_id = models.create_restore_test_report({"policy_id": env.policy_id, "task_id": env.task_id,
                                                    "status": "success", "message": "old header check"})
    db.init_schema(backend="sqlite")
    db.init_schema(backend="sqlite")
    assert models.get_restore_test_report(report_id)["status"] == "partial"
    assert models.get_restore_verify_stats()["success_count"] == 0


def pool_setup(env, tmp_path, policy=None):
    sites = []
    for n in range(3):
        target = db.execute("INSERT INTO storage_targets(name,type,endpoint,enabled) VALUES (?,?,?,1)",
                            ("pool-%s" % n, "local", str(tmp_path / ("pool-%s" % n))))
        sites.append({"target_id": target, "site_role": ("production", "same_city", "remote")[n]})
    return pool.create_chain({"name": "assurance-chain", "sites": sites,
                               "policy": policy or {"max_retries": 1}})


def test_replica_manifest_and_original_loss_recovery(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    result = pool.run_chain(chain_id, env.record_id, str(env.source))
    assert result["ok"]
    digest = db.sha256_file(str(env.source))
    env.source.unlink()
    view = pool.replica_view(env.record_id)
    assert view["n_ok"] == 3 and all(row["recoverable"] for row in view["replicas"])
    replica = view["replicas"][-1]
    out = pool.fetch_back(env.record_id, replica["id"], str(tmp_path / "recovered"))
    assert out["verified"] and db.sha256_file(out["dest"]) == digest
    target = db.query_one("SELECT endpoint FROM storage_targets WHERE id=?", (replica["target_id"],))
    manifest = json.loads((__import__("pathlib").Path(target["endpoint"]) / replica["manifest_key"]).read_text())
    assert manifest["record_id"] == env.record_id and manifest["checksum"] == digest
    assert "password" not in manifest


@pytest.mark.parametrize("damage", ["payload", "manifest", "missing"])
def test_corrupt_replica_preserves_existing_destination(env, tmp_path, damage):
    from pathlib import Path
    chain_id = pool_setup(env, tmp_path)
    assert pool.run_chain(chain_id, env.record_id, str(env.source))["ok"]
    replica = pool.replica_view(env.record_id)["replicas"][-1]
    target = db.query_one("SELECT endpoint FROM storage_targets WHERE id=?", (replica["target_id"],))
    payload = Path(target["endpoint"]) / replica["object_key"]
    if damage == "payload":
        payload.write_bytes(b"wrong")
    elif damage == "missing":
        payload.unlink()
    else:
        manifest_path = Path(target["endpoint"]) / replica["manifest_key"]
        manifest = json.loads(manifest_path.read_text())
        manifest["record_id"] += 1000
        manifest_path.write_text(json.dumps(manifest))
    destination = tmp_path / "recovered"
    destination.mkdir()
    existing = destination / os.path.basename(replica["object_key"])
    existing.write_bytes(b"keep this original")
    with pytest.raises(pool.PoolError):
        pool.fetch_back(env.record_id, replica["id"], str(destination))
    assert existing.read_bytes() == b"keep this original"
    assert not list(destination.glob(".aidbm-fetch-*"))


def test_copy_must_survive_readback_before_success(env, tmp_path, monkeypatch):
    from core import storage_backends
    from core.storage_backends.local import LocalStorageBackend
    class CorruptingBackend(LocalStorageBackend):
        def save_file(self, path, key=None, **kwargs):
            ok = super().save_file(path, key, **kwargs)
            if key and not key.endswith(".json"):
                self._resolve_path(key).write_bytes(b"corrupt")
            return ok
    monkeypatch.setattr(storage_backends, "get_backend", lambda kind, target, log: CorruptingBackend(target, log))
    chain_id = pool_setup(env, tmp_path)
    result = pool.run_chain(chain_id, env.record_id, str(env.source))
    assert not result["ok"]
    assert [row["status"] for row in result["sites"]] == ["failed", "skipped", "skipped"]
    assert pool.replica_view(env.record_id)["n_ok"] == 0


def test_pool_source_checksum_mismatch_is_rejected(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    env.source.write_bytes(b"changed source")
    with pytest.raises(pool.PoolError, match="SHA256"):
        pool.run_chain(chain_id, env.record_id, str(env.source))


def test_repeat_copy_uses_unique_object_keys(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    first = pool.run_chain(chain_id, env.record_id, str(env.source))
    second = pool.run_chain(chain_id, env.record_id, str(env.source))
    assert first["object_key"] != second["object_key"]


def test_legacy_replica_not_counted_as_verified(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    pool.run_chain(chain_id, env.record_id, str(env.source))
    db.execute("UPDATE pool_replica_logs SET integrity_status='unknown'")
    view = pool.replica_view(env.record_id)
    assert view["n_ok"] == 0 and not any(row["recoverable"] for row in view["replicas"])


@pytest.mark.parametrize("suffix,position,expected", [("000002", 4, True), ("000003", 4, False),
                                                       ("000002", 99, False), ("000000", 4, False)])
def test_binlog_file_sequence(suffix, position, expected):
    previous = RecoveryPoint(rp_kind=RP_DB_LOG, binlog_end_file="mysql-bin.000001", binlog_end_pos=900)
    current = RecoveryPoint(rp_kind=RP_DB_LOG, binlog_file="mysql-bin." + suffix, binlog_pos=position)
    assert RecoveryJournal._check_position_continuity(previous, current)[0] is expected


def test_base_to_first_log_gap_is_rejected():
    previous = RecoveryPoint(rp_kind=RP_DB_FULL, binlog_file="mysql-bin.000001", binlog_pos=900)
    current = RecoveryPoint(rp_kind=RP_DB_LOG, binlog_file="mysql-bin.000001", binlog_pos=1000)
    assert not RecoveryJournal._check_position_continuity(previous, current)[0]


def test_preview_cannot_hide_log_gap():
    points = [RecoveryPoint(id=1, rp_kind=RP_DB_FULL, object_key="/base"),
              RecoveryPoint(id=2, rp_kind=RP_DB_LOG, parent_rp_id=1, object_key="/log1",
                            binlog_file="m.000001", binlog_pos=4, binlog_end_file="m.000001", binlog_end_pos=99),
              RecoveryPoint(id=3, rp_kind=RP_DB_LOG, parent_rp_id=2, object_key="/log3",
                            binlog_file="m.000003", binlog_pos=4)]
    assert not PITRRestore._quick_validate(points)[0]


def test_timezone_inputs_compare_in_catalog_timezone(monkeypatch):
    monkeypatch.setattr(db, "now_iso", lambda: "2026-11-20T21:00:00+08:00")
    assert normalize_ts("2026-11-20T13:00:00Z") == "2026-11-20T21:00:00+08:00"
    with pytest.raises(ValueError):
        normalize_ts("not a timestamp")


def pitr_points(env):
    journal = RecoveryJournal()
    base = journal.append(env.task_id, {"rp_kind": RP_DB_FULL, "pit_at": "2026-11-20T12:00:00+08:00",
                                       "object_key": str(env.source), "binlog_file": "m.000001", "binlog_pos": 4})
    log = env.root / "log"
    log.write_bytes(b"log-data")
    journal.append(env.task_id, {"rp_kind": RP_DB_LOG, "pit_at": "2026-11-20T12:10:00+08:00",
                                "object_key": str(log), "parent_rp_id": base.id,
                                "binlog_file": "m.000001", "binlog_pos": 4,
                                "binlog_end_file": "m.000001", "binlog_end_pos": 99})
    return PITRRestore()


def test_pitr_rejects_target_beyond_captured_logs(env):
    pitr = pitr_points(env)
    for strict in (False, True):
        plan = pitr.build_plan(env.task_id, "2026-11-20T12:11:00+08:00", strict=strict)
        assert not plan.complete and "覆盖" in plan.gap_reason


def test_latest_pitr_uses_last_capture_time(env):
    pitr = pitr_points(env)
    plan = pitr.build_plan(env.task_id, "")
    assert plan.complete and plan.target_ts == "2026-11-20T12:10:00+08:00"


def test_coverage_stops_at_gap_until_new_baseline():
    points = [
        RecoveryPoint(id=1, rp_kind=RP_DB_FULL, pit_at="01"),
        RecoveryPoint(id=2, rp_kind=RP_DB_LOG, pit_at="02", parent_rp_id=1,
                      binlog_end_file="m.000001", binlog_end_pos=100),
        RecoveryPoint(id=4, rp_kind=RP_DB_LOG, pit_at="04", parent_rp_id=2,
                      binlog_file="m.000003", binlog_pos=4),
        RecoveryPoint(id=5, rp_kind=RP_DB_FULL, pit_at="05"),
        RecoveryPoint(id=6, rp_kind=RP_DB_LOG, pit_at="06", parent_rp_id=5),
    ]
    result = RecoveryJournal().coverage_segments(points)
    assert [(seg["start"], seg["end"]) for seg in result["segments"]] == [("01", "02"), ("05", "06")]
    assert len(result["gaps"]) == 1

def test_replica_remains_recoverable_after_chain_deletion(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    pool.run_chain(chain_id, env.record_id, str(env.source))
    digest = db.sha256_file(str(env.source))
    pool.delete_chain(chain_id)
    env.source.unlink()
    view = pool.replica_view(env.record_id)
    assert view["n_ok"] == 3
    out = pool.fetch_back(env.record_id, view["replicas"][-1]["id"], str(tmp_path / "recovered"))
    assert out["verified"] and db.sha256_file(out["dest"]) == digest


def test_forced_partial_recovery_is_never_reported_success(env, monkeypatch):
    pitr = pitr_points(env)
    monkeypatch.setattr(pitr, "_restore_db", lambda *args: {"ok": True, "message": "command exited zero"})
    result = pitr.restore(env.task_id, "2026-11-20T12:11:00+08:00", force=True)
    assert not result["ok"] and "无法确认" in result["message"]
    row = db.query_one("SELECT status FROM restore_records WHERE id=?", (result["restore_id"],))
    assert row["status"] == "failed"


def test_ai_verification_uses_same_evidence_contract(env):
    from core.ai_agent.autonomous import _act_restore_verify
    result = _act_restore_verify({"task_id": env.task_id})
    assert result["policy_id"] and result["report_id"]
    assert result["status"] == "partial" and not result["success"]


def test_invalid_wal_lsn_does_not_hide_a_gap():
    previous = RecoveryPoint(rp_kind=RP_DB_LOG, wal_end_lsn="0/1A2B")
    current = RecoveryPoint(rp_kind=RP_DB_LOG, wal_lsn="invalid")
    assert not RecoveryJournal._check_position_continuity(previous, current)[0]

def test_independent_site_import_without_source_catalog(env, tmp_path):
    chain_id = pool_setup(env, tmp_path)
    pool.run_chain(chain_id, env.record_id, str(env.source))
    replica = pool.replica_view(env.record_id)["replicas"][0]
    digest = db.sha256_file(str(env.source))
    source_time = db.query_one("SELECT started_at FROM backup_records WHERE id=?", (env.record_id,))["started_at"]
    env.source.unlink()
    db.execute("DELETE FROM pool_replica_logs")
    db.execute("DELETE FROM pool_chains")
    db.execute("DELETE FROM backup_records WHERE id=?", (env.record_id,))
    standby_task = db.execute("INSERT INTO backup_tasks(name,db_type,enabled) VALUES ('standby','postgresql',0)")
    result = pool.import_replica(replica["target_id"], replica["manifest_key"], standby_task, digest)
    assert result["ok"] and result["verified"] and not result["runtime_verified"]
    record = models.get_record(result["record_id"])
    assert record["task_id"] == standby_task and record["checksum"] == digest
    assert record["started_at"] == source_time
    assert db.sha256_file(result["dest"]) == digest
    repeated = pool.import_replica(replica["target_id"], replica["manifest_key"], standby_task, digest)
    assert repeated["record_id"] == result["record_id"] and repeated["reused"]


@pytest.mark.parametrize("damage", ["incremental", "type", "path", "checksum", "timestamp"])
def test_import_manifest_rejects_incomplete_or_wrong_copy(env, tmp_path, damage):
    from pathlib import Path
    chain_id = pool_setup(env, tmp_path)
    pool.run_chain(chain_id, env.record_id, str(env.source))
    replica = pool.replica_view(env.record_id)["replicas"][0]
    target = db.query_one("SELECT endpoint FROM storage_targets WHERE id=?", (replica["target_id"],))
    path = Path(target["endpoint"]) / replica["manifest_key"]
    manifest = json.loads(path.read_text())
    if damage == "incremental":
        manifest["backup_type"] = "incremental"
    elif damage == "type":
        manifest["db_type"] = "mysql"
    elif damage == "path":
        manifest["object_key"] = "other/file"
    elif damage == "timestamp":
        manifest["started_at"] = "invalid"
    else:
        manifest["checksum"] = "a" * 64
    path.write_text(json.dumps(manifest))
    before = db.query_one("SELECT COUNT(*) AS c FROM backup_records")["c"]
    with pytest.raises(pool.PoolError):
        pool.import_replica(replica["target_id"], replica["manifest_key"], env.task_id)
    assert db.query_one("SELECT COUNT(*) AS c FROM backup_records")["c"] == before
