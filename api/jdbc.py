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
    """直连能力状态：原生驱动就绪状态 + JVM（JDBC 兜底）探测结果。"""
    from core import native_conn
    return jsonify({
        "success": True,
        "native": {
            "drivers": native_conn.driver_status(),
            "db_types": list(native_conn.NATIVE_DB_TYPES),
        },
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


# ---------------------------------------------------------------------------
# JDBC 驱动管理：上传 / 列表 / 下载 / 删除 jar
# ---------------------------------------------------------------------------

from flask import send_file  # noqa: E402
import io as _io
from werkzeug.utils import secure_filename as _secure_filename

import core.jdbc as _jdbc  # noqa: E402


def _driver_dir_str() -> str:
    return str(_jdbc.DRIVERS_DIR)


@api_bp.route("/jdbc/drivers", methods=["GET"])
@login_required
def jdbc_list_drivers():
    """列出 drivers/ 下所有 jar，标记是否已被 DRIVER_CONFIG 注册。"""
    try:
        items = _jdbc.list_driver_files()
        return jsonify({
            "success": True,
            "drivers": items,
            "drivers_dir": _driver_dir_str(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "drivers": []}), 500


@api_bp.route("/jdbc/drivers/upload", methods=["POST"])
@login_required
def jdbc_upload_driver():
    """上传一个 JDBC 驱动 jar（multipart/form-data，field 名=file）。"""
    f = request.files.get("file")
    if f is None:
        return jsonify({"success": False, "error": "缺少 form 字段 'file'"}), 400
    raw_name = f.filename or ""
    safe = _secure_filename(raw_name) or "driver.jar"
    if not safe.lower().endswith(".jar"):
        safe = safe + ".jar"
    try:
        data = f.read()
        info = _jdbc.save_driver_file(safe, data)
        return jsonify({
            "success": True,
            "driver": info,
            "message": f"已上传驱动 {info['name']}，"
                       f"{'已注册到 DRIVER_CONFIG' if info['registered'] else '尚未注册，请在 DRIVER_CONFIG 中添加映射以启用'}",
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"上传失败: {e}"}), 500


@api_bp.route("/jdbc/drivers/<name>/download", methods=["GET"])
@login_required
def jdbc_download_driver(name: str):
    """下载一个 jar 文件（浏览器可直接另存为）。"""
    try:
        data = _jdbc.read_driver_file(name)
    except FileNotFoundError:
        return jsonify({"success": False, "error": f"未找到驱动: {name}"}), 404
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    bio = _io.BytesIO(data)
    return send_file(
        bio,
        as_attachment=True,
        download_name=name,
        mimetype="application/java-archive",
    )


@api_bp.route("/jdbc/drivers/<name>", methods=["DELETE"])
@login_required
def jdbc_delete_driver(name: str):
    """删除一个未被 DRIVER_CONFIG 引用的 jar。"""
    try:
        info = _jdbc.delete_driver_file(name)
        return jsonify({"success": True, "driver": info})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"success": False, "error": f"未找到驱动: {name}"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": f"删除失败: {e}"}), 500
