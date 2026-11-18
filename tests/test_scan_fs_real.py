# -*- coding: utf-8 -*-
"""无备份救援（特殊恢复）回归（2026-11-13）。

背景：该功能曾被质疑"只是壳子"。真机验证结论：**是真实实现**——
SSH 只读 `find` 扫描 + SFTP 拉回 + 逐文件 sha256（170 实测：扫描 200 个文件、
拉回 1 个 2822 字节并算出 sha256）。

因此本次不改功能，而是**重新定位**：从"看起来像常规恢复"改为
「无备份救援（特殊恢复）」，并在页面显式区分能做/不能做。

锁定契约：
1. 实现里不含仿真/假数据路径；
2. 参数安全校验（绝对路径、禁止 .. 与 shell 元字符）不可回退；
3. 菜单与页面必须保持"特殊恢复"的定位文案。
"""
from pathlib import Path

import pytest

from core import scan_fs

SRC = Path(__file__).resolve().parent.parent


def test_implementation_is_real_not_simulated():
    code = (SRC / "core" / "scan_fs.py").read_text(encoding="utf-8").lower()
    for bad in ("simulat", "is_simulated", "fake_", "mock_"):
        assert bad not in code, f"扫描实现里出现仿真标记 {bad}"
    # 真实通道的关键调用必须在
    assert "sftp.get" in code, "缺少真实文件拉回"
    assert "sha256" in code, "缺少完整性校验"
    assert "find " in code or "find \\\"" in code, "缺少真实扫描命令"


def test_scan_rejects_unsafe_roots():
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.scan_host({"id": 1}, roots=["../etc"])
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.scan_host({"id": 1}, roots=["/data; rm -rf /"])
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.scan_host({"id": 1}, roots=["relative/path"])


def test_pull_requires_absolute_paths():
    with pytest.raises(scan_fs.ScanFSError):
        scan_fs.pull_objects({"id": 1}, paths=["relative/file"])


def test_menu_and_page_mark_as_special_recovery():
    base = (SRC / "templates" / "base.html").read_text(encoding="utf-8")
    assert "无备份救援（特殊恢复）" in base
    assert "不自动修复数据库一致性" in base, "菜单 title 应写明不做一致性修复"
    page = (SRC / "templates" / "scan_fs.html").read_text(encoding="utf-8")
    assert "这是「特殊恢复」，不属于常规恢复链路" in page
    assert "不能做" in page and "能做" in page
