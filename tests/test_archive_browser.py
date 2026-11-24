# -*- coding: utf-8 -*-
"""备份归档单文件浏览/下载/校验的契约与安全回归。"""
import hashlib
import io
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import config  # noqa: E402
from core import archive_browser as ab  # noqa: E402


def _make_archive(root):
    """在备份根目录下造一个含中文名/二进制/子目录的 tar.gz。"""
    backup_root = os.path.join(root, "backups")
    os.makedirs(backup_root, exist_ok=True)
    old = config.get_backup_root
    config.BACKUP_ROOT = backup_root
    config.get_backup_root = lambda: backup_root
    arc = os.path.join(backup_root, "task1__full.tar.gz")
    with tarfile.open(arc, "w:gz") as tf:
        for name, data in [
            ("docs/说明.txt", "订单-甲\n中文内容".encode("utf-8")),
            ("bin/data.bin", bytes(range(256))),
            ("empty.txt", b""),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return arc, backup_root, old


def test_list_and_read_and_verify(tmp_path=None):
    import io as _io
    root = tempfile.mkdtemp(prefix="arcbr_")
    arc, backup_root, old = _make_archive(root)
    try:
        files = ab.list_files(arc)
        names = [f["name"] for f in files]
        assert "docs/说明.txt" in names and "bin/data.bin" in names

        r = ab.read_file(arc, "docs/说明.txt")
        assert r["content"].decode("utf-8").endswith("中文内容")
        assert r["sha256"] == hashlib.sha256(r["content"]).hexdigest()
        assert r["truncated"] is False

        v = ab.verify_file(arc, "docs/说明.txt", r["sha256"])
        assert v["match"] is True
        bad = ab.verify_file(arc, "docs/说明.txt", "0" * 64)
        assert bad["match"] is False

        # 前缀筛选
        assert all(f["name"].startswith("docs/") for f in ab.list_files(arc, prefix="docs/"))
    finally:
        config.get_backup_root = old


def test_security_rejects_path_traversal_and_outside_root():
    import io as _io
    root = tempfile.mkdtemp(prefix="arcsec_")
    arc, backup_root, old = _make_archive(root)
    try:
        for bad in ["../etc/passwd", "/etc/passwd", "a/../../b", "C:\\windows"]:
            try:
                ab.read_file(arc, bad)
                assert False, "应拒绝: %s" % bad
            except ab.ArchiveBrowserError:
                pass
        # 备份根目录之外的归档应拒绝
        outside = os.path.join(root, "outside.tar.gz")
        with tarfile.open(outside, "w:gz") as tf:
            pass
        try:
            ab.list_files(outside)
            assert False, "应拒绝根目录外归档"
        except ab.ArchiveBrowserError:
            pass
    finally:
        config.get_backup_root = old


def test_preview_truncation():
    root = tempfile.mkdtemp(prefix="arctrunc_")
    arc, backup_root, old = _make_archive(root)
    try:
        r = ab.read_file(arc, "docs/说明.txt", max_bytes=5)
        assert r["truncated"] is True
        assert len(r["content"]) == 5
        assert r["sha256"] == ""
    finally:
        config.get_backup_root = old
