# -*- coding: utf-8 -*-
"""对象存储能力接口（断续前述 /api/v1 契约：双前缀、code/message/details 错误码）。

与既有任务**完全解耦**：这些接口只做"配置校验 / 探活 / 列举 / 预扫描"，
真正的数据动作仍然走通用的任务与备份记录接口，因此不会影响其他功能。

- ``GET  /api/v1/object-storage/providers``       厂商预设清单
- ``POST /api/v1/object-storage/test-connection`` 连接测试（真实列桶）
- ``POST /api/v1/object-storage/buckets``         列举可用桶
- ``POST /api/v1/object-storage/preview``         预扫描（对象数/容量/最大对象）
"""
from __future__ import annotations

import json
import os
import tarfile
import time

from auth import login_required
from core.objectstore.providers import build_client, providers
from flask import jsonify, request

from . import api_bp, contract

# 业务错误码（见 docs/api_conventions.md §3，六域分段）
_PARAM = "AIDBM-1001"        # 参数校验失败
_NOT_FOUND = "AIDBM-1004"    # 资源不存在
_STORAGE = "AIDBM-4002"      # 存储后端访问失败
_RESTORE = "AIDBM-3002"      # 恢复执行失败


def _payload() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _require(keys) -> str:
    body = _payload()
    missing = [k for k in keys if not body.get(k)]
    if missing:
        return "缺少必填项: %s" % ", ".join(missing)
    return ""


def _client_from_body():
    body = _payload()
    return build_client(body), body


@api_bp.route("/object-storage/providers", methods=["GET"])
@login_required
def api_os_providers():
    """各厂商接入预设（端点/区域/寻址风格/是否必填 Region）。"""
    return jsonify({
        "items": providers(),
        "total": len(providers()),
        "default": "minio",
        "note": "MinIO/AWS S3/腾讯云 COS 走标准 SigV4；阿里云 OSS 走自有 V4"
                "签名。对象存储侧不需要安装任何软件。",
    })


@api_bp.route("/object-storage/test-connection", methods=["POST"])
@login_required
def api_os_test():
    """真实发起一次列桶请求，验证端点/密钥/Region/签名。"""
    missing = _require(["endpoint"])
    if missing:
        return contract.error_response(_PARAM, missing)
    try:
        cli, body = _client_from_body()
        ok, msg = cli.test_connection()
    except Exception as e:
        return contract.error_response(
            _STORAGE,
            "连接失败: %s" % str(e)[:300],
            details={"endpoint": (_payload().get("endpoint") or "")})
    if not ok:
        return contract.error_response(_STORAGE, msg)
    return jsonify({"ok": True, "message": msg,
                    "provider": (body or {}).get("provider") or "minio"})


@api_bp.route("/object-storage/buckets", methods=["POST"])
@login_required
def api_os_buckets():
    """列举账号可见的桶（用于任务表单的下拉选择）。"""
    missing = _require(["endpoint"])
    if missing:
        return contract.error_response(_PARAM, missing)
    try:
        cli, _body = _client_from_body()
        items = cli.list_buckets()
    except Exception as e:
        return contract.error_response(
            _STORAGE, "列举桶失败: %s" % str(e)[:300])
    return jsonify({"items": items, "total": len(items)})


@api_bp.route("/object-storage/preview", methods=["POST"])
@login_required
def api_os_preview():
    """预扫描：在真正备份前给出对象数、总容量、最大对象与桶是否存在。

    一方面让用户对耗时有预期，另一方面也是"桶名写错/前缀写错"的提前暴露。
    """
    missing = _require(["endpoint", "bucket"])
    if missing:
        return contract.error_response(_PARAM, missing)
    payload = _payload()
    limit = int(payload.get("limit") or 0)     # 0 表示不限（全量列举）
    try:
        cli, _body = _client_from_body()
        if not cli.bucket_exists(payload["bucket"]):
            return contract.error_response(
                _NOT_FOUND,
                "桶不存在: %s" % payload["bucket"])
        count = total = biggest = 0
        sample = []
        prefix = payload.get("prefix") or ""
        iterator = (cli.iter_object_versions(payload["bucket"], prefix)
                    if payload.get("include_versions")
                    else cli.iter_objects(payload["bucket"], prefix))
        for item in iterator:
            if item.get("delete_marker"):
                continue
            count += 1
            total += int(item.get("size") or 0)
            biggest = max(biggest, int(item.get("size") or 0))
            if len(sample) < 20:
                sample.append(item["key"])
            if limit and count >= limit:
                break
    except Exception as e:
        return contract.error_response(
            _STORAGE, "预扫描失败: %s" % str(e)[:300])
    truncated = bool(limit and count >= limit)
    return jsonify({
        "bucket": payload["bucket"], "prefix": prefix,
        "object_count": count, "total_bytes": total, "max_object_bytes": biggest,
        "sample_keys": sample,
        "truncated": truncated,
        "note": ("结果按 limit=%d 截断，仅用于估算" % limit) if truncated else "",
    })


def _record_manifest(record_id: int) -> tuple[dict, dict]:
    """读取备份记录对应的产物清单；(record, manifest)。"""
    from core import models
    record = models.get_record(record_id)
    if not record:
        raise LookupError("备份记录不存在: %s" % record_id)
    path = record.get("backup_path") or ""
    if not path or not os.path.exists(path):
        raise LookupError("备份产物不可访问（可能已被回收或未在同机）: %s" % path)
    with tarfile.open(path, "r:*") as tar:
        for m in tar.getmembers():
            if os.path.basename(m.name) == "manifest.json":
                man = json.loads(tar.extractfile(m).read().decode("utf-8"))
                return record, man
    raise LookupError("备份包不是对象存储产物（缺少 manifest.json）: %s"
                      % os.path.basename(path))


