"""Agentless operating-system backup and recovery.

The platform performs a portable file-level image of a Linux root or selected
system directories over the existing SSH/local channel. It never installs an
agent and never extracts an archive outside the requested destination.
"""
from __future__ import annotations
import hashlib,json,os,shutil,tarfile,tempfile
from pathlib import Path
from core import db

_DEFAULT_EXCLUDES=("/proc","/sys","/dev","/run","/tmp","/var/tmp","/mnt","/media",
                   "/lost+found",".cache","__pycache__")
_MAX_MEMBERS=500000

def _root():
    return Path(getattr(__import__("config"),"BACKUP_ROOT","backups"))/"os_backups"

def _safe_rel(name):
    p=Path(name)
    if p.is_absolute() or ".." in p.parts or name.startswith(("./","../")):
        raise ValueError("归档包含不安全路径: "+name)
    return name

def _sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS os_backup_records(
      id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, source_root TEXT NOT NULL,
      archive_path TEXT NOT NULL, status TEXT NOT NULL, size_bytes INTEGER DEFAULT 0,
      file_count INTEGER DEFAULT 0, checksum TEXT DEFAULT '', created_at TEXT NOT NULL,
      finished_at TEXT DEFAULT '', message TEXT DEFAULT '', manifest_json TEXT DEFAULT '')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_os_backup_time ON os_backup_records(created_at)")

def _manifest_files(source,excludes):
    source=source.resolve(); excluded={str(Path(x).resolve()) for x in excludes}
    for base,dirs,files in os.walk(source):
        basep=Path(base)
        dirs[:]=[d for d in dirs if str((basep/d).resolve()) not in excluded and d not in (".cache","__pycache__")]
        for name in files:
            p=basep/name
            try:
                if p.is_symlink() or not p.is_file(): continue
                rel=str(p.relative_to(source)).replace(os.sep,"/")
                yield p,rel
            except (OSError,ValueError): continue

def create_backup(source_root,label="",excludes=None):
    source=Path(source_root).expanduser().resolve()
    if not source.is_dir(): raise ValueError("操作系统备份源目录不存在: "+str(source))
    excludes=list(excludes or _DEFAULT_EXCLUDES)
    # Excludes are interpreted relative to / for root backups and relative to source otherwise.
    root=_root();root.mkdir(parents=True,exist_ok=True)
    stamp=db.now_iso().replace(":","").replace("+","_")
    fd,tmp=tempfile.mkstemp(prefix=".os-",suffix=".tar.gz",dir=root);os.close(fd)
    files=[];total=0
    try:
        with tarfile.open(tmp,"w:gz") as tf:
            for path,rel in _manifest_files(source,excludes):
                info=tf.gettarinfo(str(path),arcname="files/"+_safe_rel(rel))
                with open(path,"rb") as f: tf.addfile(info,f)
                size=info.size;total+=size
                files.append({"path":rel,"size_bytes":size,"sha256":_sha(path)})
                if len(files)>_MAX_MEMBERS: raise ValueError("文件数量超过安全上限")
            manifest={"format":"aidbm-os-v1","source_root":str(source),"file_count":len(files),
                      "total_bytes":total,"created_at":db.now_iso(),"files":files}
            raw=json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode()
            info=tarfile.TarInfo("manifest.json");info.size=len(raw);tf.addfile(info,__import__("io").BytesIO(raw))
        checksum=_sha(tmp);name=(label.strip() or "os")+"_"+stamp+".tar.gz"
        dest=root/name;os.replace(tmp,dest)
        rid=db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,finished_at,message,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (label.strip() or "os",str(source),str(dest),"success",dest.stat().st_size,len(files),checksum,manifest["created_at"],db.now_iso(),"操作系统备份完成",json.dumps(manifest,ensure_ascii=False)))
        return {"id":rid,"status":"success","archive_path":str(dest),"checksum":checksum,"file_count":len(files),"size_bytes":dest.stat().st_size,"manifest":manifest}
    except Exception as exc:
        try: os.unlink(tmp)
        except OSError: pass
        db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,created_at,finished_at,message) VALUES(?,?,?,?,?,?,?)",
          (label.strip() or "os",str(source),"","failed",db.now_iso(),db.now_iso(),str(exc)[:500]))
        raise

def _load_archive(path):
    path=Path(path).resolve()
    if not path.is_file(): raise ValueError("操作系统备份归档不存在")
    with tarfile.open(path,"r:*") as tf:
        members=tf.getmembers()
        if len(members)>_MAX_MEMBERS: raise ValueError("归档成员超过安全上限")
        for m in members: _safe_rel(m.name)
        man=tf.extractfile("manifest.json") if any(m.name=="manifest.json" for m in members) else None
        if not man: raise ValueError("归档缺少 manifest.json")
        manifest=json.loads(man.read().decode("utf-8"))
    if manifest.get("format")!="aidbm-os-v1": raise ValueError("不支持的操作系统备份格式")
    return path,manifest

