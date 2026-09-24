# -*- coding: utf-8 -*-
"""插件管理：内置组件守卫与离线包源回归——离线可跑。

背景（用户现场缺陷）：
1) Docker 部署下 Percona XtraBackup 2.4/8.0 是镜像内置二进制（物理备份
   引擎零安装推送的源），点卸载只删了插件系统的安装目录，实时探测按 PATH
   又发现内置文件 → 刷新后"还在"。修复：manifest 标记 builtin，卡片显示
   「内置」徽章、卸载被守卫拒绝并说明原因。
2) 离线环境市场安装走在线下载必然失败（urlopen error）。修复：安装优先
   使用 core/plugins/offline_packages/<pid>/ 本地包，失败文案给出放置指引。
"""
import pytest

from core import plugin_catalog as catalog
from core import plugin_installer as installer


BUILTIN_IDS = ("percona-xtrabackup-24", "percona-xtrabackup-80", "mariabackup")


def test_builtin_manifests_declared():
    for pid in BUILTIN_IDS:
        m = catalog.load_all().get(pid)
        assert m, pid
        assert m.get("builtin") is True
        assert m.get("bundled_paths"), pid


def test_builtin_detected_installed_on_dev_machine():
    """开发机/镜像内置路径存在 → installed 且 bundled 标记。"""
    for pid in BUILTIN_IDS:
        chk = catalog.check_installed(catalog.load_all()[pid])
        if chk.get("bundled"):
            assert chk["installed"] is True
            assert chk["missing"] == []


def test_list_rows_carry_builtin_flag():
    rows = {r["id"]: r for r in catalog.list_plugins()}
    for pid in BUILTIN_IDS:
        if rows[pid]["installed"]:
            assert rows[pid]["builtin"] is True, pid
            assert rows[pid]["builtin_note"]
    # 非内置插件不受影响
    assert rows["pgbackrest"]["builtin"] is False


def test_uninstall_builtin_rejected(tmp_path, monkeypatch):
    """卸载内置组件被守卫拒绝（哪怕临时挪走 bundled 路径检测）。"""
    m = catalog.load_all()["percona-xtrabackup-24"]
    assert m.get("builtin")
    res = installer.uninstall("percona-xtrabackup-24")
    assert res["ok"] is False
    assert "内置" in (res["message"] or "")


def test_install_builtin_idempotent():
    res = installer.install("percona-xtrabackup-24")
    assert res["ok"] is True and res.get("installed") is True
    assert "内置" in (res["message"] or "")


def test_find_offline_pkg(tmp_path, monkeypatch):
    pid = "mongodb-database-tools"
    d = installer.OFFLINE_PKG_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    f1 = d / "a.tar.gz"
    f2 = d / "b.tar.gz"
    try:
        f1.write_bytes(b"x" * 10)
        f2.write_bytes(b"y" * 10)
        import os as _os
        _os.utime(f1, (1, 1))          # f2 更新 → 取 f2
        got = installer._find_offline_pkg(pid)
        assert got is not None and got.name == "b.tar.gz"
    finally:
        for p in (f1, f2):
            try:
                p.unlink()
            except OSError:
                pass
    assert installer._find_offline_pkg(pid) is None


def test_download_extract_uses_offline_pkg(tmp_path, monkeypatch):
    """离线包存在时不访问网络，直接解包到 extract_dir。"""
    import io
    import tarfile as _tf
    pid = "pgbackrest"
    d = installer.OFFLINE_PKG_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    pkg = d / "fake.tar.gz"
    with _tf.open(pkg, "w:gz") as tf:
        buf = io.BytesIO(b"#!/bin/sh\necho ok\n")
        info = _tf.TarInfo("bin/pgbackrest")
        info.size = buf.getbuffer().nbytes
        tf.addfile(info, buf)
    try:
        # 策略里的 url 是外网地址：断网/拦截也无法命中——离线包应优先
        strategy = {"url": "https://invalid.example/x.tar.gz",
                    "extract_dir": str(tmp_path / "pg")}
        res = installer._download_and_extract(strategy, pid)
        assert res.get("ok") is True, res
        assert (tmp_path / "pg" / "bin" / "pgbackrest").is_file()
    finally:
        try:
            pkg.unlink()
        except OSError:
            pass
