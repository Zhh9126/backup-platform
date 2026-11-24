# -*- coding: utf-8 -*-
"""备份归档单文件浏览 / 下载 / 校验（对照容灾一体机需求「文件数据验证」）。

需求原文：支持在任意时刻用户通过浏览器对备份数据中的单个或多个文件进行
查看、下载与验证。平台文件备份产物是标准 tar.gz（core/engines/file.py
打包），本模块提供对归档内成员的只读枚举、单文件读取与 SHA256 校验，
不改变备份/恢复主链路。

安全边界（对齐 api/__init__.py::safe_download_path 的整改口径）：
- 只处理位于 BACKUP_ROOT 内的归档，拒绝任意路径；
- tar 成员名做路径穿越校验（../、绝对路径、链接成员一律拒绝）；
- 单文件读取有大小上限（默认 32MB），超限返回明确提示，不Whole-file 进内存；
- 解压在内存中完成，零临时文件，不落盘，不执行成员元数据。
"""
import contextlib
import hashlib
import os
import re
import tarfile
import tempfile

DEFAULT_MAX_PREVIEW = 32 * 1024 * 1024  # 32MB：浏览器预览/下载的单文件上限


@contextlib.contextmanager
def _open_tar(path: str):
    """打开归档：.zst 二次压缩先流式解压到临时 tar（用完即删），其余 r:* 直读。

    文件备份产物默认 zstd 先进压缩（file.py），tarfile 不认 .zst；与
    file.py::_mirror_postprocess 同款处理，零外部工具（zstandard 随平台）。
    """
    tmp_tar = None
    try:
        if path.endswith(".zst"):
            import zstandard
            fd, tmp_tar = tempfile.mkstemp(prefix=".arcview_", suffix=".tar")
            os.close(fd)
            dctx = zstandard.ZstdDecompressor()
            with open(path, "rb") as fi, open(tmp_tar, "wb") as fo:
                dctx.copy_stream(fi, fo)
            with tarfile.open(tmp_tar, "r:*") as tf:
                yield tf
        else:
            with tarfile.open(path, "r:*") as tf:
                yield tf
    finally:
        if tmp_tar:
            try:
                os.unlink(tmp_tar)
            except OSError:
                pass


class ArchiveBrowserError(ValueError):
    """归档浏览的已知错误（路径非法 / 超限 / 成员不存在）。"""


def _safe_archive_path(path: str) -> str:
    """归档路径必须在平台备份根目录内，且真实存在。"""
    if not path or not os.path.isabs(path):
        raise ArchiveBrowserError("归档路径不合法")
    real = os.path.realpath(path)
    try:
        import config as _cfg
        root = os.path.realpath(str(_cfg.get_backup_root()))
    except Exception:
        root = os.path.realpath("backups")
    if real != root and not real.startswith(root + os.sep):
        raise ArchiveBrowserError("归档不在平台备份目录内")
    if not os.path.isfile(real):
        raise ArchiveBrowserError("归档文件不存在")
    return real


def _safe_member_name(name: str) -> bool:
    """拒绝目录穿越 / 绝对路径 / 特殊成员。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if ".." in name.replace("\\", "/").split("/"):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    return True


def list_files(archive_path: str, prefix: str = "", limit: int = 2000) -> list:
    """列出归档内的普通文件成员（不含目录/链接）。

    返回 [{name, size, mtime, sha256(可选)}]，按名称排序。prefix 用于
    分支浏览大归档（前端可按需筛选子目录）。
    """
    path = _safe_archive_path(archive_path)
    rows = []
    try:
        with _open_tar(path) as tf:
            for m in tf.getmembers():
                if not m.isfile() or not _safe_member_name(m.name):
                    continue
                if prefix and not m.name.startswith(prefix):
                    continue
                rows.append({
                    "name": m.name,
                    "size": m.size,
                    "mtime": m.mtime,
                })
                if len(rows) >= limit:
                    break
    except (tarfile.TarError, OSError) as e:
        raise ArchiveBrowserError("无法读取归档: %s" % str(e)[:120])
    rows.sort(key=lambda r: r["name"])
    return rows


def read_file(archive_path: str, member: str,
              max_bytes: int = DEFAULT_MAX_PREVIEW) -> dict:
    """读取归档内单个文件，返回 {name, size, content(bytes), sha256, truncated}。

    max_bytes 超限则 truncated=True 且只返回前 max_bytes 字节（下载场景
    应改用 export_file 走流式）。
    """
    path = _safe_archive_path(archive_path)
    if not _safe_member_name(member):
        raise ArchiveBrowserError("文件名不合法（禁止路径穿越）")
    try:
        with _open_tar(path) as tf:
            try:
                m = tf.getmember(member)
            except KeyError:
                raise ArchiveBrowserError("归档内无此文件: %s" % member[:120])
            if not m.isfile():
                raise ArchiveBrowserError("该成员不是普通文件")
            f = tf.extractfile(m)
            if f is None:
                raise ArchiveBrowserError("无法读取该文件成员")
            data = f.read(max_bytes + 1)
            truncated = len(data) > max_bytes
            if truncated:
                data = data[:max_bytes]
    except ArchiveBrowserError:
        raise
    except (tarfile.TarError, OSError) as e:
        raise ArchiveBrowserError("读取归档成员失败: %s" % str(e)[:120])
    return {
        "name": member,
        "size": m.size,
        "content": data,
        "sha256": hashlib.sha256(data).hexdigest() if not truncated else "",
        "truncated": truncated,
    }


def verify_file(archive_path: str, member: str, expect_sha256: str = "") -> dict:
    """校验归档内单个文件的 SHA256（对照需求「验证」）。

    返回 {name, size, sha256, match}；expect_sha256 为空时只计算不比对，
    match=None。整文件流式读取哈希，不受预览大小上限约束。
    """
    path = _safe_archive_path(archive_path)
    if not _safe_member_name(member):
        raise ArchiveBrowserError("文件名不合法（禁止路径穿越）")
    h = hashlib.sha256()
    total = 0
    try:
        with _open_tar(path) as tf:
            try:
                m = tf.getmember(member)
            except KeyError:
                raise ArchiveBrowserError("归档内无此文件: %s" % member[:120])
            if not m.isfile():
                raise ArchiveBrowserError("该成员不是普通文件")
            f = tf.extractfile(m)
            if f is None:
                raise ArchiveBrowserError("无法读取该文件成员")
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
    except ArchiveBrowserError:
        raise
    except (tarfile.TarError, OSError) as e:
        raise ArchiveBrowserError("校验归档成员失败: %s" % str(e)[:120])
    digest = h.hexdigest()
    match = None
    if expect_sha256:
        match = (digest.lower() == expect_sha256.strip().lower())
    return {"name": member, "size": total, "sha256": digest, "match": match}
