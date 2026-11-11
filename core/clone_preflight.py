# -*- coding: utf-8 -*-
"""VDB 克隆预检（对标 zBackup `clonePreflight` / `oracleRenamePreflight`）。

只读检查，不创建任何资源、不改动源库；失败抛给调用方由
`api/clone.py` 的 `ValueError → 400` 统一透出，避免拉起半成品 VDB
后再失败（此前失败原因只能写在 note 里）。
"""
from __future__ import annotations

import os
import shutil
import socket

import config


def _check(checks, item, ok, message, hard=True):
    checks.append({"item": item, "level": "pass" if ok else
                   ("fail" if hard else "warn"), "message": message})
    return ok


def preflight(rec: dict, target_host: str = "127.0.0.1",
              target_port=None, target_password: str = None,
              note: str = "", clone_mode: str = "logical") -> dict:
    """返回 {ok, errors, warnings, checks}；ok=False 时调用方应拒绝克隆。"""
    errors, warnings, checks = [], [], []
    rec = rec or {}

    # 1) 源记录状态：失败/隔离产物不允许作为克隆源
    status = str(rec.get("status") or "").lower()
    if status and status != "success":
        errors.append(f"源备份记录状态为 {status}，不是成功记录")
        _check(checks, "source_status", False, f"源记录状态 {status} 不可用")
    else:
        _check(checks, "source_status", True, "源记录状态正常")

    # 2) 源产物存在且非空（zBackup: CLONE_FILES_MISSING 前置拦截）。
    #    template/schema/snapshot 属于活库克隆，不依赖备份产物，不能误拦。
    if clone_mode != "logical":
        _check(checks, "source_artifact", True,
               f"克隆模式 {clone_mode} 为活库克隆，跳过产物检查", hard=False)
    else:
        path = str(rec.get("backup_path") or "")
        if not path or not os.path.isfile(path):
            errors.append(f"源备份产物不存在或已被清理: {path or '(空)'}")
            _check(checks, "source_artifact", False, "产物文件缺失")
        else:
            size = os.path.getsize(path)
            if size <= 0:
                errors.append(f"源备份产物为空文件: {path}")
                _check(checks, "source_artifact", False, "产物 0 字节")
            else:
                _check(checks, "source_artifact", True,
                       f"产物 {size} 字节: {os.path.basename(path)}")

    # 3) 目标实例端口（显式给出才检查；未给时由 _launch_vdb 自行解析）
    if target_port:
        try:
            port = int(target_port)
            conn = socket.create_connection((target_host or "127.0.0.1", port),
                                            timeout=3)
            if hasattr(conn, "close"):
                conn.close()
            _check(checks, "target_port", True, f"{target_host}:{port} 可连通")
        except (OSError, ValueError) as e:
            errors.append(f"目标实例端口不可达: {target_host}:{target_port} ({e})")
            _check(checks, "target_port", False, "目标端口不可达")
    else:
        _check(checks, "target_port", True, "未指定端口，跳过连通性检查",
               hard=False)

    # 4) 平台暂存盘余量（克隆解包/导入需要落盘）
    try:
        root = str(getattr(config, "BACKUP_ROOT", "/tmp") or "/tmp")
        os.makedirs(root, exist_ok=True)
        free_gb = shutil.disk_usage(root).free / 1024 ** 3
        if free_gb < 1:
            warnings.append(f"平台备份目录剩余空间仅 {free_gb:.2f}GB，克隆可能失败")
            _check(checks, "disk_space", False,
                   f"剩余 {free_gb:.2f}GB", hard=False)
        else:
            _check(checks, "disk_space", True, f"剩余 {free_gb:.2f}GB")
    except Exception as e:
        warnings.append(f"磁盘余量检查失败: {e}")

    # 5) 目标凭据（留空不硬拦，_launch_vdb 可能沿用任务凭据）
    if not target_password:
        warnings.append("未提供目标库口令，将尝试沿用任务已存凭据")
        _check(checks, "target_credential", False,
               "未提供目标口令", hard=False)
    else:
        _check(checks, "target_credential", True, "已提供目标口令")

    # 6) 防勒索隔离态（若有该字段）
    if rec.get("quarantined") or str(rec.get("security_level") or "").lower() in \
            ("suspect", "quarantine", "critical"):
        errors.append("源恢复点已被安全隔离，禁止用于克隆")
        _check(checks, "security", False, "恢复点被隔离")

    return {"ok": not errors, "errors": errors,
            "warnings": warnings, "checks": checks,
            "note": str(note or "")[:200]}
