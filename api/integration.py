# -*- coding: utf-8 -*-
"""接口对接中心 API（2026-11-25）。

能力
----
- 对接任务 CRUD（pull 主动拉取 / push 主动推送）
- ``POST /integration-tasks/test``：**未保存配置也能测**，真实发一次请求并回显
  原始响应片段 + JSONPath 解析结果（用户填错当场可见）
- ``POST /integration-tasks/<id>/run``：立即执行一次
- 采集日志查询 / 导出（CSV）
- ``GET|POST /integration/log-source``：**对端拉取入口**——另一台 AIDBM 或任何
  带 Bearer Token 的系统可直接拉本平台日志（返回 ``data.logs``）
"""
import csv
import io
import json

from flask import jsonify, request, send_file

from auth import permission_required
from core import integration
from . import api_bp


def _operator():
    from flask import session
    user = session.get("user") or {}
    if isinstance(user, dict):
        return user.get("username") or "system"
    return session.get("username") or "system"


@api_bp.get("/integration/summary")
def integration_summary():
    return jsonify({"ok": True, "stats": integration.stats(),
                    "tasks": integration.list_tasks()})


# --------------------------------------------------------------------------- #
# 对接任务 CRUD
# --------------------------------------------------------------------------- #
@api_bp.get("/integration-tasks")
def list_integration_tasks():
    rows = integration.list_tasks()
    return jsonify({"ok": True, "tasks": rows, "count": len(rows)})


@api_bp.get("/integration-tasks/<int:tid>")
def get_integration_task(tid):
    row = integration.get_task(tid)
    if not row:
        return jsonify({"ok": False, "error": "对接任务不存在"}), 404
    return jsonify({"ok": True, "task": row})


@api_bp.post("/integration-tasks")
@permission_required("system.settings")
def create_integration_task():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "配置需为 JSON 对象"}), 400
    try:
        tid = integration.create_task(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _reload()
    return jsonify({"ok": True, "id": tid, "task": integration.get_task(tid)}), 201


@api_bp.put("/integration-tasks/<int:tid>")
@permission_required("system.settings")
def update_integration_task(tid):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "配置需为 JSON 对象"}), 400
    try:
        row = integration.update_task(tid, data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _reload()
    return jsonify({"ok": True, "task": row})


@api_bp.delete("/integration-tasks/<int:tid>")
@permission_required("system.settings")
def delete_integration_task(tid):
    if not integration.get_task(tid):
        return jsonify({"ok": False, "error": "对接任务不存在"}), 404
    integration.delete_task(tid)
    _reload()
    return jsonify({"ok": True})


def _reload():
    """任务的 cron 变更要立即生效（与备份任务同一套 reload 通道）。"""
    try:
        from core import scheduler
        scheduler.reload_scheduler()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 测试 / 立即执行
# --------------------------------------------------------------------------- #
@api_bp.post("/integration-tasks/test")
def test_integration_task():
    """真实请求一次，返回原始响应片段与解析结果；不需要先保存配置。"""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "测试配置需为 JSON 对象"}), 400
    tid = data.pop("id", None)
    try:
        result = integration.test_task(data=data, tid=tid)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 500
    return jsonify(result), (200 if result.get("ok") else 200)


@api_bp.post("/integration-tasks/<int:tid>/run")
@permission_required("system.settings")
def run_integration_task(tid):
    try:
        result = integration.run_task(tid, trigger_by="manual:" + _operator())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result), (200 if result.get("ok") else 502)


# --------------------------------------------------------------------------- #
# 采集日志 / 运行记录
# --------------------------------------------------------------------------- #
@api_bp.get("/integration-tasks/<int:tid>/logs")
def integration_task_logs(tid):
    limit, offset = _page_args()
    rows, total = integration.list_logs(
        task_id=tid, level=(request.args.get("level") or "").strip(),
        keyword=(request.args.get("q") or "").strip(),
        alerted_only=request.args.get("alerted") in ("1", "true"),
        limit=limit, offset=offset)
    return jsonify({"ok": True, "logs": rows, "total": total})


@api_bp.get("/integration/logs")
def integration_logs():
    limit, offset = _page_args()
    rows, total = integration.list_logs(
        task_id=_int_arg("task_id"), level=(request.args.get("level") or "").strip(),
        keyword=(request.args.get("q") or "").strip(),
        alerted_only=request.args.get("alerted") in ("1", "true"),
        limit=limit, offset=offset)
    return jsonify({"ok": True, "logs": rows, "total": total})


@api_bp.get("/integration/logs/export.csv")
def integration_logs_export():
    rows, _total = integration.list_logs(
        task_id=_int_arg("task_id"), level=(request.args.get("level") or "").strip(),
        keyword=(request.args.get("q") or "").strip(),
        alerted_only=request.args.get("alerted") in ("1", "true"),
        limit=20000, offset=0)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "级别", "任务", "来源", "告警", "内容"])
    for r in rows:
        writer.writerow([r.get("ts") or "", r.get("level") or "",
                         r.get("task_name") or "", r.get("source") or "",
                         "是" if r.get("alerted") else "", r.get("message") or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return send_file(io.BytesIO(data), mimetype="text/csv; charset=utf-8",
                     as_attachment=True,
                     download_name="integration_logs.csv")


@api_bp.get("/integration-tasks/<int:tid>/runs")
def integration_task_runs(tid):
    return jsonify({"ok": True, "runs": integration.list_runs(tid, limit=50)})


@api_bp.get("/integration/runs")
def integration_runs():
    return jsonify({"ok": True, "runs": integration.list_runs(limit=100)})


# --------------------------------------------------------------------------- #
# 对端拉取入口（供另一台 AIDBM / 任意带 Token 的系统采集本平台日志）
# --------------------------------------------------------------------------- #
@api_bp.route("/integration/log-source", methods=["GET", "POST"])
def integration_log_source():
    """返回本平台系统日志，供外部按水位增量拉取。

    GET  /api/v1/integration/log-source?since=123&limit=200&level=ERROR
    POST /api/v1/integration/log-source  {"since": "2026-11-25T00:00:00", "limit": 200}
    -> {"success": true, "data": {"logs": [...], "count": n, "next_since": "..."}}
    """
    params = dict(request.args)
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            params.update({k: v for k, v in payload.items() if v is not None})
    since = str(params.get("since") or params.get("since_id") or "").strip()
    try:
        limit = int(params.get("limit") or 500)
    except (TypeError, ValueError):
        limit = 500
    data = integration.log_source(
        since=since, level=str(params.get("level") or "").strip(),
        keyword=str(params.get("q") or "").strip(), limit=limit)
    return jsonify({"success": True, "data": data})


def _int_arg(name):
    raw = (request.args.get(name) or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _page_args():
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        size = max(1, min(int(request.args.get("size") or 50), 500))
    except ValueError:
        size = 50
    return size, (page - 1) * size
