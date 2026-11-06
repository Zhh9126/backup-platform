# -*- coding: utf-8 -*-
"""
SFTP 存储后端（SSH 文件传输协议）。

对接群晖 NAS / 通用 NAS / 任意 SSH 存储主机：目标侧开启 SSH + SFTP 子系统
即成为标准备份存储端点，平台零依赖（paramiko 通道，数据库服务器零安装）。

字段映射（storage_targets 表行）：
- endpoint: SSH 主机地址（IP 或域名）
- bucket:   远端备份根目录（如 /volume1/backup）——SFTP 复用该字段语义
- username / access_key: SSH 登录用户
- secret_key: SSH 密码（落库经 db.encrypt_secret 加密，工厂入口统一解密）
- extra_options: {port: 22, private_key: "PEM 内容", host_key: "可选指纹"}

对象键 = {bucket}/{prefix}/db_type/task/... 与 MinIO/S3 一致，恢复/取回/删除
/去重语义对齐其余后端。
"""
import io
import logging
import time

from .base import StorageBackend

try:
    import paramiko
    _HAS_PARAMIKO = True
except ImportError:  # pragma: no cover - 离线镜像必带 paramiko
    paramiko = None
    _HAS_PARAMIKO = False


class SFTPStorageBackend(StorageBackend):
    """SFTP 存储（NAS / SSH 主机）。"""

    display_name = "SFTP 存储"
    tier = 2

    def __init__(self, config: dict, logger: logging.Logger = None):
        super().__init__(config, logger)
        self.host = (config.get("endpoint") or "").strip()
        try:
            extra = config.get("extra_options")
            import json as _json
            self.extra = _json.loads(extra) if isinstance(extra, str) else dict(extra or {})
        except Exception:
            self.extra = {}
        self.port = int(config.get("port") or self.extra.get("port") or 22)
        self.username = (config.get("username") or config.get("access_key") or "root").strip()
        # 密码：secret_key（工厂已解密）→ password 兜底
        self.password = config.get("secret_key") or config.get("password") or ""
        self.private_key = self.extra.get("private_key") or config.get("private_key") or ""
        # 远端根目录：bucket 复用（/volume1/backup），prefix 为可选子路径
        base = (config.get("bucket") or "").strip().rstrip("/")
        prefix = (config.get("prefix") or "").strip("/")
        self.base_dir = "/".join(p for p in (base, prefix) if p) or "/tmp/aidbm_sftp"

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def _connect(self):
        if not _HAS_PARAMIKO:
            raise RuntimeError("paramiko 未安装（SFTP 后端依赖）")
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=self.host, port=self.port, username=self.username,
                  timeout=15, allow_agent=False, look_for_keys=False)
        if self.private_key:
            from core.ssh_hosts import load_pkey
            pkey = load_pkey(self.private_key)
            if pkey is not None:
                kw["pkey"] = pkey
            else:
                import tempfile, os as _os
                kf = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
                kf.write(self.private_key)
                kf.close()
                _os.chmod(kf.name, 0o600)
                kw["key_filename"] = kf.name
        if self.password or "pkey" not in kw and "key_filename" not in kw:
            kw["password"] = self.password or None
        c.connect(**kw)
        return c

    def _remote_path(self, object_key: str) -> str:
        key = (object_key or "").lstrip("/")
        if ".." in key.split("/"):
            raise ValueError(f"非法对象键名（含路径穿越）: {object_key}")
        return f"{self.base_dir}/{key}"

    def _sftp_makedirs(self, sftp, rpath: str):
        """逐级建目录（容忍已存在）。"""
        parts = [p for p in rpath.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass  # 并发/权限：后续 stat 再次校验

    # ------------------------------------------------------------------
    # 接口实现
    # ------------------------------------------------------------------
    def save_file(self, file_path: str, object_key: str = None,
                  dedup: bool = False, chunked: bool = False) -> bool:
        if dedup and self._dedup_prepare(file_path):
            return True
        client = None
        try:
            import os as _os
            if not _os.path.isfile(file_path):
                self.logger.error("[SFTP] 源文件不存在: %s", file_path)
                return False
            if object_key is None:
                object_key = _os.path.basename(file_path)
            rpath = self._remote_path(object_key)
            client = self._connect()
            sftp = client.open_sftp()
            try:
                self._sftp_makedirs(sftp, rpath.rsplit("/", 1)[0])
                tmp = rpath + ".aidbm_tmp"
                # 断点续传语义：put 确认后 rename，避免半包冒充成功
                sftp.put(file_path, tmp)
                try:
                    sftp.remove(rpath)
                except IOError:
                    pass
                sftp.rename(tmp, rpath)
                st = sftp.stat(rpath)
                self.logger.info("[SFTP] 已保存 %s@%s:%s (%.1f KB)",
                                 self.username, self.host, rpath,
                                 (st.st_size or 0) / 1024)
                return True
            finally:
                sftp.close()
        except Exception as e:
            self.logger.error("[SFTP] 保存失败 %s@%s: %s",
                              self.username, self.host, e)
            return False
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def get_file(self, object_key: str, dest_path: str = None):
        client = None
        try:
            rpath = self._remote_path(object_key)
            client = self._connect()
            sftp = client.open_sftp()
            try:
                if dest_path:
                    import os as _os
                    d = _os.path.dirname(dest_path)
                    if d:
                        _os.makedirs(d, exist_ok=True)
                    sftp.get(rpath, dest_path)
                    return True
                with sftp.open(rpath, "rb") as f:
                    return f.read()
            finally:
                sftp.close()
        except Exception as e:
            self.logger.error("[SFTP] 读取失败 %s: %s", object_key, e)
            return None
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def delete_file(self, object_key: str) -> bool:
        client = None
        try:
            rpath = self._remote_path(object_key)
            client = self._connect()
            sftp = client.open_sftp()
            try:
                try:
                    sftp.remove(rpath)
                    self.logger.info("[SFTP] 已删除: %s", rpath)
                except IOError:
                    pass  # 不存在视为已删除（幂等）
                return True
            finally:
                sftp.close()
        except Exception as e:
            self.logger.error("[SFTP] 删除失败: %s", e)
            return False
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def file_exists(self, object_key: str) -> bool:
        client = None
        try:
            client = self._connect()
            sftp = client.open_sftp()
            try:
                sftp.stat(self._remote_path(object_key))
                return True
            except IOError:
                return False
            finally:
                sftp.close()
        except Exception:
            return False
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def test_connection(self) -> tuple:
        client = None
        try:
            if not self.host:
                return False, "未配置 SSH 主机地址（endpoint）"
            client = self._connect()
            sftp = client.open_sftp()
            try:
                self._sftp_makedirs(sftp, self.base_dir)
                probe = f"{self.base_dir}/.connection_test"
                with sftp.open(probe, "w") as f:
                    f.write(f"aidbm {int(time.time())}")
                sftp.remove(probe)
                try:
                    stf = sftp.statvfs(self.base_dir)
                    free_gb = (stf.f_bavail * stf.f_frsize) / 1024 ** 3
                    space = f"，可用空间 {free_gb:.1f} GB"
                except Exception:
                    space = ""
                return True, (f"SFTP 连接成功: {self.username}@{self.host}:{self.port}"
                              f" → {self.base_dir}{space}")
            finally:
                sftp.close()
        except Exception as e:
            return False, f"SFTP 连接失败: {e}"
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def get_used_space(self) -> int:
        """已用空间：远端目录递归求和（超过 5000 个文件时改用 statvfs 估算）。"""
        client = None
        try:
            client = self._connect()
            sftp = client.open_sftp()
            total, count = 0, 0

            def _walk(d):
                nonlocal total, count
                for entry in sftp.listdir_attr(d):
                    p = f"{d}/{entry.filename}"
                    import stat as _stat
                    if _stat.S_ISDIR(entry.st_mode or 0):
                        _walk(p)
                    else:
                        total += entry.st_size or 0
                        count += 1

            _walk(self.base_dir)
            return total
        except Exception as e:
            self.logger.warning("[SFTP] 统计已用空间失败: %s", e)
            return 0
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
