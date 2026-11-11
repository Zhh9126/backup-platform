# -*- coding: utf-8 -*-
"""数据价值挖掘 API。

能力分层：
1) 资产盘点 /api/datamining/inventory       —— 盘点 → 分级 → 价值评估 → 治理建议
2) 敏感发现 /api/datamining/sensors|scan|scans —— 基于内容的敏感数据发现与国标分级
3) 合规概览 /api/datamining/compliance      —— PIPL / GB/T 35273 / 等保检查项
4) 脱敏导出 /api/datamining/export|exports  —— 让冷备份变成可用数据资产（既有能力）
"""
import os
import re
import json

from flask import request, jsonify, send_file, session

from auth import login_required
from core import models, data_mining as mining_engine
from core import asset_inventory, sensitive_scan
from . import api_bp, safe_download_path


def _operator() -> str:
    """当前操作人（审计用）。"""
    try:
        return str(session.get("username") or session.get("user") or "system")
    except Exception:  # noqa: BLE001
        return "system"


def _pct(n: float) -> int:
    return int(round(float(n or 0)))


def _status(score: int, good: int, warn: int) -> str:
    return "pass" if score >= good else ("warn" if score >= warn else "fail")


def _sensitive_map():
    """task_id → 扫描得到的 max_level（供资产盘点纳入合规敏感度维度）。"""
    m = models.latest_scan_map() or {}
    return {tid: v.get("max_level", 0) for tid, v in m.items()}


# ============================ 一、资产盘点 ============================

@api_bp.route("/datamining/inventory", methods=["GET"])
@login_required
def datamining_inventory():
    """资产盘点 + 价值评估 + 治理建议（一次出全量）。

    query: days=30
    """
    try:
        days = int(request.args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(7, min(365, days))
    try:
        data = asset_inventory.build_inventory(days=days, sensitive=_sensitive_map())
        return jsonify(data)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"资产盘点失败: {e}"}), 500


@api_bp.route("/datamining/assets", methods=["GET"])
@login_required
def datamining_assets():
    """仅返回资产清单（含价值分与冷热标签），供下拉/表格复用。"""
    days = max(7, min(365, int(request.args.get("days") or 30)))
    assets = asset_inventory.build_assets(days)
    for a in assets:
        a["sensitive_level"] = _sensitive_map().get(a["task_id"], 0)
    return jsonify(asset_inventory.score_all(assets))


# ============================ 二、敏感数据发现 ============================

@api_bp.route("/datamining/sensors", methods=["GET"])
@login_required
def list_sensors():
    """识别能力清单（类别 / 国标分级 / 合规标签 / 建议脱敏动作）。"""
    return jsonify(sensitive_scan.list_sensors())


