"""Cross-process journal publication and conflicting clone lifecycle regressions."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import os
import threading
import time
from unittest.mock import Mock
import pytest
import config
from core import db, models, lease, clone_preflight
from core.clone_service import CloneService
from core.rt_backup.journal import RecoveryJournal
from core.rt_backup.repo import LogRepository

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path))
    monkeypatch.setattr(db, "_ACTIVE", {"backend": "sqlite"})
    db.init_schema()
    task = models.create_task({"name":"qa", "db_type":"mysql", "host":"localhost",
                               "db_name":"source", "schedule_type":"manual"})
    yield task
    db.reset_thread_conn()

def _append_process(args):
    task, key = args
    db.reset_thread_conn()
    point=RecoveryJournal().append(task, {"object_key":key, "pit_at":"2026-11-20T10:00:00",
                                        "rp_kind":"db-log"})
    db.reset_thread_conn()
    return point.id,point.pit_seq

def test_cross_process_same_second_is_sequential(isolated):
    with ProcessPoolExecutor(max_workers=6) as pool:
        result=list(pool.map(_append_process, [(isolated,"segment-%s"%i) for i in range(48)]))
    assert len({r[0] for r in result})==48
    assert sorted(r[1] for r in result)==list(range(48))

def test_duplicate_segment_cross_process_is_idempotent(isolated):
    with ProcessPoolExecutor(max_workers=6) as pool:
        result=list(pool.map(_append_process, [(isolated,"same-segment")]*24))
    assert len(set(result))==1
    assert models.count_recovery_points(isolated)==1

def test_idempotent_retry_preserves_verification(isolated):
    rid=models.create_recovery_point({"task_id":isolated,"object_key":"segment",
                                     "verified":1,"verify_msg":"valid","storage_tier":3})
    assert models.create_recovery_point({"task_id":isolated,"object_key":"segment"})==rid
    row=models.get_recovery_point(rid)
    assert row["verified"]==1 and row["verify_msg"]=="valid" and row["storage_tier"]==3

def test_atomic_publication_failure_keeps_previous_file(tmp_path,monkeypatch):
    target=tmp_path/"state.json"
    target.write_bytes(b"previous-valid-state")
    monkeypatch.setattr(os,"replace",Mock(side_effect=OSError("injected publish failure")))
    with pytest.raises(OSError):
        LogRepository.atomic_write(lambda p: open(p,"wb").write(b"replacement"),str(target))
    assert target.read_bytes()==b"previous-valid-state"

def _request(status="ready"):
    return models.create_clone_request({"source_record_id":1,"target_env":"test",
                                        "requested_by":"qa","status":status})

def test_active_provisioner_is_not_swept(isolated):
    rid=_request("creating")
    old=(datetime.now()-timedelta(hours=2)).isoformat()
    db.execute("UPDATE clone_requests SET updated_at=? WHERE id=?",(old,rid))
    lease.acquire("clone-request:%s"%rid,owner="active-worker",ttl=90)
    assert CloneService()._sweep_stuck_creating()==0
    assert models.get_clone_request(rid)["status"]=="creating"
    lease.release("clone-request:%s"%rid,owner="active-worker")
    assert CloneService()._sweep_stuck_creating()==1

def test_destroy_excludes_parallel_snapshot(isolated,monkeypatch):
    rid=_request()
    started,release=threading.Event(),threading.Event()
    svc=CloneService()
    def destroy(req):
        started.set()
        assert release.wait(5)
    monkeypatch.setattr(svc,"_release_vdb",destroy)
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(svc.destroy_clone,rid)
        assert started.wait(3)
        with pytest.raises(ValueError,match="正在执行"):
            svc.create_snapshot(rid,"cannot-race")
        release.set()
        future.result()
    assert models.get_clone_request(rid)["status"]=="deleted"

def test_expiry_does_not_reclaim_creating(isolated,monkeypatch):
    rid=_request("creating")
    svc=CloneService()
    released=Mock()
    monkeypatch.setattr(svc,"_release_vdb",released)
    assert svc.expire_clone(rid)["status"]=="creating"
    released.assert_not_called()

def test_clone_preflight_rejects_tampered_archive(isolated,tmp_path):
    artifact=tmp_path/"backup.sql"
    artifact.write_bytes(b"backup-data")
    rec={"id":1,"db_type":"mysql","status":"success","backup_path":str(artifact),
         "checksum":"sha256:"+hashlib.sha256(b"other-data").hexdigest()}
    result=clone_preflight.preflight(rec)
    assert not result["ok"]
    assert any("SHA256" in e for e in result["errors"])

def _capture(task, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.rt_backup import db_rt
    monkeypatch.setattr(config,"RT_LOG_ROOT",str(tmp_path/"rt"))
    monkeypatch.setattr(db_rt,"create_daemon",lambda *a,**k:
                        SimpleNamespace(degrade_reason="",is_simulated=False))
    worker=db_rt.DbRtCapture(models.get_task(task))
    monkeypatch.setattr(worker,"_resolve_parent_id",lambda: None)
    return worker

def test_failed_log_publication_retries_before_later_segments(isolated,tmp_path,monkeypatch):
    worker=_capture(isolated,tmp_path,monkeypatch)
    paths=[]
    for i in range(2):
        path=tmp_path/("log-%s"%i)
        path.write_bytes(("sealed-%s"%i).encode())
        paths.append({"path":str(path),"size":path.stat().st_size,
                      "checksum":db.sha256_file(str(path))})
    original=worker.journal.append
    monkeypatch.setattr(worker.journal,"append",Mock(side_effect=OSError("database unavailable")))
    assert worker._register_segments(paths)==[]
    assert len(worker._pending_segments)==2
    assert models.count_recovery_points(isolated)==0
    monkeypatch.setattr(worker.journal,"append",original)
    recovered=worker._register_segments([])
    assert len(recovered)==2 and len(worker._pending_segments)==0
    assert worker.rp_count==2
    assert worker._register_segments(paths)==[]
    assert worker.rp_count==2

def test_corrupt_sealed_log_blocks_later_publication(isolated,tmp_path,monkeypatch):
    worker=_capture(isolated,tmp_path,monkeypatch)
    path=tmp_path/"damaged-log"
    path.write_bytes(b"corrupted")
    points=worker._register_segments([{"path":str(path),"size":9,
                                      "checksum":hashlib.sha256(b"valid").hexdigest()}])
    assert points==[] and models.count_recovery_points(isolated)==0
    assert "SHA256" in worker.last_error
    assert str(path) in worker._pending_segments
