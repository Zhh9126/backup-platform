"""Real HTTP + local pool I/O + archive restore + SQLite query, isolated from preview metadata."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
base = ROOT / "build" / "zbackup-qa"
base.mkdir(parents=True, exist_ok=True)
run = Path(tempfile.mkdtemp(prefix="http-", dir=base))
for name in ("instance", "backups", "logs", "tmp"):
    (run / name).mkdir()
os.environ.update(INSTANCE_DIR=str(run / "instance"), META_DB_PATH=str(run / "instance" / "meta.db"),
                  META_DB_BACKEND="sqlite", BACKUP_ROOT=str(run / "backups"),
                  LOG_DIR=str(run / "logs"), TMPDIR=str(run / "tmp"),
                  SCHEDULER_ENABLED="0", AIDBM_TESTING="1", AIDBM_RANDOM_ADMIN_PASSWORD="1",
                  CODEBUDDY_SAFE_DELETE_ENABLED="0", DEMO_MODE="off")
sys.path.insert(0, str(ROOT))

import requests
from werkzeug.serving import make_server
from app import create_app
from core import db, models
from core.rt_backup.journal import RecoveryJournal

def main():
    app = create_app()
    # Actual sample database and a file backup containing it.
    sample = run / "sample.sqlite"
    with sqlite3.connect(sample) as conn:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO items(value) VALUES(?)", [("row-%d" % n,) for n in range(512)])
    source = run / "backups" / "sample.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        archive.add(sample, arcname="sample.sqlite")
    digest = db.sha256_file(str(source))
    task_id = db.execute("INSERT INTO backup_tasks(name,db_type,db_name,enabled) VALUES ('e2e-file','file','',0)")
    record_id = models.create_record({"task_id": task_id, "db_type": "file", "status": "success",
                                      "backup_type": "FULL", "backup_path": str(source),
                                      "size_bytes": source.stat().st_size, "checksum": digest,
                                      "started_at": db.now_iso()})
    targets = []
    for n in range(3):
        targets.append(db.execute("INSERT INTO storage_targets(name,type,endpoint,enabled) VALUES (?,?,?,1)",
                                  ("e2e-pool-%d" % n, "local", str(run / ("pool-%d" % n)))))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    session = requests.Session()
    session.trust_env = False
    url = "http://127.0.0.1:%d" % server.server_port
    results = {}
    def request(method, path, payload=None, expected=200):
        response = session.request(method, url + path, json=payload, timeout=45)
        assert response.status_code == expected, (path, response.status_code, response.text[:500])
        return response.json()
    try:
        username, password = (run / "instance" / "initial_admin_password.txt").read_text().strip().split(" / ", 1)
        assert request("POST", "/login", {"username": username, "password": password})["ok"]
        policy = request("POST", "/api/restore-verify-policies",
                         {"task_id": task_id, "name": "e2e-policy", "schedule_type": "manual"}, expected=201)["data"]["id"]
        partial = request("POST", "/api/restore-verify-policies/%d/test" % policy, {})["data"]
        assert partial["status"] == "partial" and not partial["success"]
        stats = request("GET", "/api/restore-verify-stats")["data"]
        assert stats["partial_count"] == 1 and stats["success_count"] == 0
        results["http_artifact_check_reported_partial"] = True
        chain_id = request("POST", "/api/pool/chains", {"name": "e2e-chain",
            "sites": [{"target_id": t} for t in targets],
            "mode": "parallel", "policy": {"max_retries": 1}}, expected=201)["id"]
        copied = request("POST", "/api/pool/chains/%d/run" % chain_id, {"record_id": record_id})["result"]
        assert copied["ok"] and len(copied["sites"]) == 3
        assert all(row["integrity_status"] == "verified" for row in copied["sites"])
        results["three_directory_replicas_read_back_verified"] = True
        source.unlink()
        replicas = request("GET", "/api/pool/replicas?record_id=%d" % record_id)
        assert replicas["n_ok"] == 3
        replica = replicas["replicas"][-1]
        fetched = request("POST", "/api/pool/fetch", {"record_id": record_id, "log_id": replica["id"]})["result"]
        recovered_archive = Path(fetched["dest"])
        assert fetched["verified"] and db.sha256_file(str(recovered_archive)) == digest
        recovered_database = run / "restored.sqlite"
        with tarfile.open(recovered_archive, "r:gz") as archive:
            with archive.extractfile("sample.sqlite") as stream:
                recovered_database.write_bytes(stream.read())
        with sqlite3.connect(recovered_database) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count == 512
        results["deleted_original_restored_with_sqlite_query"] = {"rows": count, "archive_sha256": digest}
        target = db.query_one("SELECT endpoint FROM storage_targets WHERE id=?", (replica["target_id"],))
        (Path(target["endpoint"]) / replica["object_key"]).write_bytes(b"damaged")
        request("POST", "/api/pool/fetch", {"record_id": record_id, "log_id": replica["id"]}, expected=400)
        assert db.sha256_file(str(recovered_archive)) == digest
        results["corrupt_replica_rejected_existing_restore_preserved"] = True
        # Independent site registration with only storage configuration and the sidecar left.
        intact = replicas["replicas"][0]
        db.execute("DELETE FROM pool_replica_logs WHERE record_id=?", (record_id,))
        db.execute("DELETE FROM pool_chains WHERE id=?", (chain_id,))
        db.execute("DELETE FROM backup_records WHERE id=?", (record_id,))
        standby_task = db.execute("INSERT INTO backup_tasks(name,db_type,enabled) VALUES ('e2e-standby','file',0)")
        imported = request("POST", "/api/pool/import", {
            "target_id": intact["target_id"], "manifest_key": intact["manifest_key"],
            "task_id": standby_task, "expected_checksum": digest})
        assert imported["verified"] and db.sha256_file(imported["dest"]) == digest
        assert models.get_record(imported["record_id"])["task_id"] == standby_task
        results["standby_import_without_source_record_or_chain"] = True
        pitr_task = db.execute("INSERT INTO backup_tasks(name,db_type,enabled) VALUES ('e2e-pitr-metadata','mysql',0)")
        journal = RecoveryJournal()
        base_point = journal.append(pitr_task, {"rp_kind": "db-full", "pit_at": "2026-11-20T12:00:00+08:00",
                                               "object_key": str(recovered_archive),
                                               "binlog_file": "m.000001", "binlog_pos": 4})
        journal.append(pitr_task, {"rp_kind": "db-log", "pit_at": "2026-11-20T12:10:00+08:00",
                                   "object_key": str(sample), "parent_rp_id": base_point.id,
                                   "binlog_file": "m.000001", "binlog_pos": 4,
                                   "binlog_end_file": "m.000001", "binlog_end_pos": 100})
        preview = request("POST", "/api/rt/preview", {"task_id": pitr_task, "target_ts": "2026-11-20T12:11:00+08:00"})["plan"]
        assert not preview["complete"] and "覆盖" in preview["gap_reason"]
        window = request("GET", "/api/rt/window?task_id=%d" % pitr_task)
        assert len(window["coverage_segments"]) == 1 and window["coverage_validation"] == "metadata_only"
        results["http_pitr_outside_metadata_coverage_blocked"] = True
        for path in ("/restore-verify", "/storage", "/realtime"):
            response = session.get(url + path, timeout=30)
            # Known page routes are checked below; 404 is not accepted.
            assert response.status_code == 200, (path, response.status_code)
        results["page_http_smoke"] = True
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        session.close()
    (base / "http-e2e.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
