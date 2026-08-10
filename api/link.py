# -*- coding: utf-8 -*-
"""
容灾链路 HA API（DisasterLinkEngine）。

路由前缀: /api/disaster-links（通过共享 api_bp 注册）
- GET    /api/disaster-links                         列表（含数据源回显）
- GET    /api/disaster-links/sources                 可选数据源（同步任务 / 实时任务）
- POST   /api/disaster-links                         创建（支持按数据源自动带出站点）
- GET    /api/disaster-links/<id>                    详情
- PUT    /api/disaster-links/<id>                    更新（支持改绑数据源）
- DELETE /api/disaster-links/<id>                    删除
- POST   /api/disaster-links/<id>/select-route       手动智能选路
- POST   /api/disaster-links/<id>/fill-gap           日志间隙填补
- POST   /api/disaster-links/<id>/check-consistency  备端一致性校验

UX-20260801 模块 D：容灾链路不再要求运维手填主备站点，可直接引用已有的
"数据同步任务"或"实时保护任务"。引用关系用 ``source_kind`` + ``source_id``
持久化，而 ``primary_site`` / ``dr_site`` / ``route_policy`` 仍按**快照**落库，
保证源任务被删除或改配后，已建链路的历史语义不被篡改。
"""
import core.models as models
from auth import login_required
from core import disaster_link as dl_engine
from . import api_bp
from flask import request, jsonify

# 允许的数据源类型（与 models.DISASTER_LINK_SOURCE_KINDS 保持一致）
_SOURCE_KINDS = models.DISASTER_LINK_SOURCE_KINDS


def _fmt_endpoint(host, port, db_name) -> str:
    """把 host/port/db 拼成人类可读的站点标识，缺项自动省略。"""
    host = str(host or "").strip()
    if not host:
        return ""
    text = host
    try:
        if port not in (None, "", 0):
            text = f"{host}:{int(port)}"
    except (TypeError, ValueError):
        pass
    db_name = str(db_name or "").strip()
    if db_name:
        text = f"{text}/{db_name}"
    return text


def _sync_source_item(row: dict) -> dict:
    """把一条 sync_tasks 行转换为数据源选项。"""
    last_status = row.get("last_status") or row.get("status") or "never"
    return {
        "kind": "sync_task",
        "id": int(row.get("id") or 0),
        "name": row.get("name") or f"同步任务 #{row.get('id')}",
        "primary_site": _fmt_endpoint(row.get("src_host"), row.get("src_port"),
                                      row.get("src_db_name")),
        "dr_site": _fmt_endpoint(row.get("tgt_host"), row.get("tgt_port"),
                                 row.get("tgt_db_name")),
        "db_type": row.get("src_db_type") or "",
        "enabled": bool(row.get("enabled")),
        # status 为设计 §2 D 契约字段（前端状态徽章直接读取）；last_status 保留向后兼容
        "status": last_status,
        "last_status": last_status,
        "last_run_at": row.get("last_run_at") or "",
    }


def _rt_rpo_sec(task_id: int, state=None):
    """取实时健康运行态中的实际 RPO（秒）。

    Args:
        task_id: backup_tasks.id。
        state: 已预取的 ``rt_capture_state`` 行；传 ``None`` 时按需回查，
            传 ``{}`` 表示"确认无运行态"（避免批量场景下的 N+1 查询）。

    Returns:
        int 秒数；无运行态或无有效取值时返回 ``None``。
    """
    row = state
    if row is None:
        try:
            row = models.get_rt_state(int(task_id))
        except Exception:
            row = None
    if not row:
        return None
    raw = row.get("rpo_actual_sec")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _rt_source_item(row: dict, state=None) -> dict:
    """把一条实时保护任务（backup_tasks.rt_enabled=1）转换为数据源选项。

    实时任务的"备端"是备份落地侧：配置了远程存储时用远程主机，
    否则标记为本地日志仓库。

    Args:
        row: backup_tasks 行（rt_enabled=1）。
        state: 预取的实时运行态行，语义见 :func:`_rt_rpo_sec`。
    """
    backend = str(row.get("storage_backend") or "local").lower()
    if backend != "local" and row.get("remote_host"):
        dr_site = _fmt_endpoint(row.get("remote_host"), row.get("remote_port"),
                                row.get("remote_path"))
    else:
        dr_site = "本地日志仓库"
    task_id = int(row.get("id") or 0)
    last_status = row.get("last_status") or "never"
    return {
        "kind": "rt_task",
        "id": task_id,
        "name": row.get("name") or f"实时任务 #{row.get('id')}",
        "primary_site": _fmt_endpoint(row.get("host"), row.get("port"),
                                      row.get("db_name")),
        "dr_site": dr_site,
        "db_type": row.get("db_type") or "",
        "enabled": bool(row.get("enabled")),
        # status / rpo_sec 为设计 §2 D 契约字段；last_status 保留向后兼容
        "status": last_status,
        "last_status": last_status,
        "rpo_sec": _rt_rpo_sec(task_id, state),
        "last_run_at": row.get("last_run_at") or "",
        "rt_mode": row.get("rt_mode") or "",
        "rt_enabled": bool(row.get("rt_enabled")),
    }


