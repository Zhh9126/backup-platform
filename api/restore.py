# -*- coding: utf-8 -*-
"""数据恢复 API。"""
from flask import request, jsonify

import core.db as db
from auth import login_required, verify_code_gate, permission_required
from core import models, scheduler
from core.ransomware_guard import RansomwareBlocked
from . import api_bp


@api_bp.route("/restores", methods=["GET"])
@login_required
def list_restores():
    keyword = request.args.get("keyword", type=str)
    return jsonify(models.list_restores(limit=200, keyword=keyword))


@api_bp.route("/records/enriched", methods=["GET"])
@login_required
def list_records_enriched():
    """返回带任务/主机信息的备份记录（供恢复页面下拉使用）。"""
    rows = models.list_records(limit=500)
    enriched = []
    for r in rows:
        enriched.append({
                "id": r["id"],
                "task_id": r["task_id"],
                "task_name": r.get("task_name", "-"),
                # 展示用业务系统标签（R2 已在 models 层计算），前端只读它
                "biz_label": r.get("biz_label", "-"),
                "host_ip": r.get("host_ip", "-"),
                "db_type": r.get("db_type"),
                "db_type_display": r.get("db_type_display"),
                "backup_type": r.get("backup_type"),
                "backup_type_display": r.get("backup_type_display"),
                "source_host": r.get("host_ip", ""),
                "source_port": "",
                "source_db": "",
                "status": r.get("status"),
                "started_at": r.get("started_at"),
                "size_bytes": r.get("size_bytes", 0),
                "backup_path": r.get("backup_path"),
                "binlog_file": r.get("binlog_file"),
                "binlog_pos": r.get("binlog_pos"),
                "wal_lsn": r.get("wal_lsn"),
                "verified": r.get("verified"),
                "verify_msg": r.get("verify_msg"),
            })
    return jsonify(enriched)


@api_bp.route("/restores", methods=["POST"])
@login_required
def create_restore():
    data = request.get_json(force=True, silent=True) or {}
    gate = verify_code_gate(data)
    if gate:
        return jsonify(gate[0]), gate[1]
    record_id = data.get("record_id")
    if not record_id:
        return jsonify({"error": "record_id 必填"}), 400
    # 对象存储恢复：目标是「桶 + 前缀」，不是主机/目录，走独立参数组。
    # 支持颗粒级恢复（restore_keys 只回放指定对象）与删除应用（apply_deleted）。
    restore_options = {}
    if data.get("target_bucket"):
        restore_options["target_bucket"] = str(data["target_bucket"]).strip()
    if data.get("target_prefix") is not None:
        restore_options["target_prefix"] = str(data["target_prefix"]).strip()
    if data.get("overwrite"):
        restore_options["overwrite"] = str(data["overwrite"]).strip()
    if data.get("apply_deleted") is not None:
        restore_options["apply_deleted"] = bool(data["apply_deleted"])
    keys = data.get("restore_keys")
    if keys:
        restore_options["restore_keys"] = (
            [k.strip() for k in str(keys).replace("\n", ",").split(",")
             if k.strip()] if isinstance(keys, str) else list(keys))

    try:
        result = scheduler.run_restore_now(
            record_id,
            target_host_id=data.get("target_host_id"),
            target_host=data.get("target_host"),
            target_db=data.get("target_db"),
            target_port=data.get("target_port"),
            operator=data.get("operator"),
            target_host_user=data.get("target_host_user"),
            target_host_password=data.get("target_host_password"),
            target_time=data.get("target_time"),
            pitr_restore_dir=data.get("pitr_restore_dir"),
            tables=data.get("tables"),
            restore_options=restore_options or None,
            # 强制恢复必须同时带显式确认，避免误点绕过安全门
            force_unsafe_restore=bool(data.get("force_unsafe_restore"))
            and str(data.get("confirm_unsafe") or "").lower()
            in ("1", "true", "yes", "on"),
        )
    except RansomwareBlocked as e:
        return jsonify({
            "error": str(e), "security_blocked": True,
            "record_id": e.record_id, "reasons": e.reasons,
            "hint": "如确需恢复，请勾选「已知风险，强制恢复」并重试（将留 critical 审计）",
        }), 403
    except ValueError as e:
        # 受保护主机 / 其他业务前置校验拒绝（P0-2）
        return jsonify({"error": str(e), "protected_target": "受保护" in str(e)}), 400
    if not result:
        return jsonify({"error": "备份记录不存在"}), 404
    return jsonify(result), 201


@api_bp.route("/records/<int:record_id>/objects", methods=["GET"])
@login_required
def api_record_objects(record_id):
    """备份产物对象清单（表级恢复向导数据源）。"""
    from core import object_catalog
    return jsonify(object_catalog.list_objects(record_id))


@api_bp.route("/records/<int:record_id>/tables", methods=["GET"])
@login_required
def api_record_tables(record_id):
    """表级恢复：产物内表清单（P0-1）。

    目录为空或只有占位条目时，同步扫描一次产物（tar/单库 dump）后返回。
    """
    from core import object_catalog
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"error": "备份记录不存在"}), 404
    rows = object_catalog.list_objects(record_id)
    if not rows or (len(rows) == 1 and rows[0].get("obj_type") == "DATABASE"):
        db_type = rec.get("db_type")
        if not db_type:
            t = models.get_task(rec.get("task_id")) or {}
            db_type = t.get("db_type")
        try:
            object_catalog.scan(record_id, rec.get("backup_path"), db_type)
        except Exception:
            pass  # 扫描失败回落到现有清单/占位，不阻断向导
        rows = object_catalog.list_objects(record_id)
    return jsonify(rows)


@api_bp.route("/webhooks", methods=["GET", "POST"])
@login_required
@permission_required("security.manage")
def api_webhooks():
    """Webhooks 事件中心配置：urls（逗号/换行分隔）+ secret（HMAC 签名）。"""
    from core import webhooks as wh
    if request.method == "GET":
        urls, secret = wh._urls_and_secret()
        # 安全整改：HMAC 密钥不回显，避免低权限用户读取后伪造平台事件
        return jsonify({"urls": ",".join(urls), "has_secret": bool(secret)})
    data = request.get_json(silent=True) or {}
    from core import net_guard
    urls_raw = data.get("urls", "")
    bad = []
    for u in str(urls_raw).replace("\n", ",").split(","):
        u = u.strip()
        if not u:
            continue
        ok, reason = net_guard.validate_url(u)
        if not ok:
            bad.append("%s（%s）" % (u[:80], reason))
    if bad:
        return jsonify({"ok": False,
                        "error": "Webhook 地址被拒绝: " + "; ".join(bad[:5])}), 400
    db.set_system_config("webhook_urls", urls_raw)
    # 留空表示保持原密钥不变（GET 已不再回显明文）
    secret = (data.get("secret") or "").strip()
    if secret:
        db.set_system_config("webhook_secret", secret)
    test = data.get("test")
    if test:
        sent = wh.emit("webhook.test", {"message": "hello from backup-platform"})
        return jsonify({"ok": True, "delivered": sent})
    return jsonify({"ok": True})
