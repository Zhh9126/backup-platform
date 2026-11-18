# -*- coding: utf-8 -*-
"""FTP / FTPS 存储后端（标准库 ftplib，零第三方依赖）。

配置（storage_targets.config JSON）：
    host / port(默认 21) / username / password
    remote_path(远端根目录，默认 /)  / passive(默认 true) / tls(默认 false)
    timeout(默认 30)

语义：备份产物按对象键上传；``save_file`` 支持断点续传判断（远端已存在且
大小一致直接跳过）；删除与存在性检查走标准 FTP 命令。

诚实边界：
- FTP 无校验和，只能按**大小**核对，不做哈希比对；
- 主动模式（passive=false）在 NAT 环境常失败，默认被动模式；
- 明文 FTP 不加密，跨网段建议用 tls=true（FTP_TLS）。
"""
import ftplib
import logging
import os
from typing import Optional

from .base import StorageBackend


class FTPStorageBackend(StorageBackend):
    """FTP / FTPS 存储后端。"""

    display_name = "FTP 存储"
    tier = 2

    def __init__(self, config: dict, logger: logging.Logger = None):
        super().__init__(config, logger)
        # storage_targets 表行的字段名与后端配置名不同，这里统一兼容：
        #   endpoint   → host；secret_key → password（工厂已解密 enc: 密文）；
        #   extra_options 是 JSON 字符串 → port / remote_path / passive / tls / timeout。
        ex = config.get("extra_options")
        if isinstance(ex, str):
            import json
            try:
                ex = json.loads(ex)
            except (ValueError, TypeError):
                ex = {}
        elif not isinstance(ex, dict):
            ex = {}

        def _pick(*names, default=None):
            for n in names:
                v = config.get(n)
                if v not in (None, ""):
                    return v
                v = ex.get(n)
                if v not in (None, ""):
                    return v
            return default

        def _bool(name, default):
            """0/1、'0'/'1'、true/false 都能正确解析（不能用 `x or 默认值`——0 会被吞掉）。"""
            v = _pick(name, default=default)
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ("0", "false", "no", "off", ""):
                return False
            if s in ("1", "true", "yes", "on"):
                return True
            return bool(default)

        self.host = str(_pick("host", "endpoint", default="")).strip()
        self.port = int(_pick("port", default=21) or 21)
        self.username = str(_pick("username", "access_key", default="anonymous") or "anonymous")
        self.password = str(_pick("password", "secret_key", default="") or "")
        self.remote_root = "/" + str(_pick("remote_path", "prefix", "bucket", default="")).strip("/")
        self.passive = _bool("passive", True)
        self.use_tls = _bool("tls", False)
        self.timeout = int(_pick("timeout", default=30) or 30)

    # ---------------------------------------------------------------- 连接
    def _conn(self) -> ftplib.FTP:
        cls = ftplib.FTP_TLS if self.use_tls else ftplib.FTP
        ftp = cls()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login(self.username, self.password)
        if self.use_tls:
            try:
                ftp.prot_p()
            except Exception:
                pass
        ftp.set_pasv(self.passive)
        return ftp

    def _key_path(self, object_key: str) -> str:
        key = str(object_key or "").lstrip("/")
        if ".." in key.split("/"):
            raise ValueError("非法对象键名（含路径穿越）: %s" % object_key)
        return (self.remote_root.rstrip("/") + "/" + key) if self.remote_root != "/" else "/" + key

    def _ensure_dir(self, ftp: ftplib.FTP, remote_path: str) -> None:
        """逐级创建远端目录（已存在忽略）。"""
        parts = [p for p in os.path.dirname(remote_path).split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                ftp.mkd(cur)
            except ftplib.error_perm:
                pass          # 已存在或无权限创建，交给上传阶段暴露真实错误

    # ---------------------------------------------------------------- 接口
    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        if dedup and self._dedup_prepare(file_path):
            return True
        if not os.path.isfile(file_path):
            self.logger.error("[FTP] 源文件不存在: %s", file_path)
            return False
        key = object_key or os.path.basename(file_path)
        remote = self._key_path(key)
        ftp = None
        try:
            ftp = self._conn()
            self._ensure_dir(ftp, remote)
            size = os.path.getsize(file_path)
            try:
                if int(ftp.size(remote)) == size:
                    self.logger.info("[FTP] 远端已存在且大小一致，跳过: %s", remote)
                    return True
            except ftplib.error_perm:
                pass
            with open(file_path, "rb") as f:
                ftp.storbinary("STOR " + remote, f, blocksize=1024 * 1024)
            self.logger.info("[FTP] 已上传: %s（%.1f KB）", remote, size / 1024.0)
            return True
        except Exception as e:
            self.logger.error("[FTP] 上传失败 %s: %s", remote, e)
            return False
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        pass

    def get_file(self, object_key: str, dest_path: str = None) -> Optional[bytes]:
        remote = self._key_path(object_key)
        ftp = None
        try:
            ftp = self._conn()
            if dest_path:
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(dest_path, "wb") as f:
                    ftp.retrbinary("RETR " + remote, f.write, blocksize=1024 * 1024)
                return True
            buf = bytearray()
            ftp.retrbinary("RETR " + remote, buf.extend, blocksize=1024 * 1024)
            return bytes(buf)
        except Exception as e:
            self.logger.error("[FTP] 下载失败 %s: %s", remote, e)
            return None
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def delete_file(self, object_key: str) -> bool:
        remote = self._key_path(object_key)
        ftp = None
        try:
            ftp = self._conn()
            ftp.delete(remote)
            return True
        except Exception as e:
            self.logger.error("[FTP] 删除失败 %s: %s", remote, e)
            return False
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def file_exists(self, object_key: str) -> bool:
        remote = self._key_path(object_key)
        ftp = None
        try:
            ftp = self._conn()
            ftp.size(remote)
            return True
        except Exception:
            return False
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def test_connection(self) -> tuple:
        if not self.host:
            return False, "缺少 FTP 主机地址"
        ftp = None
        try:
            ftp = self._conn()
            try:
                ftp.cwd(self.remote_root)
            except ftplib.error_perm:
                # 根目录不存在时尝试创建后进入，仍失败则如实报错
                self._ensure_dir(ftp, self.remote_root + "/.keep")
                ftp.cwd(self.remote_root)
            return True, "FTP 连接成功（%s%s:%d%s）" % (
                "ftps://" if self.use_tls else "ftp://", self.host, self.port,
                self.remote_root)
        except Exception as e:
            return False, "FTP 连接失败: %s" % str(e)[:200]
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def get_used_space(self) -> int:
        """FTP 无标准容量查询，返回 0（不猜测）。"""
        return 0
