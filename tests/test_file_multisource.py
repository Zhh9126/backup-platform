# -*- coding: utf-8 -*-
"""文件备份多源路径布局 + 断连重试语义单测（2026-11-26 修复回归）。

覆盖：
  - 单源保持历史布局（前缀为空），多源按「去首个 / 的绝对路径」加前缀；
  - 前缀唯一化、根目录 / 不允许与其他源混用；
  - 快照布局指纹（布局变更时增量回退全量，避免误判删除）；
  - 多源打包命令走 ``-C /`` + 前缀（成员名不互相覆盖）；
  - 产物名同一秒内不互相覆盖；
  - "SSH transport dead" / "channel closed without exit status" 判为瞬时。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.engines import file as F  # noqa: E402


def test_single_source_keeps_legacy_layout():
    ents = F._src_entries([("/var/log/", F.SRC_KIND_DIR)])
    assert ents == [{"path": "/var/log", "kind": "dir", "prefix": ""}]
    assert F._entry_key(ents[0], "./sub/a.log") == "sub/a.log"
    assert F._snapshot_layout(ents) == "legacy"


def test_multi_source_prefix_is_path_without_leading_slash():
    ents = F._src_entries([("/log/", F.SRC_KIND_DIR),
                           ("/root", F.SRC_KIND_DIR),
                           ("/etc/my.cnf", F.SRC_KIND_FILE)])
    assert [e["prefix"] for e in ents] == ["log", "root", "etc/my.cnf"]
    # 目录：前缀 + 源内相对路径；文件：键就是前缀本身
    assert F._entry_key(ents[0], "./a1.txt") == "log/a1.txt"
    assert F._entry_key(ents[1], "b1.txt") == "root/b1.txt"
    assert F._entry_key(ents[2], "my.cnf") == "etc/my.cnf"
    assert F._snapshot_layout(ents).startswith("v2multi:")


def test_multi_source_prefix_uniquified_and_root_rejected():
    ents = F._src_entries([("/a", F.SRC_KIND_DIR), ("/a/", F.SRC_KIND_DIR)])
    assert ents[0]["prefix"] != ents[1]["prefix"]
    assert ents[1]["prefix"].startswith("a")
    with pytest.raises(ValueError):
        F._src_entries([("/", F.SRC_KIND_DIR), ("/log", F.SRC_KIND_DIR)])


def test_build_tar_cmd_entries_uses_root_and_prefixes():
    ents = F._src_entries([("/log/", F.SRC_KIND_DIR), ("/root/", F.SRC_KIND_DIR)])
    cmd = F._build_tar_cmd_entries(ents, ["*.tmp"])
    assert "-C /" in cmd
    assert "log" in cmd and "root" in cmd
    assert "--exclude" in cmd
    # 单源仍走老命令（-C 源目录 .），不受影响
    legacy = F._build_tar_cmd([("/log/", F.SRC_KIND_DIR)], ["*.tmp"])
    assert "-C /log/ ." in legacy


def test_unique_archive_path_avoids_overwrite(tmp_path):
    out = str(tmp_path)
    stem = "20261126_120000__task"
    p1 = F.FileBackupEngine._unique_archive_path(out, stem, "inc")
    assert os.path.basename(p1) == "20261126_120000__task__inc.tar.gz"
    open(p1, "wb").write(b"x")
    p2 = F.FileBackupEngine._unique_archive_path(out, stem, "inc")
    assert p2 != p1
    assert os.path.basename(p2) == "20261126_120000__task_2__inc.tar.gz"
    open(p2, "wb").write(b"x")
    p3 = F.FileBackupEngine._unique_archive_path(out, stem, "inc")
    assert os.path.basename(p3) == "20261126_120000__task_3__inc.tar.gz"


def test_transport_errors_are_transient():
    assert F._is_transient(RuntimeError("SSH transport dead"))
    assert F._is_transient(
        RuntimeError("SSH channel closed without exit status（未收到远端退出码）"))
    # 远端 tar 报错不能重试（重试只会重复失败）
    assert not F._is_transient(
        RuntimeError("远端打包落盘失败 rc=2: tar: No space left on device"))
