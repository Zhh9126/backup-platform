# -*- coding: utf-8 -*-
"""数据库部署 API：增删改查、立即执行、安装包上传。"""
import os
import json
import uuid
from flask import request, jsonify, current_app
from werkzeug.utils import secure_filename

from auth import login_required
from core import models, deploy as deploy_engine
from core.db import human_size
from . import api_bp


# 允许的安装包扩展名
ALLOWED_PKG_EXTS = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".zip", ".iso", ".bin", ".run", ".tar")


def _dameng_password_error(pw: str):
    """达梦 dminit 的口令硬校验前移到接口层。

    dminit 要求 SYSDBA/SYSAUDITOR 口令 8-48 位且"大写+小写+数字"组合，
    不合规会直接 fail to init db；在下发前拦掉，避免用户等半分钟后才看到失败。
    """
    if len(pw) < 8 or len(pw) > 48:
        return "达梦 SYSDBA 口令长度必须 8-48 位（当前 %d 位），dminit 会拒绝初始化" % len(pw)
    if not (any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)):
        return "达梦 SYSDBA 口令必须同时包含大写字母、小写字母和数字，dminit 会拒绝初始化"
    return None


@api_bp.route("/deploy/upload", methods=["POST"])
@login_required
def upload_package():
    """上传安装包到平台暂存目录，部署时由平台 SFTP 推送至目标主机。"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "未收到文件"}), 400
    fname = secure_filename(f.filename or "package.bin")
    ext = ""
    for e in sorted(ALLOWED_PKG_EXTS, key=lambda x: -len(x)):
        if fname.lower().endswith(e):
            ext = e
            break
    if not ext:
        return jsonify({"error": f"不支持的文件类型: {fname}（支持 .tar.gz/.tgz/.tar.xz/.zip/.iso/.bin/.run）"}), 400
    pkg_dir = os.path.join(current_app.config.get("BACKUP_ROOT", "backups"), "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    unique = f"{uuid.uuid4().hex[:8]}_{fname}"
    dest = os.path.join(pkg_dir, unique)
    f.save(dest)
    size = os.path.getsize(dest)
    return jsonify({
        "ok": True, "path": dest, "filename": fname,
        "size": size, "size_human": human_size(size),
    })


@api_bp.route("/deploy", methods=["GET"])
@login_required
def list_deployments():
    return jsonify(models.list_deployments())


@api_bp.route("/deploy", methods=["POST"])
@login_required
def create_deployment():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "部署名称必填"}), 400
    if not data.get("db_type"):
        return jsonify({"error": "数据库类型必填"}), 400
    if not data.get("host_id") and not data.get("direct_host"):
        return jsonify({"error": "请选择已纳管主机或直接输入 IP/账号/密码"}), 400
    # 初始化/校验 config_json
    cfg = {}
    try:
        cfg = json.loads(data.get("config_json") or "{}")
    except Exception:
        cfg = {}
    data["config_json"] = json.dumps(cfg, ensure_ascii=False)
    data["direct_port"] = data.get("direct_port") or 22
    data["direct_user"] = data.get("direct_user") or "root"
    if data.get("db_type") == "dameng":
        pw = data.get("password") or cfg.get("dm_sysdba") or ""
        if pw:
            err = _dameng_password_error(pw)
            if err:
                return jsonify({"error": err}), 400
    dep_id = models.create_deployment(data)
    return jsonify({"id": dep_id, "ok": True}), 201


@api_bp.route("/deploy/<int:dep_id>", methods=["GET"])
@login_required
def get_deployment(dep_id):
    d = models.get_deployment(dep_id)
    if not d:
        return jsonify({"error": "部署记录不存在"}), 404
    if d.get("password"):
        d["password"] = "***"
    return jsonify(d)


@api_bp.route("/deploy/<int:dep_id>", methods=["PUT"])
@login_required
def update_deployment(dep_id):
    data = request.get_json(force=True, silent=True) or {}
    existing = models.get_deployment(dep_id)
    if not existing:
        return jsonify({"error": "部署记录不存在"}), 404
    # 部分更新语义：PUT 只带少数字段时，未提供的字段不得被默认值覆盖。
    # 旧实现无条件写回 config_json（未传即写 "{}"）并把 direct_user 强制成 root，
    # 导致"改个口令"会把 dm_instance / version 等配置清空（171 实测）。
    cfg = {}
    if "config_json" in data:
        try:
            cfg = json.loads(data.get("config_json") or "{}")
        except Exception:
            cfg = {}
        data["config_json"] = json.dumps(cfg, ensure_ascii=False)
    if "direct_port" in data:
        data["direct_port"] = data.get("direct_port") or 22
    if "direct_user" in data:
        data["direct_user"] = data.get("direct_user") or "root"
    _db_type = data.get("db_type") or existing.get("db_type")
    if _db_type == "dameng":
        pw = data.get("password") or cfg.get("dm_sysdba") or ""
        if pw:
            err = _dameng_password_error(pw)
            if err:
                return jsonify({"error": err}), 400
    models.update_deployment(dep_id, data)
    return jsonify({"ok": True})


@api_bp.route("/deploy/<int:dep_id>", methods=["DELETE"])
@login_required
def delete_deployment(dep_id):
    if not models.get_deployment(dep_id):
        return jsonify({"error": "部署记录不存在"}), 404
    models.delete_deployment(dep_id)
    return jsonify({"ok": True})


@api_bp.route("/deploy/<int:dep_id>/run", methods=["POST"])
@login_required
def run_deployment(dep_id):
    d = models.get_deployment(dep_id)
    if not d:
        return jsonify({"error": "部署记录不存在"}), 404
    if d.get("status") == "running":
        return jsonify({"error": "正在执行中，请稍候"}), 409
    deploy_engine.run_deployment_async(dep_id)
    return jsonify({"accepted": True, "dep_id": dep_id}), 202


@api_bp.route("/deploy/<int:dep_id>/log", methods=["GET"])
@login_required
def get_deploy_log(dep_id):
    d = models.get_deployment(dep_id)
    if not d:
        return jsonify({"error": "部署记录不存在"}), 404
    return jsonify({"log": d.get("log_output", ""), "status": d.get("status")})
