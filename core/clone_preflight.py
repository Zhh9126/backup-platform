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


#: 各库型克隆所需的目标端客户端工具（**平台本机**克隆时必须在平台侧就绪）
#: 远端克隆走 SSH 通道，由平台在目标机上自动解析，目标机零安装。
_LOCAL_TOOL_NEEDS = {
    "oracle": ("impdp", "imp"),
    "mysql": ("mysql",),
    "mariadb": ("mysql",),
    "postgresql": ("psql", "pg_restore"),
    "kingbase": ("ksql", "psql"),
    "dameng": ("dimp",),
    "opengauss": ("gsql", "gs_restore"),
}

#: 与引擎 _resolve_local_tool 同源的常见安装目录（预检独立进程运行，不引引擎）
_COMMON_TOOL_DIRS = (
    "/pgdb/pgsql/bin", "/usr/pgsql-*/bin", "/usr/local/pgsql/bin",
    "/usr/local/mysql*/bin", "/opt/mysql*/bin", "/opt/pgsql*/bin",
    "/usr/local/mariadb*/bin", "/opt/mariadb*/bin",
    "/dm8/bin", "/dm*/bin", "/opt/dmdbms/bin", "/opt/dm*/bin",
    "/home/dmdba/dmdbms/bin",
)


def local_tool_available(name: str) -> str:
    """本机工具解析：PATH → 常见安装目录。返回可执行路径，找不到返回空串。"""
    hit = shutil.which(name)
    if hit:
        return hit
    import glob as _glob
    for d in _COMMON_TOOL_DIRS:
        for p in sorted(_glob.glob(os.path.join(d, name))):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ""


def _is_local_target(target_host: str) -> bool:
    return str(target_host or "127.0.0.1") in ("127.0.0.1", "localhost", "::1", "")


def preflight(rec: dict, target_host: str = "127.0.0.1",
              target_port=None, target_password: str = None,
              note: str = "", clone_mode: str = "logical") -> dict:
    """返回 {ok, errors, warnings, checks}；ok=False 时调用方应拒绝克隆。"""
    errors, warnings, checks = [], [], []
    rec = rec or {}

    from core.clone_backends import ENGINE_TYPES
    from core import clone_runtime
    supported = ENGINE_TYPES | clone_runtime.KINDS | {"mysql", "mariadb", "postgresql", "kingbase", "dameng", "oracle"}
    kind = rec.get("db_type") or ""
    allowed_modes = {"logical"}
    if kind == "postgresql":
        allowed_modes.add("template")
    if kind in ("mysql", "mariadb"):
        allowed_modes.add("snapshot")
    if kind == "oracle":
        allowed_modes.add("schema")
    if kind and kind not in supported:
        errors.append(f"{kind} 需要专用隔离实例和克隆生命周期适配器")
    if kind and clone_mode not in allowed_modes:
        errors.append(f"{kind} 不支持克隆模式 {clone_mode}")

    if kind in clone_runtime.KINDS:
        try:
            message = clone_runtime.preflight(rec, target_host)
            _check(checks, "isolated_runtime", True, message)
        except Exception as exc:
            errors.append(str(exc))
            _check(checks, "isolated_runtime", False, str(exc))

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
                expected = rec.get("checksum") or ""
                if expected:
                    from core import db
                    digest = db.normalize_sha256(expected)
                    if not digest or db.sha256_file(path) != digest:
                        errors.append("源备份产物 SHA256 校验失败，禁止创建克隆")
                        _check(checks, "source_checksum", False, "源产物校验值不匹配")
                    else:
                        _check(checks, "source_checksum", True, "源产物 SHA256 校验通过")
                else:
                    warnings.append("源备份缺少 SHA256 基线，无法证明产物完整性")


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

    # 3.5) 数据量与空间需求（对标国际先进克隆服务的「克隆前容量视图」：
    #      提交前就要告诉用户这份克隆有多大、目标端要腾多少空间）
    estimate = estimate_clone_size(rec, clone_mode=clone_mode,
                                   target_host=target_host)
    if estimate.get("source_artifact_bytes"):
        _check(checks, "data_size", True,
               "源产物 %s，克隆件逻辑数据量约 %s，目标端预计占用 %s%s"
               % (estimate.get("source_artifact_human"),
                  estimate.get("estimated_data_human"),
                  estimate.get("estimated_disk_human"),
                  ("；目标端可用 %s" % estimate["target_free_human"])
                  if estimate.get("target_free_human") else
                  "（目标端空间无法探测）"))
        if estimate.get("target_free_bytes") and \
                estimate.get("estimated_disk_bytes") and \
                estimate["target_free_bytes"] < estimate["estimated_disk_bytes"]:
            warnings.append(
                "目标端可用空间 %s 小于克隆预计占用 %s，克隆可能失败"
                % (estimate["target_free_human"], estimate["estimated_disk_human"]))
            _check(checks, "target_space", False,
                   "可用 %s < 预计 %s" % (estimate["target_free_human"],
                                          estimate["estimated_disk_human"]),
                   hard=False)
    else:
        _check(checks, "data_size", True, "活库克隆模式，无备份产物可估算",
               hard=False)

    # 4) 目标端工具就绪（克隆失败的头号原因：目标端没有导入客户端）
    #    - 目标主机 = 平台本机：导入动作在平台进程内执行，平台侧必须有对应客户端；
    #    - 目标主机 = 其他机器：走 SSH 通道，由平台在目标机上自动解析工具
    #      （数据库服务器零安装，符合产品红线）。
    #    2026-11-26 实测：Oracle 克隆到 127.0.0.1 报「imp 客户端不可用」，
    #    提交前预检本可以提前拦住并告诉用户要补什么。
    tools = _LOCAL_TOOL_NEEDS.get(kind)
    if tools:
        if _is_local_target(target_host):
            found = [(t, local_tool_available(t)) for t in tools]
            missing = [t for t, p in found if not p]
            if missing:
                errors.append(
                    "目标主机为平台本机，但平台侧缺少 %s 导入客户端（%s）"
                    % (kind or "该库型", "、".join(missing)))
                _check(checks, "target_tools", False,
                       "平台侧缺少 %s。两种处置：① 把「目标主机」改成数据库服务器"
                       "（走 SSH，用服务器自带导入工具，平台零安装）；② 在平台侧"
                       "安装对应客户端，并在任务「高级选项 → 工具路径」指定 bin 目录"
                       % "、".join(missing))
            else:
                _check(checks, "target_tools", True,
                       "平台侧客户端就绪: " + "、".join(
                           "%s@%s" % (t, os.path.dirname(p)) for t, p in found if p))
        else:
            try:
                conn = socket.create_connection((str(target_host), 22), timeout=3)
                conn.close()
                _check(checks, "target_tools", True,
                       "目标机 SSH 可达，导入工具由平台在目标机上自动解析"
                       "（目标机无需安装任何备份工具）")
            except OSError as e:
                errors.append("目标主机 SSH 不可达: %s:22 (%s)" % (target_host, e))
                _check(checks, "target_tools", False, "目标机 SSH 不可达")
    else:
        _check(checks, "target_tools", True, "该库型无特定客户端要求", hard=False)

    # 5) 平台暂存盘余量（克隆解包/导入需要落盘）
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
            "estimate": estimate,
            "note": str(note or "")[:200]}


