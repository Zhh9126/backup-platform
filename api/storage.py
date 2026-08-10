# -*- coding: utf-8 -*-
"""
存储目标管理 API：CRUD + 连接测试 + 三级复制。

路由前缀: /api/storage（通过共享 api_bp 注册）
提供存储目标的增删改查、连接测试、设为默认、手动触发复制等接口。
敏感字段（secret_key）加密存储，返回时自动脱敏。
"""
import json
import os
import shutil
import time
from flask import request, jsonify

import core.db as db
from auth import login_required
from core.storage_backends import get_backend, list_supported_types, check_dependencies, TYPE_META, TIER_NAMES
from . import api_bp


# ------------------------- 工具函数 -------------------------

def _row_to_dict(row) -> dict:
    """将数据库行转为字典，并脱敏。"""
    d = dict(row)
    if d.get("secret_key"):
        d["secret_key_masked"] = "******"
        d.pop("secret_key", None)
    if d.get("extra_options"):
        try:
            d["extra_options"] = json.loads(d["extra_options"])
        except (json.JSONDecodeError, TypeError):
            pass
    type_info = TYPE_META.get(d.get("type"), {})
    d["display_name"] = type_info.get("name", d["type"])
    d["tier_name"] = TIER_NAMES.get(d.get("tier", 1), f"L{d.get('tier', 1)}")
    return d


def _get_enabled_targets(tier: int = None) -> list[dict]:
    if tier is not None:
        rows = db.query(
            "SELECT * FROM storage_targets WHERE enabled=1 AND tier=? ORDER BY is_default DESC, id",
            (tier,),
        )
    else:
        rows = db.query(
            "SELECT * FROM storage_targets WHERE enabled=1 ORDER BY tier, is_default DESC, id"
        )
    return [dict(r) for r in rows]


# ------------------------- API 端点 -------------------------

@api_bp.route("/storage/types", methods=["GET"])
@login_required
def api_storage_types():
    return jsonify({
        "types": list_supported_types(),
        "dependencies": check_dependencies(),
    })


@api_bp.route("/storage/targets", methods=["GET"])
@login_required
def api_list_targets():
    rows = db.query("SELECT * FROM storage_targets ORDER BY tier, id")
    targets = [_row_to_dict(r) for r in rows]
    return jsonify({"targets": targets})


@api_bp.route("/storage/targets/<int:target_id>", methods=["GET"])
@login_required
def api_get_target(target_id):
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (target_id,))
    if not row:
        return jsonify({"error": "存储目标不存在"}), 404
    d = dict(row)
    if d.get("secret_key"):
        d["has_secret_key"] = True
        d["secret_key_masked"] = "******"
        d.pop("secret_key", None)
    if d.get("extra_options"):
        try:
            d["extra_options"] = json.loads(d["extra_options"])
        except (json.JSONDecodeError, TypeError):
            pass
    return jsonify(d)


