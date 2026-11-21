"""Agentless operating-system backup and recovery.

The platform performs a portable file-level image of a Linux root or selected
system directories over the existing SSH/local channel. It never installs an
agent and never extracts an archive outside the requested destination.

执行模型（2026-11-21 修正）：备份与恢复都是长任务，统一走
「先落 running 记录 → 后台线程执行 → 持续更新进度与终态」，前端轮询记录表。
此前版本在 HTTP 请求里同步打包整机根文件系统，前端只能干等，
表现为「点立即备份没反应」，且请求断开后任务仍在后台继续跑。
"""
from __future__ import annotations
import hashlib,json,os,re,shutil,tarfile,tempfile,subprocess,threading,time
from pathlib import Path
from core import db
import config

_DEFAULT_EXCLUDES=("/proc","/sys","/dev","/run","/tmp","/var/tmp","/mnt","/media",
                   "/lost+found",".cache","__pycache__")
_MAX_MEMBERS=500000
_PROGRESS_EVERY=200

def _root():
    return Path(getattr(config,"BACKUP_ROOT","backups"))/"os_backups"

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
    for col,typ in (("started_at","TEXT DEFAULT ''"),("duration_sec","REAL DEFAULT 0"),
                    ("kind","TEXT DEFAULT 'backup'"),("progress_files","INTEGER DEFAULT 0"),
                    ("progress_bytes","INTEGER DEFAULT 0"),("target_root","TEXT DEFAULT ''"),
                    ("error_detail","TEXT DEFAULT ''")):
        try: conn.execute("ALTER TABLE os_backup_records ADD COLUMN %s %s"%(col,typ))
        except Exception: pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_os_backup_time ON os_backup_records(created_at)")

# ------------------------- 系统 / 引导 / 分区元数据采集 -------------------------
# 依据整机恢复的业界骨架（ReaR / Clonezilla / Veeam BMR 的共同做法），
# 备份除文件外还必须留存：分区表与磁盘布局、引导信息、fstab/initramfs，
# 否则换机或换盘恢复时无法重建分区与引导，只能靠人工回忆。

_METADATA_CMDS=(
    ("uname","uname -a"),
    ("lsblk","lsblk -e7 -J -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,UUID,PARTUUID,MODEL,SERIAL 2>/dev/null || lsblk -o NAME,KNAME,TYPE,SIZE,FSTYPE,UUID,PARTUUID,MODEL,SERIAL 2>/dev/null || lsblk 2>/dev/null"),
    ("blkid","blkid"),
    ("fstab","cat /etc/fstab 2>/dev/null"),
    ("crypttab","cat /etc/crypttab 2>/dev/null"),
    ("boot_mode","if [ -d /sys/firmware/efi ]; then echo UEFI; else echo BIOS; fi"),
    ("root_source","findmnt -no SOURCE / 2>/dev/null"),
    ("mounts","findmnt -rno TARGET,SOURCE,FSTYPE 2>/dev/null | head -40"),
    ("mdstat","cat /proc/mdstat 2>/dev/null"),
    ("lvm","{ pvs --noheadings -o pv_name,vg_name,pv_size 2>/dev/null; vgs --noheadings -o vg_name,vg_size,vg_free 2>/dev/null; lvs --noheadings -o lv_name,vg_name,lv_size 2>/dev/null; } | head -60"),
    ("grub_files","ls -1 /boot/grub2 /boot/grub 2>/dev/null | head -50"),
    ("grub_entries","grep -m8 -E '^(menuentry|linux|initrd)' /boot/grub2/grub.cfg /boot/grub/grub.cfg 2>/dev/null | head -24"),
    ("efi","ls -1 /boot/efi/EFI 2>/dev/null | head -20"),
    ("initramfs","ls -1 /boot/initramfs-*.img /boot/initrd* /boot/vmlinuz-* 2>/dev/null | head -30"),
    ("boot_files","ls -1 /boot 2>/dev/null | head -300"),
    ("bootloader_tools","{ command -v grub2-install; command -v grub-install; command -v dracut; command -v update-initramfs; } 2>/dev/null | head -10"),
    ("bootloader_version","grub2-install --version 2>/dev/null | head -1; grub-install --version 2>/dev/null | head -1"),
)

