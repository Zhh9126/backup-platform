import os,time
from pathlib import Path
import pytest,config
from core import db,os_backup

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"META_DB_PATH",str(tmp_path/"meta.db"))
    monkeypatch.setattr(config,"BACKUP_ROOT",str(tmp_path/"backups"))
    monkeypatch.setattr(db,"_ACTIVE",{"backend":"sqlite"})
    (tmp_path/"backups").mkdir()
    db.init_schema()
    yield tmp_path
    db.reset_thread_conn()

def _wait(rid,timeout=60):
    deadline=time.time()+timeout
    while time.time()<deadline:
        rec=os_backup.get_record(rid)
        if rec and rec.get("status") in ("success","failed"): return rec
        time.sleep(0.2)
    return os_backup.get_record(rid)

def test_start_backup_runs_in_background_and_reaches_success(env):
    src=env/"src";src.mkdir();(src/"a.conf").write_text("hello-os");(src/"b.bin").write_bytes(b"x"*2048)
    rid=os_backup.start_backup(str(src),"async-test")
    assert isinstance(rid,int) and rid>0
    rec=_wait(rid)
    assert rec["status"]=="success",rec
    assert rec["file_count"]==2 and rec["size_bytes"]>0 and rec["checksum"]
    assert Path(rec["archive_path"]).is_file()
    assert rec["started_at"] and rec["finished_at"]

def test_source_root_is_honored_not_forced_to_root(env):
    src=env/"etc-like";src.mkdir();(src/"hosts").write_text("127.0.0.1 localhost")
    rid=os_backup.start_backup(str(src),"scope-check")
    rec=_wait(rid)
    assert rec["status"]=="success"
    assert rec["source_root"]==str(src.resolve())

def test_platform_backup_root_is_excluded_from_archive(env,monkeypatch):
    """备份源位于平台产物目录之上时，平台自身产物不得进入归档（防自包含）。"""
    src=env/"whole";src.mkdir()
    (src/"keep.txt").write_text("keep")
    plat=src/"backups";(plat/"os_backups").mkdir(parents=True)
    (plat/"os_backups"/"self.tar.gz").write_bytes(b"self")
    monkeypatch.setattr(config,"BACKUP_ROOT",str(plat))
    rid=os_backup.start_backup(str(src),"selfcheck")
    rec=_wait(rid)
    assert rec["status"]=="success",rec
    import tarfile,json
    with tarfile.open(rec["archive_path"],"r:*") as tf:
        names=[m.name for m in tf.getmembers()]
        man=json.loads(tf.extractfile("manifest.json").read().decode())
    assert "files/keep.txt" in names
    assert not any(n.startswith("files/backups/") for n in names),names
    assert not any(f["path"].startswith("backups/") for f in man["files"])

def test_backup_of_platform_own_directory_is_rejected(env):
    own=Path(config.BACKUP_ROOT)
    with pytest.raises(ValueError,match="自包含"):
        os_backup.create_backup(str(own),"bad")

def test_failed_background_backup_records_failure(env,monkeypatch):
    def boom(*a,**k): raise RuntimeError("模拟磁盘故障")
    monkeypatch.setattr(os_backup,"create_backup",boom)
    rid=os_backup.start_backup(str(env),"failcase")
    rec=_wait(rid)
    assert rec["status"]=="failed"
    assert "模拟磁盘故障" in (rec.get("error_detail") or "")

def test_restore_job_runs_in_background_and_verifies_content(env):
    src=env/"src";src.mkdir();(src/"etc.conf").write_text("restore-me");(src/"sub").mkdir();(src/"sub"/"x").write_text("中文内容")
    rid=os_backup.start_backup(str(src),"restore-src")
    rec=_wait(rid)
    assert rec["status"]=="success"
    out=env/"restored"
    jid=os_backup.start_restore(rec["archive_path"],str(out))
    jrec=_wait(jid)
    assert jrec["status"]=="success",jrec
    assert jrec["kind"]=="restore"
    assert (out/"etc.conf").read_text()=="restore-me"
    assert (out/"sub"/"x").read_text()=="中文内容"

def _mock_remote(monkeypatch,files=("files/etc.conf",)):
    import core.remote_dump as rd
    import tarfile,io
    cmds=[]
    def cap(host,cmd,timeout=None,**kw):
        cmds.append(cmd);return {"returncode":0,"stdout":"","stderr":""}
    monkeypatch.setattr(rd,"remote_exec_capture",cap)
    monkeypatch.setattr(rd,"_connect",lambda h:object())
    def pull(client,remote,local,**kw):
        with tarfile.open(local,"w:gz") as t:
            for name in files:
                data=b"x";i=tarfile.TarInfo(name);i.size=1;t.addfile(i,io.BytesIO(data))
        return {"size":1}
    monkeypatch.setattr(rd,"sftp_pull_resumable",pull)
    return cmds

def test_remote_tar_command_keeps_dot_member_and_hard_dereference(env,monkeypatch):
    """目录模式必须带 "." 成员（否则 tar 报空归档），并解引用硬链接。"""
    cmds=_mock_remote(monkeypatch)
    out=os_backup.create_remote_backup({"host_key":"root@test:22","hostname":"test"},"/etc","remote")
    assert out["status"]=="success"
    tar_cmds=[c for c in cmds if " -czf " in c or "-czf" in c]
    assert tar_cmds,"未捕获到远端 tar 命令：%s"%cmds
    cmd=tar_cmds[0]
    assert "--hard-dereference" in cmd,cmd
    import re as _re
    assert _re.search(r" -C \S+ \.(\s|$)",cmd),cmd  # 成员 "." 单独作为一个 tar 参数（否则 tar 报空归档）
    assert "files/" in cmd,cmd
    assert out["manifest"]["files"][0]["path"]=="etc.conf"

def test_remote_background_job_updates_single_record_with_manifest(env,monkeypatch):
    """远程作业不得产生重复记录，且清单必须落到同一记录上。"""
    _mock_remote(monkeypatch)
    before=len(os_backup.list_records(500))
    rid=os_backup.start_backup("/etc","remote-async",host={"host_key":"root@test:22","hostname":"test"})
    rec=_wait(rid)
    assert rec["status"]=="success",rec
    assert len(os_backup.list_records(500))==before+1
    assert rec["manifest_json"]
    import json
    man=json.loads(rec["manifest_json"])
    assert man["files"] and man["files"][0]["path"]=="etc.conf"
    assert man["system_metadata"]

def test_restore_plan_follows_industry_restore_skeleton(env):
    """恢复作业单必须覆盖业界整机恢复骨架的 8 个步骤并给出命令模板。"""
    src=env/"src";src.mkdir();(src/"etc.conf").write_text("x")
    rid=os_backup.start_backup(str(src),"plan-src")
    rec=_wait(rid);assert rec["status"]=="success"
    plan=os_backup.build_restore_plan(rec["archive_path"])
    assert [s["id"] for s in plan["steps"]]==["media","partition","mount","files","fstab","initramfs","grub","verify"]
    script=os_backup.render_restore_script(plan)
    for kw in ("grub2-install","initramfs","fstab"): assert kw in script
    assert plan["platform_scope"] and plan["not_supported"]
    assert plan["metadata"] and plan["metadata"].get("uname")

def test_restore_plan_rejects_unknown_archive(env):
    with pytest.raises(ValueError):
        os_backup.build_restore_plan(str(env/"nope.tar.gz"))
