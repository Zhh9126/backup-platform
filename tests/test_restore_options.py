# -*- coding: utf-8 -*-
"""恢复选项细化（跳过已存在表 / Mongo oplog 时点 / 扇区雕刻）契约回归。"""
import os
import sys
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "agent"))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import plugins  # noqa: E402
from core import table_extract  # noqa: E402


DUMP = """-- MySQL dump 10.13
--
-- Table structure for table `t1`
--
DROP TABLE IF EXISTS `t1`;
CREATE TABLE `t1` (`id` int, `name` varchar(20));
--
-- Dumping data for table `t1`
--
INSERT INTO `t1` VALUES (1,'甲'),(2,'乙');
-- Dump completed
"""


def test_skip_existing_rewrites_sql(tmp_path):
    """跳过已存在表：去掉 DROP、CREATE→IF NOT EXISTS、INSERT→IGNORE。"""
    p = tmp_path / "d.sql"
    p.write_text(DUMP, encoding="utf-8")
    tmp, found, notes = table_extract.slice_tables(
        str(p), ["t1"], skip_existing=True)
    try:
        sql = open(tmp, encoding="utf-8").read()
    finally:
        os.unlink(tmp)
    assert "DROP TABLE" not in sql
    assert "CREATE TABLE IF NOT EXISTS `t1`" in sql
    assert "INSERT IGNORE INTO `t1`" in sql
    assert any("跳过已存在表" in n for n in notes)


def test_default_slice_unchanged(tmp_path):
    """未开启 skip_existing 时切片语义与旧版完全一致（零回归）。"""
    p = tmp_path / "d.sql"
    p.write_text(DUMP, encoding="utf-8")
    tmp, found, notes = table_extract.slice_tables(str(p), ["t1"])
    try:
        sql = open(tmp, encoding="utf-8").read()
    finally:
        os.unlink(tmp)
    assert "DROP TABLE IF EXISTS `t1`" in sql
    assert "INSERT INTO `t1`" in sql
    assert "IGNORE" not in sql


def test_mongo_oplog_args():
    """Mongo oplog 时点：命令构造含 --oplogReplay/--oplogLimit 且校验格式。"""
    from core.engines.mongodb import MongoEngine
    src = inspect.getsource(MongoEngine.restore)
    assert "--oplogReplay" in src and "--oplogLimit" in src
    assert "oplog_limit" in src
    assert "BSON" in src or "seconds" in src


def test_block_carve_plugin():
    """扇区雕刻：插件注册 + 签名表含常见格式 + 不写源端。"""
    classes = plugins.describe(None)
    names = [p["name"] for p in classes]
    assert "block-carve" in names
    carve = [p for p in classes if p["name"] == "block-carve"][0]
    assert carve["supported"] and set(carve["actions"]) == {"scan", "extract"}
    assert any(sig == b"%PDF-" for sig, _ in plugins._FILE_SIGNATURES)
    assert any(sig.startswith(b"\x89PNG") for sig, _ in plugins._FILE_SIGNATURES)
    src = inspect.getsource(plugins._carve_scan)
    assert '"rb"' in src  # 只读打开


def test_consistency_check_plugin():
    """一致性检测：插件存在、提供修复建议但 auto_execute=False。"""
    import tempfile
    p = tempfile.mkdtemp(prefix="cons_")
    fp = os.path.join(p, "ibd1")
    with open(fp, "wb") as f:
        f.write(b"\x00" * 1024)
    r = plugins.invoke("block-cdp", "consistency_check",
                       {"path": fp, "db_type": "generic"}, None)
    assert r["ok"] and "checks" in r
    assert r["repair_plan"]["auto_execute"] is False
    assert len(r["repair_plan"]["suggestions"]) >= 3


def test_restore_api_wires_options():
    """API/scheduler 把 skip_existing_tables 与 oplog 参数贯通。"""
    from api import restore as api_restore
    from core import scheduler
    assert "skip_existing_tables" in inspect.getsource(api_restore.create_restore)
    assert "oplog_replay" in inspect.getsource(api_restore.create_restore)
    sig = inspect.signature(scheduler.run_restore_now)
    assert "skip_existing_tables" in sig.parameters
    src = inspect.getsource(scheduler.run_restore_now)
    assert "oplog_replay" in src
