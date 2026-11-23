# -*- coding: utf-8 -*-
"""容灾一键切换 API（编排层）。

路由（共享 api_bp）：
- GET  /api/dr/overview                       容灾总览（链路健康 + 最近切换 + 演练）
- POST /api/disaster-links/<id>/switch/precheck   切换预检（不执行）
- POST /api/disaster-links/<id>/switch            发起切换（execute / dry_run）
- GET  /api/dr/switch-runs                    切换作业列表（可按链路过滤）
- GET  /api/dr/switch-runs/<run_id>           作业详情（逐步明细 / RTO）
- GET  /api/dr/switch-runs/stats              RTO 统计（均值/极值/SLO 达标率/趋势）
- GET  /api/dr/switch-runs/export             切换记录导出（csv / xlsx）
- POST /api/dr/switch-runs/<run_id>/cancel    中止进行中的切换作业
- GET  /api/dr/switch-runs/<run_id>/compensation      失败/中止的补偿建议（卡点+可执行动作）
- POST /api/dr/switch-runs/<run_id>/compensation/runbook
- GET  /api/dr/timeline                       容灾统一事件时间线（切换/演练/巡检/告警/目标缺口）
- GET  /api/dr/compliance                     容灾合规概览（演练合规率/切换成功率/平均 RTO）
"""
from flask import jsonify, request

import core.models as models
from auth import login_required
from core import dr_orchestrator as orch
from . import api_bp


def _operator() -> str:
    # auth 模块只提供 current_operator()（session['user'] 已标准化为 dict）。
    # 此前误调不存在的 current_user，ImportError 被吞导致切换审计人恒为空。
    try:
        from auth import current_operator
        return current_operator()
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
    # 结构化预检结果通过 HTTP 200 + ok/ready 字段表达：BKP.api 对 4xx
    # 直接抛错，若这里返回 400，前端拿不到 risks/快照明细（P1-14）。
    return jsonify(res), 200


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
    # blocked 是结构化业务结果（含 precheck 快照/风险），不是传输错误；
    # 返回 200 让前端能渲染明细而非只剩一条 toast（P1-14）。
    return jsonify(res), 200


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


@api_bp.route("/dr/switch-runs/<int:run_id>/cancel", methods=["POST"])
@login_required
def dr_switch_run_cancel(run_id: int):
    """中止进行中的切换作业（步骤边界生效，不强杀正在执行的步骤）。"""
    res = orch.cancel_run(int(run_id))
    return jsonify(res), (200 if res.get("ok") else 400)


@api_bp.route("/dr/switch-runs/<int:run_id>/compensation", methods=["GET"])
@login_required
def dr_switch_run_compensation(run_id: int):
    """失败/中止切换的补偿建议：卡点判定 + 可执行的补偿动作。

    只做建议（不代替人工决策）；可执行动作给出 scope/direction 参数，
    由前端二次确认后复用既有切换端点执行。
    """
    from core import dr_compensation
    run = orch.get_run(int(run_id))
    if not run:
        return jsonify({"error": "作业不存在"}), 404
    link = models.get_disaster_link(int(run.get("link_id") or 0)) or {}
    return jsonify(dr_compensation.analyze(run, link))


@api_bp.route("/dr/switch-runs/<int:run_id>/compensation/runbook", methods=["POST"])
@login_required
def dr_switch_run_compensation_runbook(run_id: int):
    """把失败切换的补偿建议编排成一份**可审批的恢复剧本**并落库。

    只生成剧本，**不自动执行**：执行仍需在「恢复剧本」页由人工发起并逐步审批。
    剧本步骤里带审批闸的是会改变线上流量入口的动作（仅 DNS 牵引），
    其余为人工处置确认点。
    """
    from core import dr_compensation, runbook
    run = orch.get_run(int(run_id))
    if not run:
        return jsonify({"error": "作业不存在"}), 404
    link = models.get_disaster_link(int(run.get("link_id") or 0)) or {}
    plan = dr_compensation.build_runbook(run, link)
    try:
        rb = runbook.create_runbook(
            {"name": plan["name"], "description": plan["description"],
             "steps": plan["steps"], "enabled": 1},
            operator=(request.get_json(silent=True) or {}).get("operator") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "runbook_id": rb.get("id"), "runbook": rb,
                    "plan": plan}), 201


@api_bp.route("/dr/switch-runs/stats", methods=["GET"])
@login_required
def dr_switch_run_stats():
    """切换作业统计：次数 / 成功率 / RTO 均值与极值 / SLO 达标率 / 趋势序列。

    参数：link_id（可选，按链路过滤）、days（默认 30）、slo_rto_sec（可选基准）。
    """
    link_id = request.args.get("link_id")
    days = request.args.get("days") or 30
    slo = request.args.get("slo_rto_sec")
    try:
        lid = int(link_id) if link_id else None
    except (TypeError, ValueError):
        lid = None
    try:
        slo_v = float(slo) if slo not in (None, "") else None
    except (TypeError, ValueError):
        slo_v = None
    try:
        days_v = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days_v = 30
    return jsonify(orch.stats(link_id=lid, days=days_v, slo_rto_sec=slo_v))


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


