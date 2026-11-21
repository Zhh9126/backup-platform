"""Authenticated acceptance history and bounded evidence previews."""
from flask import request,jsonify
from auth import permission_required
from . import api_bp
from core import test_records as records

@api_bp.route("/test-records",methods=["GET"])
@permission_required("backup.task.view")
def test_records_list():
    try:
        return jsonify(ok=True,**records.list_runs(
            request.args.get("category",""),request.args.get("status",""),
            request.args.get("q",""),request.args.get("page",1,type=int),
            request.args.get("size",20,type=int)))
    except (TypeError,ValueError) as exc:
        return jsonify(ok=False,error=str(exc)),400

@api_bp.route("/test-records",methods=["POST"])
@permission_required("system.settings")
def test_records_create():
    try:
        rid=records.save_run(request.get_json(silent=True) or {},operator="authenticated-api")
        return jsonify(ok=True,id=rid),201
    except (TypeError,ValueError) as exc:
        return jsonify(ok=False,error=str(exc)),400

@api_bp.route("/test-records/<int:run_id>",methods=["GET"])
@permission_required("backup.task.view")
def test_record_detail(run_id):
    row=records.get_run(run_id)
    return (jsonify(ok=True,record=row) if row else (jsonify(ok=False,error="测试记录不存在"),404))

@api_bp.route("/test-records/<int:run_id>/evidence/<int:index>",methods=["GET"])
@permission_required("backup.task.view")
def test_record_evidence(run_id,index):
    try:
        return jsonify(ok=True,**records.read_evidence(run_id,index))
    except (TypeError,ValueError) as exc:
        return jsonify(ok=False,error=str(exc)),400
