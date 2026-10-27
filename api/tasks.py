# -*- coding: utf-8 -*-
"""备份任务相关 API：增删改查、立即执行、模板下载、批量导入。"""
import csv
import io
from flask import request, jsonify, make_response

from auth import login_required
from core import models, scheduler, db
from core.engines import supported_types, get_engine
from . import api_bp, contract

# 各类型拉库时过滤的系统库/模板库
_LIST_DB_SKIP = {
    "mysql": {"information_schema", "performance_schema", "mysql", "sys"},
    "mariadb": {"information_schema", "performance_schema", "mysql", "sys"},
    "postgresql": {"template0", "template1"},
    "kingbase": {"template0", "template1"},
    "sqlserver": {"master", "tempdb", "model", "msdb"},
    # Neo4j：system 为系统库（不可 dump），引擎经 cypher-shell SHOW DATABASES 拉取
    "neo4j": {"system"},
}


def _secure_ssh_cred(extra_options):
    """extra_options.ssh_cred.password 加密保存（免纳管 SSH 执行通道）。

    - 前端传明文密码（无 _enc 标记）→ 加密后落库；
    - 前端未改密码（带 _enc=1，密文原样传回）→ 原样保留；
    - 解析失败时不修改原值（不影响任务其它字段保存）。
    """
    if not extra_options:
        return extra_options
    try:
        import json as _json
        eo = (_json.loads(extra_options) if isinstance(extra_options, str)
              else dict(extra_options))
        if not isinstance(eo, dict):
            return extra_options
        cred = eo.get("ssh_cred")
        if isinstance(cred, dict) and cred.get("password"):
            if not cred.get("_enc"):
                cred["password"] = db.encrypt_secret(str(cred["password"]))
                cred["_enc"] = 1
            eo["ssh_cred"] = cred
            return _json.dumps(eo, ensure_ascii=False)
    except Exception:
        pass
    return extra_options


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
        # 无 JDBC/原生直连通道的类型（如 Neo4j，只能靠 cypher-shell）不该被判成故障：
        # 引擎侧拿不到列表时返回空集，由前端提示「请手工填写库名」。
        if db_type not in jdbc.JDBC_DB_TYPES:
            return [], False, None
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


