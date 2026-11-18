# -*- coding: utf-8 -*-
"""整洁化回归：消除重复造轮子 + 登出行为（2026-11-14）。

本轮全量巡检发现两类问题：

1. **同一逻辑在多个模块各写一份**（真正的"重复造轮子"）：
   - sha256（7 处）、epoch（2 处）、operator（2 处）、cell_str（2 处）；
2. **登出后访问受管页面返回 403**（"功能项不可用"），而正确行为是跳登录页——
   原因是 before_request 用空 user 做功能判定，抢在 login_required 之前拦截。

这里锁死"收敛后仍然只保留一处实现"+"行为不回退"。
"""
import ast
import inspect
from pathlib import Path

import auth
import core.db as db
from core.cdc import util as cdc_util
from core.rt_backup import common as rt_common

SRC = Path(__file__).resolve().parent.parent


def test_sha256_implementations_delegate_to_db():
    """各模块的 _sha256 必须是薄封装，不得再各自写循环。"""
    files = [
        "core/plugin_bundle.py", "core/remote_pull.py",
        "core/storage_backends/tape.py", "core/engines/synology.py",
        "core/objectstore/resume.py",
    ]
    for rel in files:
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "sha256_file" in src, rel
        # 不允许再自建 hashlib 哈希循环（统一走 db.sha256_file / sha256_stream）
        assert "hashlib.sha256()" not in src, f"{rel} 仍有自建 sha256 实现"


def test_file_sha256_delegates():
    for rel in ("core/dedup_source.py", "core/rsync_delta.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "sha256_file" in src, rel
        assert "hashlib.sha256()" not in src, f"{rel} 仍有自建实现"


def test_stream_hash_single_source():
    """tar 成员流哈希（磁带/群晖校验）也必须走 db.sha256_stream。"""
    for rel in ("core/storage_backends/tape.py", "core/engines/synology.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "sha256_stream" in src, rel
        assert "hashlib.sha256()" not in src, f"{rel} 仍有自建流式哈希"


def test_epoch_and_operator_and_cell_str_single_source():
    assert rt_common.epoch_of("2026-11-14T03:00:00+08:00") > 0
    assert rt_common.epoch_of("not-a-time") == 0.0
    assert rt_common.epoch_of("") == 0.0
    # current_operator 依赖 request context，这里只校验「唯一实现 + 调用方委托」
    assert callable(auth.current_operator)
    for rel in ("api/ransomware.py", "api/tape.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "current_operator" in src, rel
    assert cdc_util.cell_str([1, "x"], 0) == "1"
    assert cdc_util.cell_str([None], 0) == ""
    assert cdc_util.cell_str([], 5) == ""
    # db_rt / file_rt 的 _epoch 也应委托
    for rel in ("core/rt_backup/db_rt.py", "core/rt_backup/file_rt.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "common import epoch_of" in src, rel


def test_logout_redirects_instead_of_403():
    """登出后访问受管页面必须 302 跳登录（而非 403 功能项不可用）。"""
    src = (SRC / "app.py").read_text(encoding="utf-8")
    seg = src.split("def _touch_session()", 1)[1][:2200]
    assert "if not _user:" in seg, "before_request 未对未登录会话放行"
    assert "return None" in seg


def test_no_bare_duplicate_helper_left():
    """确保收敛后没有把 helper 又复制回多个文件（防回退）。"""
    hits = []
    for base in ("core", "api"):
        for p in (SRC / base).rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            src = p.read_text(encoding="utf-8")
            if "def _cell_str(row, index: int) -> str:" in src and "util import cell_str" not in src:
                hits.append(str(p))
    assert not hits, f"cell_str 又出现重复实现: {hits}"
