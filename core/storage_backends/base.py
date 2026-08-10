# -*- coding: utf-8 -*-
"""
存储后端基类：定义统一接口，所有存储驱动（Local / MinIO / S3）均实现此接口。

设计参考 Databasus 的 StorageFileSaver 接口，适配本平台 Python + Flask 技术栈。
"""
import os
import abc
import logging
from pathlib import Path
from typing import Optional, IO


class StorageBackend(abc.ABC):
    """存储后端抽象基类。"""

    display_name = "通用存储"
    tier = 1  # 1=本地 2=热数据(MinIO) 3=冷数据(S3)

    def __init__(self, config: dict, logger: logging.Logger = None):
        """
        Args:
            config: 存储目标配置字典（来自 storage_targets 表行）
            logger: 日志记录器
        """
        self.config = config
        self.logger = logger or logging.getLogger("storage")

    # ---- 抽象方法（子类必须实现） ----

    @abc.abstractmethod
    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        """将本地文件上传/复制到存储目标。

        Args:
            file_path: 本地备份文件绝对路径
            object_key: 存储中的对象键名（若为 None 则自动生成）
            dedup: 是否启用对象级去重。为 True 时，若 backup_sets 已存在相同
                   checksum 的对象，则只记录引用、不重复落盘，并在对应备份集的
                   dedup_saved_bytes 累加节省量（命中亦返回 True，表示数据已落库）。
            chunked: 是否启用分块上传（大文件）。具体后端自行决定分块大小；
                     本地后端为顺序写入，MinIO/S3 走 multipart。

        Returns:
            是否成功（去重命中亦返回 True）。
        """
        ...

    # ---- 去重前置检查（子类 save_file 在 dedup=True 时调用） ----
    def _dedup_prepare(self, file_path: str) -> bool:
        """去重前置：若 backup_sets 已存在相同 checksum 的对象，则跳过写入。

        命中时累加对应备份集的 dedup_saved_bytes，并返回 True（调用方应直接
        返回成功，不重复落盘）；未命中返回 False（调用方正常写入）。
        """
        import core.db as db
        import core.models as models
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = 0
        checksum = db.sha256_file(file_path)
        existing = models.find_backup_set_by_checksum(checksum) if checksum else None
        if existing:
            try:
                models.add_dedup_saved(existing["id"], size)
            except Exception:
                pass
            self.logger.info(
                "[%s][dedup] 命中已存在对象(set#%s)，跳过写入，节省 %d 字节",
                self.display_name, existing.get("id"), size)
            return True
        return False

    @abc.abstractmethod
    def get_file(self, object_key: str, dest_path: str = None) -> Optional[bytes]:
        """从存储中获取文件。

        Args:
            object_key: 对象键名
            dest_path: 若指定则写入本地路径，否则返回 bytes

        Returns:
            文件内容 bytes（未指定 dest_path 时），或 None 表示失败
        """
        ...

    @abc.abstractmethod
    def delete_file(self, object_key: str) -> bool:
        """从存储中删除文件。"""
        ...

    @abc.abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """测试连接是否可用。

        Returns:
            (是否成功, 消息)
        """
        ...

    @abc.abstractmethod
    def file_exists(self, object_key: str) -> bool:
        """检查对象是否存在。"""
        ...

    # ---- 通用辅助方法 ----

    def build_object_key(self, db_type: str, task_id: int, task_name: str,
                         timestamp: str, filename: str) -> str:
        """构造标准化的对象键名。

        格式: {prefix}/{db_type}/{task_id}_{task_name}/{timestamp}__{filename}
        """
        prefix = (self.config.get("prefix") or "").strip("/")
        parts = [prefix] if prefix else []
        key = f"{db_type}/{task_id}_{task_name}/{timestamp}__{filename}"
        parts.append(key)
        return "/".join(parts)

    def get_info(self) -> dict:
        """返回存储目标基本信息（用于 API 响应，自动脱敏）。"""
        cfg = dict(self.config)
        # 脱敏敏感字段
        if cfg.get("secret_key"):
            cfg["secret_key"] = "******"
        if cfg.get("access_key") and len(cfg.get("access_key", "")) > 8:
            cfg["access_key"] = cfg["access_key"][:4] + "****" + cfg["access_key"][-4:]
        return {
            "id": cfg.get("id"),
            "name": cfg.get("name"),
            "type": cfg.get("type"),
            "tier": cfg.get("tier", self.tier),
            "display_name": self.display_name,
            "endpoint": cfg.get("endpoint"),
            "bucket": cfg.get("bucket"),
            "region": cfg.get("region"),
            "enabled": cfg.get("enabled", 1),
            "is_default": cfg.get("is_default", 0),
            "last_error": cfg.get("last_error"),
            "last_test_at": cfg.get("last_test_at"),
        }
