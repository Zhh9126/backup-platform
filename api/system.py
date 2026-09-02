# -*- coding: utf-8 -*-
"""系统/仪表盘/调度/日志/元信息 API。"""
import os
import logging
from flask import jsonify, request, session
import json

logger = logging.getLogger("api.system")

from auth import login_required
from core import models, scheduler, db
from core.engines import supported_types, ENGINE_DISPLAY
import config
from . import api_bp


@api_bp.route("/meta", methods=["GET"])
@login_required
def meta():
    return jsonify({
        "db_types": supported_types(),
        "display_names": ENGINE_DISPLAY,
        "default_ports": config.DEFAULT_PORTS,
        "demo_mode": config.DEMO_MODE,
        "scheduler_enabled": config.SCHEDULER_ENABLED,
        "backup_modes": {
            "logical": "逻辑备份（mysqldump / pg_dump / expdp）",
            "physical": "物理备份（XtraBackup / pg_basebackup / RMAN）",
        },
    })


@api_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    tasks = models.list_tasks(include_secret=False)
    records = models.list_records(limit=500)
    from collections import Counter
    from datetime import datetime
    status_counter = Counter(r["status"] for r in records)
    db_counter = Counter(t["db_type"] for t in tasks)
    total_size = sum((r.get("size_bytes") or 0) for r in records)
    db_task_count = sum(1 for t in tasks if t.get("db_type") != "file")
    file_task_count = sum(1 for t in tasks if t.get("db_type") == "file")
    total_size_gb = round(total_size / (1024 ** 3), 2)
    # ---- 综合健康评分 (0~100) ----
    health, health_details = _calc_health(tasks, records, db_task_count, file_task_count)
    # ---- 压缩率统计 ----
    compression_ratio = 0
    try:
        import os
        compressed = sum(1 for r in records if (r.get("backup_path") or "").endswith(".gz"))
        comp_ratio = round(compressed / max(len(records), 1) * 100)
    except Exception:
        comp_ratio = 0
    # ---- 构造任务名/业务系统 索引（recent_records 展示用） ----
    task_name_map = {t["id"]: t.get("name") for t in tasks}
    task_biz_map = {t["id"]: t.get("biz_system") or t.get("name") for t in tasks}
    task_host_map = {t["id"]: t.get("host") for t in tasks}
    task_mode_map = {t["id"]: t.get("backup_mode") for t in tasks}
    # ---- 存储池加密任务数（extra_options.encrypt_pool === true） ----
    encrypt_pool_tasks = 0
    for t in tasks:
        try:
            _eo = json.loads(t.get("extra_options") or "{}")
        except Exception:
            _eo = {}
        if _eo.get("encrypt_pool") is True:
            encrypt_pool_tasks += 1

    def _enrich(r: dict) -> dict:
        """在 record dict 上补仪表盘需要的展示字段（中文 + 关联任务信息）。"""
        tid = r.get("task_id")
        mode = r.get("backup_mode") or task_mode_map.get(tid) or ""
        return {
            "id": r.get("id"),
            "task_id": tid,
            "task_name": task_name_map.get(tid) or "-",
            "biz_system": task_biz_map.get(tid) or "-",
            "host_ip": r.get("host_ip") or task_host_map.get(tid) or "-",
            "db_type": r.get("db_type"),
            "db_type_display": r.get("db_type_display") or config.DB_DISPLAY_NAMES.get(r.get("db_type"), r.get("db_type") or "-"),
            "backup_type": r.get("backup_type"),
            "backup_type_display": r.get("backup_type_display") or config.BACKUP_TYPE_DISPLAY_NAMES.get(r.get("backup_type"), r.get("backup_type") or "-"),
            "backup_mode": mode,
            "backup_mode_display": config.BACKUP_MODE_DISPLAY_NAMES.get(mode, mode or "-"),
            "status": r.get("status"),
            "status_display": config.BACKUP_STATUS_DISPLAY_NAMES.get(r.get("status"), r.get("status") or "-"),
            "duration_sec": r.get("duration_sec"),
            "size_bytes": r.get("size_bytes"),
            "size_human": r.get("size_human") or db.human_size(r.get("size_bytes") or 0),
            "started_at": r.get("started_at"),
        }

    recent = [_enrich(r) for r in records[:10]]
    return jsonify({
        "task_count": len(tasks),
        "db_task_count": db_task_count,
        "file_task_count": file_task_count,
        "record_count": len(records),
        "status_counter": {
            k: v for k, v in dict(status_counter).items()
        },
        "status_counter_display": {
            k: config.BACKUP_STATUS_DISPLAY_NAMES.get(k, k)
            for k, v in dict(status_counter).items()
        },
        "db_counter": dict(db_counter),
        "db_counter_display": {
            k: config.DB_DISPLAY_NAMES.get(k, k)
            for k, v in dict(db_counter).items()
        },
        "total_size": total_size,
        "total_size_gb": total_size_gb,
        "total_size_human": db.human_size(total_size),
        "recent_records": recent,
        "recent_tasks": tasks[:5],
        "health_score": health,
        "health_details": health_details,
        "compression_pct": comp_ratio,
        "encrypt_pool_tasks": encrypt_pool_tasks,
    })


