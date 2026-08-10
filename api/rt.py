# -*- coding: utf-8 -*-
"""准 CDP 实时备份 REST API。

统一挂载在 ``api_bp`` 上，对外路由前缀 ``/api/rt/``（不新建独立蓝图）。

路由一览：
  ==========================================  ======  ================================
  路径                                        方法    说明
  ==========================================  ======  ================================
  ``/api/rt/status``                          GET     守护总体状态 + 各 worker 健康
  ``/api/rt/health``                          GET     健康汇总（看板卡片）
  ``/api/rt/tasks``                           GET     实时任务列表（含健康）
  ``/api/rt/points``                          GET     恢复点列表（时间轴选点）
  ``/api/rt/timeline``                        GET     时间轴聚合（分桶 + 缺口）
  ``/api/rt/window``                          GET     可恢复窗口
  ``/api/rt/preview``                         POST    恢复计划预览（不落地）
  ``/api/rt/recover``                         POST    执行 PITR 恢复
  ``/api/rt/tasks/<id>/trigger``              POST    立即捕获一次
  ``/api/rt/tasks/<id>/restart``              POST    复位并重启 worker
  ``/api/rt/tasks/<id>/config``               PUT     更新实时保护配置
  ``/api/rt/capabilities``                    GET     环境自检（watchdog/CDC 客户端）
  ``/api/rt/control``                         POST    start / stop / reconcile
  ==========================================  ======  ================================

所有接口统一返回 JSON；失败一律 ``{"ok": false, "message": "..."}``，
HTTP 状态码用 400（参数错）/ 404（资源不存在）/ 500（内部错），
不让异常栈直接冒到前端。
"""
from flask import jsonify, request, session

from auth import login_required
from core import models, rt_backup
from . import api_bp

# 恢复接口允许的最大链长预览条数
_MAX_POINT_LIMIT = 2000

# rt 配置（backup_tasks.rt_* 列）→ rt_tasks 扩展表列的映射。
# 前者是"用户填的配置"，后者是"守护进程读取的运行参数"，两边必须同步，
# 否则会出现"页面已保存但实时保护不生效"的问题（UX-20260801 模块 B）。
_RT_CONFIG_TO_TASK_COLUMN = {
    "rt_mode": "rt_mode",
    "rt_interval_sec": "capture_interval",
    "rt_log_retention_days": "db_log_retention_days",
}

# 按数据库类型推断默认捕获模式：关系型走 CDC（binlog/WAL），其余走文件轮询
_DB_CDC_TYPES = ("mysql", "mariadb", "postgresql")


def _default_rt_mode(task: dict) -> str:
    """按任务的 db_type 推断缺省实时模式。"""
    db_type = str((task or {}).get("db_type") or "").lower()
    if db_type in _DB_CDC_TYPES:
        return "db_cdc"
    return "file_polling"


