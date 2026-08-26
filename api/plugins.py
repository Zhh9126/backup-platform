# -*- coding: utf-8 -*-
"""备份依赖插件管理 API：一键安装/卸载/查询外部备份客户端。

支持主机维度：通过 host_id 参数指定目标 SSH 主机，安装/卸载/查询均按
「主机 × 插件」二维维度操作。host_id 为空时兼容旧的平台本机维度。

- GET  /api/plugins            列出全部插件 + 运行时状态（?host_id=）
- GET  /api/plugins/categories  分类聚合
- GET  /api/plugins/recommend   根据已配置任务数据库类型 + 当前 OS 推荐待装插件
- GET  /api/plugins/hosts       返回 SSH 主机列表 + 本机选项
- GET  /api/plugins/<id>        详情（含完整 manifest 与当前 OS 匹配策略）（?host_id=）
- POST /api/plugins/<id>/install   一键安装（异步，body: {"host_id": 11}）
- POST /api/plugins/<id>/uninstall 卸载（body: {"host_id": 11}）
- GET  /api/plugins/<id>/state   安装进度（前端轮询）（?host_id=）
- GET  /api/plugins/<id>/log     安装日志（?host_id=）
- POST /api/plugins/batch-install 一键安装多插件（body: {"host_id": 11, "ids":[...]} 或 {"host_id": 11, "db_types": ["mysql"]}）
"""
from flask import jsonify, request

from auth import login_required
from core import plugin_catalog, plugin_installer, plugin_runtime
from . import api_bp


