# -*- coding: utf-8 -*-
"""备份记录与下载 API。"""
import os
import json
from collections import Counter
from flask import request, jsonify, send_file, Response

from auth import login_required
from core import models, db, reports
from . import api_bp


# 超长 / 超频 默认阈值（秒 / 分钟），可由 query string 覆盖
DEFAULT_LONG_DURATION_SEC = 1800      # 30 分钟
DEFAULT_FREQ_WINDOW_MIN   = 5         # 5 分钟窗口
DEFAULT_FREQ_THRESHOLD    = 3         # 窗口内 >= 3 次视为超频

# 备份质量阈值配置 key
QUALITY_THRESHOLDS_KEY = "backup_quality_thresholds"

# 默认按"速度"判定超长时的预期备份吞吐：500 GB/h（≈ 138 MB/s）
DEFAULT_EXPECTED_SPEED_GB_PER_HOUR = 500.0
# 浮动容忍度：20%（允许突发 / 抖动 / 压缩等）
DEFAULT_SPEED_TOLERANCE_PCT = 20.0


def _default_thresholds() -> dict:
    return {
        "long_minutes": 30,                # 固定超长阈值（分钟）
        "expected_speed_gb_per_hour": DEFAULT_EXPECTED_SPEED_GB_PER_HOUR,
        "speed_tolerance_pct": DEFAULT_SPEED_TOLERANCE_PCT,
        "freq_window_minutes": 5,          # 超频窗口（分钟）
        "freq_threshold": 3,               # 窗口内次数阈值
        "long_rule": "speed",              # long_rule: fixed | speed | both
    }


def get_quality_thresholds() -> dict:
    """读取备份质量阈值（缺省返回默认值）。"""
    raw = db.get_system_config(QUALITY_THRESHOLDS_KEY)
    base = _default_thresholds()
    if raw:
        try:
            user = json.loads(raw)
            if isinstance(user, dict):
                # 容错：用户可能只填了部分字段
                for k, v in user.items():
                    if k in base:
                        base[k] = v
        except Exception:
            pass
    return base


def save_quality_thresholds(cfg: dict) -> dict:
    """保存备份质量阈值，返回合并后的配置。"""
    base = _default_thresholds()
    # 类型校验
    try:
        long_min = int(cfg.get("long_minutes", base["long_minutes"]))
        long_min = max(1, min(long_min, 24 * 60))  # 1min ~ 24h
        speed = float(cfg.get("expected_speed_gb_per_hour", base["expected_speed_gb_per_hour"]))
        speed = max(0.1, min(speed, 100000.0))      # 0.1 ~ 100k GB/h
        tol = float(cfg.get("speed_tolerance_pct", base["speed_tolerance_pct"]))
        tol = max(0.0, min(tol, 100.0))              # 0% ~ 100%
        fmin = int(cfg.get("freq_window_minutes", base["freq_window_minutes"]))
        fmin = max(1, min(fmin, 24 * 60))
        fthr = int(cfg.get("freq_threshold", base["freq_threshold"]))
        fthr = max(2, min(fthr, 100))
        rule = (cfg.get("long_rule") or base["long_rule"]).lower()
        if rule not in ("fixed", "speed", "both"):
            rule = base["long_rule"]
    except Exception as e:
        raise ValueError(f"配置参数无效: {e}")
    merged = {
        "long_minutes": long_min,
        "expected_speed_gb_per_hour": speed,
        "speed_tolerance_pct": tol,
        "freq_window_minutes": fmin,
        "freq_threshold": fthr,
        "long_rule": rule,
    }
    db.set_system_config(QUALITY_THRESHOLDS_KEY, json.dumps(merged, ensure_ascii=False))
    return merged


def compute_expected_duration_sec(size_bytes: int, expected_gb_per_hour: float) -> float:
    """根据数据量和预期速度（GB/h）计算"应在多长时间内完成"（秒）。"""
    if size_bytes <= 0 or expected_gb_per_hour <= 0:
        return 0.0
    gb = size_bytes / (1024 ** 3)
    return gb / expected_gb_per_hour * 3600.0  # GB ÷ (GB/h) × 3600s/h = s


@api_bp.route("/records", methods=["GET"])
@login_required
def list_records():
    task_id = request.args.get("task_id", type=int)
    keyword = request.args.get("keyword", type=str)
    rows = models.list_records(task_id=task_id, keyword=keyword, limit=500)
    for r in rows:
        r["size_human"] = db.human_size(r.get("size_bytes") or 0)
    return jsonify(rows)