def _calc_health(tasks, records, db_task_count, file_task_count):
    """综合健康评分：任务覆盖 30 分 + 备份成功率 40 分 + 调度完备 20 分 + 演练 10 分。"""
    from datetime import datetime, timezone, timedelta
    details = []
    score = 0
    # 1. 任务覆盖 (max 30)
    total = len(tasks)
    cover = min(1.0, total / max(db_task_count + file_task_count, 1)) if total > 0 else 0
    task_score = int(30 * (1 if total > 0 else 0))
    score += task_score
    details.append(f"任务覆盖: {task_score}/30 ({total} 个任务)")
    # 2. 备份成功率 (max 40)
    if records:
        ok = sum(1 for r in records if r.get("status") in ("success", "simulated"))
        fail = sum(1 for r in records if r.get("status") == "failed")
        rate = ok / len(records) if records else 0
        rec_score = int(40 * rate)
        score += rec_score
        details.append(f"备份成功率: {rec_score}/40 ({ok}/{len(records)} 条)")
    else:
        details.append("备份成功率: 0/40 (暂无记录)")
    # 3. 调度完备 (max 20)
    enabled = sum(1 for t in tasks if t.get("enabled"))
    scheduled = sum(1 for t in tasks if t.get("enabled") and t.get("schedule_type") not in (None, "none", ""))
    sched_score = int(20 * (scheduled / max(enabled, 1))) if enabled > 0 else 0
    score += sched_score
    details.append(f"调度完备: {sched_score}/20 ({scheduled}/{max(enabled,1)} 个启用任务已配调度)")
    # 4. 同步延迟 (max 10) - 最近备份几小时内
    sync_score = 0
    now = datetime.utcnow()
    for r in records:
        if r.get("status") in ("success", "simulated"):
            try:
                at = r.get("finished_at") or r.get("started_at") or ""
                dt = datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")
                lag = (now - dt).total_seconds()
                if lag < 3600: sync_score = 10  # < 1h
                elif lag < 86400: sync_score = max(sync_score, 5)  # < 24h
                elif lag < 604800: sync_score = max(sync_score, 2)  # < 7d
            except Exception:
                pass
    score += sync_score
    details.append(f"同步延迟: {sync_score}/10 (最近成功备份 < 1h)")
    return min(score, 100), details


@api_bp.route("/scheduler", methods=["GET"])
@login_required
def sched_status():
    return jsonify(scheduler.scheduler_status())


@api_bp.route("/scheduler/reload", methods=["POST"])
@login_required
def sched_reload():
    scheduler.reload_scheduler()
    return jsonify(scheduler.scheduler_status())


@api_bp.route("/logs", methods=["GET"])
@login_required
def logs():
    limit = min(int(request.args.get("limit", 200)), 1000)
    level = (request.args.get("level") or "").strip().upper()
    source = (request.args.get("source") or "").strip()
    rows = models.list_logs(limit=limit, level=level, source=source)
    sources = models.list_log_sources()
    return jsonify({
        "ok": True,
        "logs": rows,
        "count": len(rows),
        "sources": sources,
    })


@api_bp.route("/logs/clear", methods=["POST"])
@login_required
def clear_logs():
    deleted = models.clear_logs()
    logger.info("system logs cleared by user (%s rows)", deleted)
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/notify-config", methods=["GET"])
@login_required
def get_notify_config():
    import json
    raw = db.get_system_config("notify")
    cfg = json.loads(raw) if raw else dict(config.NOTIFY_DEFAULTS)
    # 不回显密码明文
    channels = []
    for ch in cfg.get("channels", []):
        c = dict(ch)
        if c.get("type") == "email":
            c.pop("smtp_password", None)
        channels.append(c)
    return jsonify({
        "enabled": cfg.get("enabled", config.NOTIFY_DEFAULTS.get("enabled", False)),
        "on_success": cfg.get("on_success", config.NOTIFY_DEFAULTS.get("on_success", False)),
        "on_failure": cfg.get("on_failure", config.NOTIFY_DEFAULTS.get("on_failure", True)),
        "channels": channels,
    })