def estimate_clone_size(rec: dict, clone_mode: str = "logical",
                        target_host: str = "127.0.0.1") -> dict:
    """估算克隆件的数据量与目标端空间需求。

    口径说明（页面如实展示，不混算）：
    - source_artifact_bytes   源备份产物在磁盘上的占用（压缩后）
    - source_original_bytes   源产物**原始**大小（未压缩），最接近库真实体量
    - estimated_data_bytes    克隆件逻辑数据量估算（= 原始大小）
    - estimated_disk_bytes    克隆件在目标端的磁盘占用估算
                              （逻辑导入按 1.3 倍膨胀估；template/snapshot 活库克隆
                                走 COW，初始占用≈0，增长量取决于写增量）
    - target_free_bytes       目标端可用空间（本机可探测；远端无法探测时为 None）
    """
    out = {"source_artifact_bytes": 0, "source_artifact_human": "-",
           "source_original_bytes": 0, "source_original_human": "-",
           "estimated_data_bytes": 0, "estimated_data_human": "-",
           "estimated_disk_bytes": 0, "estimated_disk_human": "-",
           "target_free_bytes": None, "target_free_human": "",
           "mode": clone_mode or "logical"}
    rec = rec or {}
    try:
        from core import db as _db
        human = _db.human_size
    except Exception:
        human = lambda n: "%d B" % int(n or 0)
    path = str(rec.get("backup_path") or "")
    try:
        if path and os.path.isfile(path):
            out["source_artifact_bytes"] = os.path.getsize(path)
    except Exception:
        pass
    try:
        out["source_original_bytes"] = int(rec.get("original_size_bytes") or 0)
    except Exception:
        out["source_original_bytes"] = 0
    if not out["source_original_bytes"]:
        # 没有原始大小基线时用压缩产物 × 经验膨胀系数估算（压缩比未知，取 3 倍）
        out["source_original_bytes"] = int(out["source_artifact_bytes"] * 3)
    out["source_artifact_human"] = human(out["source_artifact_bytes"])
    out["source_original_human"] = human(out["source_original_bytes"])
    out["estimated_data_bytes"] = out["source_original_bytes"]
    out["estimated_data_human"] = human(out["estimated_data_bytes"])
    if clone_mode in ("template", "snapshot", "schema"):
        # 活库克隆：COW 共享底层数据块，初始占用≈0
        out["estimated_disk_bytes"] = 0
        out["estimated_disk_human"] = "≈0（COW 共享，仅按写增量增长）"
    else:
        out["estimated_disk_bytes"] = int(out["estimated_data_bytes"] * 1.3)
        out["estimated_disk_human"] = human(out["estimated_disk_bytes"])
    # 目标端可用空间：仅本机可探测
    if str(target_host or "") in ("127.0.0.1", "localhost", "::1", ""):
        try:
            root = str(getattr(config, "BACKUP_ROOT", "/tmp") or "/tmp")
            os.makedirs(root, exist_ok=True)
            out["target_free_bytes"] = shutil.disk_usage(root).free
            out["target_free_human"] = human(out["target_free_bytes"])
        except Exception:
            pass
    return out
