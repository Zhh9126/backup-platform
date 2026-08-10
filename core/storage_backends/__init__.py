# -*- coding: utf-8 -*-
"""
存储后端包：统一注册表与工厂方法。

提供 get_backend(type, config) 工厂函数，根据存储类型返回对应后端实例。
"""
import logging
from .base import StorageBackend
from .local import LocalStorageBackend
from .minio import MinIOStorageBackend
from .s3 import S3StorageBackend

# 类型 → 类 映射
BACKEND_REGISTRY: dict[type[StorageBackend]] = {
    "local": LocalStorageBackend,
    "minio": MinIOStorageBackend,
    "s3": S3StorageBackend,
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
}


def get_backend(storage_type: str, config: dict, logger: logging.Logger = None) -> StorageBackend:
    """工厂方法：根据类型创建存储后端实例。

    Args:
        storage_type: 存储类型 (local | minio | s3)
        config: 来自 storage_targets 表的配置字典
        logger: 日志记录器

    Returns:
        对应的 StorageBackend 实例

    Raises:
        ValueError: 不支持的存储类型
    """
    cls = BACKEND_REGISTRY.get(storage_type.lower())
    if not cls:
        raise ValueError(f"不支持的存储类型: {storage_type}（支持: {', '.join(BACKEND_REGISTRY)}）")
    return cls(config, logger)


def list_supported_types() -> list[dict]:
    """返回所有支持的存储类型及其元信息。"""
    return [
        {"type": k, **v} for k, v in TYPE_META.items()
    ]


def check_dependencies() -> dict[str, bool]:
    """检查各后端的依赖是否已安装。"""
    deps = {
        "local": True,  # 无额外依赖
        "minio": False,
        "s3": False,
    }
    try:
        import minio
        deps["minio"] = True
        deps["s3"] = True
    except ImportError:
        pass
    return deps
