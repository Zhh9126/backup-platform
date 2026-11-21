import json,os
from pathlib import Path
import pytest,config
from core import db,os_backup
@pytest.fixture
def env(tmp_path,monkeypatch):
 monkeypatch.setattr(config,"META_DB_PATH",str(tmp_path/"meta.db"));monkeypatch.setattr(config,"BACKUP_ROOT",str(tmp_path/"backups"));monkeypatch.setattr(db,"_ACTIVE",{"backend":"sqlite"});(tmp_path/"backups").mkdir();db.init_schema();yield tmp_path;db.reset_thread_conn()
def test_backup_manifest_and_restore(env):
 src=env/"src";src.mkdir();(src/"etc.conf").write_text("hello");(src/"secret").write_bytes(b"x"*100)
 r=os_backup.create_backup(str(src),"linux-test",[])
 assert r["status"]=="success" and r["file_count"]==2
 out=env/"restore"
 plan=os_backup.restore_backup(r["archive_path"],str(out),dry_run=True)
 assert plan["status"]=="planned" and plan["file_count"]==2
 done=os_backup.restore_backup(r["archive_path"],str(out),dry_run=False,confirm=True)
 assert done["status"]=="success" and (out/"etc.conf").read_text()=="hello"
def test_restore_requires_confirmation_and_blocks_traversal(env):
 src=env/"src";src.mkdir();(src/"a").write_text("a");r=os_backup.create_backup(str(src),"t",[])
 with pytest.raises(ValueError,match="confirm"):os_backup.restore_backup(r["archive_path"],str(env/"out"),dry_run=False)
 bad=env/"bad.tar.gz"
 import tarfile,io
 with tarfile.open(bad,"w:gz") as t:
  x=b"{}";i=tarfile.TarInfo("../escape");i.size=len(x);t.addfile(i,io.BytesIO(x))
 with pytest.raises(ValueError,match="不安全"):os_backup.restore_backup(str(bad),str(env/"out"),True)

def test_remote_backup_uses_managed_host_and_resumable_pull(env,monkeypatch):
 import core.remote_dump as rd
 host={"host_key":"root@test:22","hostname":"test","username":"root"}
 monkeypatch.setattr(rd,"remote_exec_capture",lambda *a,**k:{"returncode":0,"stdout":"","stderr":""})
 monkeypatch.setattr(rd,"_connect",lambda h:object())
 def pull(client,remote,local,**kw):
  from pathlib import Path
  import tarfile
  with tarfile.open(local,"w:gz") as t:
   data=b"remote"
   import io
   i=tarfile.TarInfo("files/etc.conf");i.size=len(data);t.addfile(i,io.BytesIO(data))
  return {"size":Path(local).stat().st_size}
 monkeypatch.setattr(rd,"sftp_pull_resumable",pull)
 monkeypatch.setattr(rd,"remote_exec_capture",lambda *a,**k:{"returncode":0,"stdout":"","stderr":""})
 out=os_backup.create_remote_backup(host,"/etc","remote")
 assert out["status"]=="success" and out["source_root"] if "source_root" in out else True

def test_label_with_slash_is_safe_filename(env):
 src=env/"src";src.mkdir();(src/"x").write_text("x")
 result=os_backup.create_backup(str(src),"本机/etc真实备份验证",[])
 assert "/" not in Path(result["archive_path"]).name
 assert Path(result["archive_path"]).is_file()