def _root_disk_of(src,run):
    """由根设备推导根磁盘：dm/raid slaves -> lsblk PKNAME -> 去分区后缀启发式。"""
    base=src.split("/")[-1]
    try:
        real=(run("readlink -f %s 2>/dev/null"%src) or "").strip()
        if real and not real.startswith("unavailable") and "/" in real:
            base=real.split("/")[-1]
    except Exception:
        pass
    try:
        out=(run("ls -1 /sys/class/block/%s/slaves/ 2>/dev/null | head -1"%base) or "").strip()
        if out and not out.startswith("unavailable"):
            slave="/dev/"+out.splitlines()[0].strip()
            try:
                pk=(run("lsblk -no PKNAME %s 2>/dev/null | head -1"%slave) or "").strip()
            except Exception:
                pk=""
            if pk and not pk.startswith("unavailable"): return "/dev/"+pk
            return slave
    except Exception:
        pass
    try:
        pk=(run("lsblk -no PKNAME %s 2>/dev/null | head -1"%src) or "").strip()
        if pk and not pk.startswith("unavailable"): return "/dev/"+pk
    except Exception:
        pass
    m=re.match(r"^(nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)(?:\d+p?\d+)?$",base)
    return "/dev/"+m.group(1) if m else ""

def _collect_metadata(run):
    """统一采集整机恢复所需的元数据；run(cmd,timeout) -> 文本。"""
    meta={}
    for key,cmd in _METADATA_CMDS:
        try: meta[key]=run(cmd)
        except Exception as exc: meta[key]="unavailable: "+str(exc)
    src=(meta.get("root_source") or "").strip()
    src=src.splitlines()[0] if src else ""
    disk=""
    if src and not src.startswith("unavailable"):
        try: disk=_root_disk_of(src,run)
        except Exception: disk=""
    meta["root_disk"]=disk or "unavailable"
    if disk:
        try:
            meta["partition_table"]=run("sgdisk -p %s 2>/dev/null || parted -s %s unit s print 2>/dev/null || fdisk -l %s 2>/dev/null"%(disk,disk,disk))
        except Exception as exc:
            meta["partition_table"]="unavailable: "+str(exc)
    else:
        meta["partition_table"]="unavailable"
    return meta

def _local_run(cmd,timeout=20):
    try:
        return subprocess.run(cmd,shell=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout).stdout[-200000:]
    except Exception as exc:
        return "unavailable: "+str(exc)

def _system_metadata():
    return _collect_metadata(_local_run)

def _platform_excludes(source,excludes):
    """本机备份必须排除平台自身的产物与元数据库，避免归档自包含（递归膨胀）。

    只排除位于备份源之下的路径；备份源本身若就是平台目录则直接拒绝。
    """
    excluded={str(Path(x).resolve()) for x in (excludes or [])}
    try: src=Path(source).resolve()
    except OSError: src=Path(source)
    candidates=[]
    for getter in (lambda: getattr(config,"BACKUP_ROOT","backups"),
                   lambda: getattr(config,"META_DB_PATH","instance/meta.db")):
        try:
            val=getter()
            candidates.append(Path(val).resolve().parent if Path(val).name.endswith(".db") else Path(val).resolve())
        except Exception:
            pass
    for p in candidates:
        rp=str(p)
        if rp==str(src):
            raise ValueError("备份源就是平台自身目录（%s），会形成自包含归档，请改选系统目录"%rp)
        if str(src) in Path(rp).parents:
            excluded.add(rp)
    excluded.add(str(_root().resolve()))
    return excluded

def _manifest_files(source,excludes):
    source=source.resolve(); excluded={str(Path(x).resolve()) for x in excludes}
    for base,dirs,files in os.walk(source):
        basep=Path(base)
        keep=[]
        for d in dirs:
            try: rp=str((basep/d).resolve())
            except OSError: continue
            if rp in excluded or d in (".cache","__pycache__"): continue
            keep.append(d)
        dirs[:]=keep
        for name in files:
            p=basep/name
            try:
                if p.is_symlink() or not p.is_file(): continue
                rel=str(p.relative_to(source)).replace(os.sep,"/")
                yield p,rel
            except (OSError,ValueError): continue