@api_bp.route("/tasks/<int:task_id>/precheck", methods=["POST"])
@login_required
def api_task_precheck(task_id):
    """备份预检：先探测该任务能否成功备份（配置/通道/源库/工具/空间）。

    预检是探测不是演练：不落盘产物、不改数据。ok=False 表示存在阻断项。
    """
    from core import backup_precheck
    try:
        res = backup_precheck.precheck_backup_by_id(task_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": "预检失败: %s" % e}), 500
    return jsonify(res)


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


@api_bp.route("/custom-script/template", methods=["GET"])
@api_bp.route("/custom-scripts/template", methods=["GET"])
@login_required
def custom_script_template():
    """按数据库类型 + 备份范围返回自定义备份/恢复脚本模板。

    query: db_type（mysql/postgresql/oracle/dameng/sqlserver/mongodb/...）、
           scope（full_instance | full_database | single_table）
    返回的脚本可直接执行，界面一键填充后按需微调即可（全库/单表均支持）。
    """
    from core import custom_scripts
    db_type = (request.args.get("db_type") or "mysql").strip()
    scope = (request.args.get("scope") or custom_scripts.SCOPE_DATABASE).strip()
    tpl = custom_scripts.template(db_type, scope)
    tpl["scopes"] = [{"value": s, "label": custom_scripts.SCOPE_LABELS[s]}
                     for s in custom_scripts.SCOPES]
    return jsonify(tpl)


@api_bp.route("/tasks/<int:task_id>/list-tables", methods=["GET"])
@login_required
def list_task_tables(task_id):
    """列出任务库中的基础表（供「自定义备份 → 单表/多表」勾选表名）。

    直连优先（core/native_conn），失败回退 JDBC（core/data_compare 的表清单逻辑）。
    """
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    db_type = (task.get("db_type") or "").lower()
    database = request.args.get("db") or task.get("db_name") or ""
    schema = request.args.get("schema") or ""
    conn = None
    err = ""
    try:
        from core import native_conn
        conn = native_conn.connect(
            db_type, task.get("host"), task.get("port"), database,
            task.get("username"), db.decrypt_secret(task.get("password") or ""))
    except Exception as e:
        err = str(e)
    if conn is None:
        try:
            from core import jdbc
            conn = jdbc.connect(
                db_type, task.get("host"), task.get("port"), database,
                task.get("username"), db.decrypt_secret(task.get("password") or ""))
        except Exception as e:
            # 连不上/库不存在属于"任务配置或目标环境问题"，不是服务端异常：
            # 返回 400（带 error 与空表清单）而非 500，前端可提示用户、压测不会误判 5xx
            return jsonify({"error": f"连接失败: {err or e}", "tables": []}), 400
    try:
        from core.data_compare import _list_tables
        tables = _list_tables(conn, db_type, database, schema)
    except Exception as e:
        return jsonify({"error": f"拉取表清单失败: {e}", "tables": []}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify({"tables": tables, "db": database, "count": len(tables)})


@api_bp.route("/tasks", methods=["GET"])
@login_required
def list_tasks():
    """列出备份任务（分页见 docs/api_conventions.md §2）。"""
    db_type = request.args.get("db_type")
    db_type_exclude = request.args.get("db_type_exclude")
    tasks = models.list_tasks(include_secret=False, db_type=db_type,
                              db_type_exclude=db_type_exclude)
    # 统一分页：v1 默认 100/页；兼容路径默认返回全量（历史行为不变）
    default_size = 100 if request.path.startswith(contract.V1_PREFIX) else len(tasks) or 1
    _page, size, offset = contract.pagination_args(default_size=default_size,
                                                   max_size=1000)
    return contract.list_response(tasks[offset:offset + size], total=len(tasks),
                                  legacy=tasks)


@api_bp.route("/tasks", methods=["POST"])
@login_required
def create_task():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("db_type") not in supported_types():
        return jsonify({"error": f"不支持的数据库类型: {data.get('db_type')}"}), 400
    if not data.get("name"):
        return jsonify({"error": "任务名称为必填"}), 400
    # backup_type 校验：必须是备份类型（full/incremental/differential/snapshot）。
    # 用户常把备份模式（logical/physical）误填进此字段——错误前置给出明确提示，
    # 而不是等运行时 BackupType() 枚举异常 500。
    _bt = (data.get("backup_type") or "full").strip().lower()
    if _bt in ("logical", "physical"):
        return jsonify({"error": "backup_type 应为备份类型（full/incremental/"
                                 "differential/snapshot），"
                                 f"不能填备份模式 '{_bt}'（logical/physical 应填"
                                 " backup_mode 字段）"}), 400
    if _bt not in ("full", "incremental", "differential", "snapshot"):
        return jsonify({"error": f"不支持的备份类型: {_bt}（可选 full/"
                                 "incremental/differential/snapshot）"}), 400
    data["backup_type"] = _bt
    # 新建通道强校验（设计 §4.2.1 / §8.6）
    err = _validate_biz_system(data.get("biz_system"), required=True)
    if err:
        return jsonify({"error": err}), 400
    if data.get("extra_options"):
        data["extra_options"] = _secure_ssh_cred(data["extra_options"])
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
    if data.get("extra_options"):
        # ssh_cred 保护：局部更新（如只改 enabled/备注/环境变量）时若请求未携带
        # ssh_cred，自动保留任务原有凭据——防止整列覆盖导致远程执行通道静默丢失
        try:
            import json as _json
            new_eo = (_json.loads(data["extra_options"])
                      if isinstance(data["extra_options"], str)
                      else dict(data["extra_options"]))
            if isinstance(new_eo, dict) and "ssh_cred" not in new_eo:
                old = models.get_task(task_id, include_secret=True) or {}
                old_eo = old.get("extra_options")
                old_eo = (_json.loads(old_eo) if isinstance(old_eo, str) else old_eo) or {}
                if isinstance(old_eo, dict) and old_eo.get("ssh_cred"):
                    new_eo["ssh_cred"] = old_eo["ssh_cred"]
                    data["extra_options"] = _json.dumps(new_eo, ensure_ascii=False)
        except Exception:
            pass
        data["extra_options"] = _secure_ssh_cred(data["extra_options"])
    models.update_task(task_id, data)
    scheduler.reload_scheduler()
    # 语义明确化：实时保护依赖任务启用。任务停用（enabled=0）时实时捕获
    # 不会被守护接管（重启后也不会自动拉起），必须提示，避免保护盲区
    warnings = []
    try:
        merged = models.get_task(task_id) or {}
        if int(merged.get("rt_enabled") or 0) == 1 and not merged.get("enabled"):
            warnings.append(
                "任务已停用但开启了实时备份保护：停用状态下实时捕获不会被守护接管"
                "（重启平台后也不会自动拉起），存在保护盲区；如需实时保护请同时启用任务")
    except Exception:
        pass
    return jsonify({"ok": True, "warnings": warnings})


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


@api_bp.route("/tasks/quick-backup", methods=["POST"])
@login_required
def quick_backup():
    """一键备份（对标中科热备"一键备份"）：批量立即执行备份。

    body: {"task_ids": [..]} 可选——缺省对**全部启用任务**触发；给了则按
    勾选清单触发。每个任务跑一次 full（异步，逐任务独立，失败互不影响）。
    返回触发计数与逐任务受理结果，前端 toast 汇报即可。
    """
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("task_ids") or []
    tasks = models.list_tasks(include_secret=False) or []
    enabled = [t for t in tasks if t.get("enabled")]
    targets = ([t for t in enabled if int(t.get("id")) in set(int(i) for i in ids)]
               if ids else enabled)
    if not targets:
        return jsonify({"error": "没有可备份的启用任务"}), 400
    results = []
    for t in targets:
        tid = int(t["id"])
        try:
            rec = scheduler.run_task_now(tid, backup_type="full",
                                         operator="quick-backup")
            ok = bool(rec) and (rec.get("status") in ("running", "success")
                                or rec.get("accepted"))
            results.append({"task_id": tid, "task_name": t.get("name"),
                            "ok": ok,
                            "message": (rec or {}).get("message", "")[:120]})
        except Exception as exc:  # 单任务失败不影响其余
            results.append({"task_id": tid, "task_name": t.get("name"),
                            "ok": False, "message": str(exc)[:120]})
    triggered = sum(1 for r in results if r["ok"])
    return jsonify({"ok": True, "triggered": triggered,
                    "total": len(targets), "results": results,
                    "message": f"一键备份已触发 {triggered}/{len(targets)} 个任务"})


@api_bp.route("/tasks/<int:task_id>/discover", methods=["POST"])
@login_required
def discover_task_databases(task_id):
    """新建库自动发现（对标 AXUS「备新建库」）：手动触发一次源实例新库扫描。"""
    from core import auto_discover
    data = request.get_json(force=True, silent=True) or {}
    r = auto_discover.discover_for_task(task_id, operator=data.get("operator") or "")
    return jsonify(r), (200 if r.get("ok") else 400)


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
