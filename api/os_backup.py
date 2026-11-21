from flask import request,jsonify
from auth import permission_required
from . import api_bp
from core import os_backup,ssh_hosts
import shutil,os,json
from pathlib import Path

def _estimate_size(source,cap=64*1024**3,seconds=10.0):
    """估算备份源占用：显式栈遍历，跳过运行时伪文件目录，带时间上限。

    超时或达到上限时返回一个保守下限（宁可拦截也不放行写爆磁盘）。
    """
    import time
    skip=("/proc","/sys","/dev","/run","/tmp","/var/tmp")
    start=time.time();total=0;stack=[source]
    while stack:
        cur=stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.path in skip: continue
                            stack.append(e.path)
                        else:
                            total+=e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
        if total>cap: return total
        if time.time()-start>seconds:
            return max(total,20*1024**3)
    return total

def _space_guard(source):
    """备份前空间预检：可用空间明显小于源体积时直接拒绝，避免把磁盘写爆。"""
    try:
        src=Path(source).expanduser().resolve()
        usage=shutil.disk_usage(str(src))
        need=_estimate_size(str(src))
        # 压缩后通常能到原体积的三分之一左右，留两倍余量更安全
        if usage.free < need//3:
            return "备份空间不足：源约 %.1f GB，可用 %.1f GB"%(need/1024**3,usage.free/1024**3)
    except Exception:
        return None
    return None

@api_bp.route("/os-backup/records",methods=["GET"])
@permission_required("backup.task.view")
def os_records():
    return jsonify(ok=True,items=os_backup.list_records(request.args.get("limit",100,type=int)))

@api_bp.route("/os-backup/records/<int:record_id>",methods=["GET"])
@permission_required("backup.task.view")
def os_record_detail(record_id):
    rec=os_backup.get_record(record_id)
    if not rec: return jsonify(ok=False,error="记录不存在"),404
    raw=rec.pop("manifest_json","")
    if request.args.get("include_manifest") in ("1","true","yes"):
        try: rec["manifest"]=json.loads(raw or "{}")
        except Exception: rec["manifest"]={}
    return jsonify(ok=True,item=rec)

@api_bp.route("/os-backup/backup",methods=["POST"])
@permission_required("backup.task.manage")
def os_backup_now():
    try:
        b=request.get_json(silent=True) or {};hid=b.get("host_id")
        direct=b.get("ssh_cred") or {}
        source=str(b.get("source_root") or "/").strip() or "/"
        if not source.startswith("/"):
            return jsonify(ok=False,error="备份源必须是绝对路径"),400
        label=str(b.get("label") or "").strip()
        excludes=b.get("excludes")
        if direct.get("hostname") or direct.get("host"):
            host=dict(direct);host["hostname"]=host.get("hostname") or host.get("host")
            host["host_key"]="%s@%s:%s"%(host.get("username") or "root",host["hostname"],int(host.get("port") or 22))
            host["auth_type"]="password"
            rid=os_backup.start_backup(source,label,excludes,host=host)
        elif hid:
            h=ssh_hosts.get_host(int(hid),include_secret=True)
            if not h:return jsonify(ok=False,error="SSH 主机不存在"),404
            rid=os_backup.start_backup(source,label,excludes,host=h)
        else:
            err=_space_guard(source)
            if err:return jsonify(ok=False,error=err),400
            rid=os_backup.start_backup(source,label,excludes)
        return jsonify(ok=True,id=rid,status="running",message="备份任务已提交，后台执行中"),202
    except (TypeError,ValueError,OSError) as e:return jsonify(ok=False,error=str(e)),400

@api_bp.route("/os-backup/restore",methods=["POST"])
@permission_required("restore.run")
def os_restore():
    try:
        b=request.get_json(silent=True) or {}
        archive=b.get("archive_path");target=b.get("target_root")
        man=os_backup.record_manifest(archive) if archive else None
        if b.get("dry_run",True):
            return jsonify(ok=True,result=os_backup.restore_backup(archive,target,dry_run=True,manifest=man))
        rid=os_backup.start_restore(archive,target,manifest=man)
        return jsonify(ok=True,id=rid,status="running",message="恢复任务已提交，后台执行"),202
    except (TypeError,ValueError,OSError) as e:return jsonify(ok=False,error=str(e)),400

@api_bp.route("/os-backup/restore-plan",methods=["GET"])
@permission_required("restore.run")
def os_restore_plan():
    """整机恢复作业单：按业界恢复骨架给出步骤、命令模板与平台能力边界。"""
    try:
        archive=request.args.get("archive_path") or ""
        if not archive: return jsonify(ok=False,error="缺少归档路径"),400
        plan=os_backup.build_restore_plan(archive)
        return jsonify(ok=True,plan=plan,script=os_backup.render_restore_script(plan))
    except (TypeError,ValueError,OSError) as e:return jsonify(ok=False,error=str(e)),400