@api_bp.route("/notify-config", methods=["POST"])
@login_required
def save_notify_config():
    import json
    data = request.get_json(force=True, silent=True) or {}
    # 兼容两种入参：
    #   新格式（前端当前使用）：{enabled, on_success, on_failure, channels: [...]}
    #   旧格式：{notify: "<json string>"}
    if isinstance(data.get("notify"), (str, dict)):
        raw = data["notify"]
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    else:
        # 防静默丢字段：识别到平铺 SMTP 字段（契约要求 channels 数组结构）时
        # 直接 400 提示，而不是忽略后返回"保存成功"造成配置丢失假象
        flat_smtp = {"smtp_host", "smtp_port", "smtp_user", "smtp_password",
                     "username", "password", "from_addr", "to_addrs", "to"}
        suspect = sorted(flat_smtp & set(data.keys()))
        if suspect and "channels" not in data:
            return jsonify({"error": (
                "通知配置格式不正确：请使用 {enabled, on_success, on_failure, "
                "channels: [{type:'email', smtp_host, smtp_port, smtp_user, "
                "smtp_password, from_addr, to, use_tls}]} 结构"
                f"（检测到疑似误传的字段: {', '.join(suspect)}）")}), 400
        cfg = {
            "enabled": bool(data.get("enabled", False)),
            "on_success": bool(data.get("on_success", False)),
            "on_failure": bool(data.get("on_failure", True)),
            "channels": data.get("channels", []),
        }
    # 合并旧配置（保留未改动字段，如已存密码）
    old_raw = db.get_system_config("notify")
    old = json.loads(old_raw) if old_raw else {}
    old_channels = {c.get("smtp_host"): c for c in old.get("channels", [])}
    new_channels = []
    for ch in cfg.get("channels", []):
        if ch.get("type") != "email":
            new_channels.append(ch)
            continue
        c = {
            "type": "email",
            "smtp_host": ch.get("smtp_host", ""),
            "smtp_port": int(ch.get("smtp_port", 25)),
            "smtp_user": ch.get("smtp_user", ""),
            "from_addr": ch.get("from_addr", ch.get("smtp_user", "")),
            "to": [x.strip() for x in (ch.get("to", "") or "").split(",") if x.strip()],
            "use_tls": bool(ch.get("use_tls")),
        }
        pw = ch.get("smtp_password")
        if pw:  # 仅在有新密码时覆盖
            c["smtp_password"] = pw
        elif ch.get("smtp_host") in old_channels:
            c["smtp_password"] = old_channels[ch["smtp_host"]].get("smtp_password", "")
        new_channels.append(c)
    cfg = {
        "enabled": bool(cfg.get("enabled")),
        "on_success": bool(cfg.get("on_success")),
        "on_failure": bool(cfg.get("on_failure")),
        "channels": new_channels,
    }
    db.set_system_config("notify", json.dumps(cfg, ensure_ascii=False))
    return jsonify({"ok": True, "channels": len(new_channels), "enabled": cfg["enabled"]})


