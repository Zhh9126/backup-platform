# -*- coding: utf-8 -*-
"""备份任务相关 API：增删改查、立即执行、模板下载、批量导入。"""
import csv
import io
from flask import request, jsonify, make_response

from auth import login_required
from core import models, scheduler, db
from core.engines import supported_types, get_engine
from . import api_bp

# 各类型拉库时过滤的系统库/模板库
_LIST_DB_SKIP = {
    "mysql": {"information_schema", "performance_schema", "mysql", "sys"},
    "mariadb": {"information_schema", "performance_schema", "mysql", "sys"},
    "postgresql": {"template0", "template1"},
    "kingbase": {"template0", "template1"},
}


def _fetch_db_list(db_type: str, task: dict):
    """拉取库列表：优先原有连接方式（SSH/本机客户端），失败或为空时回退 JDBC 通道。

    返回 (databases, via_jdbc, error)。databases 为空且 error 非空表示整体失败。
    """
    original_err = ""
    try:
        eng = get_engine(db_type, task, "", None)
        dbs = eng.list_databases()
        if dbs:
            return dbs, False, None
        original_err = "原有连接方式（SSH/本机客户端）未返回库列表"
    except Exception as e:
        original_err = f"{e}"
    try:
        from core import jdbc
        dbs = jdbc.list_databases(
            db_type,
            task.get("host") or "127.0.0.1",
            int(task.get("port") or 0) or None,
            task.get("db_name") or "",
            task.get("username") or "",
            db.decrypt_secret(task.get("password") or ""),
        )
        return dbs, True, None
    except Exception as e2:
        return None, True, f"原有连接方式失败: {original_err}；JDBC 兜底失败: {e2}"

# 业务系统字段长度上限（字符数，中文按 1 计；设计 §10 A2）
BIZ_SYSTEM_MAX_LEN = 64


def _validate_biz_system(value, required: bool = True):
    """校验业务系统字段。返回错误提示字符串；通过时返回 None。

    Args:
        value: 请求体中的 biz_system 原始值（可能为 None / 非字符串）。
        required: True 表示空值判为错误（新建通道）；False 仅在非空时校验长度。

    Returns:
        错误信息字符串，或 None（校验通过）。
    """
    s = ("" if value is None else str(value)).strip()
    if not s:
        return "业务系统为必填" if required else None
    if len(s) > BIZ_SYSTEM_MAX_LEN:
        return f"业务系统长度不能超过 {BIZ_SYSTEM_MAX_LEN} 字符"
    return None


@api_bp.route("/tasks/<int:task_id>/list-databases", methods=["GET"])
@login_required
def list_task_databases(task_id):
    """获取该任务对应的数据库实例的库列表（用于备份范围多选 UI）。

    MySQL/MariaDB: SHOW DATABASES
    PostgreSQL:     SELECT datname FROM pg_database WHERE NOT datistemplate
    Kingbase:       同 PG（兼容协议）
    Oracle/达梦:    通过 JDBC 拉取 schema 列表
    其他类型：返回 []
    原有连接方式失败时自动回退 JDBC 通道（core/jdbc.py）。
    """
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    db_type = task.get("db_type")
    if db_type not in _LIST_DB_SKIP:
        return jsonify({"databases": [], "type": "none"})
    dbs, via_jdbc, err = _fetch_db_list(db_type, task)
    if err and not dbs:
        return jsonify({"error": f"拉取失败: {err}", "databases": []}), 500
    skip = _LIST_DB_SKIP[db_type]
    dbs = [d for d in (dbs or []) if d not in skip]
    return jsonify({
        "databases": dbs,
        "type": "schemas" if db_type in ("postgresql", "kingbase") else "databases",
        "via_jdbc": via_jdbc,
    })


@api_bp.route("/tasks", methods=["GET"])
@login_required
def list_tasks():
    db_type = request.args.get("db_type")
    db_type_exclude = request.args.get("db_type_exclude")
    tasks = models.list_tasks(include_secret=False, db_type=db_type,
                              db_type_exclude=db_type_exclude)
    return jsonify(tasks)


@api_bp.route("/tasks", methods=["POST"])
@login_required
def create_task():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("db_type") not in supported_types():
        return jsonify({"error": f"不支持的数据库类型: {data.get('db_type')}"}), 400
    if not data.get("name"):
        return jsonify({"error": "任务名称为必填"}), 400
    # 新建通道强校验（设计 §4.2.1 / §8.6）
    err = _validate_biz_system(data.get("biz_system"), required=True)
    if err:
        return jsonify({"error": err}), 400
    tid = models.create_task(data)
    scheduler.reload_scheduler()
    return jsonify({"id": tid, "ok": True}), 201


@api_bp.route("/tasks/<int:task_id>", methods=["GET"])
@login_required
def get_task(task_id):
    task = models.get_task(task_id, include_secret=False)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@api_bp.route("/tasks/<int:task_id>", methods=["PUT"])