@api_bp.route("/dr/timeline", methods=["GET"])
@login_required
def dr_timeline():
    """容灾统一事件时间线：切换 / 演练 / 巡检 / 告警聚合为一条流。

    参数：days（默认 14）、kinds（逗号分隔过滤）、link_id、limit。
    """
    from core import dr_timeline as tl
    days = request.args.get("days") or 14
    kinds = request.args.get("kinds") or ""
    link_id = request.args.get("link_id")
    limit = request.args.get("limit") or 200
    try:
        lid = int(link_id) if link_id else None
    except (TypeError, ValueError):
        lid = None
    try:
        days_v = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days_v = 14
    try:
        limit_v = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit_v = 200
    return jsonify(tl.timeline(days=days_v, kinds=kinds, link_id=lid,
                               limit=limit_v))


@api_bp.route("/dr/compliance", methods=["GET"])
@login_required
def dr_compliance():
    """容灾合规概览：演练合规率 / 切换成功率 / 平均 RTO（口径见 core.dr_timeline）。"""
    from core import dr_timeline as tl
    days = request.args.get("days") or 30
    try:
        days_v = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days_v = 30
    return jsonify(tl.compliance(days=days_v))


@api_bp.route("/dr/switch-runs/export", methods=["GET"])
@login_required
def dr_switch_runs_export():
    """切换作业记录导出：csv（Excel 直接打开）/ xlsx（概要 + 明细两表）。"""
    import csv
    import io

    from flask import Response
    import core.db as db
    from core import dr_timeline as tl

    fmt = str(request.args.get("format") or "csv").lower()
    days = request.args.get("days") or 30
    try:
        days_v = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days_v = 30
    since = tl._since(days_v)
    runs = [r for r in orch.list_runs(limit=1000)
            if tl._in_window(tl._norm_ts(r.get("created_at") or ""), since)]
    fname = "dr_switch_runs_%s" % (db.now_iso()[:10])
    scope_name = {"full": "业务+DNS", "business": "仅业务", "dns_only": "仅DNS"}
    status_name = {"success": "成功", "failed": "失败", "running": "执行中",
                   "blocked": "被预检拦截", "cancelled": "已中止",
                   "dry_run": "试运行"}
    header = ["编号", "链路", "方向", "范围", "模式", "状态", "RTO(秒)",
              "操作人", "开始时间", "结束时间", "结论"]

    if fmt == "xlsx":
        try:
            from openpyxl import Workbook
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": "openpyxl 不可用: %s" % str(e)[:120]}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = "切换记录"
        ws.append(header)
        for r in runs:
            ws.append([
                r.get("id"), r.get("link_name") or "",
                "回切" if r.get("direction") == "failback" else "切换",
                scope_name.get(r.get("scope") or "full", "业务+DNS"),
                "试运行" if (r.get("mode") or "") == "dry_run" else "执行",
                status_name.get(r.get("status") or "", r.get("status") or ""),
                round(float(r.get("rto_sec") or 0), 1),
                r.get("operator") or "",
                (r.get("started_at") or "").replace("T", " "),
                (r.get("finished_at") or "").replace("T", " "),
                r.get("message") or "",
            ])
        ws2 = wb.create_sheet("步骤明细")
        ws2.append(["编号", "序号", "步骤", "结果", "耗时(ms)", "明细"])
        for r in runs:
            for i, s in enumerate(r.get("steps") or [], 1):
                ws2.append([r.get("id"), i, s.get("name"),
                            "成功" if s.get("ok") else "失败",
                            s.get("ms") or 0,
                            (s.get("detail") or {}).get("error")
                            or str(s.get("detail") or "")[:200]])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet",
            headers={"Content-Disposition":
                     "attachment; filename=%s.xlsx" % fname})

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in runs:
        w.writerow([
            r.get("id"), r.get("link_name") or "",
            "回切" if r.get("direction") == "failback" else "切换",
            scope_name.get(r.get("scope") or "full", "业务+DNS"),
            "试运行" if (r.get("mode") or "") == "dry_run" else "执行",
            status_name.get(r.get("status") or "", r.get("status") or ""),
            round(float(r.get("rto_sec") or 0), 1),
            r.get("operator") or "",
            (r.get("started_at") or "").replace("T", " "),
            (r.get("finished_at") or "").replace("T", " "),
            r.get("message") or "",
        ])
    return Response(
        "\ufeff" + out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=%s.csv" % fname})

