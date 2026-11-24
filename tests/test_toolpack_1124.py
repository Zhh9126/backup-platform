# -*- coding: utf-8 -*-
"""ToolPack 免安装工具包 + CDP 裸块 + Mongo oplog 契约回归。"""
import os
import sys
import inspect
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

from core import toolpack  # noqa: E402
from core.engines import mongodb  # noqa: E402


def test_toolpack_local_path_mongo():
    """平台随包目录能解析 mongodump/mongorestore（离线包已烘焙）。"""
    for name in ("mongodump", "mongorestore"):
        p = toolpack.local_path(name)
        assert p and os.path.isfile(p), "ToolPack 缺少 %s" % name


def test_toolpack_elf_arch_and_cleanup():
    p = toolpack.local_path("mongodump")
    if p:
        assert toolpack.elf_arch(p) in ("x86_64", "aarch64", "arm", "")
    suffix = toolpack.cleanup_suffix("/tmp/aidbm_tool_x_123")
    assert "rm -f" in suffix and "exit $" in suffix
    assert toolpack.cleanup_suffix("") == ""


def test_remote_mongo_uses_toolpack_and_oplog():
    """远端 mongodump 缺失时必须走 ToolPack 推送，且 oplog 选项进入命令。"""
    src = inspect.getsource(toolpack.push_tool.__module__ and
                            __import__("core.remote_dump", fromlist=["x"])._remote_mongodb_dump_shell)
    assert "toolpack.push_tool" in src
    assert "--oplog" in src
    assert "PIPESTATUS[0]" in src, "压缩管道须保留 mongodump 真实 rc"


def test_mongo_engine_oplog_and_same_db_guard():
    src = inspect.getsource(mongodb.MongoEngine.restore)
    assert "oplogReplay" in src and "oplog_limit" in src
    assert "--oplogReplay 与 --nsFrom" in src, "须显式拒绝 oplog+异名库语义冲突"
    local_src = inspect.getsource(mongodb.MongoEngine._backup_local)
    assert "_resolve_local_tool" in local_src
    assert "--oplog" in local_src


def test_cdp_raw_block_branch():
    """CDP 位图必须有真裸块分支（stat.S_ISBLK）并标注 raw_block。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent"))
    import plugins
    src = inspect.getsource(plugins._cdp_bitmap)
    assert "stat.S_ISBLK" in src
    assert '"raw_block"' in src
    assert "os.read(fd, block)" in src
