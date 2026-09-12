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
from flask import request, jsonify, current_app

import config
import core.db as db
from auth import login_required
from core.storage_backends import get_backend, list_supported_types, check_dependencies, TYPE_META, TIER_NAMES
from . import api_bp


# ------------------------- 本地备份存储位置（L1 落点） -------------------------
# 未部署 MinIO/S3 时本地目录是备份的唯一落点，故该位置必须对用户可见且可配置：
# 界面配置持久化到 system_config.backup_root，重启后仍生效。
_LOCAL_ROOT_KEY = "backup_root"
# 拒绝把备份目录设到系统关键目录（防误操作）
_FORBIDDEN_ROOTS = {
    "/", "/bin", "/sbin", "/lib", "/lib64", "/usr", "/etc", "/boot", "/proc",
    "/sys", "/dev", "/run", "/var/run", "/var/log", "/tmp",
}
_MAX_STAT_ENTRIES = 20000  # 目录用量统计上限（防超大目录拖慢接口）


def _dir_usage(path: str) -> dict:
    """统计目录内文件总量（超过上限则标记为近似值）。"""
    total = 0
    files = 0
    scanned = 0
    approximate = False
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for fn in filenames:
            scanned += 1
            if scanned > _MAX_STAT_ENTRIES:
                approximate = True
                break
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                files += 1
            except OSError:
                continue
        if approximate:
            break
    return {"bytes": total, "files": files, "approximate": approximate}


@api_bp.route("/storage/local-root", methods=["GET"])
@login_required
def api_get_local_root():
    """本地备份存储位置：实际路径、来源、磁盘用量、持久化风险提示。"""
    info = config.backup_root_info()
    path = info["path"]
    info["exists"] = os.path.isdir(path)
    info["writable"] = (os.access(path, os.W_OK) if info["exists"]
                        else os.access(os.path.dirname(path) or "/", os.W_OK))
    try:
        du = shutil.disk_usage(path)
        info["disk"] = {
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "used_percent": round(du.used / du.total * 100, 1),
        }
    except Exception:
        info["disk"] = {}
    info["data"] = _dir_usage(path) if info["exists"] else {"bytes": 0, "files": 0, "approximate": False}
    # 是否已配置对象存储（决定"仅本地保存"的提示强度）
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM storage_targets "
        "WHERE enabled=1 AND type IN ('minio','s3')"
    )
    info["object_storage_configured"] = bool(row and row["cnt"])
    return jsonify(info)


@api_bp.route("/storage/local-root", methods=["PUT"])
@login_required
def api_set_local_root():
    """修改本地备份存储位置（界面配置，重启后仍生效）。

    body: {"path": "/data/backups", "migrate": false}
    migrate=true 时把旧目录内的历史备份移动到新目录（同名已存在则跳过并报告）。
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    migrate = bool(data.get("migrate"))
    if not raw:
        return jsonify({"error": "请填写备份存储目录"}), 400
    if not os.path.isabs(os.path.expanduser(raw)):
        return jsonify({"error": "请填写绝对路径，例如 /data/backups"}), 400

    new_path = os.path.abspath(os.path.expanduser(raw))
    if new_path.rstrip("/") in _FORBIDDEN_ROOTS:
        return jsonify({"error": f"不允许将备份目录设置为系统目录：{new_path}"}), 400
    if os.path.isfile(new_path):
        return jsonify({"error": "该路径已是一个文件，请填写目录路径"}), 400

    old_path = os.path.abspath(str(config.get_backup_root()))
    try:
        os.makedirs(new_path, exist_ok=True)
        probe = os.path.join(new_path, ".aidbm_write_test")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
    except Exception as e:
        return jsonify({"error": f"目录不可写：{e}"}), 400

    if os.path.realpath(new_path) == os.path.realpath(old_path):
        return jsonify({"ok": True, "path": new_path, "unchanged": True,
                        "message": "路径未变化"})

    # 持久化 + 运行时生效（config 全局值 + flask 配置同步）
    db.execute(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
        (_LOCAL_ROOT_KEY, new_path),
    )
    config.set_backup_root(new_path, origin="ui")
    try:
        current_app.config["BACKUP_ROOT"] = new_path
    except Exception:
        pass

    migrated = None
    if migrate and os.path.isdir(old_path):
        moved, failed = [], []
        for name in sorted(os.listdir(old_path)):
            src = os.path.join(old_path, name)
            dst = os.path.join(new_path, name)
            try:
                if os.path.exists(dst):
                    failed.append(f"{name}（目标已存在，已跳过）")
                    continue
                shutil.move(src, dst)
                moved.append(name)
            except Exception as e:
                failed.append(f"{name}（{e}）")
        migrated = {"from": old_path, "moved": moved, "failed": failed}

    db.add_log("info", "storage",
               f"本地备份存储位置变更：{old_path} -> {new_path}"
               + (f"（迁移 {len(migrated['moved'])} 项）" if migrated else ""))
    return jsonify({
        "ok": True,
        "path": new_path,
        "old_path": old_path,
        "migrated": migrated,
        "message": "本地备份存储位置已更新",
        "info": config.backup_root_info(),
    })


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
    """本地存储（L1 落点）所在磁盘的容量/用量概览。

    路径以实际生效的备份根目录为准（界面配置 > 环境变量 > 默认），
    不再依赖 storage_targets 中是否登记了 local 目标——未登记时也必须
    能告诉用户备份到底存在哪里。
    """
    path = os.path.abspath(str(config.get_backup_root()))
    try:
        du = shutil.disk_usage(path)
        used_percent = round(du.used / du.total * 100, 1)
        return jsonify({
            "path": path,
            "source": config.backup_root_source(),
            "source_label": config.backup_root_source_label(),
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "used_percent": used_percent,
        })
    except Exception as e:
        return jsonify({"path": path, "error": str(e)}), 400


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
