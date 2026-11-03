# -*- coding: utf-8 -*-
"""P0-1 单表切片器回归：tar 布局 / 锚点切片 / 头部净化 / 诚实失败。"""
import gzip
import io
import os
import tarfile

import pytest

from core.table_extract import TableExtractError, list_tables, slice_tables


DUMP_A = """-- MySQL dump 10.13
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET TIME_ZONE='+00:00' */;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `e2e_a` ;
USE `e2e_a`;
--
-- Table structure for table `ta1`
--
DROP TABLE IF EXISTS `ta1`;
/*!40101 SET @saved_cs_client = @@character_set_client */;
CREATE TABLE `ta1` (
  `id` int NOT NULL,
  `name` varchar(50) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
--
-- Dumping data for table `ta1`
--
LOCK TABLES `ta1` WRITE;
INSERT INTO `ta1` VALUES (1,'订单-一'),(2,'二');
UNLOCK TABLES;
--
-- Table structure for table `ta2`
--
DROP TABLE IF EXISTS `ta2`;
CREATE TABLE `ta2` (`id` int);
--
-- Dumping data for table `ta2`
--
INSERT INTO `ta2` VALUES (9);
--
-- Dump completed on 2026-11-03 10:00:00
"""

DUMP_B = """-- MySQL dump 10.13
/*!40101 SET NAMES utf8mb4 */;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `e2e_b` ;
USE `e2e_b`;
--
-- Table structure for table `tb1`
--
DROP TABLE IF EXISTS `tb1`;
CREATE TABLE `tb1` (`id` int);
--
-- Dumping data for table `tb1`
--
INSERT INTO `tb1` VALUES (1);
--
-- Dump completed on 2026-11-03 10:00:00
"""


def _make_tar(tmp_path):
    path = os.path.join(str(tmp_path), "full.tar.gz")
    with tarfile.open(path, "w:gz") as tf:
        for db, dump in (("e2e_a", DUMP_A), ("e2e_b", DUMP_B)):
            data = dump.encode("utf-8")
            info = tarfile.TarInfo(name=f"dbs/{db}.sql")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        mf = b'{"format": "multi-db-tar"}'
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(mf)
        tf.addfile(info, io.BytesIO(mf))
    return path


def _make_sql_gz(tmp_path):
    path = os.path.join(str(tmp_path), "e2e_a.sql.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(DUMP_A)
    return path


def test_list_tables_tar(tmp_path):
    rows = list_tables(_make_tar(tmp_path))
    got = {(r["schema"], r["obj_name"]) for r in rows}
    assert ("e2e_a", "ta1") in got and ("e2e_a", "ta2") in got
    assert ("e2e_b", "tb1") in got


def test_slice_from_tar_single_table(tmp_path):
    tmp, found, notes = slice_tables(_make_tar(tmp_path), ["e2e_a.ta1"])
    try:
        sql = open(tmp, encoding="utf-8").read()
    finally:
        os.unlink(tmp)
    assert [f["obj_name"] for f in found] == ["ta1"]
    assert found[0]["schema"] == "e2e_a"
    assert not notes
    # 含建表与数据
    assert "CREATE TABLE `ta1`" in sql and "订单-一" in sql
    # 头部净化：USE / CREATE DATABASE 已剔除
    assert "USE `e2e_a`" not in sql and "CREATE DATABASE" not in sql
    # 会话前缀保留
    assert "SET NAMES utf8mb4" in sql
    # 其他表不混入
    assert "ta2" not in sql and "tb1" not in sql


def test_slice_from_tar_multi_tables_two_dbs(tmp_path):
    tmp, found, notes = slice_tables(
        _make_tar(tmp_path), ["e2e_a.ta1", "e2e_a.ta2", "e2e_b.tb1"])
    try:
        sql = open(tmp, encoding="utf-8").read()
    finally:
        os.unlink(tmp)
    assert {f["obj_name"] for f in found} == {"ta1", "ta2", "tb1"}
    assert "INSERT INTO `ta2` VALUES (9)" in sql
    assert "INSERT INTO `tb1` VALUES (1)" in sql


def test_slice_from_sql_gz_bare_table(tmp_path):
    tmp, found, notes = slice_tables(_make_sql_gz(tmp_path), ["ta1"])
    try:
        sql = open(tmp, encoding="utf-8").read()
    finally:
        os.unlink(tmp)
    assert [f["obj_name"] for f in found] == ["ta1"]
    assert "订单-一" in sql


def test_missing_table_honest_failure(tmp_path):
    with pytest.raises(TableExtractError, match="未找到指定表"):
        slice_tables(_make_sql_gz(tmp_path), ["no_such_table"])
    with pytest.raises(TableExtractError, match="不存在库"):
        slice_tables(_make_tar(tmp_path), ["ghost_db.ta1"])


def test_view_section_not_picked(tmp_path):
    dump = DUMP_A.replace(
        "--\n-- Table structure for table `ta2`",
        "--\n-- Temporary table structure for view `v1`")
    path = os.path.join(str(tmp_path), "v.sql")
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump)
    rows = list_tables(path)
    assert all(r["obj_name"] != "v1" for r in rows)


def test_oracle_restore_tables_override():
    """P0-1：恢复时 tables 覆盖任务 extra（TABLES 与 SCHEMAS 互斥，TABLES 优先）。"""
    from core.engines.oracle import OracleEngine
    eng = OracleEngine.__new__(OracleEngine)
    merged = eng._restore_extra_with_tables(
        {"schemas": ["SCOTT"], "service": "orcl"}, {"tables": ["T1", "S.T2"]})
    assert merged["tables"] == ["T1", "S.T2"]
    assert "schemas" not in merged          # 互斥剔除
    assert merged["service"] == "orcl"      # 其余 extra 保留
    # 未传 tables：原样返回（零行为变化）
    assert eng._restore_extra_with_tables({"schemas": ["SCOTT"]}, {}) == \
        {"schemas": ["SCOTT"]}
    assert eng._restore_extra_with_tables({}, {"tables": ["  "]}) == {}
