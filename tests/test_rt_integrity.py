import hashlib
from pathlib import Path
from core import db,models
from core.rt_backup.journal import RecoveryJournal
def test_journal_integrity_detects_tamper(tmp_path,monkeypatch):
 monkeypatch.setattr(db,"_ACTIVE",{"backend":"sqlite"});monkeypatch.setattr(__import__("config"),"META_DB_PATH",str(tmp_path/"m.db"));db.init_schema()
 t=models.create_task({"name":"rt","db_type":"file","host":"x","db_name":"x"})
 f=tmp_path/"seg";f.write_bytes(b"valid")
 models.create_recovery_point({"task_id":t,"rp_kind":"file-inc","rp_type":"incremental","object_key":str(f),"pit_at":"2026-01-01T00:00:00+08:00","pit_seq":0,"checksum":hashlib.sha256(b"valid").hexdigest(),"size_bytes":5})
 f.write_bytes(b"tampered")
 out=RecoveryJournal().integrity(t)
 assert out["status"]=="failed" and any("SHA256" in x for x in out["issues"])