@api_bp.route("/datamining/scan", methods=["POST"])
@login_required
def scan_sensitive():
    """对备份产物做敏感数据发现与分级，结果落库（只存脱敏样例）。

    body: 三选一
      {"record_id": int}            扫单条备份记录产物
      {"task_id": int, "limit": 3}  扫该任务最近 N 条成功记录
      {"text": "..."}               在线试扫（不落库）
    """
    data = request.get_json(force=True, silent=True) or {}
    operator = _operator()

    # 在线试扫：不落库，方便分析师先贴一段样本验证识别效果
    if data.get("text"):
        res = sensitive_scan.scan_text(str(data["text"])[:2 * 1024 * 1024])
        res["stored"] = False
        return jsonify(res)

    record_id = data.get("record_id")
    task_id = data.get("task_id")
    limit = max(1, min(10, int(data.get("limit") or 3)))

    targets = []
    if record_id:
        rec = models.get_record(int(record_id))
        if not rec:
            return jsonify({"error": "备份记录不存在"}), 404
        if rec.get("backup_path"):
            targets.append((int(rec.get("task_id") or 0) or None, int(record_id),
                            rec["backup_path"]))
    elif task_id:
        rows = models.list_records(task_id=int(task_id), limit=limit)
        for r in (rows or []):
            if r.get("status") == "success" and r.get("backup_path"):
                targets.append((int(task_id), int(r["id"]), r["backup_path"]))
    else:
        return jsonify({"error": "record_id / task_id / text 至少填一个"}), 400

    if not targets:
        # 细化原因：任务名 + 最近记录状态（无成功记录 / 产物文件已清理不在本机）
        hint = "未找到可扫描的备份产物"
        if task_id:
            t = models.get_task(int(task_id)) or {}
            hint += "（任务「%s」" % (t.get("name") or ("任务 " + str(task_id)))
            recent = models.list_records(task_id=int(task_id), limit=5) or []
            ok_rows = [r for r in recent if r.get("status") == "success"]
            if not ok_rows:
                hint += "：最近没有成功的备份记录）"
            else:
                hint += "：最近成功记录的产物文件已不在本机（可能已被保留策略清理，"
                hint += "或备份在远端主机未拉回））"
        else:
            hint += "（该备份记录的产物可能不在本机）"
        return jsonify({"error": hint}), 404

    results = []
    for tid, rid, path in targets:
        r = sensitive_scan.scan_file(path)
        r["record_id"] = rid
        r["task_id"] = tid
        try:
            r["scan_id"] = models.create_scan_result({
                "task_id": tid, "record_id": rid, "path": path,
                "size_bytes": r.get("size_bytes") or 0,
                "scanned_chars": r.get("scanned_chars") or 0,
                "max_level": r.get("max_level") or 0,
                "risk_score": r.get("risk_score") or 0,
                "hit_count": sum(f["hits"] for f in r.get("findings", [])),
                "findings": r.get("findings", []),
                "scannable": 1 if r.get("scannable") else 0,
                "reason": r.get("reason") or "",
                "scanned_by": operator,
            })
        except Exception as e:  # noqa: BLE001
            r["store_error"] = str(e)
        results.append(r)

    agg = sensitive_scan.scan_files([p for _, _, p in targets])
    return jsonify({
        "stored": True,
        "scanned_files": agg["scanned_files"],
        "total_files": len(targets),
        "findings": agg["findings"],
        "max_level": agg["max_level"],
        "total_hits": agg["total_hits"],
        "files": results,
        "suggested_mask_rules": sensitive_scan.to_mask_rules(agg["findings"]),
    })


@api_bp.route("/datamining/scans", methods=["GET"])
@login_required
def list_scans():
    """扫描历史（含每条产物的风险等级，仅脱敏样例）。"""
    task_id = request.args.get("task_id")
    return jsonify(models.list_scan_results(
        task_id=int(task_id) if task_id else None, limit=100))


@api_bp.route("/datamining/scans/<int:scan_id>", methods=["DELETE"])
@login_required
def delete_scan(scan_id):
    if not models.get_scan_result(scan_id):
        return jsonify({"error": "扫描记录不存在"}), 404
    models.delete_scan_result(scan_id)
    return jsonify({"ok": True})


@api_bp.route("/datamining/classify", methods=["POST"])
@login_required
def classify_columns():
    """按字段名做轻量分级（无内容取样时的兜底推断）。"""
    data = request.get_json(force=True, silent=True) or {}
    cols = data.get("columns") or []
    return jsonify({
        "columns": sensitive_scan.classify_columns(cols),
        "suggested_mask_rules": sensitive_scan.to_mask_rules(
            [c for c in sensitive_scan.classify_columns(cols) if c.get("type")]),
    })


# ============================ 三、合规概览 ============================