def restore_backup(archive_path,target_root,dry_run=True,confirm=False):
    path,manifest=_load_archive(archive_path)
    target=Path(target_root).expanduser().resolve()
    if not dry_run and not confirm: raise ValueError("实际恢复必须显式 confirm=true")
    checks=[];seen=set()
    with tarfile.open(path,"r:*") as tf:
        by={m.name:m for m in tf.getmembers()}
        for item in manifest.get("files",[]):
            rel=_safe_rel(item["path"]); member="files/"+rel
            m=by.get(member)
            if not m: checks.append({"path":rel,"status":"failed","detail":"归档成员缺失"});continue
            if m.size!=int(item.get("size_bytes") or 0): checks.append({"path":rel,"status":"failed","detail":"大小不一致"});continue
            checks.append({"path":rel,"status":"passed"})
            seen.add(rel)
    if any(c["status"]=="failed" for c in checks): raise ValueError("归档完整性校验失败")
    if dry_run: return {"status":"planned","target_root":str(target),"file_count":len(checks),"checks":checks}
    target.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".os-restore-",dir=target.parent))
    try:
        with tarfile.open(path,"r:*") as tf:
            for m in tf.getmembers():
                if not m.name.startswith("files/") or m.name=="files/": continue
                rel=_safe_rel(m.name[6:]);out=(stage/rel).resolve()
                if not str(out).startswith(str(stage)+os.sep): raise ValueError("恢复路径越界")
                out.parent.mkdir(parents=True,exist_ok=True)
                src=tf.extractfile(m)
                if src:
                    with open(out,"wb") as f: shutil.copyfileobj(src,f)
                os.chmod(out,m.mode & 0o7777)
        for item in manifest.get("files",[]):
            actual=stage/item["path"]
            if _sha(actual)!=item["sha256"]: raise ValueError("恢复后校验失败: "+item["path"])
        for item in manifest.get("files",[]):
            src=stage/item["path"];dst=target/item["path"];dst.parent.mkdir(parents=True,exist_ok=True);os.replace(src,dst)
        shutil.rmtree(stage,ignore_errors=True)
        return {"status":"success","target_root":str(target),"file_count":len(checks),"checks":checks}
    except Exception:
        shutil.rmtree(stage,ignore_errors=True);raise

def list_records(limit=100):
    return db.query("SELECT id,label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,finished_at,message FROM os_backup_records ORDER BY id DESC LIMIT ?",(max(1,min(500,int(limit))),))

def _archive_manifest(path):
    files=[];total=0
    with tarfile.open(path,"r:*") as tf:
        members=[m for m in tf.getmembers() if m.name.startswith("files/") and m.isfile()]
        for m in members:
            rel=_safe_rel(m.name[6:]);src=tf.extractfile(m);h=hashlib.sha256()
            while src:
                block=src.read(1024*1024)
                if not block: break
                h.update(block)
            files.append({"path":rel,"size_bytes":m.size,"sha256":h.hexdigest()});total+=m.size
    return {"format":"aidbm-os-v1","source_root":"","file_count":len(files),
            "total_bytes":total,"created_at":db.now_iso(),"files":files}

def _register_archive(path,label,source_root,manifest=None):
    path=Path(path);manifest=manifest or _archive_manifest(path)
    manifest["source_root"]=str(source_root);checksum=_sha(path)
    rid=db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,finished_at,message,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (label or "os",str(source_root),str(path),"success",path.stat().st_size,manifest["file_count"],checksum,manifest["created_at"],db.now_iso(),"远程操作系统备份完成",json.dumps(manifest,ensure_ascii=False)))
    return {"id":rid,"status":"success","archive_path":str(path),"checksum":checksum,
            "file_count":manifest["file_count"],"size_bytes":path.stat().st_size,"manifest":manifest}

def create_remote_backup(host,source_root,label="",excludes=None):
    """Create a tar on a managed Linux host and pull it with resumable SFTP.
    No package or agent is installed on the target."""
    import shlex,uuid
    from core import remote_dump
    host=dict(host or {});source=str(source_root or "").strip()
    if not source or not source.startswith("/"): raise ValueError("远程源目录必须是绝对路径")
    name="aidbm-os-"+uuid.uuid4().hex+".tar.gz";remote="/tmp/"+name
    local_root=_root();local_root.mkdir(parents=True,exist_ok=True);local=local_root/name
    args=["tar","--numeric-owner","--xattrs","--acls","-czf",remote,"-C",source]
    for item in (excludes or _DEFAULT_EXCLUDES):
        raw=str(item).strip()
        if raw.startswith("/"): raw=raw[1:]
        if raw: args.append("--exclude="+raw)
    args.append(".")
    command=" ".join(shlex.quote(x) for x in args)
    result=remote_dump.remote_exec_capture(host,command,timeout=7200)
    if result.get("returncode")!=0: raise ValueError("远程 OS 归档失败: "+(result.get("stderr") or result.get("stdout") or "")[-800:])
    client=None
    try:
        client=remote_dump._connect(host)
        pulled=remote_dump.sftp_pull_resumable(client,remote,str(local),
          task={"id":0,"extra_options":{"transfer_resume":True,"transfer_retry_max":3}},
          host_key=host.get("host_key") or "",db_type="os",label=label or source)
        if int(pulled.get("size") or 0)<=0: raise ValueError("远程 OS 归档为空")
        return _register_archive(local,label or source,host.get("host_key") or host.get("hostname") or source)
    finally:
        try:
            remote_dump.remote_exec_capture(host,"rm -f "+shlex.quote(remote),timeout=60)
        except Exception: pass
        try:
            if client: client.close()
        except Exception: pass
