"""Persist actual, partial and failed test evidence without fabricating protection."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import config
from core import db,test_records as records
from core.test_record_import import import_junit,import_pytest_log

@pytest.fixture
def registry(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"META_DB_PATH",str(tmp_path/"meta.db"))
    monkeypatch.setattr(db,"_ACTIVE",{"backend":"sqlite"})
    monkeypatch.setattr(records,"ROOT",tmp_path)
    (tmp_path/"build").mkdir()
    db.init_schema()
    yield tmp_path
    db.reset_thread_conn()

def run(key="real",**patch):
    value={"source_key":key,"category":"vm","title":"real VM test","status":"partial",
           "evidence_level":"external_real","evidence":{"checks":[{"name":"boot","status":"passed"},{"name":"data","status":"failed"}]}}
    value.update(patch)
    return value

def test_partial_and_failed_history_and_idempotency(registry):
    rid=records.save_run(run())
    assert records.save_run(run())==rid
    records.save_run(run("failed",status="failed"))
    assert records.list_runs(category="vm")["total"]==2
    assert records.list_runs(category="vm")["counts"]["partial"]==1
    assert records.list_runs(status="failed")["total"]==1
    assert records.list_runs(q="nonexistent")["total"]==0

def test_real_success_requires_checks_without_failures(registry):
    with pytest.raises(ValueError):
        records.save_run(run(status="passed"))
    with pytest.raises(ValueError):
        records.save_run(run(status="passed",evidence={}))
    rid=records.save_run(run(status="passed",evidence={"checks":[{"name":"read","status":"passed"}]}))
    assert records.get_run(rid)["evidence_level"]=="external_real"

def test_secrets_are_redacted_and_artifact_download_is_blocked(registry):
    archive=registry/"build"/"full.tar.gz"
    archive.write_bytes(b"do-not-download")
    rid=records.save_run(run(evidence={"password":"private","token":"ghp_abcdefghijklmnopqrstuvwxyz",
        "files":[{"path":"build/full.tar.gz","kind":"backup"}]}))
    assert records.get_run(rid)["evidence"]["password"]=="[REDACTED]"
    with pytest.raises(ValueError,match="仅展示"):
        records.read_evidence(rid,0)

def test_path_escape_and_symlinks_are_rejected(registry,tmp_path):
    outside=registry/"outside.log";outside.write_text("outside")
    linked=registry/"build"/"link.log";linked.symlink_to(outside)
    rid=records.save_run(run(evidence={"files":[{"path":"outside.log","kind":"log"},{"path":"build/link.log","kind":"log"}]}))
    for index in (0,1):
        with pytest.raises(ValueError,match="允许目录"):
            records.read_evidence(rid,index)

def test_bounded_log_preview_and_redaction(registry):
    log=registry/"build"/"run.log"
    log.write_text("password=private ghp_abcdefghijklmnopqrstuvwxyz\n"+"x"*70000)
    rid=records.save_run(run(evidence={"files":[{"path":"build/run.log","kind":"log"}]}))
    result=records.read_evidence(rid,0)
    assert result["truncated"] and "private" not in result["text"]
    assert "ghp_" not in result["text"]

def test_junit_contains_every_case_and_failure_is_preserved(registry,monkeypatch):
    from core import test_record_import as imports
    monkeypatch.setattr(imports,"ROOT",registry)
    path=registry/"build"/"pytest.xml"
    path.write_text('<testsuites><testsuite><testcase classname="vm" name="boot"/><testcase classname="vm" name="data"><failure>mismatch</failure></testcase><testcase name="optional"><skipped/></testcase></testsuite></testsuites>')
    rid=import_junit(path,category="vm",exit_code=1)
    row=records.get_run(rid)
    assert row["status"]=="failed"
    assert row["evidence"]["counts"]=={"passed":1,"failed":1,"skipped":1}
    assert len(row["evidence"]["checks"])==3

def test_failed_historical_summary_not_changed_to_success(registry,monkeypatch):
    from core import test_record_import as imports
    monkeypatch.setattr(imports,"ROOT",registry)
    path=registry/"build"/"regression.log"
    path.write_text("previous lines\n2 failed, 100 passed, 1 skipped in 2.0s\n")
    rid=import_pytest_log(path)
    row=records.get_run(rid)
    assert row["status"]=="failed"
    assert row["evidence"]["counts"]["failed"]==2
