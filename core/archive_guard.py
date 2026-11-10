# -*- coding: utf-8 -*-
"""归档安全解包：拒绝路径穿越、绝对路径、链接与设备文件。

备份摆渡包、插件离线包、跨主机恢复 tar 都可能来自平台外部，直接
``tar.extractall`` 会因 ``../../`` 成员名或符号链接写穿目标目录。本模块
提供统一的成员过滤器，供所有解包入口共用。

设计边界：只校验归档成员，不校验解压后的磁盘配额/文件数量上限；超大归档
仍应由调用方的大小限制兜底。
"""
import os
import tarfile
import zipfile


def _member_name(name: str) -> str:
    # Windows 盘符 / UNC 路径在 Linux 解包会被当成文件名，同样拒绝
    return str(name or "").replace("\\", "/")


def is_safe_member(name: str, dest_dir: str) -> bool:
    """成员名是否可安全解到 dest_dir 内。"""
    n = _member_name(name)
    if not n or n.startswith("/") or n.startswith("//"):
        return False
    if ":" in n.split("/")[0]:
        return False
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    root = os.path.realpath(dest_dir)
    target = os.path.realpath(os.path.join(root, n))
    return target == root or target.startswith(root + os.sep)


def _tar_ok(member: tarfile.TarInfo, dest_dir: str) -> bool:
    if not is_safe_member(member.name, dest_dir):
        return False
    # 符号链接/硬链接/设备/FIFO 都可被用来写穿目录或制造设备节点
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        return False
    return True


def safe_extract_tar(tf: tarfile.TarFile, dest_dir: str) -> int:
    """安全解包 tar，返回解出的成员数；遇到危险成员抛 ValueError。"""
    os.makedirs(dest_dir, exist_ok=True)
    safe = []
    for m in tf.getmembers():
        if not _tar_ok(m, dest_dir):
            raise ValueError("归档含危险成员，已拒绝解包: %s" % m.name)
        safe.append(m)
    tf.extractall(dest_dir, members=safe)
    return len(safe)


def safe_extract_zip(zf: zipfile.ZipFile, dest_dir: str) -> int:
    """安全解包 zip，返回解出的成员数；遇到危险成员抛 ValueError。"""
    os.makedirs(dest_dir, exist_ok=True)
    safe = []
    for info in zf.infolist():
        if not is_safe_member(info.filename, dest_dir):
            raise ValueError("归档含危险成员，已拒绝解包: %s" % info.filename)
        # zip 无标准符号链接标记，用外部属性位粗判（Unix symlink S_IFLNK）
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError("归档含符号链接，已拒绝解包: %s" % info.filename)
        safe.append(info)
    zf.extractall(dest_dir, members=safe)
    return len(safe)