def _retention_overdue(assets: list) -> list:
    """找出已超过各自保留天数仍未被清理的成功记录。"""
    out = []
    for a in assets:
        rd = int(a.get("retention_days") or 0)
        if rd <= 0:
            continue
        rows = models.db.query(
            "SELECT id, started_at, size_bytes FROM backup_records "
            "WHERE task_id=? AND status='success' "
            "AND julianday('now') - julianday(COALESCE(started_at,'')) > ? "
            "ORDER BY id DESC LIMIT 100", (a["task_id"], rd))
        if rows:
            out.append({
                "task_id": a["task_id"], "name": a["name"],
                "retention_days": rd,
                "overdue_count": len(rows),
                "bytes": sum(int(r["size_bytes"] or 0) for r in rows),
            })
    out.sort(key=lambda x: -x["bytes"])
    return out


@api_bp.route("/datamining/compliance", methods=["GET"])
@login_required
def compliance_overview():
    """合规概览：PIPL / GB/T 35273 / PCI-DSS / 等保 2.0 检查项 + 敏感资产清单。"""
    days = max(7, min(365, int(request.args.get("days") or 30)))
    smap = models.latest_scan_map()
    inv = asset_inventory.build_inventory(days=days,
                                          sensitive={t: v["max_level"] for t, v in smap.items()})
    assets = inv["assets"]
    total = max(1, len(assets))

    sensitive_assets = [a for a in assets if (a.get("sensitive_level") or 0) >= 3]
    l4_assets = [a for a in assets if (a.get("sensitive_level") or 0) >= 4]
    scanned = [a for a in assets if a["task_id"] in smap]

    # C1 敏感资产识别覆盖率
    c1 = _pct(100.0 * len(scanned) / total) if total else 0
    # C2 L4 重要数据是否有脱敏导出留痕
    exports = models.list_anonymized_exports(limit=500)
    exported_tasks = set()
    loose_export = []          # 导出规则里把敏感列设为 none 的
    for e in exports:
        rec = models.get_record(int(e.get("source_record_id") or 0) or 0)
        if rec:
            exported_tasks.add(int(rec.get("task_id") or 0) or 0)
        rules = e.get("mask_rules") or {}
        if isinstance(rules, str):
            try:
                rules = json.loads(rules)
            except (json.JSONDecodeError, TypeError):
                rules = {}
        risky = [c for c, r in (rules or {}).items()
                 if r in ("none", "") and re.search(
                     r"phone|mobile|email|mail|id_card|idcard|bank_card|card_no|"
                     r"password|token|secret|name|address|ip", str(c), re.I)]
        if risky:
            loose_export.append({"export_id": e.get("id"), "columns": risky[:8]})
    l4_exported = [a for a in l4_assets if a["task_id"] in exported_tasks]
    # C3 备份留存是否超期（与是否扫描无关，永远可算）
    overdue = _retention_overdue(assets)
    c3 = 100 if not overdue else max(0, 100 - _pct(100.0 * len(overdue) / total))

    # 关键设计：**未被识别覆盖的资产不能算作合规**。
    # 识别覆盖率不足时，依赖"敏感资产清单"的检查项一律记为 unknown（不计入总分），
    # 否则「没扫描 = 没发现敏感数据 = 100 分」会给出虚假的安全感。
    covered = c1 >= 80
    unknown = {"score": None, "status": "unknown", "counted": False}

    def _sens_check(cid, name, std, hit_list, advice, scope=None):
        ref = len(scope) if scope is not None else len(sensitive_assets)
        if ref <= 0:
            return {"id": cid, "name": name, "standard": std,
                    "detail": "尚未发现满足范围的资产（识别覆盖率不足时不予评分）",
                    "advice": advice, **unknown}
        num = _pct(100.0 * len(hit_list) / max(1, ref))
        good, warn = (100, 80) if cid == "C5" else (80, 50)
        st = _status(num, good, warn)
        if not covered:
            st = "unknown"
        return {"id": cid, "name": name, "standard": std,
                "score": num, "status": st, "counted": covered,
                "detail": f"{len(hit_list)}/{ref} 个{'敏感' if scope is None else '重要数据'}资产满足要求",
                "advice": advice}

    drilled = [a for a in sensitive_assets if (a.get("drill_count") or 0) > 0]
    protected = [a for a in sensitive_assets if a.get("scheduled") and a.get("enabled")]
    multi = [a for a in sensitive_assets if (a.get("storage_backend") or "local") != "local"]

    checks = [
        {"id": "C1", "name": "敏感数据识别覆盖", "standard": "GB/T 43697-2024",
         "score": c1, "status": _status(c1, 80, 50), "counted": True,
         "detail": f"{len(scanned)}/{len(assets)} 个资产已完成内容采样识别",
         "advice": "对未扫描资产执行「敏感发现」，重要数据方能进入分级管控"},
        _sens_check("C2", "重要数据脱敏外发留痕", "PIPL / GB/T 35273", l4_exported,
                    "含重要数据的资产对外提供前必须先脱敏导出，并留存导出审计记录",
                    scope=l4_assets),
        {"id": "C3", "name": "备份留存周期合规", "standard": "数据安全法 / 等保 2.0",
         "score": c3, "status": _status(c3, 80, 50), "counted": True,
         "detail": f"{len(overdue)} 个任务存在超保留期未清理的备份",
         "advice": "执行保留策略清理超期数据，避免「超期留存」被判定为违规处理"},
        _sens_check("C4", "敏感资产恢复演练", "GB/T 35273 / 等保 2.0", drilled,
                    "对敏感数据资产定期做恢复演练并留存报告，作为举证材料"),
        _sens_check("C5", "敏感资产定时保护", "等保 2.0", protected,
                    "敏感资产必须纳入定时保护策略，避免依赖人工触发"),
        _sens_check("C6", "敏感数据异地副本", "等保 2.0 / 灾难恢复", multi,
                    "为含敏感数据的资产配置多级存储，形成异地副本"),
    ]
    if loose_export:
        checks.append({
            "id": "C7", "name": "导出规则未覆盖敏感列", "standard": "PIPL",
            "score": 0, "status": "fail", "counted": True,
            "detail": f"{len(loose_export)} 条脱敏导出将敏感列设为「不脱敏」",
            "advice": "复核这些导出文件，敏感列应调整为打码/哈希/仿真替换",
        })

    counted = [c["score"] for c in checks if c.get("counted") and c.get("score") is not None]
    score = int(round(sum(counted) / max(1, len(counted))))
    level_dist = {"L4": len(l4_assets), "L3": len([a for a in assets if a.get("sensitive_level") == 3]),
                  "L2": len([a for a in assets if a.get("sensitive_level") == 2]),
                  "L1": len([a for a in assets if a.get("sensitive_level") <= 1])}

    return jsonify({
        "score": score,
        "grade": "良好" if score >= 85 else ("基本合规" if score >= 60 else "待整改"),
        "checks": checks,
        "level_dist": level_dist,
        "sensitive_assets": [{
            "task_id": a["task_id"], "name": a["name"], "biz_system": a.get("biz_system"),
            "db_type": a["db_type"], "instance": a["instance"],
            "sensitive_level": a["sensitive_level"],
            "risk_score": smap.get(a["task_id"], {}).get("risk_score", 0),
            "last_scan_at": smap.get(a["task_id"], {}).get("scanned_at", ""),
            "protected": bool(a.get("scheduled") and a.get("enabled")),
            "drill_count": a.get("drill_count", 0),
            "total_bytes": a["total_bytes"],
            "scheduled": a.get("scheduled"), "enabled": a.get("enabled"),
        } for a in sorted(sensitive_assets, key=lambda x: -x["sensitive_level"])],
        "overdue": overdue[:10],
        "loose_exports": loose_export[:10],
        "recent_exports": exports[:10],
        "levels": sensitive_scan.LEVELS,
    })


