# -*- coding: utf-8 -*-
"""JDBC 连接方式 API：连接测试、拉取库列表、能力状态。

设计：保持「原有连接方式（SSH/本机客户端）优先」，本模块提供显式的 JDBC
通道（测试/拉库），供任务表单、连接诊断与引擎兜底使用。
"""
from flask import request, jsonify

from auth import login_required
from core import jdbc, models, db
from . import api_bp

# 各类型默认端口（与引擎默认值对齐）
DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "kingbase": 54321,
    "oracle": 1521,
    "dameng": 5236,
}


@api_bp.route("/jdbc/status", methods=["GET"])
@login_required
def jdbc_status():
    """JDBC 能力状态：JVM 探测结果 + 各类型驱动 jar 就绪状态。"""
    return jsonify({
        "success": True,
        "jvm": jdbc.jvm_info(),
        "drivers": jdbc.available_drivers(),
        "db_types": list(jdbc.JDBC_DB_TYPES),
    })


def _resolve_params(body: dict) -> dict:
    """解析连接参数：支持直接传参（前端测试）或传 task_id 复用任务连接配置。"""
    if not isinstance(body, dict):
        body = {}
    params = {
        "db_type": body.get("db_type"),
        "host": body.get("host"),
        "port": body.get("port"),
        "db_name": body.get("db_name"),
        "username": body.get("username"),
        "password": body.get("password"),
    }
    task_id = body.get("task_id")
    if task_id:
        task = models.get_task(int(task_id), include_secret=True)
        if not task:
            raise ValueError("任务不存在")
        params["db_type"] = params["db_type"] or task.get("db_type")
        params["host"] = params["host"] or task.get("host")
        params["port"] = params["port"] if params["port"] not in (None, "", 0) else task.get("port")
        params["db_name"] = params["db_name"] or task.get("db_name")
        params["username"] = params["username"] or task.get("username")
        if not params["password"]:
            params["password"] = db.decrypt_secret(task.get("password") or "")
    db_type = params["db_type"]
    if not db_type:
        raise ValueError("缺少 db_type")
    if db_type not in jdbc.DRIVER_CONFIG:
        raise ValueError(f"暂不支持 JDBC 连接类型: {db_type}")
    params["host"] = params["host"] or "127.0.0.1"
    if params["port"] in (None, "", 0):
        params["port"] = DEFAULT_PORTS.get(db_type, 0)
    return params


@api_bp.route("/jdbc/test-connection", methods=["POST"])
@login_required
def jdbc_test_connection():
    """测试 JDBC 连接。Body: {task_id} 或 {db_type,host,port,db_name,username,password}。"""
    body = request.get_json(silent=True) or {}
    try:
        p = _resolve_params(body)
        ok, msg, info = jdbc.test_connection(
            p["db_type"], p["host"], p["port"], p["db_name"], p["username"], p["password"])
        if ok:
            return jsonify({"success": True, "message": msg, "info": info})
        return jsonify({"success": False, "message": msg, "info": None})
    except Exception as e:
        return jsonify({"success": False, "message": f"JDBC 连接测试失败: {e}", "info": None}), 400


@api_bp.route("/jdbc/list-databases", methods=["POST"])
@login_required
def jdbc_list_databases():
    """通过 JDBC 拉取库/schema 列表。Body 同 test-connection。"""
    body = request.get_json(silent=True) or {}
    try:
        p = _resolve_params(body)
        dbs = jdbc.list_databases(
            p["db_type"], p["host"], p["port"], p["db_name"], p["username"], p["password"])
        return jsonify({"success": True, "databases": dbs})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "databases": []}), 400
