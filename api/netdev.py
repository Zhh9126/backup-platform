# -*- coding: utf-8 -*-
"""网络设备配置备份 REST API。

路由列表：
- 厂商与预设：
  - GET    /api/netdev/presets                    厂商及指令预设与设备类型
- 设备管理：
  - GET    /api/netdev/devices                    网络设备列表
  - POST   /api/netdev/devices                    新增网络设备
  - GET    /api/netdev/devices/<id>               获取网络设备详情
  - PUT    /api/netdev/devices/<id>               更新网络设备
  - DELETE /api/netdev/devices/<id>               删除网络设备
  - POST   /api/netdev/devices/<id>/test          测试已保存设备的 SSH 连接
  - POST   /api/netdev/devices/batch-backup       批量选择备份网络设备
  - POST   /api/netdev/devices/batch-test         批量探测 SSH 连通性
- 连通性与探测：
  - POST   /api/netdev/test-ssh                   测试临时 SSH 连接（未保存前校验）
  - POST   /api/netdev/test-ftp                   测试远程 FTP 连接
- 统一凭据令牌管理（Token）：
  - GET    /api/netdev/tokens                     获取凭据令牌列表
  - POST   /api/netdev/tokens                     创建凭据令牌
  - GET    /api/netdev/tokens/<id>                获取凭据令牌详情
  - PUT    /api/netdev/tokens/<id>                更新凭据令牌
  - DELETE /api/netdev/tokens/<id>                删除凭据令牌
- 自动备份与定时调度：
  - GET    /api/netdev/schedule/config            获取自动备份配置
  - POST   /api/netdev/schedule/config            设置自动备份配置
  - POST   /api/netdev/schedule/trigger           立即触发自动排程备份检查
- Excel 导入与模板导出：
  - GET    /api/netdev/template/excel             下载网络设备批量导入 Excel 模板
  - POST   /api/netdev/import                     批量导入 Excel 文件
- 备份执行与管理：
  - POST   /api/netdev/devices/<id>/backup        执行配置备份（强制 SSH 连通才允许）
  - GET    /api/netdev/backups                    获取配置备份历史记录
  - GET    /api/netdev/backups/<id>               获取单次备份详情
  - GET    /api/netdev/backups/<id>/content       获取备份配置文本内容（在线预览）
  - GET    /api/netdev/backups/<id>/download      下载备份配置文件
  - POST   /api/netdev/backups/diff               对比两份配置备份差异
  - DELETE /api/netdev/backups/<id>               删除单次备份记录与文件
"""
import io
from flask import request, jsonify, send_file, session

from auth import login_required
import core.netdev as netdev
from . import api_bp


# =========================================================================
# 预设与设备
# =========================================================================

@api_bp.route("/netdev/presets", methods=["GET"])
@login_required
def netdev_presets():
    """获取厂商指令预设与设备类型。"""
    return jsonify({
        "device_types": netdev.DEVICE_TYPES,
        "vendor_presets": netdev.VENDOR_PRESETS,
    })


@api_bp.route("/netdev/devices", methods=["GET"])
@login_required
def list_netdev_devices():
    """获取所有网络设备。"""
    return jsonify(netdev.list_devices(include_secret=False))


@api_bp.route("/netdev/devices", methods=["POST"])
@login_required
def create_netdev_device():
    """新增网络设备。"""
    data = request.get_json(silent=True) or {}
    try:
        dev_id = netdev.create_device(data)
        if data.get("auto_test"):
            test_res = netdev.test_ssh_connection(dev_id)
            return jsonify({"ok": True, "id": dev_id, "test": test_res}), 201
        return jsonify({"ok": True, "id": dev_id}), 201
    except netdev.license_mod.QuotaExceededError as qe:
        return jsonify({"ok": False, "error": str(qe), "quota_exceeded": True}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"创建设备失败: {e}"}), 500


@api_bp.route("/netdev/devices/<int:device_id>", methods=["GET"])
@login_required
def get_netdev_device(device_id: int):
    """获取网络设备详情。"""
    d = netdev.get_device(device_id, include_secret=False)
    if not d:
        return jsonify({"error": "设备不存在"}), 404
    return jsonify(d)


@api_bp.route("/netdev/devices/<int:device_id>", methods=["PUT"])
@login_required
def update_netdev_device(device_id: int):
    """更新网络设备。"""
    data = request.get_json(silent=True) or {}
    ok = netdev.update_device(device_id, data)
    if not ok:
        return jsonify({"error": "设备不存在"}), 404
    return jsonify({"ok": True})


@api_bp.route("/netdev/devices/<int:device_id>", methods=["DELETE"])
@login_required
def delete_netdev_device(device_id: int):
    """删除网络设备。"""
    ok = netdev.delete_device(device_id)
    return jsonify({"ok": ok})