class _HashReader:
    """边写归档边算 SHA256，避免为每个文件再读一遍盘（整机备份 IO 减半）。"""
    def __init__(self,fh,hasher): self._fh=fh; self._h=hasher
    def read(self,n=-1):
        b=self._fh.read(n)
        if b: self._h.update(b)
        return b

def create_backup(source_root,label="",excludes=None,progress_cb=None,record_id=None):
    source=Path(source_root).expanduser().resolve()
    if not source.is_dir(): raise ValueError("操作系统备份源目录不存在: "+str(source))
    excludes=list(excludes or _DEFAULT_EXCLUDES)
    excluded=_platform_excludes(source,excludes)
    # Excludes are interpreted relative to / for root backups and relative to source otherwise.
    root=_root();root.mkdir(parents=True,exist_ok=True)
    stamp=db.now_iso().replace(":","").replace("+","_")
    started_at=db.now_iso();started_ts=time.time()
    fd,tmp=tempfile.mkstemp(prefix=".os-",suffix=".tar.gz",dir=root);os.close(fd)
    files=[];total=0
    try:
        with tarfile.open(tmp,"w:gz") as tf:
            for path,rel in _manifest_files(source,excluded):
                info=tf.gettarinfo(str(path),arcname="files/"+_safe_rel(rel))
                hasher=hashlib.sha256()
                with open(path,"rb") as f:
                    tf.addfile(info,_HashReader(f,hasher))
                size=info.size;total+=size
                files.append({"path":rel,"size_bytes":size,"sha256":hasher.hexdigest()})
                if len(files)>_MAX_MEMBERS: raise ValueError("文件数量超过安全上限")
                if progress_cb and len(files)%_PROGRESS_EVERY==0:
                    try: progress_cb(len(files),total)
                    except Exception: pass
            manifest={"format":"aidbm-os-v2","scope":"full_system","source_root":str(source),"file_count":len(files),
                      "total_bytes":total,"created_at":db.now_iso(),"files":files,"system_metadata":_system_metadata()}
            raw=json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode()
            info=tarfile.TarInfo("manifest.json");info.size=len(raw);tf.addfile(info,__import__("io").BytesIO(raw))
        checksum=_sha(tmp)
        safe_label=re.sub(r"[^A-Za-z0-9_.-]+","_",label.strip() or "os").strip("._") or "os"
        name=safe_label+"_"+stamp+".tar.gz"
        dest=root/name;os.replace(tmp,dest)
        finished_at=db.now_iso();duration=round(time.time()-started_ts,3)
        payload=(str(dest),dest.stat().st_size,len(files),checksum,manifest["created_at"],started_at,finished_at,duration,
                 "操作系统备份完成",json.dumps(manifest,ensure_ascii=False))
        if record_id:
            db.execute("UPDATE os_backup_records SET status='success',archive_path=?,size_bytes=?,file_count=?,checksum=?,created_at=?,started_at=?,finished_at=?,duration_sec=?,message=?,manifest_json=?,progress_files=?,progress_bytes=? WHERE id=?",
                       payload+(len(files),total,record_id))
            rid=record_id
        else:
            rid=db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,started_at,finished_at,duration_sec,message,manifest_json,kind,progress_files,progress_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (label.strip() or "os",str(source),str(dest),"success",dest.stat().st_size,len(files),checksum,manifest["created_at"],started_at,finished_at,duration,"操作系统备份完成",json.dumps(manifest,ensure_ascii=False),"backup",len(files),total))
        return {"id":rid,"status":"success","archive_path":str(dest),"checksum":checksum,"file_count":len(files),"size_bytes":dest.stat().st_size,"manifest":manifest}
    except Exception as exc:
        try: os.unlink(tmp)
        except OSError: pass
        finished_at=db.now_iso();duration=round(time.time()-started_ts,3)
        if record_id:
            db.execute("UPDATE os_backup_records SET status='failed',finished_at=?,duration_sec=?,message=?,error_detail=? WHERE id=?",
                       (finished_at,duration,"操作系统备份失败",str(exc)[:500],record_id))
        else:
            db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,created_at,started_at,finished_at,duration_sec,message,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (label.strip() or "os",str(source),"","failed",finished_at,started_at,finished_at,duration,str(exc)[:500],"backup"))
        raise

