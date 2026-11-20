"""Recovery catalogue and object/VM execution contracts for the unified recovery page."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest
import config
from core import db, models
from core.recovery_catalog import catalog
from core.engines.base import BackupResult
from core.engines.object_storage import ObjectStorageEngine
from core.objectstore import providers, client


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "DEMO_MODE", "off")
    (tmp_path / "backups").mkdir()
    db.init_schema(backend="sqlite")
    def task(kind, name, extra=None):
        return db.execute("INSERT INTO backup_tasks(name,db_type,host,db_name,backup_mode,extra_options,enabled) "
                          "VALUES (?,?,?,?,?,?,0)", (name, kind, "source.example.invalid", "source-db", "logical",
                                                     json.dumps(extra or {})))
    tasks = {kind: task(kind, "task-" + kind, {"os_provider": "obs"} if kind == "object_storage" else {})
             for kind in ("oracle", "file", "object_storage", "synology", "vm")}
    records = {}
    for kind, tid in tasks.items():
        source = tmp_path / (kind + ".bin")
        source.write_bytes((kind * 20).encode())
        records[kind] = models.create_record({"task_id": tid, "db_type": kind, "backup_type": "full",
            "backup_mode": "physical" if kind == "oracle" else kind if kind != "object_storage" else "object",
            "status": "success", "started_at": db.now_iso(), "backup_path": str(source),
            "checksum": db.sha256_file(str(source)), "size_bytes": source.stat().st_size})
    hv = db.execute("INSERT INTO vm_hypervisors(name,provider,endpoint) VALUES ('test-hv','pve','https://example.invalid')")
    vm_id = db.execute("INSERT INTO vm_protected(hypervisor_id,task_id,vm_ref,vm_name,node) VALUES (?,?,?,?,?)",
                       (hv, tasks["vm"], "101", "protected-vm", "node-a"))
    rp = db.execute("INSERT INTO vm_recovery_points(vm_id,task_id,record_id,pit_at,artifact_path,size_bytes) "
                    "VALUES (?,?,?,?,?,?)", (vm_id, tasks["vm"], records["vm"], db.now_iso(),
                                            str(tmp_path / "vm.bin"), 40))
    return SimpleNamespace(root=tmp_path, tasks=tasks, records=records, vm_id=vm_id, rp=rp, task=task)


def test_all_recovery_types_are_disjoint_and_obs_is_object_storage(env):
    result = catalog()
    assert result["counts"] == {"db": 1, "file": 2, "obj": 1, "vm": 1}
    assert result["items"][0]["db_type"] == "oracle"
    obj = catalog("obj")["items"][0]
    assert obj["provider"] == "obs"
    assert "extra_options" not in obj and "password" not in obj
    assert catalog("vm")["items"][0]["vm_id"] == env.vm_id


def test_vm_record_deep_link_uses_vm_recovery_point_not_database_restore(env):
    result = catalog(record_id=env.records["vm"])
    assert result["category"] == "vm"
    assert result["items"][0]["source_id"] == env.rp


def test_catalog_never_offers_failed_or_simulated_backups(env):
    db.execute("UPDATE backup_records SET status='failed' WHERE id=?", (env.records["oracle"],))
    db.execute("UPDATE backup_records SET is_simulated=1 WHERE id=?", (env.records["file"],))
    db.execute("UPDATE vm_recovery_points SET is_simulated=1")
    assert catalog()["counts"] == {"db": 0, "file": 1, "obj": 1, "vm": 0}


def test_catalog_pages_all_records_and_filters_without_secrets(env):
    for n in range(21):
        models.create_record({"task_id": env.tasks["oracle"], "db_type": "oracle", "status": "success",
                              "backup_path": "/artifact", "started_at": db.now_iso(), "size_bytes": n})
    result = catalog("db", keyword="task-oracle", page=2, size=8)
    assert result["total"] == 22 and result["pages"] == 3 and len(result["items"]) == 8
    assert not ({"password", "username", "extra_options"} & set(result["items"][0]))


def test_unknown_deep_link_does_not_select_an_unrelated_backup(env):
    assert catalog(record_id=999999)["items"] == []


def test_catalog_distinguishes_record_ids_from_vm_point_ids(env):
    database = catalog(record_id=env.records["oracle"])["items"][0]
    vm = catalog(rp_id=env.rp)["items"][0]
    assert database["key"] != vm["key"] and vm["category"] == "vm"


class Bucket:
    def __init__(self, existing=False, forbidden=False):
        self.objects = {("dest", "a.txt"): b"existing"} if existing else {}
        self.forbidden = forbidden
        self.calls = []
    def bucket_exists(self, bucket):
        return True
    def head_object(self, bucket, key):
        if self.forbidden:
            raise client.ObjectStoreError("AccessDenied", status=403, code="AccessDenied")
        if (bucket, key) not in self.objects:
            raise client.ObjectStoreError("NoSuchKey", status=404, code="NoSuchKey")
        return {"size": len(self.objects[(bucket, key)]), "etag": "different",
                "last_modified": "2030-01-01T00:00:00Z"}
    def upload_file(self, bucket, key, source):
        self.calls.append((bucket, key))
        self.objects[(bucket, key)] = Path(source).read_bytes()


def package(env, bad_hash=False):
    stage = env.root / "package"
    (stage / "objects").mkdir(parents=True)
    data = b"recover-this-object"
    (stage / "objects/a.txt").write_bytes(data)
    manifest = {"version": 1, "backup_type": "full", "bucket": "original",
                "objects": [{"key": "a.txt", "path": "objects/a.txt", "size": len(data),
                             "etag": "src-etag", "last_modified": "2026-01-01T00:00:00Z",
                             "sha256": ("a" * 64 if bad_hash else hashlib.sha256(data).hexdigest())}],
                "deleted": []}
    (stage / "manifest.json").write_text(json.dumps(manifest))
    archive = env.root / "objects.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(stage / "objects", arcname="objects")
        stream.add(stage / "manifest.json", arcname="manifest.json")
    task = {"id": env.tasks["object_storage"], "name": "obs-source", "db_type": "object_storage",
            "host": "source.example.invalid", "db_name": "original", "extra_options": '{"os_provider":"obs"}'}
    return ObjectStorageEngine(task, config.BACKUP_ROOT), str(archive), data


def test_obs_archive_restores_to_selected_other_connection(env, monkeypatch):
    engine, archive, data = package(env)
    original, destination = Bucket(), Bucket()
    engine._cli = original
    target_id = env.task("object_storage", "target-obs", {"os_provider": "obs"})
    captured = []
    monkeypatch.setattr(providers, "client_from_task",
                        lambda task: captured.append(task["id"]) or destination)
    result = engine.restore(archive, target_task_id=target_id, target_bucket="dest",
                            target_prefix="drill", overwrite="never")
    assert result.success, result.message
    assert destination.objects[("dest", "drill/a.txt")] == data
    assert not original.calls and captured == [target_id]


@pytest.mark.parametrize("strategy", ["never", "skip", "if_newer"])
def test_object_restore_preserves_existing_objects_when_requested(env, strategy):
    engine, archive, _ = package(env)
    engine._cli = Bucket(existing=True)
    result = engine.restore(archive, target_bucket="dest", overwrite=strategy)
    assert result.success and engine._cli.objects[("dest", "a.txt")] == b"existing"
    assert not engine._cli.calls


def test_head_permission_failure_does_not_fall_through_to_overwrite(env):
    engine, archive, _ = package(env)
    engine._cli = Bucket(forbidden=True)
    result = engine.restore(archive, target_bucket="dest", overwrite="never")
    assert not result.success and not engine._cli.calls


def test_corrupt_object_payload_is_never_uploaded(env):
    engine, archive, _ = package(env, bad_hash=True)
    engine._cli = Bucket()
    result = engine.restore(archive, target_bucket="dest", overwrite="always")
    assert not result.success and not engine._cli.calls


def test_invalid_overwrite_strategy_is_rejected(env):
    engine, archive, _ = package(env)
    engine._cli = Bucket()
    result = engine.restore(archive, target_bucket="dest", overwrite="unknown")
    assert not result.success and not engine._cli.calls


def test_vm_recovery_workers_do_not_overlap_for_same_vm(env, monkeypatch):
    import core.vm as vm
    entered, released = threading.Event(), threading.Event()
    calls = []
    def restore(*args, **kwargs):
        calls.append(kwargs["mode"])
        entered.set()
        assert released.wait(5)
        return BackupResult(success=True, message="stub VM provider completed")
    monkeypatch.setattr(vm, "_engine_for", lambda record: SimpleNamespace(restore=restore))
    first = vm.restore_in_place(env.vm_id, env.rp)
    assert entered.wait(5)
    second = vm.restore_in_place(env.vm_id, env.rp)
    try:
        for _ in range(100):
            job = models.get_vm_job(second["job_id"])
            if job["status"] == "failed":
                break
            time.sleep(.01)
        assert job["status"] == "failed" and "正在执行" in job["message"]
        assert calls == ["restore_in_place"]
    finally:
        released.set()
    for _ in range(100):
        if models.get_vm_job(first["job_id"])["status"] == "ready":
            break
        time.sleep(.01)
    assert models.get_vm_job(first["job_id"])["status"] == "ready"