@login_required
def update_task(task_id):
    data = request.get_json(force=True, silent=True) or {}
    if not models.get_task(task_id):
        return jsonify({"error": "任务不存在"}), 404
    # 编辑通道「存在才校验」（设计 §4.2.2）：键缺失 → 跳过（保留部分更新语义）；
    # 键存在但为空/纯空白 → 400，防止已填值被清空。
    if "biz_system" in data:
        s = ("" if data.get("biz_system") is None else str(data["biz_system"])).strip()
        if not s:
            return jsonify({"error": "业务系统不能为空"}), 400
        err = _validate_biz_system(s, required=True)
        if err:
            return jsonify({"error": err}), 400
    models.update_task(task_id, data)
    scheduler.reload_scheduler()
    return jsonify({"ok": True})


@api_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
@login_required
def delete_task(task_id):
    models.delete_task(task_id)
    scheduler.reload_scheduler()
    return jsonify({"ok": True})


@api_bp.route("/tasks/<int:task_id>/run", methods=["POST"])
@login_required
def run_task(task_id):
    data = request.get_json(force=True, silent=True) or {}
    backup_type = data.get("backup_type")
    record = scheduler.run_task_now(task_id, backup_type=backup_type)
    if not record:
        return jsonify({"error": "任务不存在"}), 404
    # 文件备份异步执行时返回 202 + accepted
    if record.get("accepted"):
        return jsonify(record), 202
    return jsonify(record)


# ------------------------- 模板下载 -------------------------
@api_bp.route("/tasks/template", methods=["GET"])
@login_required
def download_template():
    t = request.args.get("type", "db")
    buf = io.StringIO()
    w = csv.writer(buf)
    # biz_system 紧随 name 之后；批量通道不强制必填（设计 §4.2.3 / §8.6），
    # 缺列或留空时落 NULL，由 R2 回退到任务名展示。
    w.writerow(["name", "biz_system", "db_type", "host", "port", "username", "password", "db_name",
                "backup_type", "backup_mode", "schedule_type", "cron_expr", "interval_minutes",
                "enabled", "retention_days", "extra_options", "备注说明"])
    if t == "db":
        w.writerow(["示例-mysql库备份", "OA 办公系统", "mysql", "192.168.1.1", "3306", "root", "yourpassword",
                    "mydb", "full", "logical", "cron", "0 2 * * *", "", "1", "30", "", "每天凌晨2点全量逻辑备份"])
        w.writerow(["示例-pg逻辑备份", "核心交易库", "postgresql", "192.168.1.2", "5432", "postgres", "pass",
                    "mydb", "full", "logical", "none", "", "", "1", "30", "", "手动执行"])
    else:
        w.writerow(["示例-本地文件", "影像归档系统", "file", "", "", "", "", "",
                    "full", "logical", "none", "", "", "1", "30",
                    '{"source_type":"local","source_paths":["C:/data"],"target_type":"local","target_path":"D:/backup"}',
                    "本地文件全量备份"])
        w.writerow(["示例-远程文件", "日志采集平台", "file", "", "", "", "", "",
                    "full", "logical", "none", "", "", "1", "30",
                    '{"source_type":"remote","source_paths":["/opt"],"source_host":"root@192.168.1.100:22","target_type":"local","target_path":"E:/backup"}',
                    "远程文件无Agent备份"])
    resp = make_response(buf.getvalue().encode("utf-8-sig"))
    resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    resp.headers["Content-Disposition"] = "attachment; filename=backup_task_template.csv"
    return resp


# ------------------------- 批量导入 -------------------------
@api_bp.route("/tasks/import", methods=["POST"])
@login_required
def import_tasks():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "请上传 CSV 文件"}), 400
    try:
        content = f.read().decode("utf-8-sig")
    except Exception:
        return jsonify({"error": "无法读取文件，请确认是 UTF-8 编码的 CSV"}), 400
    reader = csv.DictReader(io.StringIO(content))
    created, skipped = 0, 0
    errors = []
    for i, row in enumerate(reader, start=2):
        name = (row.get("name") or "").strip()
        db_type = (row.get("db_type") or "").strip().lower()
        if not name or not db_type:
            skipped += 1; continue
        if name.startswith("示例-"):
            skipped += 1; continue
        if db_type not in supported_types():
            errors.append(f"第{i}行: 不支持的数据库类型 '{db_type}'"); continue
        data = {
            "name": name, "db_type": db_type,
            # 批量导入不强制必填：缺列/空值落 NULL，展示走 R2 回退（设计 §4.2.3）
            "biz_system": (row.get("biz_system") or "").strip() or None,
            "host": (row.get("host") or "").strip(),
            "port": int(row["port"]) if row.get("port") else None,
            "username": (row.get("username") or "").strip(),
            "password": (row.get("password") or "").strip(),
            "db_name": (row.get("db_name") or "").strip(),
            "backup_type": (row.get("backup_type") or "full").strip(),
            "backup_mode": (row.get("backup_mode") or "logical").strip(),
            "schedule_type": (row.get("schedule_type") or "none").strip(),
            "cron_expr": (row.get("cron_expr") or "").strip() or None,
            "interval_minutes": int(row["interval_minutes"]) if row.get("interval_minutes") else None,
            "enabled": int(row.get("enabled", "0") or 0),
            "retention_days": int(row.get("retention_days", "30") or 30),
            "extra_options": (row.get("extra_options") or "").strip(),
            "demo_only": 0,
        }
        try:
            models.create_task(data)
            created += 1
        except Exception as e:
            errors.append(f"第{i}行({name}): {e}")
    scheduler.reload_scheduler()
    return jsonify({"created": created, "skipped": skipped, "errors": errors})