# ============================ 四、脱敏导出（既有能力） ============================

@api_bp.route("/datamining/exports", methods=["GET"])
@login_required
def list_exports():
    """列出脱敏导出历史。"""
    return jsonify(mining_engine.DataMiner().list_exports())


@api_bp.route("/datamining/export", methods=["POST"])
@login_required
def export_anonymized():
    """触发一次脱敏导出。body: {source_record_id, columns?, mask_rules?}。"""
    data = request.get_json(force=True, silent=True) or {}
    source_record_id = data.get("source_record_id")
    if not source_record_id:
        return jsonify({"error": "source_record_id 必填"}), 400
    try:
        result = mining_engine.DataMiner().export_anonymized(
            int(source_record_id),
            columns=data.get("columns"),
            mask_rules=data.get("mask_rules"),
            row_count=int(data.get("row_count") or 50),
        )
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"导出失败: {e}"}), 500


@api_bp.route("/datamining/exports/<int:export_id>/download", methods=["GET"])
@login_required
def download_export(export_id):
    """下载生成的脱敏文件（本地下载）。"""
    exp = models.get_anonymized_export(export_id)
    if not exp:
        return jsonify({"error": "导出记录不存在"}), 404
    # 安全整改：仅允许下载备份根目录内的导出文件
    fp = safe_download_path(exp.get("file_path") or "")
    if fp is None:
        return jsonify({"error": "文件不存在或路径不合法"}), 404
    return send_file(
        fp, as_attachment=True,
        download_name=os.path.basename(fp),
        mimetype="text/csv; charset=utf-8",
    )


