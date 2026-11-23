# -*- coding: utf-8 -*-
"""
存储后端包：统一注册表与工厂方法。

提供 get_backend(type, config) 工厂函数，根据存储类型返回对应后端实例。
"""
import logging
import os
import shutil
from .base import StorageBackend
from .local import LocalStorageBackend
from .minio import MinIOStorageBackend
from .s3 import S3StorageBackend
from .tape import TapeStorageBackend
from .sftp import SFTPStorageBackend, _HAS_PARAMIKO as _HAS_SFTP_DEP
from .mount import MountStorageBackend
from .ftp import FTPStorageBackend

# 类型 → 类 映射
BACKEND_REGISTRY: dict[type[StorageBackend]] = {
    "local": LocalStorageBackend,
    "minio": MinIOStorageBackend,
    "s3": S3StorageBackend,
    "tape": TapeStorageBackend,
    "sftp": SFTPStorageBackend,
    # 2026-11-13：按「存储介质」补齐外挂/NFS 与 FTP 通道
    "mount": MountStorageBackend,       # 外挂介质 / NFS / CIFS 挂载点
    # 明文 FTP 默认禁用，只有显式 AIDBM_ENABLE_INSECURE_FTP=true 才注册。
}

# Tier 显示名
TIER_NAMES = {
    1: "L1 MinIO 热数据",
    2: "L2 S3 冷数据",
    3: "L3 源端导出",
}

# 类型显示信息
TYPE_META = {
    "local": {"name": "源端本地路径", "icon": "bi-hdd", "tier": 3, "desc": "服务端本地文件系统导出（可离线转移）"},
    "minio": {"name": "MinIO", "icon": "bi-cloud-arrow-up", "tier": 1, "desc": "热数据对象存储（S3 兼容），备份第一落点"},
    "s3": {"name": "S3", "icon": "bi-cloud-check", "tier": 2, "desc": "冷数据归档存储（AWS S3 / 兼容服务）"},
    "tape": {"name": "磁带库 (D2T)", "icon": "bi-layers", "tier": 3, "desc": "磁带归档：真实设备 /dev/nst*（mt+tar）或目录模拟带库；顺序写入，适合离线长期保存"},
    "sftp": {"name": "SFTP / NAS", "icon": "bi-hdd-network", "tier": 2, "desc": "SFTP 存储（群晖 NAS 等 SSH 主机）：目标侧开启 SSH 即可作为备份仓库，数据库/存储端零安装"},
    "mount": {"name": "外挂存储 / NFS", "icon": "bi-hdd-stack", "tier": 2, "desc": "已挂载的外挂介质（NFS / CIFS / USB 移动硬盘 / 本地磁盘阵列），平台只校验可写与挂载状态，不执行 mount 特权操作"},
    "ftp": {"name": "FTP / FTPS", "icon": "bi-server", "tier": 2, "desc": "标准库 ftplib 直连 FTP/FTPS：上传备份产物、按需回取；无校验和，仅按大小核对（如实标注）"},
}


def get_backend(storage_type: str, config: dict, logger: logging.Logger = None) -> StorageBackend:
    """工厂方法：根据类型创建存储后端实例。

    Args:
        storage_type: 存储类型 (local | minio | s3 | tape | sftp)
        config: 来自 storage_targets 表的配置字典
        logger: 日志记录器

    Returns:
        对应的 StorageBackend 实例

    Raises:
        ValueError: 不支持的存储类型
    """
    if storage_type.lower() == "ftp" and os.environ.get("AIDBM_ENABLE_INSECURE_FTP", "false").lower() != "true":
        raise ValueError("FTP 默认禁用，请改用 SFTP/FTPS/对象存储，或显式设置 AIDBM_ENABLE_INSECURE_FTP=true")
    cls = BACKEND_REGISTRY.get(storage_type.lower())
    if not cls:
        raise ValueError(f"不支持的存储类型: {storage_type}（支持: {', '.join(BACKEND_REGISTRY)}）")
    # storage_targets.secret_key 落库时经 db.encrypt_secret 加密（现有格式 sm4v1:，
    # 历史格式 enc:），后端（MinIO/S3/SFTP/FTP）必须拿到明文才能认证。
    # 这里统一在工厂入口解密：一次覆盖所有后端与所有调用方（lifecycle/tier_replication/手工复制等）。
    # 注意：不能只判 enc: 前缀——SM4 迁移后密文是 sm4v1:，只判 enc: 会把密文当密码发出去
    #       （表现为 AccessDenied / 530 认证失败）；decrypt_secret 对明文原样返回，可无条件调用。
    # 注意必须复制一份配置，避免把明文写回调用方手里的数据库行对象。
    config = dict(config) if config else {}
    sk = config.get("secret_key") or ""
    if isinstance(sk, str) and sk:
        try:
            from core import db as _db
            config["secret_key"] = _db.decrypt_secret(sk)
        except Exception:  # 解密失败不应阻断创建，交由后端连接时报错
            pass
    return cls(config, logger)


def list_supported_types() -> list[dict]:
    """返回当前安全策略允许的存储类型。"""
    meta = dict(TYPE_META)
    if os.environ.get("AIDBM_ENABLE_INSECURE_FTP", "false").lower() != "true":
        meta.pop("ftp", None)
    return [{"type": k, **v} for k, v in meta.items()]


def check_dependencies() -> dict[str, bool]:
    """检查各后端的依赖是否已安装。"""
    deps = {
        "local": True,  # 无额外依赖
        "minio": False,
        "s3": False,
        # 这里只判「磁带后端能否工作」：目录模拟模式只需 tar。
        # 真实设备模式的工具链/设备/cgroup 就绪度由 tape_library.runtime_hint()
        # 与 /api/tape/devices 明确报告（旧实现恒 True 会掩盖容器缺 mt-st）。
        "tape": bool(shutil.which("tar")),
        "sftp": _HAS_SFTP_DEP,
        "mount": True,    # 纯 os/pathlib，无外部依赖
        "ftp": os.environ.get("AIDBM_ENABLE_INSECURE_FTP", "false").lower() == "true",
    }
    try:
        import minio
        deps["minio"] = True
        deps["s3"] = True
    except ImportError:
        pass
    return deps
