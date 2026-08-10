# -*- coding: utf-8 -*-
"""高级恢复 API：PITR / 对象级 / 副本克隆（VDB）。"""
import re
from datetime import datetime, timedelta
from flask import request, jsonify

from auth import login_required
from core import models, restore_extras, db
from . import api_bp


# ============================================================
# PITR：任意时间点恢复
# ============================================================
@api_bp.route("/restores/pitr", methods=["POST"])
@login_required
def restore_pitr():
    """按指定时间点恢复：record_id + target_time + 目标连接信息。"""
    data = request.get_json(force=True, silent=True) or {}
    record_id = data.get("record_id")
    target_time = data.get("target_time")
    if not record_id or not target_time:
        return jsonify({"error": "record_id 和 target_time 必填"}), 400
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"error": "备份记录不存在"}), 404
    task = models.get_task(rec["task_id"], include_secret=True)
    pwd = db.decrypt_secret(task.get("password") or "")
    # 解析 target_host（支持字符串 或 {host, port, user, password, ...} 对象）
    th = data.get("target_host")
    if isinstance(th, dict):
        target = {
            "host": th.get("host") or task.get("host"),
            "port": th.get("port") or task.get("port"),
            "user": th.get("user") or task.get("username"),
            "password": th.get("password") or pwd,
            "db": data.get("target_db") or task.get("db_name"),
            "binlog_file": rec.get("binlog_file"),
            "binlog_pos": rec.get("binlog_pos"),
        }
    else:
        target = {
            "host": th or task.get("host"),
            "port": data.get("target_port") or task.get("port"),
            "user": data.get("target_user") or task.get("username"),
            "password": data.get("target_password") or pwd,
            "db": data.get("target_db") or task.get("db_name"),
            "binlog_file": rec.get("binlog_file"),
            "binlog_pos": rec.get("binlog_pos"),
        }
    if rec.get("db_type") == "mysql":
        # PITR 需要 data_dir
        target["binlog_dir"] = data.get("binlog_dir") or "/var/lib/mysql"
        res = restore_extras.mysql_pitr_restore(rec["backup_path"], target_time, target)
    elif rec.get("db_type") == "postgresql":
        target["data_dir"] = data.get("data_dir") or "/var/lib/pgsql/data"
        res = restore_extras.pg_pitr_restore(rec["backup_path"], target_time, target)
    else:
        return jsonify({"error": f"db_type={rec.get('db_type')} 不支持 PITR"}), 400
    return jsonify(res)


# ============================================================
# 对象级精准恢复（单表 / 单 schema）
# ============================================================
@api_bp.route("/restores/object", methods=["POST"])
@login_required
def restore_object():
    """从备份中精准恢复指定对象（MySQL 表 / PG 表或 schema）。"""
    data = request.get_json(force=True, silent=True) or {}
    record_id = data.get("record_id")
    object_name = data.get("object_name")
    if not record_id or not object_name:
        return jsonify({"error": "record_id 和 object_name 必填"}), 400
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"error": "备份记录不存在"}), 404
    task = models.get_task(rec["task_id"], include_secret=True)
    pwd = db.decrypt_secret(task.get("password") or "")
    # 支持字符串 或 dict 形式
    th = data.get("target_host")
    if isinstance(th, dict):
        target = {
            "host": th.get("host") or task.get("host"),
            "port": th.get("port") or task.get("port"),
            "user": th.get("user") or task.get("username"),
            "password": th.get("password") or pwd,
            "db": data.get("target_db") or task.get("db_name"),
        }
    else:
        target = {
            "host": th or task.get("host"),
            "port": data.get("target_port") or task.get("port"),
            "user": data.get("target_user") or task.get("username"),
            "password": data.get("target_password") or pwd,
            "db": data.get("target_db") or task.get("db_name"),
        }
    db_type = rec.get("db_type")
    if db_type == "mysql":
        res = restore_extras.mysql_restore_object(rec["backup_path"], object_name, target)
    elif db_type == "postgresql":
        res = restore_extras.pg_restore_object(rec["backup_path"], object_name, target)
    else:
        return jsonify({"error": f"db_type={db_type} 不支持对象级恢复"}), 400
    return jsonify(res)


# ============================================================
# 副本克隆（VDB / 测试库）
# ============================================================
@api_bp.route("/vdb/clone", methods=["POST"])
@login_required
def clone_vdb():
    """从备份快速创建一个测试库（VDB 风格）。"""
    data = request.get_json(force=True, silent=True) or {}
    record_id = data.get("record_id")
    name = data.get("name")
    ttl_hours = int(data.get("ttl_hours") or 24)
    note = data.get("note", "")
    if not record_id or not name:
        return jsonify({"error": "record_id 和 name 必填"}), 400
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,30}$", name):
        return jsonify({"error": "name 非法（仅支持字母/数字/下划线，≤30 字符）"}), 400
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"error": "备份记录不存在"}), 404
    task = models.get_task(rec["task_id"], include_secret=True)
    db_type = rec.get("db_type")
    # 名字加 _vdb 区分
    instance_name = f"{name}_vdb"
    if db_type == "mysql":
        res = restore_extras.mysql_clone_to_test(rec["backup_path"], instance_name)
        port = 3306
        user = "root"
    elif db_type == "postgresql":
        res = restore_extras.pg_clone_to_test(rec["backup_path"], instance_name)
        port = 5432
        user = "postgres"
    else:
        return jsonify({"error": f"db_type={db_type} 不支持 VDB 克隆"}), 400
    if not res.get("ok"):
        return jsonify(res), 500
    # 记录到 vdb_instances
    expires = (datetime.utcnow() + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    vdb_id = models.create_vdb({
        "name": instance_name, "source_record_id": record_id, "task_id": rec["task_id"],
        "db_type": db_type, "port": port, "host": "127.0.0.1",
        "database_name": instance_name, "username": user,
        "status": "ready", "expires_at": expires, "note": note,
    })
    res["vdb_id"] = vdb_id
    res["expires_at"] = expires
    return jsonify(res)


@api_bp.route("/vdb", methods=["GET"])
@login_required
def list_vdb():
    return jsonify(models.list_vdbs())


@api_bp.route("/vdb/<int:vdb_id>", methods=["DELETE"])
@login_required
def drop_vdb(vdb_id):
    v = models.get_vdb(vdb_id)
    if not v:
        return jsonify({"error": "VDB 不存在"}), 404
    res = restore_extras.drop_clone(v.get("db_type"), v.get("name"))
    models.delete_vdb(vdb_id)
    return jsonify(res)