def _load_archive(path,manifest=None):
    path=Path(path).resolve()
    if not path.is_file(): raise ValueError("操作系统备份归档不存在")
    with tarfile.open(path,"r:*") as tf:
        members=tf.getmembers()
        if len(members)>_MAX_MEMBERS: raise ValueError("归档成员超过安全上限")
        for m in members: _safe_rel(m.name)
        has_manifest=any(m.name=="manifest.json" for m in members)
    if manifest is None:
        if not has_manifest:
            raise ValueError("归档缺少 manifest.json，且记录中无可用清单")
        with tarfile.open(path,"r:*") as tf:
            manifest=json.loads(tf.extractfile("manifest.json").read().decode("utf-8"))
    if manifest.get("format") not in ("aidbm-os-v1","aidbm-os-v2"): raise ValueError("不支持的操作系统备份格式")
    return path,manifest

def restore_backup(archive_path,target_root,dry_run=True,confirm=False,progress_cb=None,manifest=None):
    path,manifest=_load_archive(archive_path,manifest)
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
            checks.append({"path":rel,"status":"passed","size_bytes":int(item.get("size_bytes") or 0)})
            seen.add(rel)
            if progress_cb and len(checks)%_PROGRESS_EVERY==0:
                try: progress_cb(len(checks),sum(int(c.get("size_bytes") or 0) for c in checks))
                except Exception: pass
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
                if m.isdir():
                    out.mkdir(parents=True,exist_ok=True)
                    continue
                if not m.isfile():
                    # 符号链接等特殊成员不还原内容（目标常指向 /proc 等伪文件系统，
                    # 重建可能产生越权链接），仅保留路径占位
                    continue
                try:
                    src=tf.extractfile(m)
                except KeyError:
                    # 硬链接成员的目标不在归档内（多见 /proc 伪文件系统），内容由完整成员提供
                    continue
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
    return db.query("SELECT id,kind,label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,started_at,finished_at,duration_sec,message,target_root,error_detail,progress_files,progress_bytes FROM os_backup_records ORDER BY id DESC LIMIT ?",(max(1,min(500,int(limit))),))

def get_record(record_id):
    return db.query_one("SELECT id,kind,label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,started_at,finished_at,duration_sec,message,target_root,error_detail,progress_files,progress_bytes,manifest_json FROM os_backup_records WHERE id=?",(int(record_id),))

def find_by_archive(archive_path):
    """按归档路径反查记录（远程归档不含 manifest.json，清单保存在记录里）。"""
    return db.query_one("SELECT id,kind,label,source_root,archive_path,status,manifest_json FROM os_backup_records WHERE archive_path=? ORDER BY id DESC LIMIT 1",(str(archive_path),))

def record_manifest(archive_path):
    """取归档对应的清单（优先记录中保存的清单，其次归档内 manifest.json）。"""
    rec=find_by_archive(archive_path)
    if rec and rec.get("manifest_json"):
        try:
            man=json.loads(rec["manifest_json"])
            if isinstance(man,dict) and man.get("files") is not None: return man
        except Exception:
            pass
    return None

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

def _remote_metadata(host):
    """采集远端主机的系统/引导/磁盘布局元数据（整机保护必需，不装任何 agent）。"""
    from core import remote_dump
    def _run(cmd,timeout=25):
        try:
            r=remote_dump.remote_exec_capture(host,cmd,timeout=timeout)
            return (r.get("stdout") or r.get("stderr") or "").strip()
        except Exception as exc:
            return "unavailable: "+str(exc)
    return _collect_metadata(_run)

