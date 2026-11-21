from flask import request,jsonify
from auth import permission_required
from . import api_bp
from core import os_backup

@api_bp.route("/os-backup/records",methods=["GET"])
@permission_required("backup.task.view")
def os_records():
    return jsonify(ok=True,items=os_backup.list_records(request.args.get("limit",100,type=int)))
@api_bp.route("/os-backup/backup",methods=["POST"])
@permission_required("backup.task.manage")
def os_backup_now():
    try:
        b=request.get_json(silent=True) or {}
        return jsonify(ok=True,result=os_backup.create_backup(b.get("source_root"),b.get("label",""),b.get("excludes"))),201
    except (TypeError,ValueError,OSError) as e: return jsonify(ok=False,error=str(e)),400
@api_bp.route("/os-backup/restore",methods=["POST"])
@permission_required("restore.run")
def os_restore():
    try:
        b=request.get_json(silent=True) or {}
        return jsonify(ok=True,result=os_backup.restore_backup(b.get("archive_path"),b.get("target_root"),bool(b.get("dry_run",True)),bool(b.get("confirm",False))))
    except (TypeError,ValueError,OSError) as e: return jsonify(ok=False,error=str(e)),400
