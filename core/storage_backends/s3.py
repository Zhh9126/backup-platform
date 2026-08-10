# -*- coding: utf-8 -*-
"""
S3 冷数据存储后端（Tier 3 — 归档层）。

使用 MinIO Python SDK 连接 AWS S3 或任何 S3 兼容的冷存储服务。
适用于长期归档、低频访问的备份数据。
支持 S3 Glacier / Intelligent-Tiering 等存储类配置。
"""
import logging
import time
from io import BytesIO

from .base import StorageBackend
from .minio import _get_minio


class S3StorageBackend(StorageBackend):
    """S3 兼容冷数据存储。"""

    display_name = "S3 冷存储"
    tier = 3

    def __init__(self, config: dict, logger: logging.Logger = None):
        super().__init__(config, logger)
        self.endpoint = config.get("endpoint", "s3.amazonaws.com").rstrip("/")
        self.access_key = config.get("access_key", "")
        self.secret_key = config.get("secret_key", "")
        self.bucket = config.get("bucket", "")
        self.region = config.get("region", "") or "us-east-1"
        self.prefix = (config.get("prefix") or "").strip("/")
        # 解析额外选项
        extra = config.get("extra_options") if isinstance(config.get("extra_options"), dict) else {}
        if isinstance(config.get("extra_options"), str) and config.get("extra_options"):
            import json
            try:
                extra = json.loads(config["extra_options"])
            except Exception:
                extra = {}
        self.storage_class = extra.get("storage_class", "STANDARD_IA")  # 默认低频访问
        self.secure = not extra.get("insecure", False)
        self._client = None

    def _get_client(self):
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
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)

            if object_key is None:
                from pathlib import Path
                object_key = Path(file_path).name

            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key

            metadata = {"storage-tier": "cold", "storage-class": self.storage_class}

            # chunked=True 时使用更大的分块以优化冷存储写入
            part_size = (64 * 1024 * 1024) if chunked else (32 * 1024 * 1024)
            with open(file_path, "rb") as fdata:
                fdata.seek(0, 2)
                size_mb = fdata.tell() / (1024 * 1024)
                fdata.seek(0)

                result = client.put_object(
                    self.bucket, full_key, fdata,
                    length=-1,
                    part_size=part_size,
                    metadata=metadata,
                )

            self.logger.info(
                "[S3-Cold] 已归档: %s (%.1f MB, class=%s)",
                full_key, size_mb, self.storage_class,
            )
            return True
        except Exception as e:
            self.logger.error("[S3-Cold] 上传失败: %s", e)
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
            self.logger.error("[S3-Cold] 下载失败（可能需要从 Glacier 恢复）: %s", e)
            return None

    def delete_file(self, object_key: str) -> bool:
        try:
            client = self._get_client()
            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key
            client.remove_object(self.bucket, full_key)
            self.logger.info("[S3-Cold] 已删除: %s", full_key)
            return True
        except Exception as e:
            self.logger.error("[S3-Cold] 删除失败: %s", e)
            return False

    def test_connection(self) -> tuple:
        try:
            client = self._get_client()
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)

            test_key = f"{self.prefix}/__conn_test__".lstrip("/") if self.prefix else "__conn_test__"
            test_data = f"s3_cold_test_{int(time.time())}".encode()
            client.put_object(
                self.bucket, test_key,
                BytesIO(test_data), length=len(test_data),
            )
            client.remove_object(self.bucket, test_key)

            return True, f"S3 冷存储连接正常 (bucket={self.bucket}, region={self.region}, class={self.storage_class})"
        except Exception as e:
            return False, f"S3 连接失败: {e}"

    def file_exists(self, object_key: str) -> bool:
        try:
            client = self._get_client()
            full_key = f"{self.prefix}/{object_key}".lstrip("/") if self.prefix else object_key
            client.stat_object(self.bucket, full_key)
            return True
        except Exception:
            return False
