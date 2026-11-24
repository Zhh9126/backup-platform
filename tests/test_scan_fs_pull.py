# -*- coding: utf-8 -*-
"""特殊恢复（无备份救援）拉回增强的契约与安全回归。"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import config  # noqa: E402
from core import scan_fs  # noqa: E402


class FakeStat:
    def __init__(self, size, mtime=1000):
        self.st_size = size
        self.st_mtime = mtime


class FakeSFTP:
    def __init__(self, files):
        self.files = files  # {path: bytes}

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return FakeStat(len(self.files[path]))

    def open(self, path, mode):
        import io
        if path not in self.files:
            raise FileNotFoundError(path)
        return io.BytesIO(self.files[path])

    def close(self):
        pass


class FakeClient:
    def __init__(self, files):
        self._sftp = FakeSFTP(files)

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


def _setup(tmp):
    old = config.get_backup_root
    config.BACKUP_ROOT = tmp
    config.get_backup_root = lambda: tmp
    os.environ["BACKUP_ROOT"] = tmp
    return old


def test_pull_rejects_over_500_files():
    """超 500 文件直接报错，不再静默截断（假成功）。"""
    tmp = tempfile.mkdtemp(prefix="sfsp_")
    old = _setup(tmp)
    try:
        paths = ["/data/f%d" % i for i in range(501)]
        try:
            scan_fs.pull_objects({"host": "h"}, paths, dest_dir=tmp)
            assert False, "应拒绝超 500 文件"
        except scan_fs.ScanFSError as e:
            assert "500" in str(e)
    finally:
        config.get_backup_root = old


def test_pull_sha256_and_cache_resume(tmp_path=None):
    """拉回落 sha256 sidecar；同路径二次拉回命中缓存不重传。"""
    import hashlib
    tmp = tempfile.mkdtemp(prefix="sfsr_")
    old = _setup(tmp)
    files = {"/data/订单.txt": "订单-甲 中文".encode("utf-8")}
    fake = FakeClient(files)
    try:
        with mock.patch.object(scan_fs, "_client", return_value=fake):
            r1 = scan_fs.pull_objects(
                {"host": "192.168.1.1"}, ["/data/订单.txt"], dest_dir=tmp)
            assert r1["ok"] is True and r1["pulled_count"] == 1
            assert r1["pulled"][0]["sha256"] == hashlib.sha256(
                files["/data/订单.txt"]).hexdigest()
            assert r1["pulled"][0]["cached"] is False
            # 二次：命中缓存
            r2 = scan_fs.pull_objects(
                {"host": "192.168.1.1"}, ["/data/订单.txt"], dest_dir=tmp)
            assert r2["ok"] is True and r2["pulled"][0]["cached"] is True
    finally:
        config.get_backup_root = old


def test_pull_ok_reflects_failures():
    """全部失败时 ok=False，不再恒 True。"""
    tmp = tempfile.mkdtemp(prefix="sfsf_")
    old = _setup(tmp)
    fake = FakeClient({})  # 空：stat 必失败
    try:
        with mock.patch.object(scan_fs, "_client", return_value=fake):
            r = scan_fs.pull_objects(
                {"host": "h"}, ["/data/missing"], dest_dir=tmp)
            assert r["ok"] is False
            assert r["failed_count"] == 1
            assert r["pulled_count"] == 0
    finally:
        config.get_backup_root = old


def test_pull_api_requires_manage_permission():
    """拉回接口（写操作）必须 backup.task.manage，不能仅登录。"""
    import inspect
    from api import scan_fs as api_mod
    src = inspect.getsource(api_mod.scan_fs_pull)
    assert "@permission_required(\"backup.task.manage\")" in src