def _sync_rt_task_row(task: dict, body: dict) -> dict:
    """把实时配置同步到 rt_tasks 扩展行（不存在则创建，存在则更新）。

    该扩展行是守护进程 reconcile 的数据来源，缺失会导致 worker 拉不起来。
    任何异常都被吞掉并以 ``error`` 字段返回，不影响主配置保存结果。

    Args:
        task: ``models.update_rt_config`` 返回的最新任务行。
        body: 本次请求体（用于判断用户显式提交了哪些字段）。

    Returns:
        形如 ``{"action": "created"|"updated"|"skipped", "task_id": int}``
        的同步结果；失败时附带 ``error``。
    """
    task_id = int((task or {}).get("id") or 0)
    if not task_id:
        return {"action": "skipped", "reason": "任务 ID 缺失"}

    # 只映射用户本次显式提交的字段，避免把未提交项覆盖成默认值
    payload: dict = {}
    for cfg_key, col in _RT_CONFIG_TO_TASK_COLUMN.items():
        if cfg_key not in body:
            continue
        value = task.get(cfg_key)
        if value in (None, ""):
            continue
        payload[col] = value

    rt_enabled = 1 if task.get("rt_enabled") in (1, "1", True, "true", "on") else 0
    try:
        existing = models.get_rt_task(task_id)
    except Exception as exc:
        return {"action": "skipped", "task_id": task_id,
                "error": f"读取 rt_tasks 失败: {exc}"}

    try:
        if existing:
            # 关闭实时保护时同步停掉运行标记，避免看板残留"运行中"
            if not rt_enabled:
                payload["is_running"] = 0
                payload["health_status"] = "stopped"
            if not payload:
                return {"action": "skipped", "task_id": task_id,
                        "reason": "无需要同步的字段"}
            models.update_rt_task(task_id, payload)
            return {"action": "updated", "task_id": task_id,
                    "fields": sorted(payload.keys())}

        # 扩展行缺失：仅在实时保护开启时补建，关闭状态不制造脏数据
        if not rt_enabled:
            return {"action": "skipped", "task_id": task_id,
                    "reason": "实时保护未开启且扩展行不存在"}
        payload.setdefault("rt_mode", task.get("rt_mode") or _default_rt_mode(task))
        payload.setdefault("capture_interval",
                           int(task.get("rt_interval_sec") or 180))
        payload.setdefault("db_log_retention_days",
                           int(task.get("rt_log_retention_days") or 7))
        payload["task_id"] = task_id
        payload["is_running"] = 0
        payload["health_status"] = "unknown"
        models.create_rt_task(payload)
        return {"action": "created", "task_id": task_id,
                "rt_mode": payload["rt_mode"]}
    except Exception as exc:
        return {"action": "skipped", "task_id": task_id,
                "error": f"同步 rt_tasks 失败: {exc}"}


def _operator() -> str:
    """当前操作人（审计用）。取不到时回落 ``system``。"""
    try:
        return str(session.get("username") or session.get("user") or "system")
    except Exception:
        return "system"


def _int_arg(name: str, default: int = 0) -> int:
    """安全读取整型查询参数。"""
    raw = request.args.get(name)
    if raw in (None, ""):
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _fail(message: str, code: int = 400):
    """统一失败响应。

    同时带 ``error`` 字段：前端 ``api()`` 助手在非 2xx 时读取 ``data.error``
    作为异常消息，缺失会退化成「请求失败 HTTP 4xx」。
    """
    return jsonify({"ok": False, "message": message, "error": message}), code


# ======================================================================
# 状态与健康
# ======================================================================
@api_bp.route("/rt/status", methods=["GET"])
@login_required
def rt_status():
    """守护总体状态：是否持锁、驱动方式、tick 计数、各 worker 健康。"""
    try:
        data = rt_backup.status()
        data["ok"] = True
        return jsonify(data)
    except Exception as exc:
        return _fail(f"读取实时守护状态失败: {exc}", 500)


@api_bp.route("/rt/health", methods=["GET"])
@login_required
def rt_health():
    """健康汇总卡片：绿/黄/红分布 + RPO 达标率 + 今日产出。"""
    try:
        monitor = rt_backup.get_health_monitor()
        return jsonify({"ok": True, "summary": monitor.summary(),
                        "items": monitor.snapshot()})
    except Exception as exc:
        return _fail(f"读取实时健康失败: {exc}", 500)


@api_bp.route("/rt/tasks", methods=["GET"])
@login_required
def rt_tasks():
    """实时保护任务列表（含健康快照，供时间轴页左侧任务选择器）。"""
    try:
        only_enabled = request.args.get("only_enabled", "0") in ("1", "true", "on")
        rows = models.list_rt_tasks(only_enabled=only_enabled)
        monitor = rt_backup.get_health_monitor()
        out = []
        for row in rows:
            task_id = int(row.get("id") or 0)
            if task_id <= 0:
                continue
            try:
                health = monitor.of(task_id).to_dict()
            except Exception:
                health = {}
            out.append({
                "id": task_id,
                "name": row.get("name") or f"task_{task_id}",
                "db_type": row.get("db_type") or "",
                "enabled": bool(row.get("enabled", 1)),
                "rt_enabled": bool(row.get("rt_enabled")),
                "rt_mode": row.get("rt_mode") or "",
                "rt_interval_sec": row.get("rt_interval_sec"),
                "rt_consistency": row.get("rt_consistency") or "crash",
                "rt_log_retention_days": row.get("rt_log_retention_days"),
                "rt_rpo_target_sec": row.get("rt_rpo_target_sec"),
                "health": health,
            })
        return jsonify({"ok": True, "items": out, "total": len(out)})
    except Exception as exc:
        return _fail(f"读取实时任务失败: {exc}", 500)