@api_bp.route("/netdev/devices/<int:device_id>/test", methods=["POST"])
@login_required
def test_saved_device_ssh(device_id: int):
    """对已纳管的网络设备进行真实 SSH 连通性探测。"""
    res = netdev.test_ssh_connection(device_id)
    return jsonify(res)


@api_bp.route("/netdev/devices/batch-test", methods=["POST"])
@login_required
def batch_test_devices():
    """批量对所选网络设备进行 SSH 连通性探测。"""
    data = request.get_json(silent=True) or {}
    dev_ids = data.get("device_ids") or []
    if not dev_ids:
        return jsonify({"ok": False, "error": "未提供要探测的设备 ID 列表"}), 400

    results = []
    connected_count = 0
    for did in dev_ids:
        r = netdev.test_ssh_connection(int(did))
        if r.get("ok"):
            connected_count += 1
        results.append({"device_id": did, "ok": r.get("ok"), "message": r.get("message")})

    return jsonify({
        "ok": True,
        "total": len(dev_ids),
        "connected_count": connected_count,
        "results": results
    })


@api_bp.route("/netdev/devices/batch-backup", methods=["POST"])
@login_required
def batch_backup_devices():
    """批量选择设备执行配置备份。"""
    data = request.get_json(silent=True) or {}
    dev_ids = data.get("device_ids") or []
    if not dev_ids:
        return jsonify({"ok": False, "error": "未选择任何要备份的网络设备"}), 400

    user = session.get("user") or {}
    operator = user.get("username") or user.get("display_name") or "admin"
    summary = netdev.batch_backup([int(x) for x in dev_ids], operator=operator)
    return jsonify({"ok": True, **summary})


# =========================================================================
# 临时探测
# =========================================================================

@api_bp.route("/netdev/test-ssh", methods=["POST"])
@login_required
def test_temp_ssh():
    """在表单未保存时预先测试 SSH 连接。"""
    data = request.get_json(silent=True) or {}
    host = data.get("host")
    username = data.get("username")
    token_id = data.get("token_id")
    if not host:
        return jsonify({"ok": False, "message": "主机 IP 不能为空"}), 400
    if not username and not token_id:
        return jsonify({"ok": False, "message": "SSH 用户名或认证令牌不能为空"}), 400

    res = netdev.test_ssh_connection(data)
    return jsonify(res)


@api_bp.route("/netdev/test-ftp", methods=["POST"])
@login_required
def test_ftp():
    """测试远程 FTP 服务器连接。"""
    data = request.get_json(silent=True) or {}
    res = netdev.test_ftp_connection(
        ftp_host=data.get("ftp_host", ""),
        ftp_port=int(data.get("ftp_port") or 21),
        ftp_user=data.get("ftp_user", ""),
        ftp_password=data.get("ftp_password", ""),
        ftp_path=data.get("ftp_path", "/"),
    )
    return jsonify(res)


# =========================================================================
# 凭据令牌管理（Tokens）
# =========================================================================

@api_bp.route("/netdev/tokens", methods=["GET"])
@login_required
def list_netdev_tokens():
    """获取所有统一凭据令牌列表。"""
    return jsonify(netdev.list_tokens(include_secret=False))


@api_bp.route("/netdev/tokens", methods=["POST"])
@login_required
def create_netdev_token():
    """新增统一凭据令牌。"""
    data = request.get_json(silent=True) or {}
    try:
        tok_id = netdev.create_token(data)
        return jsonify({"ok": True, "id": tok_id}), 201
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"创建令牌失败: {e}"}), 500


@api_bp.route("/netdev/tokens/<int:token_id>", methods=["GET"])
@login_required
def get_netdev_token(token_id: int):
    """获取凭据令牌详情。"""
    t = netdev.get_token(token_id, include_secret=False)
    if not t:
        return jsonify({"error": "令牌不存在"}), 404
    return jsonify(t)


@api_bp.route("/netdev/tokens/<int:token_id>", methods=["PUT"])
@login_required
def update_netdev_token(token_id: int):
    """更新凭据令牌。"""
    data = request.get_json(silent=True) or {}
    ok = netdev.update_token(token_id, data)
    if not ok:
        return jsonify({"error": "令牌不存在"}), 404
    return jsonify({"ok": True})


@api_bp.route("/netdev/tokens/<int:token_id>", methods=["DELETE"])
@login_required
def delete_netdev_token(token_id: int):
    """删除凭据令牌。"""
    ok = netdev.delete_token(token_id)
    return jsonify({"ok": ok})


# =========================================================================
# 定时自动备份调度配置
# =========================================================================

@api_bp.route("/netdev/schedule/config", methods=["GET"])
@login_required
def get_netdev_schedule_config():
    """获取网络设备自动备份排程配置。"""
    return jsonify(netdev.get_auto_backup_config())


