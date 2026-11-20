# -*- coding: utf-8 -*-
"""高级恢复 API：PITR / 对象级 / 副本克隆（VDB）。"""
import os
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
    # 密码必须落库：VDB 快照/接管检查/就绪校验都要靠它连库。
    # 历史缺陷：本路径此前**不传 password**，导致这类 VDB 密码为空——
    # 快照一定连不上库（连接被拒），且是「看起来创建成功」的隐性失败。
    # models.create_vdb 已统一做幂等加密（明文才加密，密文原样存）。
    src_pw = ""
    try:
        src_pw = (task or {}).get("password") or ""
    except Exception:
        src_pw = ""
    expires = (datetime.utcnow() + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    vdb_id = models.create_vdb({
        "name": instance_name, "source_record_id": record_id, "task_id": rec["task_id"],
        "db_type": db_type, "port": port, "host": "127.0.0.1",
        "database_name": instance_name, "username": user,
        "password": src_pw,
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


# ------------------------- VDB 快照（按实例维度） -------------------------
@api_bp.route("/vdb/<int:vdb_id>/snapshots", methods=["GET"])
@login_required
def list_vdb_snapshots(vdb_id):
    """列出某个 VDB 实例的快照（与克隆请求维度等价，VDB 页直接用）。"""
    v = models.get_vdb(vdb_id)
    if not v:
        return jsonify({"error": "VDB 不存在"}), 404
    rows = db.query("SELECT * FROM vdb_snapshots WHERE vdb_instance_id=? "
                    "AND status!='deleted' ORDER BY id DESC", (int(vdb_id),))
    return jsonify([dict(r) for r in (rows or [])])


@api_bp.route("/vdb/<int:vdb_id>/snapshots", methods=["POST"])
@login_required
def create_vdb_snapshot(vdb_id):
    """对 VDB 实例创建快照。

    body.kind：``auto``（默认，优先原生产物）/ ``native``（标准备份产物，
    含 DDL + 一致性快照）/ ``logical``（数据级 JSON）。
    """
    v = models.get_vdb(vdb_id)
    if not v:
        return jsonify({"error": "VDB 不存在"}), 404
    body = request.get_json(silent=True) or {}
    from core import vdb_snapshot as vs
    from core.clone_service import clone_service as _cs
    try:
        pw = _cs._vdb_password(v, None)
        if not pw:
            raise ValueError("该 VDB 没有可用密码，无法连接数据库做快照"
                             "（请补配 VDB 凭据）")
        meta = None
        kind = str(body.get("kind") or "auto").lower()
        note = ""
        if kind in ("auto", "native"):
            try:
                meta = vs.export_native_snapshot(
                    v, pw, name=body.get("name") or "",
                    created_by=body.get("created_by") or "")
            except Exception as e:
                if kind == "native":
                    raise
                note = ("原生导出不可用，已回退为数据级快照（不含 DDL）：%s"
                        % str(e)[:160])
                meta = None
        if meta is None:
            meta = vs.export_snapshot(v, pw, name=body.get("name") or "",
                                      created_by=body.get("created_by") or "")
        # 快照与克隆请求解绑：clone_request_id 允许为空（历史 DDL 是 NOT NULL，
        # 这里用 0 表示「按 VDB 实例创建」）
        parent = None
        last = db.query("SELECT id FROM vdb_snapshots WHERE vdb_instance_id=? "
                        "AND status='ready' ORDER BY id DESC LIMIT 1",
                        (int(vdb_id),))
        if last:
            parent = last[0]["id"]
        sid = db.execute(
            "INSERT INTO vdb_snapshots(clone_request_id, vdb_instance_id, name,"
            " snap_kind, parent_snapshot_id, backup_path, size_bytes, checksum,"
            " table_count, row_count, status, note, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (0, int(vdb_id), meta["name"], meta["snap_kind"], parent,
             meta["backup_path"], meta["size_bytes"], meta["checksum"],
             meta["table_count"], meta["row_count"], "ready",
             note or meta.get("note") or "", db.now_iso(),
             body.get("created_by") or "unknown"))
        db.add_log("INFO", "clone",
                   "VDB 快照 %s 创建：VDB %s，类型 %s，%s 表，%s 字节"
                   % (sid, vdb_id, meta["snap_kind"], meta["table_count"],
                      meta["size_bytes"]))
        try:
            vs.apply_retention(vdb_id=int(vdb_id),
                               keep=int(os.environ.get(
                                   "AIDBM_VDB_SNAPSHOT_KEEP") or 10))
        except Exception:
            pass
        row = db.query_one("SELECT * FROM vdb_snapshots WHERE id=?", (sid,))
        return jsonify({"ok": True, "snapshot": dict(row or {})}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "创建快照失败: %s" % str(e)[:200]}), 500


@api_bp.route("/vdb/<int:vdb_id>/snapshots/<int:snapshot_id>/rollback",
              methods=["POST"])
@login_required
def rollback_vdb_snapshot(vdb_id, snapshot_id):
    """把 VDB 回滚到指定快照点（高危操作，走操作验证码闸）。"""
    from core.clone_service import clone_service as _cs
    from auth import verify_code_gate
    body = request.get_json(silent=True) or {}
    gate = verify_code_gate(body)
    if gate:
        return jsonify(gate[0]), gate[1]
    try:
        res = _cs.rollback_vdb_snapshot(int(vdb_id), int(snapshot_id),
                                        operator=body.get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **res})


@api_bp.route("/vdb/<int:vdb_id>/snapshots/<int:snapshot_id>/purge",
              methods=["POST"])
@login_required
def purge_vdb_snapshot(vdb_id, snapshot_id):
    """物理清理某个快照的产物文件。

    清理后该快照**同时置为已删除**（元数据保留、审计可追溯）——否则列表里
    会出现「有快照点但产物已不存在」的僵尸条目，点回滚只会报产物不存在。
    """
    from core import vdb_snapshot as vs
    row = db.query_one("SELECT id, vdb_instance_id FROM vdb_snapshots WHERE id=?",
                       (int(snapshot_id),))
    if not row or int(row.get("vdb_instance_id") or 0) != int(vdb_id):
        return jsonify({"error": "快照不存在"}), 404
    body = request.get_json(silent=True) or {}
    try:
        res = vs.purge_snapshot(snapshot_id,
                                operator=body.get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(res)