def _load_source(kind: str, source_id: int) -> dict:
    """按 kind + id 读取单个数据源；不存在返回 ``{}``。"""
    try:
        sid = int(source_id)
    except (TypeError, ValueError):
        return {}
    if sid <= 0:
        return {}
    try:
        if kind == "sync_task":
            row = models.get_sync_task(sid)
            return _sync_source_item(row) if row else {}
        if kind == "rt_task":
            row = models.get_task(sid)
            if not row:
                return {}
            return _rt_source_item(row)
    except Exception:
        return {}
    return {}


def _list_sources() -> dict:
    """汇总全部可引用数据源，供前端下拉选择。异常时降级为空列表。"""
    sync_items, rt_items = [], []
    try:
        sync_items = [_sync_source_item(r) for r in models.list_sync_tasks()]
    except Exception:
        sync_items = []
    # 预取全部实时运行态，避免逐任务回查（无运行态的任务显式传 {}）
    try:
        states = {int(s.get("task_id") or 0): s for s in models.list_rt_states()}
    except Exception:
        states = {}
    try:
        rt_items = [_rt_source_item(r, states.get(int(r.get("id") or 0), {}))
                    for r in models.list_rt_tasks(only_enabled=False)]
    except Exception:
        rt_items = []
    return {"sync_task": sync_items, "rt_task": rt_items}


def _normalize_source(data: dict, require_exists: bool = True):
    """校验并归一化请求体中的数据源字段，同时回填站点快照。

    Args:
        data: 请求体（会被就地补齐 primary_site / dr_site）。
        require_exists: 为 True 时，引用的数据源必须存在，否则返回错误。

    Returns:
        ``(error_message, source_item)``：校验通过时 error_message 为 None。
    """
    kind = str(data.get("source_kind") or "manual").strip() or "manual"
    if kind not in _SOURCE_KINDS:
        return (f"数据源类型非法（应为 {'/'.join(_SOURCE_KINDS)}）", {})
    data["source_kind"] = kind

    if kind == "manual":
        data["source_id"] = None
        return (None, {})

    raw_id = data.get("source_id")
    if raw_id in (None, "", 0, "0"):
        return ("引用数据源时必须提供 source_id", {})
    try:
        data["source_id"] = int(raw_id)
    except (TypeError, ValueError):
        return ("source_id 必须为整数", {})

    item = _load_source(kind, data["source_id"])
    if not item and require_exists:
        label = "同步任务" if kind == "sync_task" else "实时保护任务"
        return (f"引用的{label} #{data['source_id']} 不存在", {})

    # 站点快照回填：用户未显式填写时才由数据源带出，避免覆盖手工修正值
    if item:
        if not str(data.get("primary_site") or "").strip():
            data["primary_site"] = item.get("primary_site") or ""
        if not str(data.get("dr_site") or "").strip():
            data["dr_site"] = item.get("dr_site") or ""
    return (None, item)


def _decorate_link(link: dict, cache: dict) -> dict:
    """为链路补充数据源展示字段（源名称 / 源最近状态 / 源是否失效）。"""
    link = dict(link or {})
    kind = link.get("source_kind") or "manual"
    sid = link.get("source_id")
    if kind == "manual" or not sid:
        link["source_name"] = ""
        link["source_last_status"] = ""
        link["source_missing"] = False
        return link
    key = (kind, int(sid))
    if key not in cache:
        cache[key] = _load_source(kind, sid)
    item = cache[key]
    link["source_name"] = item.get("name") or ""
    link["source_last_status"] = item.get("last_status") or ""
    link["source_missing"] = not bool(item)
    return link


