# -*- coding: utf-8 -*-
"""容灾巡检 API。

路由（共享 api_bp）：
- GET  /api/dr-inspections              巡检记录列表
- POST /api/dr-inspections/run          立即巡检（手动触发）
- GET  /api/dr-inspections/config       巡检配置（开关/cron）
- POST /api/dr-inspections/config       保存巡检配置（inspection.manage）
- GET  /api/dr-inspections/<id>         巡检详情（逐链路逐项）
- GET  /api/dr-inspections/<id>/export  导出报表（csv / xlsx）
- DELETE /api/dr-inspections/<id>       删除巡检记录
"""
import csv
import io
import json

from flask import Response, jsonify, request

from auth import login_required, permission_required
from core import dr_inspection
from . import api_bp


def _operator() -> str:
    try:
        from auth import current_operator
        return current_operator()
    except Exception:
        return ""


@api_bp.route("/dr-inspections", methods=["GET"])
@login_required
def dri_list():
    limit = request.args.get("limit", type=int, default=50)
    return jsonify({"ok": True,
                    "items": dr_inspection.list_inspections(limit=limit)})


@api_bp.route("/dr-inspections/run", methods=["POST"])
@login_required
def dri_run():
    try:
        insp = dr_inspection.run_inspection(triggered_by="manual:" + _operator())
        return jsonify({"ok": True, "item": insp})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@api_bp.route("/dr-inspections/config", methods=["GET"])
@login_required
def dri_config():
    return jsonify({"ok": True, **dr_inspection.get_config()})


@api_bp.route("/dr-inspections/config", methods=["POST"])
@permission_required("inspection.manage")
def dri_save_config():
    data = request.get_json(force=True, silent=True) or {}
    try:
        cfg = dr_inspection.save_config(
            enabled=bool(data.get("enabled", True)),
            cron=str(data.get("cron") or ""))
        return jsonify({"ok": True, **cfg})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/dr-inspections/<int:rid>", methods=["GET"])
@login_required
def dri_detail(rid: int):
    insp = dr_inspection.get_inspection(rid)
    return (jsonify({"ok": True, "item": insp}) if insp
            else (jsonify({"ok": False, "error": "巡检记录不存在"}), 404))


@api_bp.route("/dr-inspections/<int:rid>", methods=["DELETE"])
@permission_required("inspection.manage")
def dri_delete(rid: int):
    return (jsonify({"ok": True}) if dr_inspection.delete_inspection(rid)
            else (jsonify({"ok": False, "error": "巡检记录不存在"}), 404))


@api_bp.route("/dr-inspections/<int:rid>/export", methods=["GET"])
@login_required
def dri_export(rid: int):
    """巡检报表导出：csv（明细优先）/ xlsx（三工作表：概要/链路明细/DNS）。"""
    insp = dr_inspection.get_inspection(rid)
    if not insp:
        return jsonify({"ok": False, "error": "巡检记录不存在"}), 404
    fmt = str(request.args.get("format") or "csv").lower()
    fname = "dr_inspection_%d_%s" % (rid, (insp.get("created_at") or "")[:10])

    if fmt == "xlsx":
        try:
            from openpyxl import Workbook
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": "openpyxl 不可用: %s" % str(e)[:120]}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = "概要"
        ws.append(["容灾巡检报告 #%d" % rid])
        ws.append(["结论", insp.get("overall")])
        ws.append(["巡检时间", (insp.get("created_at") or "").replace("T", " ")])
        ws.append(["触发方式", insp.get("triggered_by")])
        ws.append(["链路总数", insp.get("links_total")])
        ws.append(["正常 / 警告 / 异常",
                   "%s / %s / %s" % (insp.get("ok_count"),
                                     insp.get("warn_count"), insp.get("fail_count"))])
        ws.append(["摘要", insp.get("summary")])

        ws2 = wb.create_sheet("链路明细")
        ws2.append(["链路", "总体", "检查项", "状态", "说明"])
        for lk in (insp.get("detail") or []):
            for c in (lk.get("checks") or []):
                ws2.append([lk.get("name"), lk.get("overall"),
                            c.get("item"), c.get("status"), c.get("message")])

        ws3 = wb.create_sheet("DNS巡检")
        dns = insp.get("dns_summary") or {}
        ws3.append(["服务正常", dns.get("ok")])
        ws3.append(["域名", "配置IP", "实际解析", "一致"])
        for r in (dns.get("records") or []):
            ws3.append([r.get("domain"), r.get("ip"), r.get("resolved"),
                        r.get("consistent")])
        for e in (dns.get("errors") or []):
            ws3.append([e])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet",
            headers={"Content-Disposition":
                     "attachment; filename=%s.xlsx" % fname})

    # CSV（UTF-8 BOM，Excel 直接打开）
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["容灾巡检报告 #%d" % rid])
    w.writerow(["结论", insp.get("overall"), "时间",
                (insp.get("created_at") or "").replace("T", " "),
                "触发", insp.get("triggered_by")])
    w.writerow(["摘要", insp.get("summary")])
    w.writerow([])
    w.writerow(["链路", "总体", "检查项", "状态", "说明"])
    for lk in (insp.get("detail") or []):
        for c in (lk.get("checks") or []):
            w.writerow([lk.get("name"), lk.get("overall"),
                        c.get("item"), c.get("status"), c.get("message")])
    w.writerow([])
    dns = insp.get("dns_summary") or {}
    w.writerow(["DNS 服务正常", dns.get("ok")])
    w.writerow(["域名", "配置IP", "实际解析", "一致"])
    for r in (dns.get("records") or []):
        w.writerow([r.get("domain"), r.get("ip"), r.get("resolved"),
                    r.get("consistent")])
    for e in (dns.get("errors") or []):
        w.writerow([e])
    return Response(
        "\ufeff" + out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=%s.csv" % fname})
