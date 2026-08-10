# -*- coding: utf-8 -*-
"""
本地文件系统存储后端（Tier 1 — 基础存储层）。

备份文件直接写入服务端本地磁盘，作为第一级存储。
支持原子写入（先写临时文件 + os.replace）和目录自动创建。
"""
import os
import shutil
import time
import logging
from pathlib import Path

from .base import StorageBackend


class LocalStorageBackend(StorageBackend):
    """本地文件系统存储。"""

    display_name = "本地存储"
    tier = 1

    def __init__(self, config: dict, logger: logging.Logger = None):
        super().__init__(config, logger)
        # endpoint 字段对于本地存储表示根目录路径
        self.base_path = Path(config.get("endpoint") or config.get("base_path", "./backups"))
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, object_key: str) -> Path:
        """将对象键解析为本地绝对路径。"""
        # 安全检查：防止路径穿越
        key = object_key.lstrip("/")
        if ".." in key:
            raise ValueError(f"非法对象键名（含路径穿越）: {object_key}")
        return self.base_path / key

    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        # 对象级去重：命中已存在 checksum 则跳过写入并记录引用
        if dedup and self._dedup_prepare(file_path):
            return True
        try:
            src = Path(file_path)
            if not src.exists():
                self.logger.error("源文件不存在: %s", file_path)
                return False

            if object_key is None:
                object_key = src.name

            dest = self._resolve_path(object_key)
            dest.parent.mkdir(parents=True, exist_ok=True)

            # 原子写入：先写临时文件再 rename
            tmp_dest = dest.with_suffix(".tmp")
            if chunked:
                # 分块顺序写入（本地顺序写等价于分块，避免一次性读入大文件）
                with open(src, "rb") as fin, open(tmp_dest, "wb") as fout:
                    while True:
                        buf = fin.read(8 * 1024 * 1024)
                        if not buf:
                            break
                        fout.write(buf)
            else:
                shutil.copy2(str(src), str(tmp_dest))
            tmp_dest.replace(dest)

            size_kb = dest.stat().st_size / 1024
            self.logger.info("[Local] 已保存: %s (%.1f KB)", object_key, size_kb)
            return True
        except Exception as e:
            self.logger.error("[Local] 保存失败: %s", e)
            return False

    def get_file(self, object_key: str, dest_path: str = None):
        try:
            src = self._resolve_path(object_key)
            if not src.exists():
                self.logger.warning("[Local] 文件不存在: %s", object_key)
                return None

            if dest_path:
                d = Path(dest_path)
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(d))
                return True
            else:
                return src.read_bytes()
        except Exception as e:
            self.logger.error("[Local] 读取失败: %s", e)
            return None

    def delete_file(self, object_key: str) -> bool:
        try:
            p = self._resolve_path(object_key)
            if p.exists():
                p.unlink()
                self.logger.info("[Local] 已删除: %s", object_key)
            return True
        except Exception as e:
            self.logger.error("[Local] 删除失败: %s", e)
            return False

    def test_connection(self) -> tuple:
        try:
            test_file = self.base_path / ".connection_test"
            test_file.write_text(f"test_{int(time.time())}")
            test_file.unlink()
            return True, f"本地存储可写: {self.base_path}"
        except PermissionError:
            return False, f"无写入权限: {self.base_path}"
        except Exception as e:
            return False, str(e)

    def file_exists(self, object_key: str) -> bool:
        return self._resolve_path(object_key).exists()

    def get_used_space(self) -> int:
        """获取已用空间（字节）。"""
        total = 0
        for root, dirs, files in os.walk(self.base_path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