@api_bp.route("/storage/targets", methods=["POST"])
@login_required
def api_create_target():
    data = request.get_json(silent=True) or {}
    required = ["name", "type"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    stype = data["type"].lower()
    if stype not in TYPE_META:
        return jsonify({"error": f"不支持的存储类型: {stype}"}), 400

    now = db.now_iso()
    extra_opts = data.get("extra_options")
    if isinstance(extra_opts, dict):
        extra_opts = json.dumps(extra_opts, ensure_ascii=False)

    secret = data.get("secret_key", "")
    if secret:
        secret = db.encrypt_secret(secret)

    target_id = db.execute("""
        INSERT INTO storage_targets (name, type, tier, endpoint, access_key, secret_key,
            bucket, region, prefix, enabled, is_default, extra_options, remark, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["name"], stype, data.get("tier", TYPE_META[stype]["tier"]),
        data.get("endpoint", ""), data.get("access_key", ""),
        secret, data.get("bucket", ""), data.get("region", ""),
        (data.get("prefix") or "").strip("/"),
        1 if data.get("enabled") else 0,
        1 if data.get("is_default") else 0,
        extra_opts, data.get("remark", ""), now, now,
    ))

    db.add_log("info", "storage", f"创建存储目标: {data['name']} ({stype})")
    return jsonify({"id": target_id, "ok": True})


@api_bp.route("/storage/targets/<int:target_id>", methods=["PUT"])
@login_required
def api_update_target(target_id):
    existing = db.query_one("SELECT id FROM storage_targets WHERE id=?", (target_id,))
    if not existing:
        return jsonify({"error": "存储目标不存在"}), 404

    data = request.get_json(silent=True) or {}
    now = db.now_iso()

    allowed = [
        "name", "type", "tier", "endpoint", "access_key", "bucket",
        "region", "prefix", "enabled", "is_default", "remark",
    ]
    sets = []
    params = []
    for f in allowed:
        if f in data:
            sets.append(f"{f}=?")
            params.append(data[f])

    if "secret_key" in data and data["secret_key"]:
        sets.append("secret_key=?")
        params.append(db.encrypt_secret(data["secret_key"]))

    if "extra_options" in data:
        opts = data["extra_options"]
        sets.append("extra_options=?")
        params.append(json.dumps(opts, ensure_ascii=False) if isinstance(opts, dict) else opts)

    sets.append("updated_at=?")
    params.append(now)
    params.append(target_id)

    if sets:
        sql = f"UPDATE storage_targets SET {', '.join(sets)} WHERE id=?"
        db.execute(sql, params)

    db.add_log("info", "storage", f"更新存储目标 ID={target_id}")
    return jsonify({"ok": True})


@api_bp.route("/storage/targets/<int:target_id>", methods=["DELETE"])
@login_required
def api_delete_target(target_id):
    existing = db.query_one("SELECT id, name FROM storage_targets WHERE id=?", (target_id,))
    if not existing:
        return jsonify({"error": "存储目标不存在"}), 404

    db.execute("DELETE FROM storage_targets WHERE id=?", (target_id,))
    db.add_log("info", "storage", f"删除存储目标: {existing['name']}")
    return jsonify({"ok": True})


@api_bp.route("/storage/targets/<int:target_id>/test", methods=["POST"])
@login_required
def api_test_target(target_id):
    row = db.query_one("SELECT * FROM storage_targets WHERE id=?", (target_id,))
    if not row:
        return jsonify({"error": "存储目标不存在"}), 404

    config = dict(row)
    body = request.get_json(silent=True) or {}
    if body.get("secret_key") and not config.get("secret_key"):
        config["secret_key"] = body["secret_key"]

    try:
        backend = get_backend(config["type"], config)
        t0 = time.time()
        ok, msg = backend.test_connection()
        ms = round((time.time() - t0) * 1000)

        now = db.now_iso()
        db.execute(
            "UPDATE storage_targets SET last_error=?, last_test_at=?, updated_at=? WHERE id=?",
            ("" if ok else msg, now, now, target_id),
        )

        return jsonify({"ok": ok, "message": msg, "ms": ms})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@api_bp.route("/storage/targets/<int:target_id>/default", methods=["POST"])
@login_required
def api_set_default(target_id):
    row = db.query_one("SELECT id, tier FROM storage_targets WHERE id=?", (target_id,))
    if not row:
        return jsonify({"error": "存储目标不存在"}), 404

    tier = row["tier"]
    db.execute("UPDATE storage_targets SET is_default=0 WHERE tier=?", (tier,))
    db.execute("UPDATE storage_targets SET is_default=1 WHERE id=?", (target_id,))
    return jsonify({"ok": True})


@api_bp.route("/storage/targets/enabled", methods=["GET"])
@login_required
def api_list_enabled_targets():
    tier = request.args.get("tier", type=int)
    targets = _get_enabled_targets(tier)
    result = []
    for t in targets:
        d = _row_to_dict(t)
        result.append(d)
    return jsonify({"targets": result})


@api_bp.route("/storage/stats", methods=["GET"])
@login_required
def api_storage_stats():
    stats = {}
    for tier_num, tier_name in TIER_NAMES.items():
        total = db.query_one(
            "SELECT COUNT(*) as cnt FROM storage_targets WHERE tier=?", (tier_num,)
        )
        enabled = db.query_one(
            "SELECT COUNT(*) as cnt FROM storage_targets WHERE tier=? AND enabled=1", (tier_num,)
        )
        errors = db.query_one(
            "SELECT COUNT(*) as cnt FROM storage_targets WHERE tier=? AND enabled=1 AND last_error IS NOT NULL AND last_error!=''",
            (tier_num,),
        )
        stats[f"tier_{tier_num}"] = {
            "name": tier_name,
            "total": total["cnt"] if total else 0,
            "enabled": enabled["cnt"] if enabled else 0,
            "has_error": errors["cnt"] if errors else 0,
        }

    deps = check_dependencies()
    return jsonify({
        "tiers": stats,
        "dependencies": deps,
        "all_tiers_configured": all(
            stats.get(f"tier_{t}", {}).get("enabled", 0) > 0 for t in [1, 2, 3]
        ),
    })


@api_bp.route("/storage/usage", methods=["GET"])
@login_required
def api_storage_usage():
    """本地存储（L1）所在磁盘的容量/用量概览。"""
    row = db.query_one(
        "SELECT endpoint FROM storage_targets WHERE type='local' AND enabled=1 ORDER BY is_default DESC, id LIMIT 1"
    )
    path = (row and row["endpoint"]) or "./backups"
    path = os.path.abspath(path)
    try:
        du = shutil.disk_usage(path)
        used_percent = round(du.used / du.total * 100, 1)
        return jsonify({
            "path": path,
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "used_percent": used_percent,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/storage/replicate/<int:record_id>", methods=["POST"])
@login_required
def api_trigger_replicate(record_id):
    from core import tier_replication
    row = db.query_one("SELECT * FROM backup_records WHERE id=?", (record_id,))
    if not row:
        return jsonify({"error": "备份记录不存在"}), 404

    path = row.get("backup_path")
    task_id = row.get("task_id")
    if not path or not task_id:
        return jsonify({"error": "该记录无有效备份文件或任务信息"}), 400

    import core.models as models
    task = models.get_task(task_id)
    if not task:
        return jsonify({"error": "关联任务不存在"}), 404

    try:
        result = tier_replication.replicate_to_tiers(path, task, record_id)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/storage/replicate/<int:record_id>/status", methods=["GET"])
@login_required
def api_replicate_status(record_id):
    from core import tier_replication
    status = tier_replication.get_replication_status(record_id)
    if "error" in status:
        return jsonify(status), 404
    return jsonify(status)


# ========== 复制策略配置 ==========

_REPLICATION_CONFIG_KEY = "replication_strategy"

_DEFAULT_REPLICATION_CONFIG = {
    "push_l1_minio": 1,
    "push_l2_s3": 1,
    "push_l3_local": 1,
    "timing": "immediate",
    "max_retries": 3,
    "retry_interval": 30,
}


def _get_replication_config():
    """从 system_config 表读取复制策略，缺失字段用默认值补全。"""
    row = db.query_one(
        "SELECT value FROM system_config WHERE key=?", (_REPLICATION_CONFIG_KEY,)
    )
    if not row or not row["value"]:
        return dict(_DEFAULT_REPLICATION_CONFIG)
    try:
        cfg = json.loads(row["value"])
        # 合并默认值（确保新增字段有值）
        for k, v in _DEFAULT_REPLICATION_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    except (json.JSONDecodeError, TypeError):
        return dict(_DEFAULT_REPLICATION_CONFIG)


@api_bp.route("/storage/replication-config", methods=["GET"])
@login_required
def api_get_replication_config():
    """获取当前复制策略配置。"""
    return jsonify(_get_replication_config())


@api_bp.route("/storage/replication-config", methods=["POST"])
@login_required
def api_save_replication_config():
    """保存复制策略配置。"""
    data = request.get_json(silent=True) or {}
    # 白名单校验
    allowed = {"push_l1_minio", "push_l2_s3", "push_l3_local",
               "timing", "max_retries", "retry_interval"}
    cfg = _get_replication_config()
    for k, v in data.items():
        if k in allowed:
            cfg[k] = v

    # 校验 timing 值
    valid_timings = {"immediate", "delay_5min", "delay_30min", "delay_1hour", "manual"}
    if cfg.get("timing") not in valid_timings:
        cfg["timing"] = "immediate"

    db.execute(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
        (_REPLICATION_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)),
    )
    return jsonify({"ok": True, "config": cfg})
