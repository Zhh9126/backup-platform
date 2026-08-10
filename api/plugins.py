# -*- coding: utf-8 -*-
"""备份依赖插件管理 API：一键安装/卸载/查询外部备份客户端。

- GET  /api/plugins            列出全部插件 + 运行时状态
- GET  /api/plugins/categories  分类聚合
- GET  /api/plugins/recommend   根据已配置任务数据库类型 + 当前 OS 推荐待装插件
- GET  /api/plugins/<id>        详情（含完整 manifest 与当前 OS 匹配策略）
- POST /api/plugins/<id>/install   一键安装（异步）
- POST /api/plugins/<id>/uninstall 卸载
- GET  /api/plugins/<id>/state   安装进度（前端轮询）
- GET  /api/plugins/<id>/log     安装日志
- POST /api/plugins/batch-install 一键安装多插件（用于"一键安装本机所需"）
"""
from flask import jsonify, request

from auth import login_required
from core import plugin_catalog, plugin_installer
from . import api_bp


@api_bp.route("/plugins", methods=["GET"])
@login_required
def list_plugins():
    category = request.args.get("category") or None
    rows = plugin_catalog.list_plugins(filter_category=category)
    # 列表不返回完整 manifest，避免 payload 过大
    for r in rows:
        r.pop("manifest", None)
    return jsonify({
        "ok": True,
        "plugins": rows,
        "current_os": plugin_catalog.detect_os(),
        "package_manager": plugin_catalog.detect_package_manager(),
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


@api_bp.route("/plugins/recommend", methods=["GET"])
@login_required
def recommend_plugins():
    """根据已配置的备份任务数据库类型 + 当前 OS，推荐待装插件。

    Query:
        db_types: 逗号分隔的数据库类型列表（可选；不传则按本机推荐全部）
    """
    raw = request.args.get("db_types") or ""
    db_types = [t.strip() for t in raw.split(",") if t.strip()]
    rows = plugin_catalog.recommend_for_host(db_types)
    for r in rows:
        r.pop("manifest", None)
    return jsonify({
        "ok": True,
        "current_os": plugin_catalog.detect_os(),
        "package_manager": plugin_catalog.detect_package_manager(),
        "count": len(rows),
        "plugins": rows,
    })


@api_bp.route("/plugins/batch-install", methods=["POST"])
@login_required
def batch_install_plugins():
    """一键安装多个插件（异步派发，不阻塞）。

    Body:
        {"ids": ["percona-xtrabackup-80", "redis-tools", ...]}
        或 {"db_types": ["mysql", "redis"]} —— 自动取推荐列表
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not ids and body.get("db_types"):
        # 按 db_types 拉推荐列表
        rows = plugin_catalog.recommend_for_host(body["db_types"])
        ids = [r["id"] for r in rows]
    ids = [i for i in ids if i]
    if not ids:
        return jsonify({"ok": False, "error": "未指定要安装的插件 id / db_types"}), 400
    queued, failed = [], []
    for pid in ids:
        res = plugin_installer.install(pid)
        if res.get("ok"):
            queued.append(pid)
        else:
            failed.append({"id": pid, "error": res.get("error", "未知错误")})
    return jsonify({
        "ok": True,
        "queued": queued,
        "failed": failed,
    })


@api_bp.route("/plugins/<pid>", methods=["GET"])
@login_required
def get_plugin(pid):
    p = plugin_catalog.get_plugin(pid)
    if not p:
        return jsonify({"ok": False, "error": f"插件不存在: {pid}"}), 404
    return jsonify({"ok": True, "plugin": p})


@api_bp.route("/plugins/<pid>/install", methods=["POST"])
@login_required
def install_plugin(pid):
    res = plugin_installer.install(pid)
    status = 200 if res.get("ok") else 400
    return jsonify(res), status


@api_bp.route("/plugins/<pid>/uninstall", methods=["POST"])
@login_required
def uninstall_plugin(pid):
    res = plugin_installer.uninstall(pid)
    return jsonify(res)


@api_bp.route("/plugins/<pid>/state", methods=["GET"])
@login_required
def plugin_state(pid):
    state = plugin_installer.get_state(pid)
    if not state:
        # 没有历史状态文件时返回 installed 标记
        p = plugin_catalog.get_plugin(pid)
        if p:
            return jsonify({
                "ok": True,
                "state": {
                    "id": pid,
                    "status": "success" if p["installed"] else "idle",
                    "message": "已就绪" if p["installed"] else "尚未安装",
                    "progress": 100 if p["installed"] else 0,
                }
            })
        return jsonify({"ok": False, "error": "未知插件"}), 404
    return jsonify({"ok": True, "state": state})


@api_bp.route("/plugins/<pid>/log", methods=["GET"])
@login_required
def plugin_log(pid):
    log = plugin_installer._log_path(pid)
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