# -*- coding: utf-8 -*-
"""可插拔数据库类型（Database Adapter）的 CRUD/测试/模板 API。"""
import json

from flask import jsonify, request

from auth import login_required
from core import db_adapters
from core.engines import ENGINE_REGISTRY, ENGINE_DISPLAY
from . import api_bp


# --------------- 内置模板样例（用户在界面"复制为新类型"时直接套用） ---------------
_TEMPLATES = {
    "generic_shell": {
        "label": "通用 Shell 脚本（最常用）",
        "category": "custom",
        "icon": "bi-terminal",
        "description": "在数据库服务器（SSH 主机）执行 bash 脚本，支持 PLATFORM_DB_*/PLATFORM_PARAM_<KEY> 等环境变量与 {{...}} 占位。",
        "backup_modes": ["logical"],
        "supports_incremental": False,
        "supports_full_instance": True,
        "params_hint": {"tool_bin": ""},
        "script_backup_full": (
            "# 必须将备份产物写入 ${PLATFORM_BACKUP_DIR}\n"
            "set -e\n"
            "mkdir -p \"${PLATFORM_BACKUP_DIR}\"\n"
            "# 示例：用自定义工具备份数据库到产物目录\n"
            "# ${PLATFORM_TOOL_BIN} -h \"${PLATFORM_DB_HOST}\" -P \"${PLATFORM_DB_PORT}\" \\\n"
            "#   -u \"${PLATFORM_DB_USER}\" -p \"${PLATFORM_DB_PASSWORD}\" \\\n"
            "#   \"${PLATFORM_DB_NAME}\" > \"${PLATFORM_BACKUP_DIR}/${PLATFORM_DB_NAME}.dump\"\n"
            "echo \"BACKUP_OK\"\n"
        ),
        "script_restore": (
            "set -e\n"
            "echo \"RESTORE_FILE=${PLATFORM_BACKUP_FILE}\"\n"
            "echo \"RESTORE_DB=${PLATFORM_RESTORE_DB}\"\n"
            "# 工具调用占位\n"
            "echo \"RESTORE_OK\"\n"
        ),
        "script_test_conn": (
            "set -e\n"
            "echo \"HOST=${PLATFORM_DB_HOST} PORT=${PLATFORM_DB_PORT} DB=${PLATFORM_DB_NAME}\"\n"
            "echo \"CONN_OK\"\n"
        ),
    },
    "mysql_dump": {
        "label": "MySQL mysqldump（备用通道）",
        "category": "relational",
        "icon": "bi-database",
        "default_port": 3306,
        "backup_modes": ["logical"],
        "supports_incremental": False,
        "supports_full_instance": True,
        "script_backup_full": (
            "set -e\n"
            "mkdir -p \"${PLATFORM_BACKUP_DIR}\"\n"
            "mysqldump --no-defaults -h \"${PLATFORM_DB_HOST}\" -P \"${PLATFORM_DB_PORT}\" \\\n"
            "  -u \"${PLATFORM_DB_USER}\" -p\"${PLATFORM_DB_PASSWORD}\" \\\n"
            "  --single-transaction --routines --triggers --events \\\n"
            "  \"${PLATFORM_DB_NAME}\" | gzip > \"${PLATFORM_BACKUP_DIR}/${PLATFORM_DB_NAME}.sql.gz\"\n"
        ),
        "script_restore": (
            "set -e\n"
            "gunzip -c \"${PLATFORM_BACKUP_FILE}\" | \\\n"
            "  mysql --no-defaults -h \"${PLATFORM_DB_HOST}\" -P \"${PLATFORM_DB_PORT}\" \\\n"
            "    -u \"${PLATFORM_DB_USER}\" -p\"${PLATFORM_DB_PASSWORD}\" \\\n"
            "    \"${PLATFORM_RESTORE_DB}\"\n"
        ),
        "script_test_conn": (
            "set -e\n"
            "mysql --no-defaults -h \"${PLATFORM_DB_HOST}\" -P \"${PLATFORM_DB_PORT}\" \\\n"
            "  -u \"${PLATFORM_DB_USER}\" -p\"${PLATFORM_DB_PASSWORD}\" -e \"SELECT 1\"\n"
        ),
        "script_list_dbs": (
            "mysql --no-defaults -h \"${PLATFORM_DB_HOST}\" -P \"${PLATFORM_DB_PORT}\" \\\n"
            "  -u \"${PLATFORM_DB_USER}\" -p\"${PLATFORM_DB_PASSWORD}\" -N -e \"SHOW DATABASES\" \\\n"
            "  | grep -v -E '^(information_schema|performance_schema|mysql|sys)$'\n"
        ),
    },
}


# --------------- 工具函数 ---------------
def _list_combined() -> list:
    """合并内置引擎与自定义适配器（前端表格用）。内置标记 source=builtin。"""
    out = []
    for t, cls in ENGINE_REGISTRY.items():
        # 跳过已被 db_adapters 动态注入的（会在下方以 adapter 形态出现）
        if t not in db_adapters.BUILTIN_TYPES:
            continue
        out.append({
            "id": -hash(t) & 0x7FFFFFFF,
            "db_type": t,
            "display_name": getattr(cls, "display_name", t),
            "category": "builtin",
            "icon": "bi-hdd-stack",
            "description": "",
            "enabled": 1,
            "backup_modes": ["logical", "physical"],
            "supports_incremental": True,
            "supports_full_instance": True,
            "supports_sync": True,
            "adapter_tier": getattr(cls, "adapter_tier", "peripheral_api"),
            "builtin": 1,
            "source": "builtin",
            "client_tools": list(getattr(cls, "required_clients", []) or []),
        })
    for a in db_adapters.list_adapters(include_disabled=True):
        a["source"] = "adapter"
        out.append(a)
    out.sort(key=lambda x: (not x.get("builtin"), x.get("category") or "",
                              x.get("db_type") or ""))
    return out