@api_bp.route("/rt/capabilities", methods=["GET"])
@login_required
def rt_capabilities():
    """环境自检：watchdog 可用性、CDC 客户端、可选依赖包。"""
    try:
        return jsonify({"ok": True, "capabilities": rt_backup.probe_capabilities()})
    except Exception as exc:
        return _fail(f"环境自检失败: {exc}", 500)


# ======================================================================
# 恢复点与时间轴
# ======================================================================
@api_bp.route("/rt/points", methods=["GET"])
@login_required
def rt_points():
    """恢复点列表。

    Query:
        task_id (必填)、start、end、kind、limit、offset、order
    """
    task_id = _int_arg("task_id", 0)
    if task_id <= 0:
        return _fail("缺少参数 task_id")
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)

    limit = max(1, min(_int_arg("limit", 500), _MAX_POINT_LIMIT))
    order = request.args.get("order", "desc")
    if order not in ("asc", "desc"):
        order = "desc"
    try:
        pitr = rt_backup.get_pitr()
        items = pitr.points(
            task_id,
            start=request.args.get("start") or None,
            end=request.args.get("end") or None,
            kind=request.args.get("kind") or None,
            limit=limit,
            offset=max(0, _int_arg("offset", 0)),
            order=order,
        )
        return jsonify({"ok": True, "task_id": task_id, "items": items,
                        "total": len(items),
                        "window": pitr.window(task_id)})
    except Exception as exc:
        return _fail(f"读取恢复点失败: {exc}", 500)


@api_bp.route("/rt/timeline", methods=["GET"])
@login_required
def rt_timeline_data():
    """时间轴聚合：分桶柱状 + 缺口标记 + 明细点。"""
    task_id = _int_arg("task_id", 0)
    if task_id <= 0:
        return _fail("缺少参数 task_id")
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)
    try:
        data = rt_backup.get_pitr().timeline(
            task_id,
            start=request.args.get("start") or None,
            end=request.args.get("end") or None,
            buckets=max(10, min(_int_arg("buckets", 200), 2000)),
            detail_limit=max(10, min(_int_arg("detail_limit", 200), 1000)),
        )
        data["ok"] = True
        data["task_id"] = task_id
        return jsonify(data)
    except Exception as exc:
        return _fail(f"读取时间轴失败: {exc}", 500)


@api_bp.route("/rt/window", methods=["GET"])
@login_required
def rt_window():
    """可恢复窗口（最早/最晚恢复点）。"""
    task_id = _int_arg("task_id", 0)
    if task_id <= 0:
        return _fail("缺少参数 task_id")
    try:
        return jsonify({"ok": True, **rt_backup.get_pitr().window(task_id)})
    except Exception as exc:
        return _fail(f"读取可恢复窗口失败: {exc}", 500)


# ======================================================================
# PITR 恢复
# ======================================================================
@api_bp.route("/rt/preview", methods=["POST"])
@login_required
def rt_preview():
    """恢复计划预览（只算不做，供二次确认弹窗）。"""
    body = request.get_json(silent=True) or {}
    try:
        task_id = int(body.get("task_id") or 0)
    except (TypeError, ValueError):
        return _fail("task_id 非法")
    if task_id <= 0:
        return _fail("缺少参数 task_id")
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)
    try:
        plan = rt_backup.get_pitr().preview(task_id,
                                            str(body.get("target_ts") or ""))
        return jsonify({"ok": True, "plan": plan})
    except Exception as exc:
        return _fail(f"生成恢复计划失败: {exc}", 500)


