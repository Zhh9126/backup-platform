# -*- coding: utf-8 -*-
"""存储介质与「存储管理」二级菜单回归（2026-11-13）。

覆盖：
1. 新增后端 mount（外挂/NFS 挂载点）与 ftp（标准库 ftplib）可用；
2. 类型注册表/元信息/依赖清单包含新介质；
3. 侧边栏「存储管理」为折叠组，子项是介质类（总览 / 存储池 / 磁带库 / FTP / NFS / 外挂）；
   对象存储备份与 NAS 存储属于「备份管理」组；
4. 「数据库备份」任务表单的类型下拉不含 NAS / 对象存储 / 虚拟机 / 文件。
"""
import os
import re
from pathlib import Path

import pytest

from core.storage_backends import (
    BACKEND_REGISTRY, TYPE_META, get_backend, list_supported_types, check_dependencies,
)
from core.storage_backends.ftp import FTPStorageBackend
from core.storage_backends.mount import MountStorageBackend

ROOT = Path(__file__).resolve().parent.parent
BASE_HTML = ROOT / "templates" / "base.html"


def test_new_media_types_registered():
    assert "mount" in BACKEND_REGISTRY and "ftp" in BACKEND_REGISTRY
    types = {t["type"] for t in list_supported_types()}
    assert {"mount", "ftp"} <= types
    assert TYPE_META["mount"]["name"] and TYPE_META["ftp"]["name"]
    deps = check_dependencies()
    assert deps.get("mount") is True and deps.get("ftp") is True


def test_mount_backend_roundtrip(tmp_path):
    be = get_backend("mount", {"endpoint": str(tmp_path)})
    assert isinstance(be, MountStorageBackend)
    src = tmp_path / "artifact.bin"
    src.write_bytes(b"hello-mount" * 100)
    assert be.save_file(str(src), "a/b/artifact.bin") is True
    assert be.file_exists("a/b/artifact.bin") is True
    dest = tmp_path / "out" / "copy.bin"
    assert be.get_file("a/b/artifact.bin", str(dest)) is True
    assert dest.read_bytes() == src.read_bytes()
    ok, msg = be.test_connection()
    assert ok, msg
    assert "挂载点" in msg or "普通目录" in msg
    assert be.delete_file("a/b/artifact.bin") is True


def test_mount_rejects_path_traversal(tmp_path):
    """路径穿越必须被拒绝（本地后端内部捕获异常并返回 False，不落盘）。"""
    be = get_backend("mount", {"endpoint": str(tmp_path)})
    assert be.save_file(__file__, "../escape.bin") is False
    assert not (tmp_path.parent / "escape.bin").exists()


def test_ftp_key_path_and_conn_guard():
    be = FTPStorageBackend({"host": "", "remote_path": "backup/aidbm"})
    assert be._key_path("x/y.zip") == "/backup/aidbm/x/y.zip"
    with pytest.raises(ValueError):
        be._key_path("../etc/passwd")
    ok, msg = be.test_connection()
    assert ok is False and "主机" in msg


def test_menu_has_storage_group():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert 'id="storageNavParent"' in html, "缺少「存储管理」折叠组"
    assert 'id="storageNavSub"' in html, "缺少二级菜单容器"
    assert 'href="/storage"' in html
    assert 'href="/object_storage"' in html
    assert 'href="/tape"' in html
    assert 'href="/synology"' in html
    assert 'href="/storage?kind=ftp"' in html
    assert 'href="/storage?kind=nfs"' in html
    assert 'href="/storage?kind=mount"' in html
    # 存储管理二级菜单 = 介质与落点（6 项）：总览 / 存储池 / 磁带库 / FTP / NFS / 外挂
    sub = html.split('id="storageNavSub"', 1)[1].split("</div>", 1)[0]
    assert len(re.findall(r'class="nav-link', sub)) >= 6
    for label in ("存储介质总览", "存储池配置", "磁带库", "FTP 存储", "NFS 挂载", "外挂存储"):
        assert label in sub, "存储管理子菜单缺少: %s" % label
    # 备份对象入口必须留在「备份管理」组里（用户口径：NAS/对象存储属于备份，
    # 不属于"存储介质"配置），且不能重复出现在存储管理子菜单里。
    assert "对象存储备份" not in sub, "对象存储备份不应在存储管理子菜单"
    assert "NAS 存储（群晖）" not in sub, "NAS 存储不应在存储管理子菜单"
    backup_group = html.split("备份管理", 1)[1].split('id="storageNavParent"', 1)[0]
    assert "对象存储备份" in backup_group, "「备份管理」组必须含对象存储备份入口"
    assert "NAS 存储（群晖）" in backup_group, "「备份管理」组必须含 NAS 存储（群晖）入口"


def test_task_form_db_type_excludes_non_database():
    """「数据库备份」任务类型下拉不能出现 NAS / 对象存储 / 虚拟机 / 文件。"""
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'fillDbTypeSelect(dbTypeSel, ["file", "vm", "object_storage", "synology"])' in app_js, \
        "任务表单数据库类型下拉未排除非数据库类型"
    # 任务列表（数据库备份视图）同样要排除，避免列表里冒出 NAS/对象存储任务
    assert "file,vm,object_storage,synology" in app_js, "数据库备份列表未排除非数据库任务"
