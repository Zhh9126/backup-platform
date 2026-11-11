# -*- coding: utf-8 -*-
"""群晖 DSM 纳管 REST API。

路由分三组：

* 设备：``/api/synology/devices`` CRUD（密码 SM4 加密、列表不回显）；
* 探测/资产：``/test``（未保存先测）、``/<id>/test``、``/shares``、
  ``/browse``、``/assets``、``/scenarios``（场景矩阵与交付模板）；
* 一键保护：``/<id>/quick-task`` 从勾选的共享目录生成
  ``db_type=synology`` 的**全量 + 组合增量**备份任务。

所有探测都走真实 DSM Web API；失败返回 400 + 真实原因，不把连不上包装成成功。
"""
import json

from flask import jsonify, request

from auth import login_required, verify_code_gate
from core import synology as syn
from . import api_bp

devices = syn.devices


def _payload() -> dict:
    return request.get_json(force=True, silent=True) or {}


def _err(e, status=400):
    return jsonify({"error": str(e).strip() or type(e).__name__}), status


def _dev_or_404(dev_id):
    d = devices.get_device(dev_id)
    if not d:
        return None
    return d


# ---------------------------------------------------------------- 设备 CRUD
@api_bp.route("/synology/devices", methods=["GET"])
@login_required
def syn_list_devices():
    return jsonify(devices.list_devices(include_secret=False))


@api_bp.route("/synology/devices", methods=["POST"])
@login_required
def syn_create_device():
    data = _payload()
    try:
        did = devices.create_device(data)
    except devices.DeviceError as e:
        return _err(e)
    return jsonify({"id": did, "ok": True}), 201


@api_bp.route("/synology/devices/<int:dev_id>", methods=["GET"])
@login_required
def syn_get_device(dev_id):
    d = _dev_or_404(dev_id)
    if not d:
        return jsonify({"error": "群晖设备不存在"}), 404
    return jsonify(d)


@api_bp.route("/synology/devices/<int:dev_id>", methods=["PUT"])
@login_required
def syn_update_device(dev_id):
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    try:
        devices.update_device(dev_id, _payload())
    except devices.DeviceError as e:
        return _err(e)
    return jsonify({"ok": True})


@api_bp.route("/synology/devices/<int:dev_id>", methods=["DELETE"])
@login_required
def syn_delete_device(dev_id):
    gate = verify_code_gate(request.get_json(silent=True) or {})
    if gate:
        return jsonify(gate[0]), gate[1]
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    # 有任务引用的设备不允许直接删：否则备份任务会变成找不到凭据的孤儿
    linked = _linked_task_ids(dev_id)
    if linked:
        return jsonify({
            "error": "该设备仍被 %d 个备份任务引用（%s），请先删除或改绑这些任务"
                     % (len(linked), ", ".join(str(i) for i in linked[:10])),
            "task_ids": linked,
        }), 409
    devices.delete_device(dev_id)
    return jsonify({"ok": True})


def _linked_task_ids(dev_id: int) -> list:
    out = []
    try:
        from core import models
        for t in models.list_tasks(db_type="synology"):
            raw = t.get("extra_options") or ""
            try:
                extra = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                extra = {}
            if int(extra.get("synology_device_id") or 0) == int(dev_id):
                out.append(t.get("id"))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 探测 / 资产
@api_bp.route("/synology/test", methods=["POST"])
@login_required
def syn_test_unsaved():
    """未保存先测：body 直接是一份设备配置（可不传 id），OTP 只在此临时透传。"""
    data = _payload()
    if data.get("id"):
        return jsonify(devices.probe_device(int(data["id"]),
                                            otp_code=data.get("otp_code") or ""))
    out = devices.probe_device(dict(data), otp_code=data.get("otp_code") or "",
                               persist=False)
    return (jsonify(out), 200) if out.get("ok") else (jsonify(out), 400)


@api_bp.route("/synology/devices/<int:dev_id>/test", methods=["POST"])
@login_required
def syn_test_device(dev_id):
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    out = devices.probe_device(dev_id,
                               otp_code=(_payload().get("otp_code") or ""))
    return (jsonify(out), 200) if out.get("ok") else (jsonify(out), 400)


@api_bp.route("/synology/devices/<int:dev_id>/shares", methods=["GET"])
@login_required
def syn_list_shares(dev_id):
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    try:
        shares = devices.list_shares(dev_id)
    except (syn.DSMError, devices.DeviceError) as e:
        return _err(e)
    return jsonify({"ok": True, "count": len(shares), "shares": shares,
                    "note": "来源：SYNO.FileStation.List（官方公开 API）"})


@api_bp.route("/synology/devices/<int:dev_id>/browse", methods=["GET"])
@login_required
def syn_browse(dev_id):
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    try:
        items = devices.browse_folder(dev_id, request.args.get("path") or "")
    except (syn.DSMError, devices.DeviceError) as e:
        return _err(e)
    return jsonify({"ok": True, "count": len(items), "items": items})


@api_bp.route("/synology/devices/<int:dev_id>/assets", methods=["POST"])
@login_required
def syn_assets(dev_id):
    if not _dev_or_404(dev_id):
        return jsonify({"error": "群晖设备不存在"}), 404
    data = _payload()
    try:
        out = devices.walk_assets(
            dev_id, data.get("path") or "",
            max_files=int(data.get("max_files") or 200000))
    except (syn.DSMError, devices.DeviceError, ValueError) as e:
        return _err(e)
    return jsonify({"ok": True, **out})


