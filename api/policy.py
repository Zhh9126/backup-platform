# -*- coding: utf-8 -*-
"""
保护策略管理 API：CRUD + 任务绑定。

路由前缀: /api/policy（通过共享 api_bp 注册）
提供保护策略的增删改查，以及将策略批量绑定/解绑到备份任务的能力。
敏感字段脱敏风格保持与本平台一致（本模块无密钥，但仍统一返回结构）。
"""
import json

from flask import request, jsonify

import core.models as models
from auth import login_required
from . import api_bp


# ------------------------- 工具函数 -------------------------

def _row_to_dict(row: dict) -> dict:
    """将保护策略行转为 dict，并反序列化 JSON 字段。"""
    d = dict(row)
    for f in ("backup_strategy", "link_strategy", "retention"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except (json.JSONDecodeError, TypeError):
                pass
    d["enabled"] = bool(d.get("enabled"))
    return d


def _normalize_policy_payload(data: dict) -> None:
    """把结构化调度字段合并进 backup_strategy（就地修改）。

    前端新表单直接提交 ``type/cron_expr/interval_minutes/full_mode/full_days/
    full_time/...``，历史接口提交的是 ``backup_strategy`` 整段 JSON；两者都接受，
    统一归一后写入 ``backup_strategy``。
    """
    if not isinstance(data, dict):
        return
    bs = data.get("backup_strategy")
    if isinstance(bs, str):
        try:
            bs = json.loads(bs) if bs.strip() else {}
        except Exception:
            bs = {}
    bs = dict(bs or {})
    for k in ("schedule", "type", "cron_expr", "interval_minutes", "enabled",
              "full_mode", "full_type", "full_days", "full_time", "full_cron_expr",
              "incremental_type", "incremental_cron_expr",
              "incremental_interval_minutes", "backup_mode", "parallel"):
        if k in data and data[k] is not None:
            bs[k] = data.pop(k)
    if bs:
        try:
            from core import policy_schedule as _ps
            bs = _ps.normalize_schedule(bs)
        except Exception:
            pass
        data["backup_strategy"] = bs


# ------------------------- API 端点 -------------------------

@api_bp.route("/policy", methods=["GET"])
@login_required
def api_list_policies():
    from core import policy_schedule as _ps
    rows = models.list_protection_policies()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["bound_task_count"] = models.count_tasks_by_policy(r["id"])
        try:
            pv = _ps.policy_preview(d)
            d["schedule_summary"] = pv["summary"]
            d["next_runs"] = pv["next_runs"][:2]
            d["task_fields"] = pv["task_fields"]
        except Exception:
            d["schedule_summary"] = ""
            d["next_runs"] = []
        out.append(d)
    return jsonify(out)


@api_bp.route("/policy", methods=["POST"])
@login_required
def api_create_policy():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "策略名称为必填"}), 400
    level = data.get("level") or "general"
    if level not in ("core", "important", "general"):
        return jsonify({"error": "保护等级无效（应为 core/important/general）"}), 400
    data["level"] = level
    _normalize_policy_payload(data)
    pid = models.create_protection_policy(data)
    # 新建时通常还没绑定任务；仍尝试下发一次（绑定时会自动同步）
    try:
        from core import policy_schedule as _ps
        sync = _ps.sync_bound_tasks(pid)
    except Exception as e:                                  # 同步失败不阻断建策略
        sync = {"ok": False, "message": str(e), "updated": 0}
    return jsonify({"id": pid, "ok": True, "sync": sync}), 201


@api_bp.route("/policy/<int:policy_id>", methods=["GET"])
@login_required
def api_get_policy(policy_id):
    row = models.get_protection_policy(policy_id)
    if not row:
        return jsonify({"error": "策略不存在"}), 404
    d = _row_to_dict(row)
    d["bound_task_count"] = models.count_tasks_by_policy(policy_id)
    d["bound_tasks"] = models.list_tasks_by_policy(policy_id)
    return jsonify(d)


@api_bp.route("/policy/<int:policy_id>", methods=["PUT"])
@login_required
def api_update_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    data = request.get_json(silent=True) or {}
    if "level" in data and data["level"] not in ("core", "important", "general"):
        return jsonify({"error": "保护等级无效（应为 core/important/general）"}), 400
    _normalize_policy_payload(data)
    models.update_protection_policy(policy_id, data)
    # 策略变更即级联下发到所有绑定任务（频次 / 全量周期 / 保留），并重载调度器
    try:
        from core import policy_schedule as _ps
        sync = _ps.sync_bound_tasks(policy_id)
    except Exception as e:
        sync = {"ok": False, "message": str(e), "updated": 0}
    return jsonify({"ok": True, "sync": sync})


@api_bp.route("/policy/preview", methods=["POST"])
@login_required
def api_preview_policy_payload():
    """未保存策略的实时预览：人话摘要 + 下次执行时间 + 将下发的任务字段。"""
    data = request.get_json(silent=True) or {}
    _normalize_policy_payload(data)
    from core import policy_schedule as _ps
    return jsonify(_ps.policy_preview(data))


@api_bp.route("/policy/<int:policy_id>/preview", methods=["GET"])
@login_required
def api_preview_policy(policy_id):
    pol = models.get_protection_policy(policy_id)
    if not pol:
        return jsonify({"error": "策略不存在"}), 404
    from core import policy_schedule as _ps
    out = _ps.policy_preview(pol)
    out["bound_task_count"] = models.count_tasks_by_policy(policy_id)
    return jsonify(out)


@api_bp.route("/policy/<int:policy_id>/apply", methods=["POST"])
@login_required
def api_apply_policy(policy_id):
    """手动把策略调度重新下发给绑定任务（排障/回填用）。"""
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    from core import policy_schedule as _ps
    return jsonify(_ps.sync_bound_tasks(policy_id))


@api_bp.route("/policy/<int:policy_id>", methods=["DELETE"])
@login_required
def api_delete_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    # 先解绑所有关联任务，避免悬空引用
    models.unbind_all_tasks_by_policy(policy_id)
    models.delete_protection_policy(policy_id)
    return jsonify({"ok": True})


@api_bp.route("/policy/<int:policy_id>/bind", methods=["POST"])
@login_required
def api_bind_policy(policy_id):
    if not models.get_protection_policy(policy_id):
        return jsonify({"error": "策略不存在"}), 404
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids") or []
    if not isinstance(task_ids, list):
        return jsonify({"error": "task_ids 必须是数组"}), 400
    bound = models.bind_policy_to_tasks(policy_id, [int(t) for t in task_ids])
    return jsonify({"ok": True, "bound": bound})


@api_bp.route("/policy/<int:policy_id>/bind", methods=["DELETE"])
@login_required
def api_unbind_policy(policy_id):
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids") or []
    if task_ids:
        models.unbind_policy_from_tasks([int(t) for t in task_ids])
    else:
        models.unbind_all_tasks_by_policy(policy_id)
    return jsonify({"ok": True})