def _parse_host_id() -> int | None:
    """从 query string 或 JSON body 解析 host_id，返回 int 或 None。"""
    # 优先 query string
    raw = request.args.get("host_id")
    if raw is None and request.is_json:
        try:
            body = request.get_json(silent=True) or {}
            raw = body.get("host_id")
        except Exception:
            pass
    if raw in (None, "", 0, "0"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _host_key_from_id(host_id: int | None) -> str | None:
    """把 host_id 解析为 host_key（用于状态文件查询）。"""
    if host_id is None:
        return None
    try:
        from core import ssh_hosts
        h = ssh_hosts.get_host(host_id, include_secret=False)
        return h.get("host_key") if h else None
    except Exception:
        return None


@api_bp.route("/plugins", methods=["GET"])
@login_required
def list_plugins():
    """列出全部插件 + 运行时状态。

    Query:
        category: 按分类过滤
        host_id:  指定目标主机（本机不传或传 0）
    """
    category = request.args.get("category") or None
    host_id = _parse_host_id()
    rows = plugin_catalog.list_plugins(filter_category=category, host_id=host_id)
    # 列表不返回完整 manifest，避免 payload 过大
    for r in rows:
        r.pop("manifest", None)
    return jsonify({
        "ok": True,
        "plugins": rows,
        "current_os": plugin_catalog.detect_os() if not host_id else "",
        "package_manager": plugin_catalog.detect_package_manager() if not host_id else "",
        "host_id": host_id,
    })


@api_bp.route("/plugins/categories", methods=["GET"])
@login_required
def list_plugin_categories():
    rows = plugin_catalog.categories()
    return jsonify({
        "ok": True,
        "total": sum(r["count"] for r in rows),
        "categories": rows,
    })


@api_bp.route("/plugins/hosts", methods=["GET"])
@login_required
def list_plugin_hosts():
    """返回可用的目标主机列表（SSH 主机 + 本机选项）。

    前端用此接口渲染「目标主机」下拉框。
    """
    from core import ssh_hosts
    hosts = []
    # 本机选项
    hosts.append({
        "id": 0,
        "host_key": "local",
        "name": "本机（备份平台所在服务器）",
        "hostname": "",
        "os_type": "local",
    })
    # SSH 主机
    for h in ssh_hosts.list_hosts(include_secret=False):
        hosts.append({
            "id": h.get("id"),
            "host_key": h.get("host_key", ""),
            "name": h.get("name") or h.get("host_key", ""),
            "hostname": h.get("hostname", ""),
            "os_type": h.get("os_type", "linux"),
        })
    return jsonify({"ok": True, "hosts": hosts})


@api_bp.route("/plugins/recommend", methods=["GET"])
@login_required
def recommend_plugins():
    """根据已配置的备份任务数据库类型 + 当前 OS，推荐待装插件。

    Query:
        db_types: 逗号分隔的数据库类型列表（可选；不传则按本机推荐全部）
        host_id:  指定目标主机
    """
    raw = request.args.get("db_types") or ""
    db_types = [t.strip() for t in raw.split(",") if t.strip()]
    host_id = _parse_host_id()
    rows = plugin_catalog.recommend_for_host(db_types, host_id=host_id)
    for r in rows:
        r.pop("manifest", None)
    return jsonify({
        "ok": True,
        "current_os": plugin_catalog.detect_os() if not host_id else "",
        "package_manager": plugin_catalog.detect_package_manager() if not host_id else "",
        "count": len(rows),
        "plugins": rows,
        "host_id": host_id,
    })


@api_bp.route("/plugins/batch-install", methods=["POST"])
@login_required
def batch_install_plugins():
    """一键安装多个插件（异步派发，不阻塞）。

    Body:
        {"ids": ["percona-xtrabackup-80", "redis-tools", ...]}
        或 {"db_types": ["mysql", "redis"]} —— 自动取推荐列表
        可附加 "host_id": 11 —— 指定目标主机
    """
    body = request.get_json(silent=True) or {}
    host_id = body.get("host_id")
    if host_id in (None, "", 0, "0"):
        host_id = None
    else:
        try:
            host_id = int(host_id)
        except (TypeError, ValueError):
            host_id = None

    ids = body.get("ids") or []
    if not ids and body.get("db_types"):
        # 按 db_types 拉推荐列表
        rows = plugin_catalog.recommend_for_host(body["db_types"], host_id=host_id)
        ids = [r["id"] for r in rows]
    ids = [i for i in ids if i]
    if not ids:
        return jsonify({"ok": False, "error": "未指定要安装的插件 id / db_types"}), 400
    queued, failed = [], []
    for pid in ids:
        res = plugin_installer.install(pid, host_id=host_id)
        if res.get("ok"):
            queued.append(pid)
        else:
            failed.append({"id": pid, "error": res.get("message", "未知错误")})
    return jsonify({
        "ok": True,
        "queued": queued,
        "failed": failed,
        "host_id": host_id,
    })


@api_bp.route("/plugins/<pid>", methods=["GET"])
@login_required
def get_plugin(pid):
    """获取单个插件详情。

    Query:
        host_id: 指定目标主机
    """
    host_id = _parse_host_id()
    p = plugin_catalog.get_plugin(pid, host_id=host_id)
    if not p:
        return jsonify({"ok": False, "error": f"插件不存在: {pid}"}), 404
    return jsonify({"ok": True, "plugin": p})


@api_bp.route("/plugins/<pid>/install", methods=["POST"])
@login_required
def install_plugin(pid):
    """异步安装插件。

    Body:
        {"host_id": 11}  —— 不传则本机安装
    """
    host_id = _parse_host_id()
    res = plugin_installer.install(pid, host_id=host_id)
    status = 200 if res.get("ok") else 400
    return jsonify(res), status


@api_bp.route("/plugins/<pid>/uninstall", methods=["POST"])
@login_required
def uninstall_plugin(pid):
    """卸载插件。

    Body:
        {"host_id": 11}  —— 不传则本机卸载
    """
    host_id = _parse_host_id()
    res = plugin_installer.uninstall(pid, host_id=host_id)
    return jsonify(res)


@api_bp.route("/plugins/<pid>/state", methods=["GET"])
@login_required
def plugin_state(pid):
    """查询安装状态（前端轮询）。

    Query:
        host_id: 指定目标主机
    """
    host_id = _parse_host_id()
    host_key = _host_key_from_id(host_id) if host_id else None
    state = plugin_installer.get_state(pid, host_key=host_key)
    if not state:
        # 没有历史状态文件时返回 installed 标记
        p = plugin_catalog.get_plugin(pid, host_id=host_id)
        if p:
            return jsonify({
                "ok": True,
                "state": {
                    "id": pid,
                    "status": "success" if p.get("installed") else "idle",
                    "message": "已就绪" if p.get("installed") else "尚未安装",
                    "progress": 100 if p.get("installed") else 0,
                }
            })
        return jsonify({"ok": False, "error": "未知插件"}), 404
    return jsonify({"ok": True, "state": state})


@api_bp.route("/plugins/<pid>/log", methods=["GET"])
@login_required
def plugin_log(pid):
    """查询安装日志。

    Query:
        host_id: 指定目标主机
    """
    host_id = _parse_host_id()
    host_key = _host_key_from_id(host_id) if host_id else None
    log = plugin_installer._log_path(pid, host_key=host_key)
    if not log.exists():
        return jsonify({"ok": True, "log": ""})
    # 默认返回最近 200 行
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = "".join(lines[-200:])
    except Exception as e:
        tail = f"读取日志失败: {e}"
    return jsonify({"ok": True, "log": tail})