@api_bp.route("/synology/scenarios", methods=["GET"])
@login_required
def syn_scenarios():
    """场景矩阵：iSCSI/SAN、Initiator、集群红线、文件协议差异与交付信息模板。"""
    return jsonify({"ok": True, **devices.scenarios()})


@api_bp.route("/synology/tasks", methods=["GET"])
@login_required
def syn_tasks():
    from core import models
    did = request.args.get("device_id")
    out = []
    for t in models.list_tasks(db_type="synology"):
        raw = t.get("extra_options") or ""
        try:
            extra = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (ValueError, TypeError):
            extra = {}
        ref = int(extra.get("synology_device_id") or 0)
        if did and ref != int(did):
            continue
        out.append({
            "id": t.get("id"), "name": t.get("name"),
            "biz_system": t.get("biz_system"),
            "device_id": ref, "paths": extra.get("synology_paths") or [],
            "mixed_backup": int(t.get("mixed_backup") or 0),
            "enabled": int(t.get("enabled") or 0),
            "last_status": t.get("last_status"),
            "last_run_at": t.get("last_run_at"),
            "created_at": t.get("created_at"),
        })
    return jsonify(out)


# ---------------------------------------------------------------- 一键保护
@api_bp.route("/synology/devices/<int:dev_id>/quick-task", methods=["POST"])
@login_required
def syn_quick_task(dev_id):
    """从勾选的共享目录一键生成全量 + 增量备份任务。

    ``strategy=mixed`` （默认）：``mixed_backup=1``，全量走 cron、增量走
    interval 双调度，与既有组合策略调度器（``task_<id>_full`` /
    ``task_<id>_incremental``）完全兼容；``strategy=full`` 只建全量 cron 任务。
    """
    dev = _dev_or_404(dev_id)
    if not dev:
        return jsonify({"error": "群晖设备不存在"}), 404
    data = _payload()
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]
    paths = [str(p).strip() for p in paths if str(p).strip().startswith("/")]
    if not paths:
        return jsonify({"error": "请至少选择一个共享文件夹路径"}), 400
    strategy = (data.get("strategy") or "mixed").strip().lower()
    if strategy not in ("mixed", "full"):
        return jsonify({"error": "strategy 只能是 mixed 或 full"}), 400
    full_cron = str(data.get("full_cron") or "0 2 * * 0").strip()
    # 显式传 0 也要钳制到下限（60 秒），不能被 `or 默认值` 吞成默认 60 分钟
    _inc_raw = data.get("incremental_minutes")
    try:
        inc_min = 60 if _inc_raw in (None, "") else max(1, int(_inc_raw))
    except (TypeError, ValueError):
        inc_min = 60
    _ret_raw = data.get("retention_days")
    try:
        retention = 30 if _ret_raw in (None, "") else max(1, int(_ret_raw))
    except (TypeError, ValueError):
        retention = 30

    extra = {
        "synology_device_id": int(dev_id),
        "synology_paths": paths,
        "synology_secure": bool(dev.get("secure")),
        "synology_verify_ssl": bool(dev.get("verify_ssl")),
        "synology_resume": True,
        "synology_max_files": int(data.get("max_files") or 200000),
        "synology_excludes": data.get("excludes") or [],
    }
    payload = {
        "name": str(data.get("name") or ("群晖-%s-全量+增量" % dev.get("name"))),
        "biz_system": str(data.get("biz_system") or dev.get("name") or "群晖NAS"),
        "db_type": "synology",
        # 凭据只引用设备表，不在任务里复制一份密码
        "host": dev.get("hostname"), "port": dev.get("port"),
        "username": dev.get("username"), "password": "",
        "db_name": ",".join(paths),
        "backup_type": "full", "backup_mode": "logical",
        "schedule_type": "cron", "cron_expr": full_cron,
        "interval_minutes": inc_min,
        "enabled": 0 if data.get("enabled") in (0, False, "0") else 1,
        "retention_days": retention, "compress": 1,
        "extra_options": extra,
    }
    if strategy == "mixed":
        payload.update({
            "mixed_backup": 1,
            "full_schedule_type": "cron", "full_schedule_expr": full_cron,
            "incremental_schedule_type": "interval",
            "incremental_schedule_expr": str(inc_min),
        })
    from core import models
    from core import scheduler
    from core import license as license_mod
    try:
        tid = models.create_task(payload)
    except license_mod.QuotaExceededError as qe:
        return jsonify({"error": str(qe), "quota_exceeded": True}), 403
    except Exception as e:
        return _err(e, 400)
    scheduler.reload_scheduler()
    out = {"id": tid, "ok": True, "strategy": strategy, "paths": paths,
           "mixed_backup": 1 if strategy == "mixed" else 0,
           "incremental_minutes": inc_min, "full_cron": full_cron,
           "note": "组合策略：全量 %s + 每 %d 分钟增量；凭据引用纳管设备 %s"
                   % (full_cron, inc_min, dev_id)}
    if data.get("run_now"):
        try:
            rec = scheduler.run_task_now(tid, backup_type="full",
                                         operator="synology-quick")
            out["run"] = {"accepted": True,
                          "status": (rec or {}).get("status") or "running"}
        except Exception as e:
            out["run"] = {"accepted": False, "error": str(e)}
    return jsonify(out), 201
