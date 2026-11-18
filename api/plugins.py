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
import core.models as models
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
        "name": "本机（AIDBM 所在服务器）",
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


@api_bp.route("/plugins/version-matrix", methods=["GET"])
@login_required
def api_plugin_version_matrix():
    """插件版本矩阵：期望版本 vs 各主机已装版本 + 升级建议。"""
    from core import plugin_version_matrix as pvm
    host_id = request.args.get("host_id", type=int)
    return jsonify(pvm.matrix(host_id))


@api_bp.route("/plugins/bundle/build", methods=["POST"])
@login_required
def api_plugin_bundle_build():
    """把一组插件打成单文件离线包（无网现场一次导入）。"""
    from core import plugin_bundle
    body = request.get_json(silent=True) or {}
    res = plugin_bundle.build(body.get("plugins") or [],
                              out_path=str(body.get("out_path") or ""),
                              note=str(body.get("note") or ""))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/plugins/bundle/inspect", methods=["GET"])
@login_required
def api_plugin_bundle_inspect():
    """只读查看离线包内容（不安装）。"""
    from core import plugin_bundle
    return jsonify(plugin_bundle.inspect(request.args.get("path") or ""))


@api_bp.route("/plugins/bundle/import", methods=["POST"])
@login_required
def api_plugin_bundle_import():
    """导入单文件离线包：校验 sha256 → 落离线包目录 → 逐个安装。"""
    from core import plugin_bundle
    body = request.get_json(silent=True) or {}
    path = str(body.get("path") or "")
    if not path:
        return jsonify({"ok": False, "message": "请提供 path（bundle 文件路径）"}), 400
    res = plugin_bundle.import_bundle(
        path, host_id=body.get("host_id"),
        install=bool(body.get("install", True)))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/plugins/coverage", methods=["GET"])
@login_required
def api_plugin_coverage():
    """备份类型 × 插件覆盖矩阵（DB / 文件 / 对象存储 / 虚拟机 / 通用）。"""
    from core import plugin_policy
    return jsonify(plugin_policy.coverage())


@api_bp.route("/plugins/policy/<int:task_id>", methods=["GET"])
@login_required
def api_plugin_policy_get(task_id: int):
    """查询任务的插件使用策略与能力协商结果。"""
    from core import plugin_policy
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    res = plugin_policy.resolve(task)
    res["task_id"] = task_id
    res["db_type"] = task.get("db_type")
    return jsonify(res)


@api_bp.route("/plugins/policy/<int:task_id>", methods=["POST"])
@login_required
def api_plugin_policy_set(task_id: int):
    """设置任务级插件模式：auto（默认）/ off（走零安装）/ require（必须用插件）。"""
    from core import plugin_policy
    body = request.get_json(silent=True) or {}
    res = plugin_policy.set_task_mode(task_id, body.get("mode"))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/plugins/bundle", methods=["POST"])
@login_required
def api_plugin_bundle():
    """对外「一个包」：按一组任务推导所需插件并一键安装。

    body: {"task_ids": [..], "dry_run": false}
    返回：需要的插件清单 + 逐个安装结果（失败如实返回，不吞错）。
    """
    from core import plugin_policy
    from core import plugin_installer
    body = request.get_json(silent=True) or {}
    ids = body.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"success": False, "message": "请提供 task_ids"}), 400
    plan = plugin_policy.bundle_for_tasks([int(x) for x in ids])
    if body.get("dry_run"):
        return jsonify({"success": True, "dry_run": True, "plan": plan})
    results = []
    for p in plan["plugins"]:
        try:
            r = plugin_installer.install(p["id"]) if hasattr(plugin_installer, "install") \
                else {"ok": False, "message": "安装器缺少 install 接口"}
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "message": str(e)[:300]}
        results.append({"plugin": p["id"], "name": p.get("name"),
                        "ok": bool(r.get("ok")), "message": (r.get("message") or "")[:300]})
    ok_n = sum(1 for x in results if x["ok"])
    return jsonify({"success": True, "plan": plan, "results": results,
                    "installed": ok_n, "failed": len(results) - ok_n})


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
