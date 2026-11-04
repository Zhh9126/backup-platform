# -*- coding: utf-8 -*-
"""数据库日常监控 API（自定义 SQL 指标 + 时间序列图表数据，对标 DSG/鼎甲 C6）。"""
from flask import jsonify, request

from auth import login_required, permission_required
from core import db, db_monitor
from . import api_bp


def _err(msg, code=400):
    return jsonify({"success": False, "message": msg}), code


@api_bp.route("/db-monitor/metrics", methods=["GET"])
@login_required
def dbm_list():
    return jsonify({"success": True, "data": {"metrics": db_monitor.list_metrics()}})


@api_bp.route("/db-monitor/metrics", methods=["POST"])
@login_required
@permission_required("backup.task.manage")
def dbm_create():
    d = request.get_json(force=True, silent=True) or {}
    if not (d.get("name") or "").strip():
        return _err("指标名称必填")
    if not (d.get("sql_text") or "").strip():
        return _err("监控 SQL 必填（结果第一行第一列作为指标值）")
    if not (d.get("db_type") and d.get("host")):
        return _err("数据库类型与主机地址必填")
    mid = db_monitor.create_metric(d)
    return jsonify({"success": True, "data": {"id": mid}}), 201


@api_bp.route("/db-monitor/metrics/<int:mid>", methods=["PUT"])
@login_required
@permission_required("backup.task.manage")
def dbm_update(mid):
    d = request.get_json(force=True, silent=True) or {}
    if not db_monitor.update_metric(mid, d):
        return _err("指标不存在", 404)
    return jsonify({"success": True, "data": {"id": mid}})


@api_bp.route("/db-monitor/metrics/<int:mid>", methods=["DELETE"])
@login_required
@permission_required("backup.task.manage")
def dbm_delete(mid):
    db_monitor.delete_metric(mid)
    return jsonify({"success": True, "data": {"deleted": mid}})


@api_bp.route("/db-monitor/metrics/<int:mid>/run", methods=["POST"])
@login_required
def dbm_run(mid):
    res = db_monitor.run_metric(mid)
    if not res.get("ok"):
        return _err(res.get("error") or "采集失败")
    return jsonify({"success": True, "data": res})


@api_bp.route("/db-monitor/metrics/<int:mid>/points", methods=["GET"])
@login_required
def dbm_points(mid):
    try:
        hours = int(request.args.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    return jsonify({"success": True,
                    "data": {"points": db_monitor.points(mid, hours=hours)}})


@api_bp.route("/db-monitor/metrics/test", methods=["POST"])
@login_required
@permission_required("backup.task.manage")
def dbm_test():
    """试跑监控 SQL（不落库）。"""
    d = request.get_json(force=True, silent=True) or {}
    metric = {
        "db_type": d.get("db_type"), "host": d.get("host"),
        "port": d.get("port"), "db_name": d.get("db_name"),
        "username": d.get("username"),
        "password_enc": db.encrypt_secret(d.get("password") or "")
        if d.get("password") else "",
        "sql_text": d.get("sql_text") or "",
    }
    if not metric["sql_text"].strip():
        return _err("监控 SQL 必填")
    try:
        num, text = db_monitor._first_value(metric)
        return jsonify({"success": True,
                        "data": {"value": num if num is not None else text}})
    except Exception as e:
        return _err("试跑失败：%s" % str(e)[:300])
