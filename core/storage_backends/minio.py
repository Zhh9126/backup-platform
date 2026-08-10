# -*- coding: utf-8 -*-
"""
MinIO 存储后端（Tier 2 — 热数据层）。

使用 MinIO 官方 Python SDK（minio），兼容所有 S3 协议服务。
适用于高频访问的热备数据，提供快速读写能力。
支持分块上传（multipart）以处理大文件。
"""
import logging
import time
import hashlib
from io import BytesIO

from .base import StorageBackend

# 延迟导入 minio（可选依赖）
_minio = None


def _get_minio():
    global _minio
    if _minio is None:
        try:
            import minio as _m
            _minio = _m
        except ImportError:
            raise RuntimeError(
                "MinIO 后端需要安装 minio SDK。请执行: pip install minio"
            )
    return _minio


class MinIOStorageBackend(StorageBackend):
    """MinIO / S3 兼容对象存储（热数据层）。"""

    display_name = "MinIO 热存储"
    tier = 2

    def __init__(self, config: dict, logger: logging.Logger = None):
        super().__init__(config, logger)
        self.endpoint = config.get("endpoint", "").rstrip("/")
        self.access_key = config.get("access_key", "")
        self.secret_key = config.get("secret_key", "")
        self.bucket = config.get("bucket", "")
        self.region = config.get("region", "") or "us-east-1"
        self.prefix = (config.get("prefix") or "").strip("/")
        extra = config.get("extra_options")
        if not isinstance(extra, dict):
            extra = {}
        self.secure = not extra.get("insecure", False)
        self._client = None

    def _get_client(self):
        """懒加载 MinIO 客户端（单例）。"""
        if self._client is None:
            m = _get_minio()
            extra_kwargs = {"region": self.region}
            if not self.secure:
                extra_kwargs["secure"] = False
            self._client = m.Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                **extra_kwargs,
            )
        return self._client

    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        # 对象级去重：命中已存在 checksum 则跳过写入并记录引用
        if dedup and self._dedup_prepare(file_path):
            return True
        try:
            client = self._get_client()
            # 确保 bucket 存在
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)

            if object_key is None:
                from pathlib import Path
                object_key = Path(file_path).name

            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key

            # 分块上传：chunked=True 时使用更大的分块（multipart 思路）
            part_size = (32 * 1024 * 1024) if chunked else (16 * 1024 * 1024)
            with open(file_path, "rb") as fdata:
                # 计算内容 SHA256 用于日志
                fdata.seek(0, 2)
                size_mb = fdata.tell() / (1024 * 1024)
                fdata.seek(0)

                result = client.put_object(
                    self.bucket, full_key, fdata,
                    length=-1,  # -1 表示未知长度，SDK 自动检测
                    part_size=part_size,
                )

            self.logger.info(
                "[MinIO] 已上传: %s (%.1f MB, etag=%s)",
                full_key, size_mb, getattr(result, "etag", "N/A"),
            )
            return True
        except Exception as e:
            self.logger.error("[MinIO] 上传失败: %s", e)
            return False

    def get_file(self, object_key: str, dest_path: str = None):
        try:
            client = self._get_client()
            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key

            response = client.get_object(self.bucket, full_key)
            try:
                data = response.read()
            finally:
                response.close()
                response.release_conn()

            if dest_path:
                from pathlib import Path
                d = Path(dest_path)
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_bytes(data)
                return True
            else:
                return data
        except Exception as e:
            self.logger.error("[MinIO] 下载失败: %s", e)
            return None

    def delete_file(self, object_key: str) -> bool:
        try:
            client = self._get_client()
            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key
            client.remove_object(self.bucket, full_key)
            self.logger.info("[MinIO] 已删除: %s", full_key)
            return True
        except Exception as e:
            self.logger.error("[MinIO] 删除失败: %s", e)
            return False

    def test_connection(self) -> tuple:
        try:
            client = self._get_client()
            # 检查 bucket 是否存在或可创建
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)

            # 写入测试对象
            test_key = f"{self.prefix}/__conn_test__".lstrip("/") if self.prefix else "__conn_test__"
            test_data = f"storage_test_{int(time.time())}".encode()
            client.put_object(
                self.bucket, test_key,
                BytesIO(test_data), length=len(test_data),
            )
            client.remove_object(self.bucket, test_key)

            return True, f"MinIO 连接正常 (bucket={self.bucket}, endpoint={self.endpoint})"
        except Exception as e:
            return False, f"MinIO 连接失败: {e}"

    def file_exists(self, object_key: str) -> bool:
        try:
            client = self._get_client()
            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key
            client.stat_object(self.bucket, full_key)
            return True
        except Exception:
            return False

    def list_objects(self, prefix_filter: str = None) -> list:
        """列出 bucket 中指定前缀的对象。"""
        try:
            client = self._get_client()
            search_prefix = f"{self.prefix}/{prefix_filter}".lstrip("/") if prefix_filter else (self.prefix or None)
            objects = client.list_objects(self.bucket, prefix=search_prefix, recursive=True)
            return [
                {
                    "key": obj.object_name,
                    "size": obj.size,
                    "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                    "etag": obj.etag,
                }
                for obj in objects
            ]
        except Exception as e:
            self.logger.error("[MinIO] 列举对象失败: %s", e)
            return []

    def get_bucket_usage(self) -> dict:
        """获取 bucket 总用量（遍历所有对象累加 size_bytes）。

        Returns:
            dict with keys: total_bytes, used_bytes, free_bytes, used_percent, object_count
            若获取失败返回 None。
        """
        try:
            client = self._get_client()
            total_bytes = 0
            object_count = 0
            objects = client.list_objects(self.bucket, prefix=self.prefix or None, recursive=True)
            for obj in objects:
                total_bytes += obj.size or 0
                object_count += 1
            # MinIO/S3 通常无固定 quota，free_bytes 无法精确获知
            # 这里仅返回已用量；used_percent 依赖外部设置的 total_capacity
            return {
                "used_bytes": total_bytes,
                "object_count": object_count,
                "total_bytes": 0,   # 未知，需从外部配置获取
                "free_bytes": 0,    # 未知
                "used_percent": 0,  # 无法计算，依赖外部
            }
        except Exception as e:
            self.logger.error("[MinIO] 获取 bucket 用量失败: %s", e)
            return None