@api_bp.route("/notify-config/test", methods=["POST"])
@login_required
def test_notify_config():
    """使用当前已保存的邮件渠道发送一封测试邮件。

    用于在「保存通知配置」后立即验证 SMTP 是否通畅：避免配错
    （主机/端口/密码/授权码）后还要等下一次失败备份才察觉。
    """
    import json
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    raw = db.get_system_config("notify")
    if not raw:
        return jsonify({"ok": False, "error": "尚未保存通知配置"}), 400
    cfg = json.loads(raw)
    email_ch = next((c for c in cfg.get("channels", []) if c.get("type") == "email"), None)
    if not email_ch:
        return jsonify({"ok": False, "error": "未配置邮件渠道"}), 400
    # 基础字段校验
    missing = []
    if not email_ch.get("smtp_host"): missing.append("smtp_host")
    if not email_ch.get("smtp_user"): missing.append("smtp_user")
    if not email_ch.get("to"): missing.append("to")
    if missing:
        return jsonify({"ok": False, "error": f"字段缺失: {', '.join(missing)}"}), 400
    if not email_ch.get("smtp_password"):
        return jsonify({
            "ok": False,
            "error": "SMTP 密码未填写。QQ/163/Gmail 等需要的是「授权码」，不是登录密码。",
        }), 400
    # 组装并发送（HTML 卡片样式）
    title = "[数据备份管理平台] 通知测试邮件"
    text = (
        f"发送时间: {db.now_iso()}\n"
        f"发件人: {email_ch.get('from_addr') or email_ch.get('smtp_user')}\n"
        f"收件人: {', '.join(email_ch.get('to', []))}\n"
        f"SMTP 主机: {email_ch.get('smtp_host')}:{email_ch.get('smtp_port', 25)}\n"
        f"使用 TLS: {email_ch.get('use_tls')}\n\n"
        "如果你看到这封邮件，说明通知配置正确，备份告警/巡检异常会通过该渠道送达。\n"
        "若未收到，请检查：(1) 邮箱垃圾箱；(2) QQ/163 需要「授权码」而非登录密码；"
        "(3) QQ 邮箱请使用 smtp.qq.com:465 + SSL。"
    )
    try:
        from core.email_template import render_test_email
        html = render_test_email({
            "from_addr": email_ch.get("from_addr") or email_ch.get("smtp_user"),
            "smtp_user": email_ch.get("smtp_user"),
            "smtp_host": email_ch.get("smtp_host"),
            "smtp_port": email_ch.get("smtp_port", 25),
            "use_tls": email_ch.get("use_tls"),
            "to": email_ch.get("to", []),
        })
    except Exception:
        html = None
    # 多部分邮件：HTML 优先，纯文本兜底
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = email_ch.get("from_addr") or email_ch.get("smtp_user")
    msg["To"] = ", ".join(email_ch["to"])
    port = int(email_ch.get("smtp_port", 25))
    host = email_ch["smtp_host"]
    timeout = 15
    try:
        # QQ/Gmail/163 普遍 465 用 SSL；其它 port+use_tls 用 STARTTLS
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as s:
                s.login(email_ch["smtp_user"], email_ch["smtp_password"])
                s.sendmail(msg["From"], email_ch["to"], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                if email_ch.get("use_tls"):
                    s.starttls()
                    s.ehlo()
                if email_ch.get("smtp_user"):
                    s.login(email_ch["smtp_user"], email_ch["smtp_password"])
                s.sendmail(msg["From"], email_ch["to"], msg.as_string())
        db.add_log("INFO", "notify", f"test email sent to {', '.join(email_ch['to'])}")
        return jsonify({"ok": True, "message": "测试邮件发送成功，请检查收件箱（含垃圾邮件）"})
    except smtplib.SMTPAuthenticationError as e:
        return jsonify({
            "ok": False,
            "error": f"认证失败：账号或密码错误（QQ/163/Gmail 需使用「授权码」）。详情: {e}",
        }), 400
    except smtplib.SMTPConnectError as e:
        return jsonify({
            "ok": False,
            "error": f"无法连接 SMTP {host}:{port}。检查主机名/端口/防火墙/SSL。详情: {e}",
        }), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"发送失败: {e}"}), 500


# ------------------------- 存储池加密密钥 (KMS) -------------------------
@api_bp.route("/pool-crypto", methods=["GET"])
@login_required
def get_pool_crypto():
    """返回当前存储池加密密钥配置（不回显密钥明文）。"""
    import json
    raw = db.get_system_config("pool_crypto")
    if not raw:
        return jsonify({
            "ok": True,
            "configured": False,
            "mode": "local",
            "active": bool(os.environ.get("BACKUP_POOL_KEY")),
            "local_key_set": False,
            "kms_provider": "",
            "kms_endpoint": "",
            "kms_key_id": "",
            "kms_access_key": "",
            "kms_configured": False,
        })
    cfg = json.loads(raw)
    mode = cfg.get("mode", "local")
    return jsonify({
        "ok": True,
        "configured": True,
        "mode": mode,
        "active": True,
        "local_key_set": bool(cfg.get("pool_key")),
        "kms_provider": cfg.get("kms_provider", ""),
        "kms_endpoint": cfg.get("kms_endpoint", ""),
        "kms_key_id": cfg.get("kms_key_id", ""),
        "kms_access_key": cfg.get("kms_access_key", ""),
        "kms_configured": bool(cfg.get("kms_endpoint") and cfg.get("kms_key_id")),
    })