def _register_archive(path,label,source_root,manifest=None,record_id=None):
    started_at=db.now_iso();started_ts=time.time()
    path=Path(path);manifest=manifest or _archive_manifest(path)
    manifest["source_root"]=str(source_root);checksum=_sha(path)
    finished_at=db.now_iso();duration=round(time.time()-started_ts,3)
    if record_id:
        rid=record_id
        db.execute("UPDATE os_backup_records SET archive_path=?,status=?,size_bytes=?,file_count=?,checksum=?,created_at=?,started_at=?,finished_at=?,duration_sec=?,message=?,manifest_json=?,progress_files=?,progress_bytes=? WHERE id=?",
          (str(path),"success",path.stat().st_size,manifest["file_count"],checksum,manifest["created_at"],started_at,finished_at,duration,"远程操作系统备份完成",json.dumps(manifest,ensure_ascii=False),manifest["file_count"],manifest["total_bytes"],record_id))
    else:
        rid=db.execute("INSERT INTO os_backup_records(label,source_root,archive_path,status,size_bytes,file_count,checksum,created_at,started_at,finished_at,duration_sec,message,manifest_json,kind,progress_files,progress_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (label or "os",str(source_root),str(path),"success",path.stat().st_size,manifest["file_count"],checksum,manifest["created_at"],started_at,finished_at,duration,"远程操作系统备份完成",json.dumps(manifest,ensure_ascii=False),"backup",manifest["file_count"],manifest["total_bytes"]))
    return {"id":rid,"status":"success","archive_path":str(path),"checksum":checksum,
            "file_count":manifest["file_count"],"size_bytes":path.stat().st_size,"manifest":manifest}

def create_remote_backup(host,source_root,label="",excludes=None,progress_cb=None,record_id=None):
    """Create a tar on a managed Linux host and pull it with resumable SFTP.
    No package or agent is installed on the target."""
    import shlex,uuid
    from core import remote_dump
    host=dict(host or {});source=str(source_root or "").strip()
    if not source or not source.startswith("/"): raise ValueError("远程源路径必须是绝对路径")
    name="aidbm-os-"+uuid.uuid4().hex+".tar.gz";remote="/tmp/"+name
    local_root=_root();local_root.mkdir(parents=True,exist_ok=True);local=local_root/name
    source_is_file=False
    probe=remote_dump.remote_exec_capture(host,"test -f "+shlex.quote(source)+" && echo FILE || echo DIR",timeout=30)
    source_is_file=(probe.get("returncode")==0 and probe.get("stdout","").strip()=="FILE")
    if source_is_file:
        parent,entry=os.path.dirname(source) or "/",os.path.basename(source)
        args=["tar","--numeric-owner","--xattrs","--acls","--hard-dereference","-czf",remote,"--transform=s,^,files/,","-C",parent,entry]
    else:
        # 目录模式：成员 "." 配 transform 把 "./xxx" 改写成 "files/xxx"，
        # 与本机归档的 "files/<相对源路径>" 口径完全一致（清单与成员一一对应）。
        # 不能省略 "."：tar 会报 "Cowardly refusing to create an empty archive"（真机实测）。
        args=["tar","--numeric-owner","--xattrs","--acls","--hard-dereference","-czf",remote,"--transform=s,^\\./,files/,","-C",source,"."]
    for item in (excludes or _DEFAULT_EXCLUDES):
        raw=str(item).strip()
        if raw.startswith("/"): raw=raw[1:]
        if raw: args.append("--exclude="+raw)
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
        if record_id:
            db.execute("UPDATE os_backup_records SET message=?,progress_bytes=? WHERE id=?",
                       ("远程 OS 归档已拉回，正在生成清单",int(pulled.get("size") or 0),record_id))
        manifest=_archive_manifest(local)
        manifest["system_metadata"]=_remote_metadata(host)
        manifest["scope"]="full_system" if source=="/" else "directory"
        out=_register_archive(local,label or source,host.get("host_key") or host.get("hostname") or source,
                              manifest=manifest,record_id=record_id)
        return out
    finally:
        try:
            remote_dump.remote_exec_capture(host,"rm -f "+shlex.quote(remote),timeout=60)
        except Exception: pass
        try:
            if client: client.close()
        except Exception: pass

# ------------------------- 异步作业（长任务不占用 HTTP 请求） -------------------------

def _progress_writer(record_id,message=None):
    def _cb(files,total):
        try:
            if message:
                db.execute("UPDATE os_backup_records SET progress_files=?,progress_bytes=?,message=? WHERE id=?",
                           (files,total,message,record_id))
            else:
                db.execute("UPDATE os_backup_records SET progress_files=?,progress_bytes=? WHERE id=?",
                           (files,total,record_id))
        except Exception:
            pass
    return _cb

def start_backup(source_root="/",label="",excludes=None,host=None):
    """落一条 running 记录并后台执行；立即返回记录 id 供前端轮询。"""
    source=str(source_root or "/").strip() or "/"
    if not source.startswith("/"): raise ValueError("备份源必须是绝对路径")
    if host:
        if not source.startswith("/"): raise ValueError("备份源必须是绝对路径")
    else:
        p=Path(source).expanduser()
        if not p.is_dir(): raise ValueError("操作系统备份源目录不存在: "+str(p))
    started_at=db.now_iso()
    rid=db.execute("INSERT INTO os_backup_records(kind,label,source_root,archive_path,status,created_at,started_at,message,progress_files,progress_bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
      ("backup",label.strip() or "os",source,"","running",started_at,started_at,"备份任务已提交，等待执行",0,0))
    def _worker():
        try:
            if host:
                create_remote_backup(host,source,label,excludes,record_id=rid)
            else:
                create_backup(source,label,excludes,progress_cb=_progress_writer(rid),record_id=rid)
        except Exception as exc:
            try:
                db.execute("UPDATE os_backup_records SET status='failed',finished_at=?,message=?,error_detail=? WHERE id=?",
                           (db.now_iso(),"操作系统备份失败",str(exc)[:500],rid))
            except Exception:
                pass
    threading.Thread(target=_worker,daemon=True,name="os-backup-%s"%rid).start()
    return rid

def start_restore(archive_path,target_root,manifest=None):
    """恢复同样走后台线程，避免大归档校验/解包把 HTTP 请求挂死。"""
    if not archive_path: raise ValueError("缺少归档路径")
    if not target_root: raise ValueError("缺少恢复目标目录")
    started_at=db.now_iso()
    rid=db.execute("INSERT INTO os_backup_records(kind,label,source_root,archive_path,status,created_at,started_at,message,target_root,progress_files,progress_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      ("restore",Path(archive_path).name,str(archive_path),"","running",started_at,started_at,"恢复任务已提交，等待执行",str(target_root),0,0))
    def _worker():
        try:
            res=restore_backup(archive_path,target_root,dry_run=False,confirm=True,
                               progress_cb=_progress_writer(rid,"正在恢复并校验文件"),
                               manifest=manifest)
            db.execute("UPDATE os_backup_records SET status='success',finished_at=?,duration_sec=?,message=?,error_detail=?,progress_files=?,progress_bytes=? WHERE id=?",
                       (db.now_iso(),0,"恢复完成", "", int(res.get("file_count") or 0), 0, rid))
        except Exception as exc:
            try:
                db.execute("UPDATE os_backup_records SET status='failed',finished_at=?,message=?,error_detail=? WHERE id=?",
                           (db.now_iso(),"恢复失败",str(exc)[:500],rid))
            except Exception:
                pass
    threading.Thread(target=_worker,daemon=True,name="os-restore-%s"%rid).start()
    return rid

# ------------------------- 恢复作业单（整机恢复的落地方法） -------------------------
# 调研结论：ReaR / Clonezilla / Veeam BMR 的整机恢复骨架高度一致——
#   引导介质 → 重建分区与文件系统 → 挂载目标根 → 还原文件 → 修 fstab(UUID)
#   → 重建 initramfs(适配新硬件驱动) → 安装 GRUB → 校验切换。
# 平台不做裸机引导介质（离线交付、无 PXE 基础设施），但必须把上述每一步的
# 真实参数（分区表、磁盘布局、引导模式、fstab、initramfs）随备份一起留存，
# 并产出可执行的恢复作业单，避免恢复时靠人工回忆。

def build_restore_plan(archive_path):
    """生成整机恢复作业单：步骤 + 命令模板 + 平台能力边界。"""
    rec=find_by_archive(archive_path)
    if not rec: raise ValueError("找不到该归档对应的备份记录")
    man=record_manifest(archive_path)
    if not man: raise ValueError("该备份没有可用清单，无法生成恢复作业单")
    meta=man.get("system_metadata") or {}
    root_disk=meta.get("root_disk") or "unavailable"
    pt=str(meta.get("partition_table") or "")
    has_pt=bool(pt.strip()) and not pt.startswith("unavailable")
    boot_mode=(meta.get("boot_mode") or "").strip()
    steps=[
      {"id":"media","title":"准备恢复介质并引导目标机","manual":True,
       "detail":"使用 Clonezilla Live / ReaR ISO / 自制救援 U 盘 引导目标机；平台负责数据与作业单，不提供裸机引导介质。",
       "command":None},
      {"id":"partition","title":"重建分区与文件系统","manual":not has_pt,
       "detail":("按备份中记录的分区表重建目标盘分区并格式化（源系统盘 %s）"%root_disk) if has_pt
                else "备份中未取到分区表，需按备份记录的磁盘布局手工创建分区",
       "command":("sgdisk --zap-all /dev/<target-disk> && sgdisk --replay=<gpt-backup.bin> /dev/<target-disk>") if has_pt
                else "parted /dev/<target-disk> mklabel gpt # 按备份布局手工建分区"},
      {"id":"mount","title":"挂载目标根分区与 /boot","manual":True,
       "detail":"按 fstab 布局挂载 / 与 /boot；LVM/RAID 主机需先激活卷组与阵列。",
       "command":"mount /dev/<target-root-part> /mnt/target && mount /dev/<target-boot-part> /mnt/target/boot"},
      {"id":"files","title":"还原系统文件","manual":False,
       "detail":"平台已在「异机目录还原」中实现同等的文件级还原（含逐文件 SHA256 校验）；此步等价于直接对目标根分区解包。",
       "command":"tar -xzf <archive> -C /mnt/target"},
      {"id":"fstab","title":"修复 fstab（UUID 映射）","manual":True,
       "detail":"源机与目标机的分区 UUID 不同，必须按新盘 blkid 结果替换 fstab 中的旧 UUID，否则启动后挂载错盘。",
       "command":"blkid && sed -i 's/<old-uuid>=<new-uuid>/g' /mnt/target/etc/fstab"},
      {"id":"initramfs","title":"重建 initramfs（适配新硬件驱动）","manual":True,
       "detail":"跨硬件恢复必须重建，否则新磁盘/网卡驱动缺失，开机即停在 dracut。",
       "command":"chroot /mnt/target dracut --force --no-hostonly /boot/initramfs-<ver>.img <ver>"},
      {"id":"grub","title":"安装引导加载程序（GRUB）","manual":True,
       "detail":("目标机为 %s 模式；UEFI 机型还需处理 ESP 分区并使用 --target=x86_64-efi。"%boot_mode) if boot_mode
                else "目标机引导模式未知，需按 BIOS/UEFI 分别安装 GRUB。",
       "command":"chroot /mnt/target grub2-install /dev/<target-disk> && chroot /mnt/target grub2-mkconfig -o /boot/grub2/grub.cfg"},
      {"id":"verify","title":"校验与业务切换","manual":True,
       "detail":"确认可引导、fstab 挂载正常、关键服务与网络配置正确后，再把业务流量切到恢复后的主机。",
       "command":"chroot /mnt/target systemctl --no-pager status <关键服务>"},
    ]
    return {"archive_path":str(archive_path),"source_root":man.get("source_root"),
            "boot_mode":boot_mode,"root_disk":root_disk,"has_partition_table":has_pt,
            "file_count":man.get("file_count"),"total_bytes":man.get("total_bytes"),
            "created_at":man.get("created_at"),"steps":steps,"metadata":meta,
            "platform_scope":["文件级异机还原（平台自动执行，逐文件 SHA256 校验）",
                             "恢复作业单生成（分区/引导/initramfs 的真实参数与命令模板）",
                             "恢复前后清单一致性与来源系统元数据留存"],
            "not_supported":["裸机引导介质制作与 PXE/网络引导（离线交付环境不具备基础设施）",
                             "卷级/块级一致性快照：当前为文件级镜像，运行中系统可能存在跨文件不一致",
                             "自动改写目标机 fstab UUID 与安装 GRUB：提供命令模板，需在救援环境人工确认执行"]}

def render_restore_script(plan):
    """把恢复作业单渲染成可直接复制到救援环境执行的脚本骨架。"""
    lines=["#!/bin/bash",
           "# 由 AIDBM 操作系统备份的恢复作业单生成",
           "# 场景：在救援介质/救援 U 盘中，把整机备份还原到目标机并重建引导",
           "# 说明：<> 尖括号内容为占位符，执行前必须按目标机实际磁盘与分区替换",
           "set -e",""]
    for st in plan.get("steps") or []:
        lines.append("# [%s] %s%s"%(st.get("id"),st.get("title"),"（需人工确认）" if st.get("manual") else ""))
        if st.get("detail"): lines.append("#   %s"%st["detail"])
        if st.get("command"): lines.append(st["command"])
        lines.append("")
    return "\n".join(lines)