@api_bp.route("/disaster-links", methods=["GET"])
@login_required
def api_list_links():
    """链路列表；每条附带数据源名称与最近状态，供前端直接展示。"""
    cache: dict = {}
    links = [_decorate_link(l, cache) for l in models.list_disaster_links()]
    return jsonify({"links": links})


@api_bp.route("/disaster-links/sources", methods=["GET"])
@login_required
def api_list_link_sources():
    """可引用的数据源清单（同步任务 / 实时保护任务），供新建链路时选择。

    响应同时提供两种视图：``items`` 为设计 §2 D 约定的扁平数组（前端
    ``loadLinkSources()`` 直接消费），``sources`` 为按 kind 分组的旧结构
    （保留以兼容既有调用方）。
    """
    sources = _list_sources()
    items = list(sources.get("sync_task") or []) + list(sources.get("rt_task") or [])
    return jsonify({
        "ok": True,
        "items": items,
        "kinds": list(_SOURCE_KINDS),
        "sources": sources,
        "total": len(items),
    })


@api_bp.route("/disaster-links", methods=["POST"])
@login_required
def api_create_link():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "链路名称为必填"}), 400
    if data.get("status") and data["status"] not in dl_engine.DisasterLinkEngine.STATUS:
        return jsonify({"error": "状态非法（应为 active/standby/filling/broken）"}), 400
    err, source = _normalize_source(data, require_exists=True)
    if err:
        return jsonify({"error": err}), 400
    link_id = models.create_disaster_link(data)
    return jsonify({"id": link_id, "ok": True,
                    "source_kind": data.get("source_kind"),
                    "source_id": data.get("source_id"),
                    "source_name": source.get("name", "")}), 201


@api_bp.route("/disaster-links/<int:link_id>", methods=["GET"])
@login_required
def api_get_link(link_id):
    link = models.get_disaster_link(link_id)
    if not link:
        return jsonify({"error": "链路不存在"}), 404
    return jsonify(_decorate_link(link, {}))


@api_bp.route("/disaster-links/<int:link_id>", methods=["PUT"])
@login_required
def api_update_link(link_id):
    current = models.get_disaster_link(link_id)
    if not current:
        return jsonify({"error": "链路不存在"}), 404
    data = request.get_json(silent=True) or {}
    if "status" in data and data["status"] not in dl_engine.DisasterLinkEngine.STATUS:
        return jsonify({"error": "状态非法（应为 active/standby/filling/broken）"}), 400
    # 仅当请求体涉及数据源时才做改绑校验，避免影响仅改名/改状态的旧调用
    if "source_kind" in data or "source_id" in data:
        data.setdefault("source_kind", current.get("source_kind") or "manual")
        if "source_id" not in data:
            data["source_id"] = current.get("source_id")
        err, _ = _normalize_source(data, require_exists=True)
        if err:
            return jsonify({"error": err}), 400
    models.update_disaster_link(link_id, data)
    updated = models.get_disaster_link(link_id) or {}
    return jsonify({"ok": True, "link": _decorate_link(updated, {})})


@api_bp.route("/disaster-links/<int:link_id>", methods=["DELETE"])
@login_required
def api_delete_link(link_id):
    if not models.get_disaster_link(link_id):
        return jsonify({"error": "链路不存在"}), 404
    models.delete_disaster_link(link_id)
    return jsonify({"ok": True})


@api_bp.route("/disaster-links/<int:link_id>/select-route", methods=["POST"])
@login_required
def api_select_route(link_id):
    if not models.get_disaster_link(link_id):
        return jsonify({"error": "链路不存在"}), 404
    result = dl_engine.DisasterLinkEngine().select_route(link_id)
    return jsonify(result)


@api_bp.route("/disaster-links/<int:link_id>/fill-gap", methods=["POST"])
@login_required
def api_fill_gap(link_id):
    if not models.get_disaster_link(link_id):
        return jsonify({"error": "链路不存在"}), 404
    result = dl_engine.DisasterLinkEngine().fill_log_gap(link_id)
    return jsonify(result)


@api_bp.route("/disaster-links/<int:link_id>/check-consistency", methods=["POST"])
@login_required
def api_check_consistency(link_id):
    if not models.get_disaster_link(link_id):
        return jsonify({"error": "链路不存在"}), 404
    result = dl_engine.DisasterLinkEngine().run_consistency_check(link_id)
    return jsonify(result)