@api_bp.route("/pool-crypto", methods=["POST"])
@login_required
def save_pool_crypto():
    """保存存储池加密密钥配置（本地密钥库 / KMS）。

    body: {
      mode: "local" | "kms",
      pool_key: <明文主密钥，仅 local 模式，留空表示不修改>,
      kms_provider, kms_endpoint, kms_key_id, kms_access_key, kms_secret,
      local_fallback_key: <KMS 不可达时的回退主密钥>
    }
    """
    import json
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "local")
    cfg = {}
    if mode == "local":
        # 仅当填写了新密钥才更新（避免每次保存把密钥清空）
        new_key = (data.get("pool_key") or "").strip()
        old_raw = db.get_system_config("pool_crypto")
        old_key = ""
        if old_raw:
            try:
                old_key = json.loads(old_raw).get("pool_key", "")
            except Exception:
                old_key = ""
        cfg = {
            "mode": "local",
            "pool_key": new_key or old_key,
        }
        if not cfg["pool_key"]:
            return jsonify({"ok": False, "error": "本地密钥库模式下必须填写主密钥"}), 400
    else:
        # KMS 模式：保存连接参数，主密钥运行时从 KMS 拉取
        cfg = {
            "mode": "kms",
            "kms_provider": data.get("kms_provider", "custom"),
            "kms_endpoint": (data.get("kms_endpoint") or "").strip(),
            "kms_key_id": (data.get("kms_key_id") or "").strip(),
            "kms_access_key": (data.get("kms_access_key") or "").strip(),
            "kms_secret": (data.get("kms_secret") or "").strip(),
            "local_fallback_key": (data.get("local_fallback_key") or "").strip(),
        }
        if not cfg["kms_endpoint"] or not cfg["kms_key_id"]:
            return jsonify({"ok": False, "error": "KMS 模式需填写 endpoint 与 key_id"}), 400
    db.set_system_config("pool_crypto", json.dumps(cfg, ensure_ascii=False))
    # 保存后立即自检：用测试文件加密→解密，验证密钥真实可用
    try:
        from core import crypto_pool as cp
        st = cp.self_test()
        return jsonify({
            "ok": True,
            "self_test": st,
            "message": "存储池加密密钥已保存，自检通过（AES-256-GCM 可用）",
        })
    except Exception as e:
        logger.warning("pool_crypto 自检失败: %s", e)
        return jsonify({
            "ok": True,
            "self_test": {"ok": False, "error": str(e)},
            "message": "配置已保存，但密钥自检失败（加密可能未生效，请检查密钥/环境变量）",
        })


@api_bp.route("/pool-crypto/test", methods=["POST"])
@login_required
def test_pool_crypto():
    """测试 KMS 连通性（仅 KMS 模式有意义）。"""
    import json
    data = request.get_json(force=True, silent=True) or {}
    provider = (data.get("kms_provider") or "custom").lower()
    endpoint = (data.get("kms_endpoint") or "").strip()
    key_id = (data.get("kms_key_id") or "").strip()
    if not endpoint or not key_id:
        return jsonify({"ok": False, "error": "需填写 endpoint 与 key_id"}), 400
    cfg = {
        "mode": "kms",
        "kms_provider": provider,
        "kms_endpoint": endpoint,
        "kms_key_id": key_id,
        "kms_access_key": (data.get("kms_access_key") or "").strip(),
        "kms_secret": (data.get("kms_secret") or "").strip(),
    }
    from core import crypto_pool as cp
    pw = cp._resolve_kms_passphrase(cfg)
    if pw:
        return jsonify({"ok": True, "message": "KMS 连通成功，已取回主密钥明文"})
    return jsonify({
        "ok": False,
        "error": "KMS 不可达或凭证无效（请确认 endpoint/key_id/access_key/secret，或网络是否可达）",
    }), 400


# ======================================================================
# 外部 API 调用令牌管理（页面会话鉴权；供外部系统调用的 Bearer Token）
# ======================================================================
@api_bp.route("/tokens", methods=["GET"])
@login_required
def api_list_tokens():
    return jsonify({"success": True, "data": models.list_api_tokens()})


@api_bp.route("/tokens", methods=["POST"])
@login_required
def api_create_token():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "令牌名称必填"}), 400
    plain = models.create_api_token(name, created_by=session.get("user") or "system")
    return jsonify({"success": True, "token": plain,
                    "warning": "令牌明文仅此一次展示，请立即保存；平台仅存哈希"}), 201


@api_bp.route("/tokens/<int:token_id>", methods=["DELETE"])
@login_required
def api_revoke_token(token_id):
    ok = models.revoke_api_token(token_id)
    if not ok:
        return jsonify({"error": "令牌不存在"}), 404
    return jsonify({"success": True, "message": "令牌已吊销"})
