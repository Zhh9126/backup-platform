# -*- coding: utf-8 -*-
"""
存储管理：本地落盘目录、SFTP 远程上传、备份保留策略清理。

真实备份文件统一存放于 config.BACKUP_ROOT/<db_type>/<id>_<name>/ 下。
保留策略按“保留份数”与“保留天数”两者中更严格者执行。
"""
import os
import time
from pathlib import Path

import config
import core.db as db


class StorageManager:
    def __init__(self, task: dict, logger=None):
        self.task = task
        self.logger = logger or db.get_logger("storage")

    def local_dir(self) -> str:
        d = os.path.join(
            config.BACKUP_ROOT, self.task.get("db_type"),
            f"{self.task.get('id')}_{self.task.get('name')}")
        os.makedirs(d, exist_ok=True)
        return d

    def ensure(self) -> str:
        return self.local_dir()

    def list_backup_files(self) -> list:
        d = self.local_dir()
        files = []
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                files.append(p)
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files

    def upload_to_remote(self, local_path: str) -> (bool, str):
        backend = (self.task.get("storage_backend") or "local").lower()
        if backend != "sftp":
            return True, "local"
        try:
            import paramiko  # 可选依赖
        except ImportError:
            self.logger.error("未安装 paramiko，无法上传 SFTP；回退为本地保留")
            return False, "未安装 paramiko"
        host = self.task.get("remote_host")
        if not host:
            return False, "未配置 SFTP 主机"
        try:
            port = int(self.task.get("remote_port") or 22)
            transport = paramiko.Transport((host, port))
            user = self.task.get("remote_user") or "root"
            key = self.task.get("remote_key")
            if key and os.path.exists(key):
                pkey = paramiko.RSAKey.from_private_key_file(key)
                transport.connect(username=user, pkey=pkey)
            else:
                pw = db.decrypt_secret(self.task.get("remote_password") or "")
                transport.connect(username=user, password=pw)
            sftp = paramiko.SFTPClient.from_transport(transport)
            remote_base = (self.task.get("remote_path") or "/backups").rstrip("/")
            sftp.makedirs(remote_base) if hasattr(sftp, "makedirs") else None
            remote_path = remote_base + "/" + os.path.basename(local_path)
            sftp.put(local_path, remote_path)
            sftp.close()
            transport.close()
            self.logger.info("已上传至 SFTP: %s%s", remote_base, "/" + os.path.basename(local_path))
            return True, remote_path
        except Exception as e:
            self.logger.error("SFTP 上传失败: %s", e)
            return False, str(e)

    def apply_retention(self) -> None:
        retention_count = int(self.task.get("retention_count") or 0)
        retention_days = int(self.task.get("retention_days") or 0)
        files = self.list_backup_files()
        to_delete = []
        if retention_count and retention_count > 0:
            to_delete += files[retention_count:]  # 超出份数的旧文件
        if retention_days and retention_days > 0:
            cutoff = time.time() - retention_days * 86400
            for p in files:
                try:
                    if os.path.getmtime(p) < cutoff and p not in to_delete:
                        to_delete.append(p)
                except OSError:
                    continue
        for p in to_delete:
            try:
                os.remove(p)
                self.logger.info("保留策略清理: 删除 %s", p)
            except OSError as e:
                self.logger.warning("清理失败 %s: %s", p, e)