@api_bp.route("/rt/recover", methods=["POST"])
@login_required
def rt_recover():
    """执行 PITR 恢复。

    Body::

        {
          "task_id": 7,
          "target_ts": "2025-07-31T10:20:00+08:00",   // 留空=恢复到最新
          "target": {"target_dir": "/tmp/restore"},   // File
          // 或 {"host","port","user","password","db","data_dir"}  // DB
          "dry_run": false,
          "force": false                              // 链不完整时强行恢复
        }
    """
    body = request.get_json(silent=True) or {}
    try:
        task_id = int(body.get("task_id") or 0)
    except (TypeError, ValueError):
        return _fail("task_id 非法")
    if task_id <= 0:
        return _fail("缺少参数 task_id")
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)

    target = body.get("target")
    if target is not None and not isinstance(target, dict):
        return _fail("target 必须是对象")

    try:
        result = rt_backup.get_pitr().restore(
            task_id,
            target_ts=str(body.get("target_ts") or ""),
            target=target or {},
            operator=_operator(),
            dry_run=bool(body.get("dry_run")),
            force=bool(body.get("force")),
        )
        if not result.get("ok"):
            result["error"] = result.get("message") or "恢复失败"
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:
        return _fail(f"恢复执行失败: {exc}", 500)


# ======================================================================
# 任务级操作
# ======================================================================
@api_bp.route("/rt/tasks/<int:task_id>/trigger", methods=["POST"])
@login_required
def rt_trigger(task_id: int):
    """立即触发一次捕获（守护未启动时会临时建 worker 跑一次）。"""
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)
    body = request.get_json(silent=True) or {}
    try:
        result = rt_backup.trigger_now(task_id,
                                       reason=str(body.get("reason") or "manual"))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:
        return _fail(f"手动捕获失败: {exc}", 500)


@api_bp.route("/rt/tasks/<int:task_id>/restart", methods=["POST"])
@login_required
def rt_restart(task_id: int):
    """复位并重启某任务的实时 worker（清空重启预算）。"""
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)
    try:
        result = rt_backup.restart_worker(task_id)
        if not result.get("ok"):
            result["error"] = result.get("message") or "重启失败"
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:
        return _fail(f"重启实时捕获失败: {exc}", 500)


@api_bp.route("/rt/tasks/<int:task_id>/config", methods=["PUT"])
@login_required
def rt_update_config(task_id: int):
    """更新实时保护配置（白名单字段），并立即对账使其生效。"""
    if not models.get_task(task_id):
        return _fail(f"任务 {task_id} 不存在", 404)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return _fail("请求体为空")
    try:
        task = models.update_rt_config(task_id, body)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"更新实时配置失败: {exc}", 500)

    # 同步 rt_tasks 扩展行：守护进程 reconcile 依赖它，缺失会导致配置"保存了但不生效"
    rt_task_sync = _sync_rt_task_row(task, body)

    reconciled = {}
    try:
        reconciled = rt_backup.reconcile()
    except Exception as exc:
        reconciled = {"error": str(exc)}
    return jsonify({"ok": True, "task": task, "reconcile": reconciled,
                    "rt_task": rt_task_sync,
                    "message": "实时保护配置已更新"})


# ======================================================================
# 守护控制
# ======================================================================
@api_bp.route("/rt/control", methods=["POST"])
@login_required
def rt_control():
    """守护控制：``{"action": "start" | "stop" | "reconcile"}``。"""
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").lower()
    try:
        if action == "start":
            started = rt_backup.start()
            return jsonify({"ok": bool(started),
                            "message": ("实时守护已启动" if started
                                        else "启动失败：总开关关闭或锁被其他进程持有"),
                            "status": rt_backup.status()})
        if action == "stop":
            rt_backup.stop()
            return jsonify({"ok": True, "message": "实时守护已停止",
                            "status": rt_backup.status()})
        if action == "reconcile":
            return jsonify({"ok": True, "message": "对账完成",
                            "result": rt_backup.reconcile()})
        return _fail("action 必须是 start / stop / reconcile 之一")
    except Exception as exc:
        return _fail(f"守护控制失败: {exc}", 500)
