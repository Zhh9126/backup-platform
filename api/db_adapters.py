# -*- coding: utf-8 -*-
"""可插拔数据库类型（Database Adapter）的 CRUD/测试/模板 API。"""
import json

from flask import jsonify, request

from auth import login_required
from core import db_adapters
from core import db_adapter_templates as _tpl
from core.engines import ENGINE_REGISTRY, ENGINE_DISPLAY
from . import api_bp


# --------------- 模板目录（core/db_adapter_templates.py） ---------------
# 早期只内置 2 个样例模板（generic_shell / mysql_dump），新增一种库都得自己写脚本。
# 现改为「数据库类型模板目录」：按协议族 / 场景分组，覆盖主流商业/开源 + 国产信创，
# 支持「单条套用」与「批量导入」（POST /api/db-adapters/import）。


def _template_map() -> dict:
    return {t["key"]: t for t in _tpl.all_templates()}


# --------------- 工具函数 ---------------
def _task_counts() -> dict:
    """一次聚合统计各 db_type 的任务引用数（避免逐行查询 N 次）。"""
    try:
        rows = db_adapters.db.query(
            "SELECT db_type, COUNT(*) AS c FROM backup_tasks GROUP BY db_type")
        out = {}
        for r in rows or []:
            if isinstance(r, dict):
                out[r.get("db_type")] = r.get("c", 0)
        return out
    except Exception:
        return {}


def _list_combined() -> list:
    """合并内置引擎与自定义适配器（前端表格用）。内置标记 source=builtin。

    2026-11-13 修复：原来用 `if t not in BUILTIN_TYPES: continue` 过滤，而
    BUILTIN_TYPES 是手写清单且漏了 synology/qdrant/chroma/highgo/uxdb/
    tidb/oceanbase，导致这 7 个已注册引擎在页面上**整行消失**（页面只显示
    14 项）。现改为遍历 ENGINE_REGISTRY，只跳过动态适配器（它们在下方单独列），
    并统一从 engine_meta_map() 取**真实能力**（原来硬编码 logical+physical 全能力，
    与 redis/file/mongodb 等真实能力不符）。
    """
    out = []
    try:
        from core.engines import engine_meta_map
        meta = engine_meta_map()
    except Exception:
        meta = {}
    adapters = db_adapters.list_adapters(include_disabled=True)
    adapter_types = {a["db_type"] for a in adapters}
    counts = _task_counts()
    for t, cls in ENGINE_REGISTRY.items():
        if t in adapter_types:
            continue   # 动态注册的适配器在下方以 adapter 形态出现，避免重复行
        m = meta.get(t) or {}
        out.append({
            "id": -hash(t) & 0x7FFFFFFF,
            "db_type": t,
            "display_name": m.get("display_name") or getattr(cls, "display_name", t),
            "category": m.get("category") or "builtin",
            "icon": m.get("icon") or "bi-hdd-stack",
            "description": m.get("description") or "",
            "enabled": 1,
            "default_port": m.get("default_port"),
            "backup_modes": m.get("backup_modes") or ["logical"],
            "supports_incremental": 1 if m.get("supports_incremental") else 0,
            "supports_full_instance": 1 if m.get("supports_full_instance") else 0,
            "supports_sync": 1 if m.get("supports_sync") else 0,
            "adapter_tier": getattr(cls, "adapter_tier", "peripheral_api"),
            "builtin": 1,
            "source": "builtin",
            "client_tools": list(getattr(cls, "required_clients", []) or []),
            "using_tasks": counts.get(t, 0),
        })
    for a in adapters:
        a["source"] = "adapter"
        a["using_tasks"] = counts.get(a["db_type"], 0)
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
                    "builtin_types": db_adapters.builtin_types()})


@api_bp.route("/db-adapters/templates", methods=["GET"])
@login_required
def templates():
    """数据库类型模板目录。

    - templates：平铺列表（兼容旧前端/脚本，每项带 key）
    - groups：按协议族/场景分组（前端「从模板复制」弹窗用）
    - stats：总数与按验证级别统计（protocol=与已验证引擎同族工具；template=骨架，需现场核对）
    """
    return jsonify({"templates": _tpl.all_templates(),
                    "groups": _tpl.grouped(),
                    "stats": _tpl.stats()})


def _payload_from_template(t: dict) -> dict:
    """模板 → 适配器创建参数。

    兼容两种形态：``all_templates()/by_key()`` 返回的已展开结构，以及
    ``CATALOG`` 里脚本仍嵌在 ``scripts`` 下的原始结构（测试直接遍历 CATALOG 时）。
    """
    if t.get("scripts"):
        t = {**{k: v for k, v in t.items() if k != "scripts"}, **t["scripts"]}
    db_type = (t.get("db_type_hint") or t["key"]).strip().lower()
    return {
        "db_type": db_type,
        "display_name": (t.get("display_name") or t.get("label") or db_type)[:64],
        "category": t.get("category") or "custom",
        "default_port": t.get("default_port"),
        "icon": t.get("icon") or "bi-box-seam",
        "description": t.get("description") or "",
        "enabled": 1,
        "backup_modes": t.get("backup_modes") or ["logical"],
        "supports_incremental": 1 if t.get("supports_incremental") else 0,
        "supports_full_instance": 1 if t.get("supports_full_instance") else 0,
        "supports_sync": 1 if t.get("supports_sync") else 0,
        "client_tools": t.get("client_tools") or [],
        "script_backup_full": t.get("script_backup_full") or "",
        "script_backup_incremental": t.get("script_backup_incremental") or "",
        "script_backup_full_instance": t.get("script_backup_full_instance") or "",
        "script_restore": t.get("script_restore") or "",
        "script_verify": t.get("script_verify") or "",
        "script_list_dbs": t.get("script_list_dbs") or "",
        "script_test_conn": t.get("script_test_conn") or "",
        "params": t.get("params_hint") or {},
        "timeout_sec": 7200,
    }


@api_bp.route("/db-adapters/import", methods=["POST"])
@login_required
def import_templates():
    """批量导入模板目录里的库型（勾选即建）。

    body: {"keys": ["postgresql_family 之外的模板 key", ...]}
    返回 created / skipped（已存在或内置）/ failed 三段，逐条如实反馈。
    """
    data = request.get_json(force=True, silent=True) or {}
    keys = data.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return jsonify({"success": False, "error": "请提供 keys（模板 key 列表）"}), 400
    created_by = data.get("created_by") or ""
    builtin = set(db_adapters.builtin_types())
    existing = {a["db_type"] for a in db_adapters.list_adapters(include_disabled=True)}
    created, skipped, failed = [], [], []
    for k in keys:
        t = _tpl.by_key(str(k))
        if not t:
            failed.append({"key": k, "error": "模板不存在"})
            continue
        payload = _payload_from_template(t)
        db_type = payload["db_type"]
        if db_type in builtin:
            skipped.append({"key": k, "db_type": db_type, "reason": "已是内置类型"})
            continue
        if db_type in existing:
            skipped.append({"key": k, "db_type": db_type, "reason": "适配器已存在"})
            continue
        try:
            new_id = db_adapters.create(payload, created_by=created_by)
            db_adapters.reload_one(db_type)
            existing.add(db_type)
            created.append({"key": k, "db_type": db_type, "id": new_id})
        except Exception as e:
            failed.append({"key": k, "db_type": db_type, "error": str(e)})
    return jsonify({"success": True, "created": created, "skipped": skipped,
                    "failed": failed})


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