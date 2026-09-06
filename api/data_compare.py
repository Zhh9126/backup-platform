# -*- coding: utf-8 -*-
"""数据对比（恢复数据 vs 生产库）API。"""
import threading

from flask import request, jsonify

from . import api_bp
from core import models
from core.data_compare import run_data_compare_task
from auth import login_required

# 后台执行中的对比任务（task_id -> thread），防止重复触发
_running = {}
_lock = threading.Lock()


@api_bp.route("/data-compare-tasks", methods=["GET"])
@login_required
def dc_list_tasks():
    """列出数据对比任务（密码脱敏）。"""
    return jsonify({"success": True, "data": models.list_data_compare_tasks()})


@api_bp.route("/data-compare-tasks", methods=["POST"])
@login_required
def dc_create_task():
    """创建数据对比任务。"""
    data = request.get_json(force=True, silent=True) or {}
    missing = [f for f in ("source_db_type", "source_host", "target_db_type",
                           "target_host") if not data.get(f)]
    if missing:
        return jsonify({"success": False, "message": f"缺少必填字段: {missing}"}), 400
    task_id = models.create_data_compare_task(data)
    return jsonify({"success": True, "data": {"id": task_id}}), 201


@api_bp.route("/data-compare-tasks/<int:task_id>", methods=["GET"])
@login_required
def dc_get_task(task_id: int):
    task = models.get_data_compare_task(task_id)
    if not task:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    with _lock:
        task["running"] = task_id in _running
    return jsonify({"success": True, "data": task})


@api_bp.route("/data-compare-tasks/<int:task_id>", methods=["PUT"])
@login_required
def dc_update_task(task_id: int):
    data = request.get_json(force=True, silent=True) or {}
    ok = models.update_data_compare_task(task_id, data)
    if not ok:
        return jsonify({"success": False, "message": "没有可更新内容"}), 400
    return jsonify({"success": True, "data": {"id": task_id}})


@api_bp.route("/data-compare-tasks/<int:task_id>", methods=["DELETE"])
@login_required
def dc_delete_task(task_id: int):
    models.delete_data_compare_task(task_id)
    return jsonify({"success": True, "message": "已删除"})


def _run_in_background(task_id: int) -> None:
    try:
        run_data_compare_task(task_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "[data_compare] 后台执行失败 task_id=%s", task_id)
    finally:
        with _lock:
            _running.pop(task_id, None)


@api_bp.route("/data-compare-tasks/<int:task_id>/run", methods=["POST"])
@login_required
def dc_run_task(task_id: int):
    """立即执行一次数据对比（后台线程执行，前端轮询报告）。"""
    task = models.get_data_compare_task(task_id)
    if not task:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    with _lock:
        if task_id in _running and _running[task_id].is_alive():
            return jsonify({"success": False, "message": "该任务正在对比中"}), 409
        t = threading.Thread(target=_run_in_background, args=(task_id,),
                             daemon=True, name=f"data-compare-{task_id}")
        _running[task_id] = t
        t.start()
    return jsonify({"success": True, "data": {"task_id": task_id, "started": True}})


@api_bp.route("/data-compare-tasks/<int:task_id>/reports", methods=["GET"])
@login_required
def dc_list_task_reports(task_id: int):
    limit = request.args.get("limit", 50, type=int)
    return jsonify({"success": True,
                    "data": models.list_data_compare_reports(task_id=task_id,
                                                             limit=limit)})


@api_bp.route("/data-compare-reports", methods=["GET"])
@login_required
def dc_list_reports():
    task_id = request.args.get("task_id", type=int)
    limit = request.args.get("limit", 200, type=int)
    return jsonify({"success": True,
                    "data": models.list_data_compare_reports(task_id=task_id,
                                                             limit=limit)})


@api_bp.route("/data-compare-reports/<int:report_id>", methods=["GET"])
@login_required
def dc_get_report(report_id: int):
    report = models.get_data_compare_report(report_id)
    if not report:
        return jsonify({"success": False, "message": "报告不存在"}), 404
    return jsonify({"success": True, "data": report})


@api_bp.route("/data-compare-stats", methods=["GET"])
@login_required
def dc_stats():
    """数据对比仪表盘 KPI。"""
    data = models.get_data_compare_stats()
    with _lock:
        data["running_count"] = sum(
            1 for t in _running.values() if t.is_alive())
    return jsonify({"success": True, "data": data})


@api_bp.route("/data-compare-reports/<int:report_id>/repair", methods=["POST"])
@login_required
def dc_repair(report_id: int):
    """差异修复（两步确认）。

    第一步（confirm 缺省/false）：dry_run —— 只返回将执行的修复 SQL 清单，
    供前端弹窗二次确认；
    第二步（confirm=true）：在目标库逐条执行修复 SQL，返回每条结果。
    修复只作用于目标库（恢复端），源端（生产端）只读。
    """
    from core.data_compare import _open_conn, _side_cfg
    body = request.get_json(force=True, silent=True) or {}
    confirm = bool(body.get("confirm"))
    only_table = body.get("table")
    only_pks = {str(p) for p in (body.get("pks") or [])}

    report = models.get_data_compare_report(report_id)
    if not report:
        return jsonify({"success": False, "message": "报告不存在"}), 404
    task = models.get_data_compare_task(report.get("task_id"),
                                        include_secret=True)
    if not task:
        return jsonify({"success": False, "message": "关联任务不存在或已删除"}), 404

    tables = report.get("tables_json") or []
    stmts = []
    for t in tables:
        if only_table and t.get("table") != only_table:
            continue
        for d in (t.get("diffs") or []):
            sql = (d.get("repair") or "").strip()
            if not sql or sql.startswith("--"):
                continue  # 无可执行修复 SQL（旧报告/列信息缺失）
            if only_pks and str(d.get("pk")) not in only_pks:
                continue
            stmts.append({"table": t.get("table"), "pk": d.get("pk"),
                          "op": d.get("op"), "sql": sql})
    if not stmts:
        return jsonify({"success": False,
                        "message": "没有可自动修复的差异（请先重新对比生成最新报告）"}), 400

    if not confirm:
        return jsonify({"success": True, "data": {
            "dry_run": True, "count": len(stmts), "statements": stmts,
            "message": f"共 {len(stmts)} 条修复 SQL 将在目标库执行，请确认"}})

    cfg = _side_cfg(task, "target")
    try:
        conn = _open_conn(cfg)
    except Exception as e:
        return jsonify({"success": False,
                        "message": f"无法连接目标库: {e}"}), 400
    results, ok_n, err_n = [], 0, 0
    try:
        cur = conn.cursor()
        for s in stmts:
            try:
                cur.execute(s["sql"])
                rc = getattr(cur, "rowcount", None)
                results.append({**s, "ok": True, "rowcount": rc})
                ok_n += 1
            except Exception as e:
                results.append({**s, "ok": False, "error": str(e)[:200]})
                err_n += 1
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify({"success": err_n == 0, "data": {
        "executed": ok_n, "failed": err_n, "results": results,
        "message": (f"修复完成：成功 {ok_n} 条，失败 {err_n} 条（已提交）；"
                    "建议立即重新对比验证" if err_n == 0
                    else f"修复完成：成功 {ok_n} 条，失败 {err_n} 条")}})