def _row_full(a: dict) -> dict:
    """详情：含全部字段（含脚本模板与参数）。"""
    return a


# --------------- 路由 ---------------
@api_bp.route("/db-adapters", methods=["GET"])
@login_required
def list_():
    return jsonify({"adapters": _list_combined(),
                    "builtin_types": db_adapters.BUILTIN_TYPES})


@api_bp.route("/db-adapters/templates", methods=["GET"])
@login_required
def templates():
    out = []
    for k, v in _TEMPLATES.items():
        out.append({"key": k, **v})
    return jsonify({"templates": out})


@api_bp.route("/db-adapters", methods=["POST"])
@login_required
def create():
    data = request.get_json(force=True, silent=True) or {}
    created_by = (data.pop("created_by", "") or "")
    try:
        new_id = db_adapters.create(data, created_by=created_by)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"创建失败: {e}"}), 500
    db_adapters.reload_one(data["db_type"])
    return jsonify({"success": True, "id": new_id,
                    "db_type": data["db_type"]})


@api_bp.route("/db-adapters/<int:aid>", methods=["GET"])
@login_required
def detail(aid: int):
    a = db_adapters.get_by_id(aid)
    if not a:
        return jsonify({"error": "适配器不存在"}), 404
    a["using_tasks"] = db_adapters.count_tasks_using(a["db_type"])
    return jsonify(a)


@api_bp.route("/db-adapters/<int:aid>", methods=["PUT"])
@login_required
def update(aid: int):
    data = request.get_json(force=True, silent=True) or {}
    try:
        ok = db_adapters.update(aid, data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    if not ok:
        return jsonify({"success": False, "error": "适配器不存在"}), 404
    a = db_adapters.get_by_id(aid)
    if a:
        db_adapters.reload_one(a["db_type"])
    return jsonify({"success": True})


@api_bp.route("/db-adapters/<int:aid>/toggle", methods=["POST"])
@login_required
def toggle(aid: int):
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", True))
    a = db_adapters.get_by_id(aid)
    if not a:
        return jsonify({"success": False, "error": "适配器不存在"}), 404
    db_adapters.set_enabled(aid, enabled)
    db_adapters.reload_one(a["db_type"])
    return jsonify({"success": True, "enabled": 1 if enabled else 0,
                    "db_type": a["db_type"]})


@api_bp.route("/db-adapters/<int:aid>", methods=["DELETE"])
@login_required
def delete(aid: int):
    ok, msg = db_adapters.delete(aid)
    if not ok:
        return jsonify({"success": False, "error": msg or "删除失败"}), 400
    a = db_adapters.get_by_id(aid)  # 删后取不到；从 db_adapters.delete 已返回成功
    # 同步清理引擎注册（按 db_type 删除时拿不到 db_type，所以 reload_all 更安全）
    try:
        db_adapters.reload_all()
    except Exception:
        pass
    return jsonify({"success": True})


@api_bp.route("/db-adapters/<int:aid>/test", methods=["POST"])
@login_required
def test(aid: int):
    """在指定 SSH 主机上执行 script_test_conn，返回 ok/rc/stdout/stderr。"""
    data = request.get_json(force=True, silent=True) or {}
    a = db_adapters.get_by_id(aid)
    if not a:
        return jsonify({"success": False, "error": "适配器不存在"}), 404
    ssh_host = None
    host_id = data.get("host_id")
    if host_id:
        try:
            from core.models import get_host
            h = get_host(int(host_id))
            if h:
                ssh_host = h
        except Exception:
            pass
    task = data.get("task") or {}
    res = db_adapters.test_connection(a, ssh_host=ssh_host, task=task)
    return jsonify({"success": True, **res})


@api_bp.route("/db-adapters/preview", methods=["POST"])
@login_required
def preview():
    """脚本模板渲染预览（不落库）。"""
    data = request.get_json(force=True, silent=True) or {}
    tmpl = data.get("script") or ""
    params = data.get("params") or {}
    try:
        rendered = db_adapters.render_template(tmpl, params)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "rendered": rendered,
                    "warnings": _validate_template(tmpl)})


def _validate_template(tmpl: str) -> list:
    """极简静态校验：未闭合的 {{ / 不存在的占位。"""
    import re
    warns = []
    opens = tmpl.count("{{")
    closes = tmpl.count("}}")
    if opens != closes:
        warns.append(f"占位符未闭合：{{ 出现 {opens} 次，}} 出现 {closes} 次")
    keys = set(re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", tmpl))
    known = {
        "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
        "BACKUP_TYPE", "BACKUP_DIR", "BACKUP_FILE", "RESTORE_DB",
        "TASK_ID", "TASK_NAME", "TOOL_BIN",
    }
    unknown = [k for k in keys if not k.startswith("PARAM_") and k not in known]
    if unknown:
        warns.append("非内置占位（仍可执行，但需在 params 中预置 PLATFORM_<KEY>）："
                     + ", ".join(sorted(set(unknown))))
    return warns