@api_bp.route("/object-storage/records/<int:record_id>/objects", methods=["GET"])
@login_required
def api_os_record_objects(record_id: int):
    """归档内对象清单 —— 对象级（颗粒级）恢复的选择源。"""
    try:
        record, man = _record_manifest(record_id)
    except LookupError as e:
        return contract.error_response(_NOT_FOUND, str(e))
    except Exception as e:
        return contract.error_response(
            _STORAGE, "解析备份包失败: %s" % str(e)[:300])
    page, size, _offset = contract.pagination_args(max_size=500)
    objects = man.get("objects") or []
    start = (page - 1) * size
    window = objects[start:start + size]
    return jsonify({
        "items": window, "total": len(objects), "page": page, "size": size,
        "has_more": start + len(window) < len(objects),
        "manifest": {k: man.get(k) for k in (
            "backup_type", "bucket", "provider", "created_at", "object_count",
            "object_bytes", "deleted_count", "parent")},
        "record_id": record_id,
        "backup_path": record.get("backup_path"),
    })


@api_bp.route("/object-storage/restores", methods=["POST"])
@login_required
def api_os_restore():
    """对象存储恢复（支持对象级细粒度恢复）。

    body: ``record_id`` 必填；``keys`` 为空表示整包恢复，否则只恢复指定对象；
    ``target_bucket`` / ``target_prefix`` / ``overwrite``(always|if_newer|never)
    / ``apply_deleted`` / ``workers``。同步执行并返回逐项统计。
    """
    from core import models
    from core.engines import get_engine
    payload = _payload()
    record_id = payload.get("record_id")
    if not record_id:
        return contract.error_response(_PARAM,
                                      "缺少必填项: record_id")
    try:
        _record, man = _record_manifest(int(record_id))
    except LookupError as e:
        return contract.error_response(_NOT_FOUND, str(e))
    except Exception as e:
        return contract.error_response(
            _STORAGE, "解析备份包失败: %s" % str(e)[:300])

    try:
        record = models.get_record(int(record_id))
        task = models.get_task(record.get("task_id"), include_secret=True)
        if not task:
            raise LookupError("任务不存在: %s" % record.get("task_id"))
        import config
        engine = get_engine(task.get("db_type") or "object_storage", task,
                            config.BACKUP_ROOT)
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        res = engine.restore(
            record.get("backup_path"),
            target_bucket=payload.get("target_bucket"),
            target_prefix=payload.get("target_prefix"),
            overwrite=payload.get("overwrite"),
            apply_deleted=payload.get("apply_deleted"),
            workers=payload.get("workers"),
            create_bucket=payload.get("create_bucket"),
            restore_keys=payload.get("keys") or None,
        )
        models.create_restore({
            "task_id": task.get("id"), "record_id": int(record_id),
            "target_host": payload.get("target_bucket") or man.get("bucket") or "",
            "target_db": payload.get("target_prefix") or "",
            "started_at": started, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success" if res.success else "failed",
            "message": (res.message or "")[:500],
            "operator": getattr(request, "user", None) or "",
        })
    except LookupError as e:
        return contract.error_response(_NOT_FOUND, str(e))
    except Exception as e:
        return contract.error_response(
            _RESTORE, "恢复失败: %s" % str(e)[:300])
    return jsonify({
        "success": res.success, "message": res.message,
        "written": res.size_bytes, "duration_sec": res.duration_sec,
        "target_bucket": res.target_ref or payload.get("target_bucket"),
        "granular": bool(payload.get("keys")),
        "stderr": res.stderr or "",
    }), 201 if res.success else 500


def _resume_context(task_id: int):
    """构造某任务的断点目录与 scope，供查询/清除共用。"""
    from core import models
    from core.engines import get_engine
    from core.objectstore.providers import task_cfg
    from core.objectstore import resume as osr
    import config
    task = models.get_task(task_id)
    if not task or task.get("db_type") != "object_storage":
        raise LookupError("对象存储备份任务不存在: %s" % task_id)
    cfg = task_cfg(task)
    engine = get_engine("object_storage", task, config.BACKUP_ROOT)
    bt = "incremental" if task.get("backup_type") == "incremental" else "full"
    scope = osr.scope_digest(task_id, task.get("name"), cfg, bt)
    return engine._output_dir(), scope, cfg, osr


@api_bp.route("/object-storage/tasks/<int:task_id>/resume-status", methods=["GET"])
@login_required
def api_os_resume_status(task_id):
    """断点现状：已完成对象数、已下载字节、part 残件、TTL 与是否可续。"""
    try:
        out_dir, scope, cfg, osr = _resume_context(task_id)
    except LookupError as e:
        return contract.error_response(_NOT_FOUND, str(e))
    st = osr.status(out_dir, scope, cfg.get("resume_ttl_hours") or 72)
    st.update({"ok": True, "task_id": task_id,
               "resume_enabled": bool(cfg.get("resume", True)),
               "scope": scope})
    return jsonify(st)


@api_bp.route("/object-storage/tasks/<int:task_id>/resume", methods=["DELETE"])
@login_required
def api_os_resume_clear(task_id):
    """放弃断点：清空 stage，下次备份从头全量传输。"""
    try:
        out_dir, _scope, _cfg, osr = _resume_context(task_id)
    except LookupError as e:
        return contract.error_response(_NOT_FOUND, str(e))
    osr.clear(osr.stage_path(out_dir))
    return jsonify({"ok": True, "task_id": task_id,
                    "message": "断点已清除，下次备份将从头开始"})