@api_bp.route("/netdev/schedule/config", methods=["POST"])
@login_required
def set_netdev_schedule_config():
    """设置网络设备自动备份排程配置。"""
    data = request.get_json(silent=True) or {}
    netdev.set_auto_backup_config(data)
    return jsonify({"ok": True})


@api_bp.route("/netdev/schedule/trigger", methods=["POST"])
@login_required
def trigger_scheduled_backups():
    """手动立即触发自动备份排程检查。"""
    res = netdev.run_scheduled_backups()
    return jsonify({"ok": True, **res})


# =========================================================================
# Excel 导入与模板导出
# =========================================================================

@api_bp.route("/netdev/template/excel", methods=["GET"])
@login_required
def download_excel_template():
    """下载网络设备批量导入的 Excel 模板。"""
    bio = netdev.generate_excel_template()
    return send_file(
        bio,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="网络设备批量导入模板.xlsx"
    )


@api_bp.route("/netdev/import", methods=["POST"])
@login_required
def import_devices():
    """上传 Excel 文件批量导入网络设备。"""
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "请选择要上传的 Excel 文件 (.xlsx)"}), 400

    filename = file.filename or ""
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        return jsonify({"ok": False, "error": "文件格式不正确，请上传 .xlsx 格式文件"}), 400

    auto_test = request.form.get("auto_test", "true").lower() == "true"
    try:
        content = file.read()
        summary = netdev.import_devices_from_excel(content, auto_test=auto_test)
        return jsonify({"ok": True, **summary})
    except Exception as e:
        return jsonify({"ok": False, "error": f"解析并导入 Excel 异常: {e}"}), 500


# =========================================================================
# 备份执行与记录管理
# =========================================================================

@api_bp.route("/netdev/devices/<int:device_id>/backup", methods=["POST"])
@login_required
def run_netdev_backup(device_id: int):
    """执行配置备份（前置强校验：必须 SSH 连接成功才允许备份）。"""
    user = session.get("user") or {}
    operator = user.get("username") or user.get("display_name") or "admin"
    try:
        result = netdev.execute_backup(device_id, operator=operator)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"备份过程发生内部异常: {e}"}), 500


@api_bp.route("/netdev/backups", methods=["GET"])
@login_required
def list_netdev_backups():
    """获取网络设备配置备份记录。"""
    dev_id = request.args.get("device_id", type=int)
    limit = request.args.get("limit", default=100, type=int)
    return jsonify(netdev.list_backups(device_id=dev_id, limit=limit))


@api_bp.route("/netdev/backups/<int:backup_id>", methods=["GET"])
@login_required
def get_netdev_backup(backup_id: int):
    """获取单次备份详情。"""
    b = netdev.get_backup(backup_id)
    if not b:
        return jsonify({"error": "备份记录不存在"}), 404
    return jsonify(b)


@api_bp.route("/netdev/backups/<int:backup_id>/content", methods=["GET"])
@login_required
def get_netdev_backup_content(backup_id: int):
    """获取备份配置文件的完整内容（供前端弹窗高亮预览）。"""
    try:
        content = netdev.get_backup_file_content(backup_id)
        b = netdev.get_backup(backup_id)
        return jsonify({
            "ok": True,
            "backup_id": backup_id,
            "file_name": b.get("file_name") if b else "",
            "device_name": b.get("device_name") if b else "",
            "created_at": b.get("created_at") if b else "",
            "content": content
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404


@api_bp.route("/netdev/backups/<int:backup_id>/download", methods=["GET"])
@login_required
def download_netdev_backup(backup_id: int):
    """下载网络设备备份配置文件。"""
    try:
        b = netdev.get_backup(backup_id)
        if not b:
            return jsonify({"error": "备份记录不存在"}), 404
        content = netdev.get_backup_file_content(backup_id)
        filename = b.get("file_name") or f"netdev_backup_{backup_id}.cfg"
        bio = io.BytesIO(content.encode("utf-8"))
        bio.seek(0)
        return send_file(
            bio,
            mimetype="text/plain; charset=utf-8",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({"error": f"下载失败: {e}"}), 500


@api_bp.route("/netdev/backups/diff", methods=["POST"])
@login_required
def diff_netdev_backups():
    """比对两次备份配置的差异。"""
    data = request.get_json(silent=True) or {}
    id1 = data.get("backup_id1")
    id2 = data.get("backup_id2")
    if not id1 or not id2:
        return jsonify({"ok": False, "error": "请提供需要比对的两个备份 ID"}), 400
    try:
        diff_text = netdev.diff_backups(int(id1), int(id2))
        return jsonify({"ok": True, "diff": diff_text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/netdev/backups/<int:backup_id>", methods=["DELETE"])
@login_required
def delete_netdev_backup(backup_id: int):
    """删除单次备份记录及本地对应文件。"""
    ok = netdev.delete_backup(backup_id)
    return jsonify({"ok": ok})