@api_bp.route("/records/<int:record_id>", methods=["GET"])
@login_required
def get_record(record_id):
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"error": "记录不存在"}), 404
    rec["size_human"] = db.human_size(rec.get("size_bytes") or 0)
    rec["db_type_display"] = models._db_type_display(rec.get("db_type"))
    rec["backup_type_display"] = models._backup_type_display(rec.get("backup_type"))
    return jsonify(rec)


@api_bp.route("/records/<int:record_id>/download", methods=["GET"])
@login_required
def download_record(record_id):
    rec = models.get_record(record_id)
    if not rec or not rec.get("backup_path"):
        return jsonify({"error": "无备份文件可下载"}), 404
    path = rec["backup_path"]
    if not os.path.exists(path):
        return jsonify({"error": "备份文件已不存在（可能已被保留策略清理或位于远程）"}), 404
    return send_file(path, as_attachment=True)


@api_bp.route("/records/export", methods=["GET"])
@login_required
def export_records():
    """导出备份记录。支持 csv / docx / pdf 三种格式（?format=xxx）。"""
    fmt = (request.args.get("format") or "csv").lower()
    rows = models.list_records(limit=5000)
    # 导出报表保留「备份方式」并新增「业务系统」（设计 D4 §4.6）——与前端展示层不同：
    # 展示层删备份方式，导出层两者并存。headers 与 table 行必须同序位插入（第 3 位）。
    headers = ["ID", "任务ID", "业务系统", "类型", "备份方式", "开始时间", "完成时间",
               "耗时(s)", "状态", "大小", "路径", "校验和", "备注"]
    table = [[r.get("id"), r.get("task_id"), r.get("biz_label"),
              r.get("db_type"), r.get("backup_type"),
              r.get("started_at"), r.get("finished_at"), r.get("duration_sec"),
              r.get("status"), db.human_size(r.get("size_bytes", 0) or 0),
              r.get("backup_path"), (r.get("checksum") or "")[:16],
              r.get("message", "")]
             for r in rows]
    # 汇总
    status_count = Counter((r.get("status") or "") for r in rows)
    total_size = sum((r.get("size_bytes") or 0) for r in rows)
    summary = {
        "报告类型": "备份记录报告",
        "记录总数": len(rows),
        "成功 (success)": status_count.get("success", 0),
        "失败 (failed)": status_count.get("failed", 0),
        "仿真 (simulated)": status_count.get("simulated", 0),
        "累计备份体积": db.human_size(total_size),
        "导出范围": f"最近 {len(rows)} 条记录",
    }
    try:
        mime, ext, content = reports.build_report(
            fmt, "备份记录报告", summary, headers, table)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    fname = f"backup_records_report.{ext}"
    return Response(
        content,
        mimetype=mime,
        headers={"Content-Disposition": f"attachment; filename={fname}"})