@api_bp.route("/datamining/exports/<int:export_id>", methods=["DELETE"])
@login_required
def delete_export(export_id):
    """删除一条导出记录（同时删除物理文件）。"""
    if not models.get_anonymized_export(export_id):
        return jsonify({"error": "导出记录不存在"}), 404
    ok = mining_engine.DataMiner().delete_export(export_id)
    return jsonify({"ok": ok})


@api_bp.route("/datamining/rule-templates", methods=["GET"])
@login_required
def list_rule_templates():
    """返回脱敏规则模板（最小/标准/严格），前端一键套用。"""
    return jsonify(mining_engine.DataMiner().list_rule_templates())


@api_bp.route("/datamining/db-schemas", methods=["GET"])
@login_required
def list_db_schemas():
    """返回每个 db_type 对应的典型表/列集合（用于「按来源记录推荐列」）。"""
    return jsonify(mining_engine.DataMiner().list_db_schemas())


@api_bp.route("/datamining/records/<int:source_record_id>/suggest", methods=["GET"])
@login_required
def suggest_columns(source_record_id):
    """根据来源备份记录推荐可选列（解决"列固定"问题）。"""
    return jsonify(mining_engine.DataMiner().suggest_columns_for_record(source_record_id))


@api_bp.route("/datamining/preview-rules", methods=["POST"])
@login_required
def preview_mask_rules():
    """预览每列最终生效的脱敏规则 + 含义。
    body: {columns: [...], mask_rules?: {col: rule}}
    """
    data = request.get_json(force=True, silent=True) or {}
    columns = data.get("columns") or []
    mask_rules = data.get("mask_rules") or None
    return jsonify(mining_engine.DataMiner().preview_mask_rules(columns, mask_rules))


@api_bp.route("/datamining/mask-preview", methods=["POST"])
@login_required
def mask_value_preview():
    """按规则预览单列样例的脱敏效果（新增：可视化展示脱敏前后对比）。

    body: {samples: ["13812341234", ...], rule: "mask"}
    """
    data = request.get_json(force=True, silent=True) or {}
    rule = data.get("rule") or "mask"
    samples = data.get("samples") or []
    # 安全：返回值中「脱敏前」也统一按 mask 处理，接口不回传原始敏感值
    return jsonify({
        "rule": rule,
        "results": [{"before": sensitive_scan.mask_value(s, "mask"),
                     "after": sensitive_scan.mask_value(s, rule)}
                    for s in samples[:10]],
    })
