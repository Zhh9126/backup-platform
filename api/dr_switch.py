# -*- coding: utf-8 -*-
"""容灾一键切换 API（编排层）。

路由（共享 api_bp）：
- GET  /api/dr/overview                       容灾总览（链路健康 + 最近切换 + 演练）
- POST /api/disaster-links/<id>/switch/precheck   切换预检（不执行）
- POST /api/disaster-links/<id>/switch            发起切换（execute / dry_run）
- GET  /api/dr/switch-runs                    切换作业列表（可按链路过滤）
- GET  /api/dr/switch-runs/<run_id>           作业详情（逐步明细 / RTO）
"""
from flask import jsonify, request

import core.models as models
from auth import login_required
from core import dr_orchestrator as orch
from . import api_bp


def _operator() -> str:
    try:
        from auth import current_user
        u = current_user()
        if isinstance(u, dict):
            return u.get("username") or ""
        return getattr(u, "username", "") or ""
    except Exception:
        return ""


@api_bp.route("/dr/overview", methods=["GET"])
@login_required
def dr_overview():
    # ?light=1：大屏高频轮询用，跳过逐链路实时探活（避免 5 秒一刷被 SSH 探测拖住）
    light = str(request.args.get("light") or "").lower() in ("1", "true", "yes", "on")
    return jsonify(orch.overview(light=light))


@api_bp.route("/disaster-links/<int:link_id>/switch/precheck", methods=["POST"])
@login_required
def dr_switch_precheck(link_id: int):
    body = request.get_json(silent=True) or {}
    res = orch.precheck(link_id,
                        direction=str(body.get("direction") or "failover"),
                        force=bool(body.get("force")),
                        scope=str(body.get("scope") or "full"))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/disaster-links/<int:link_id>/switch", methods=["POST"])
@login_required
def dr_switch_start(link_id: int):
    """一键切换：body {direction, mode, scope, force, confirm}。

    scope: full=业务+DNS（默认）/ business=仅业务主备切换 / dns_only=仅 DNS 牵引。
    ``confirm=true`` 为二次确认闸门（防误点）；未确认一律拒绝，
    避免「点一下就动了生产主库」。
    """
    body = request.get_json(silent=True) or {}
    if not (body.get("confirm") in (True, 1, "1", "true", "yes", "on")):
        return jsonify({"ok": False,
                        "error": "请勾选二次确认后再执行切换（确认已知业务中断风险）",
                        "need_confirm": True}), 400
    direction = str(body.get("direction") or "failover").lower()
    if direction not in ("failover", "failback"):
        return jsonify({"ok": False, "error": "direction 只能是 failover / failback"}), 400
    mode = "dry_run" if str(body.get("mode") or "execute").lower() == "dry_run" else "execute"
    res = orch.start_switch(link_id, direction=direction, mode=mode,
                            force=bool(body.get("force")),
                            operator=_operator(),
                            scope=str(body.get("scope") or "full"))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/dr/switch-context/<int:link_id>", methods=["GET"])
@login_required
def dr_switch_context(link_id: int):
    """切换页上下文：当前承载 / 站点地域 / DNS 配置摘要（不含密码）。"""
    link = models.get_disaster_link(int(link_id))
    if not link:
        return jsonify({"error": "链路不存在"}), 404
    ctx = {
        "id": link.get("id"), "name": link.get("name"),
        "primary_site": link.get("primary_site"),
        "dr_site": link.get("dr_site"),
        "primary_region": orch._parse_region(link.get("primary_region")),
        "dr_region": orch._parse_region(link.get("dr_region")),
        "has_failover_config": bool((link.get("failover_config") or "").strip()),
        "dns": None, "active_site": "unknown", "health": None,
    }
    from core import dr_failover
    try:
        snap = dr_failover.health_snapshot(int(link_id))
        ctx["health"] = snap
        ctx["active_site"] = orch._active_site(
            snap, (snap.get("primary") or {}).get("host") or "",
            (snap.get("standby") or {}).get("host") or "")
    except Exception as e:
        ctx["health_error"] = str(e)[:200]
    try:
        cfg = dr_failover._load_config(dict(link))
        dns_cfg = cfg.get("dns") or {}
        if dns_cfg.get("domain"):
            ctx["dns"] = {
                "domain": dns_cfg.get("domain"),
                "provider": dns_cfg.get("provider") or "dnsmasq",
                "conf_path": dns_cfg.get("conf_path") or "",
                "container": dns_cfg.get("container") or "",
            }
    except Exception:
        pass
    return jsonify(ctx)


@api_bp.route("/dr/switch-runs", methods=["GET"])
@login_required
def dr_switch_runs():
    link_id = request.args.get("link_id")
    limit = request.args.get("limit") or 50
    try:
        lid = int(link_id) if link_id else None
    except (TypeError, ValueError):
        lid = None
    return jsonify({"items": orch.list_runs(lid, limit=int(limit))})


@api_bp.route("/dr/switch-runs/<int:run_id>", methods=["GET"])
@login_required
def dr_switch_run_detail(run_id: int):
    run = orch.get_run(run_id)
    if not run:
        return jsonify({"error": "作业不存在"}), 404
    return jsonify(run)


@api_bp.route("/dr/links", methods=["GET"])
@login_required
def dr_links_brief():
    """切换页用的链路清单（含是否已配置切换参数）。"""
    out = []
    for lk in models.list_disaster_links() or []:
        out.append({
            "id": lk.get("id"), "name": lk.get("name"),
            "status": lk.get("status"),
            "primary_site": lk.get("primary_site"), "dr_site": lk.get("dr_site"),
            "source_kind": lk.get("source_kind"),
            "has_failover_config": bool((lk.get("failover_config") or "").strip()),
        })
    return jsonify({"links": out})