@api_bp.route("/records/overrun-stats", methods=["GET"])
@login_required
def records_overrun_stats():
    """备份质量统计：超长备份 + 超频备份。

    阈值来源：system_config.backup_quality_thresholds（用户在仪表盘设置）。

    返回：
        {
          "thresholds": {
              "long_minutes": 30,
              "expected_speed_gb_per_hour": 500,
              "speed_tolerance_pct": 20,
              "freq_window_minutes": 5,
              "freq_threshold": 3,
              "long_rule": "speed"   # fixed | speed | both
          },
          "long_running": {
            "count": N,
            "items": [{id, task_id, db_type, backup_type, started_at,
                       finished_at, duration_sec, size_human,
                       expected_sec, reason, status, message}, ...]
          },
          "frequency": {
            "count": M,
            "items": [{task_id, db_type, count, window_start, window_end,
                       record_ids: [...]}]
          },
          "totals": { "records": ..., "long_count": ..., "freq_task_count": ... }
        }

    超长判定规则（long_rule）：
      - fixed: 仅按"耗时 >= long_minutes"判定。
      - speed: 仅按"实际耗时 > 预期耗时 × (1 + tolerance)"判定，
              预期耗时 = size_bytes / expected_speed_gb_per_hour × 3600。
              例：500GB 数据，预期 500 GB/h → 预期 1h；tolerance 20% → 超过 1h12min 视为超长。
      - both:  满足任一规则即视为超长。
    """
    thr = get_quality_thresholds()
    long_rule = (thr.get("long_rule") or "speed").lower()
    long_min = int(thr.get("long_minutes") or 30)
    long_fixed_sec = long_min * 60
    expected_speed = float(thr.get("expected_speed_gb_per_hour") or DEFAULT_EXPECTED_SPEED_GB_PER_HOUR)
    tolerance = float(thr.get("speed_tolerance_pct") or 0.0) / 100.0
    freq_min = int(thr.get("freq_window_minutes") or 5)
    freq_cnt = int(thr.get("freq_threshold") or 3)

    rows = models.list_records(limit=5000)

    # ---- 1) 超长备份 ----
    long_items = []
    for r in rows:
        d = float(r.get("duration_sec") or 0)
        size_bytes = int(r.get("size_bytes") or 0)
        expected_sec = compute_expected_duration_sec(size_bytes, expected_speed)
        reasons = []
        if long_rule in ("fixed", "both") and d >= long_fixed_sec:
            reasons.append(f"耗时 {d/60:.1f}min ≥ 阈值 {long_min}min")
        if long_rule in ("speed", "both") and expected_sec > 0:
            allowed = expected_sec * (1.0 + tolerance)
            if d > allowed:
                reasons.append(
                    f"耗时 {d/60:.1f}min 超出 {size_bytes/(1024**3):.1f}GB 的"
                    f"预期 {expected_sec/60:.1f}min（容差 +{tolerance*100:.0f}%）"
                )
        if reasons:
            rec = dict(r)
            rec["size_human"] = db.human_size(size_bytes)
            rec["expected_sec"] = round(expected_sec, 1)
            rec["reason"] = " · ".join(reasons)
            long_items.append(rec)
    long_items.sort(key=lambda r: r.get("duration_sec") or 0, reverse=True)
    long_items = long_items[:50]

    # ---- 2) 超频备份：按 task_id 在 freq_window 窗口内出现 >= freq_threshold 次 ----
    by_task = {}
    for r in rows:
        ts = r.get("started_at") or ""
        if not ts:
            continue
        by_task.setdefault(r.get("task_id"), []).append((ts, r))
    freq_items = []
    window_sec = freq_min * 60
    for tid, items in by_task.items():
        items.sort(key=lambda x: x[0])
        # 双指针扫描：同任务滑动窗口
        n = len(items)
        for i in range(n):
            j = i
            while j + 1 < n and (
                _ts_diff_sec(items[j + 1][0], items[i][0]) < window_sec
            ):
                j += 1
            cnt = j - i + 1
            if cnt >= freq_cnt:
                db_type = items[i][1].get("db_type") or ""
                ids = [items[k][1].get("id") for k in range(i, j + 1)]
                freq_items.append({
                    "task_id": tid,
                    "db_type": db_type,
                    "count": cnt,
                    "window_start": items[i][0],
                    "window_end": items[j][0],
                    "record_ids": ids,
                })
                # 跳过这一组以避免重复计数
                break
    freq_items.sort(key=lambda x: x["count"], reverse=True)
    freq_items = freq_items[:50]

    return jsonify({
        "thresholds": {
            "long_minutes": long_min,
            "long_sec": long_fixed_sec,
            "expected_speed_gb_per_hour": expected_speed,
            "speed_tolerance_pct": tolerance * 100,
            "freq_window_minutes": freq_min,
            "freq_threshold": freq_cnt,
            "long_rule": long_rule,
        },
        "long_running": {"count": len(long_items), "items": long_items},
        "frequency":    {"count": len(freq_items), "items": freq_items},
        "totals": {
            "records": len(rows),
            "long_count": len(long_items),
            "freq_task_count": len(freq_items),
        },
    })


# ----------------------------------------------------------------------------
# 备份质量阈值配置 API
# ----------------------------------------------------------------------------
@api_bp.route("/settings/backup-quality-thresholds", methods=["GET"])
@login_required
def get_quality_thresholds_api():
    """读取当前备份质量阈值（用户在仪表盘 / 设置页配置）。"""
    return jsonify(get_quality_thresholds())


@api_bp.route("/settings/backup-quality-thresholds", methods=["POST", "PUT"])
@login_required
def save_quality_thresholds_api():
    """保存备份质量阈值。

    Body (JSON)：
      {
        "long_minutes": 30,                 # 固定超长阈值
        "expected_speed_gb_per_hour": 500,  # 预期备份速度
        "speed_tolerance_pct": 20,          # 浮动容忍度 (%)
        "freq_window_minutes": 5,           # 超频窗口
        "freq_threshold": 3,                # 窗口内次数
        "long_rule": "speed"                # fixed | speed | both
      }
    """
    body = request.get_json(silent=True) or {}
    try:
        merged = save_quality_thresholds(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "thresholds": merged})


def _ts_diff_sec(ts1: str, ts2: str) -> int:
    """计算两个 ISO 字符串的时间差（秒）。失败时返回极大值。"""
    from datetime import datetime
    try:
        t1 = datetime.fromisoformat(ts1.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(ts2.replace("Z", "+00:00"))
        return int(abs((t1 - t2).total_seconds()))
    except Exception:
        return 10 ** 9
