# -*- coding: utf-8 -*-
"""清单/相对路径处理单测（2026-11-26 隐藏文件回归）。

锁定一个真实缺陷：``"./x".lstrip("./")`` 是"字符集合"剥离，会把隐藏文件的
前导点一起吃掉（``'./.hidden.txt'`` → ``'hidden.txt'``），导致远端路径还原
错、SFTP 拉取 ENOENT、恢复后文件名少一个点。凡涉及相对路径还原的位置都
必须用"只剥一次 ./ 前缀"的语义。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import dedup_source as ds  # noqa: E402


def test_rel_key_preserves_hidden_dot():
    assert ds.rel_key("./.hidden.txt") == ".hidden.txt"
    assert ds.rel_key("./.config/app.conf") == ".config/app.conf"
    assert ds.rel_key("./a/.b/c") == "a/.b/c"
    assert ds.rel_key("./plain.txt") == "plain.txt"
    assert ds.rel_key("plain.txt") == "plain.txt"


def test_manifest_keeps_hidden_prefix():
    out = (
        "aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889990  ./.hidden.txt\n"
        "MSTAT\n"
        "./.hidden.txt|5|1700000000|644|0|0|12345|1\n"
        "MDIR\n"
        "./|1700000000|755|0|0\n"
        "MLNK\n"
        "MDONE\n"
    )
    m = ds.parse_file_manifest(out, "/src")
    assert "./.hidden.txt" in m["files"], "隐藏文件名必须保留前导点"
    assert m["files"]["./.hidden.txt"]["size"] == 5
    assert m["files"]["./.hidden.txt"]["ino"] == 12345
    assert m["files"]["./.hidden.txt"]["nlink"] == 1
    assert "./" in m["dirs"], "目录条目应被解析（含根目录）"


def test_manifest_links_v2_shape():
    out = (
        "MSTAT\n"
        "MDIR\n"
        "MLNK\n"
        "./.lnk|target.txt|1700000000|777|0|0\n"
        "./plain_link|other|1700000000|777|0|0\n"
        "MDONE\n"
    )
    m = ds.parse_file_manifest(out, "/src")
    assert m["links"]["./.lnk"]["target"] == "target.txt"
    assert m["links"]["./.lnk"]["mtime"] == 1700000000
    assert m["links"]["./plain_link"]["target"] == "other"


def test_remote_abs_uses_rel_key():
    """远端路径还原必须只剥一次 './'（隐藏文件才能被 SFTP 找到）。"""
    rel = "./.hidden.txt"
    src_root = "/data"
    abs_p = src_root + "/" + ds.rel_key(rel)
    assert abs_p == "/data/.hidden.txt"
