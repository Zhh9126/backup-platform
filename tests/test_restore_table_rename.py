# -*- coding: utf-8 -*-
"""表级恢复「指定表 + 改表名 + UAT 异名库 + 跨主机」回归。

R3/R4 由 /root/e2e/e2e_restore_uat_170171.py 在 170/171 真机验证；
本文件覆盖切片器改写规则、非法映射拦截与全链路参数契约。
"""
import os

import pytest

from core.table_extract import TableExtractError, slice_tables

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DUMP = """-- MySQL dump 10.13
/*!40101 SET NAMES utf8mb4 */;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `src_db` ;
USE `src_db`;
--
-- Table structure for table `t_order`
--
DROP TABLE IF EXISTS `t_order`;
CREATE TABLE `t_order` (
  `id` int NOT NULL,
  `name` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`)
);
--
-- Dumping data for table `t_order`
--
LOCK TABLES `t_order` WRITE;
INSERT INTO `t_order` VALUES (1,'订单-甲 提及 `t_order` 表'),(2,'订单-乙');
UNLOCK TABLES;
/*!40000 ALTER TABLE `t_order` ENABLE KEYS */;
--
-- Table structure for table `t_log`
--
DROP TABLE IF EXISTS `t_log`;
CREATE TABLE `t_log` (`id` int);
INSERT INTO `t_log` VALUES (9);
-- Dump completed on 2026-11-10 10:00:00
"""


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _dump(tmp_path):
    p = os.path.join(str(tmp_path), "src.sql")
    with open(p, "w", encoding="utf-8") as f:
        f.write(DUMP)
    return p


def _slice(path, tables, renames=None):
    tmp, found, notes = slice_tables(path, tables, renames=renames)
    with open(tmp, encoding="utf-8") as f:
        sql = f.read()
    os.unlink(tmp)
    return sql, found, notes


def test_single_table_rename(tmp_path):
    sql, found, notes = _slice(_dump(tmp_path), ["t_order"],
                               {"t_order": "t_order_uat"})
    assert "CREATE TABLE `t_order_uat`" in sql
    assert "DROP TABLE IF EXISTS `t_order_uat`" in sql
    assert "INSERT INTO `t_order_uat`" in sql
    assert "LOCK TABLES `t_order_uat` WRITE" in sql
    assert "ALTER TABLE `t_order_uat` ENABLE KEYS" in sql
    assert "Table structure for table `t_order_uat`" in sql
    assert "订单-甲" in sql
    assert "CREATE TABLE `t_log`" not in sql
    assert found[0]["obj_name"] == "t_order"
    assert found[0]["renamed_to"] == "t_order_uat"
    assert any("表改名" in n for n in notes)


def test_rename_keeps_string_data_untouched(tmp_path):
    """数据字符串里出现的 `t_order` 不能被当成标识符误改。"""
    sql, _, _ = _slice(_dump(tmp_path), ["t_order"],
                       {"t_order": "t_order_uat"})
    assert "提及 `t_order` 表" in sql


def test_multi_table_rename(tmp_path):
    sql, found, _ = _slice(
        _dump(tmp_path), ["t_order", "t_log"],
        {"t_order": "order_uat", "t_log": "log_uat"})
    assert "CREATE TABLE `order_uat`" in sql
    assert "INSERT INTO `log_uat`" in sql
    assert {f["renamed_to"] for f in found} == {"order_uat", "log_uat"}


def test_db_qualified_source_key(tmp_path):
    sql, _, _ = _slice(_dump(tmp_path), ["t_order"],
                       {"src_db.t_order": "order_uat"})
    assert "CREATE TABLE `order_uat`" in sql


@pytest.mark.parametrize("renames", [
    {"t_order": "1bad"},
    {"t_order": "bad-name"},
    {"t_order": ""},
    {"t_order": "order_uat", "t_log": "ORDER_UAT"},
])
def test_invalid_or_conflicting_renames_rejected(tmp_path, renames):
    with pytest.raises(TableExtractError):
        _slice(_dump(tmp_path), ["t_order", "t_log"], renames)


def test_without_renames_behavior_unchanged(tmp_path):
    sql, found, notes = _slice(_dump(tmp_path), ["t_order"])
    assert "CREATE TABLE `t_order`" in sql
    assert "CREATE TABLE `t_log`" not in sql
    assert all(f["renamed_to"] == "" for f in found)
    assert notes == []


def test_non_dict_renames_rejected(tmp_path):
    with pytest.raises(TableExtractError):
        _slice(_dump(tmp_path), ["t_order"], ["t_order_uat"])


def test_full_chain_contract():
    """API → 编排 → 引擎 → 切片器 → 前端，参数不得在任一层被吞掉。"""
    api = _src("api/restore.py")
    assert 'table_renames=data.get("table_renames")' in api

    sched = _src("core/scheduler.py")
    assert "table_renames: dict = None" in sched
    assert "table_renames=table_renames" in sched
    assert "表改名" in sched

    mysql = _src("core/engines/mysql.py")
    # 表级切片必须排在跨主机分支之前（真机 170 暴露的吞表缺陷）
    assert mysql.index("表级恢复（P0-1）优先") < mysql.index("# 1) 跨主机恢复")
    assert "renames=renames" in mysql
    assert "经跨主机通道推送切片 SQL" in mysql

    extract = _src("core/table_extract.py")
    assert "_normalize_renames" in extract and "_rename_lines" in extract

    js = _src("static/js/app.js")
    assert "table_renames: renames" in js
    assert "_parseTableRenames" in js
    html = _src("templates/records.html")
    assert 'id="object_table_renames"' in html


def test_pg_engine_contract():
    """PG 系：改名走真实 ALTER TABLE；不支持的组合如实失败，不静默全量恢复。"""
    pg = _src("core/engines/postgresql.py")
    assert "_apply_table_renames" in pg
    assert "ALTER TABLE IF EXISTS" in pg
    assert "ON_ERROR_STOP=1" in pg
    assert "纯文本 .sql 产物不支持表级恢复" in pg
    assert "目录格式 / 全实例 tar 产物暂不支持表改名" in pg
    assert "表改名失败" in pg


def test_dameng_table_scope_contract():
    """达梦 170:5236 真机暴露的两个缺陷不得回退。

    1) dimp 的 SCHEMAS/OWNER/FULL 与 TABLES 互斥（rc=1「暂时不支持多种
       导入模式」），指定表时只保留 REMAP_SCHEMA 且表名带源模式前缀；
    2) 表改名必须按本次实际导入的 schema 定位，旧实现写死 SYSDBA 会
       让自建模式下的表改名找不到对象。
    """
    dm = _src("core/engines/dameng.py")
    assert "暂时不支持多种导入模式" in dm
    assert 'a.startswith("REMAP_SCHEMA=")' in dm
    assert "cmd_parts.extend(scope_args)" in dm
    assert "_prefix + str(x).strip()" in dm
    assert "_imp_schema" in dm
    assert 'target_db or "SYSDBA"' not in dm


def test_dameng_scope_args_remap_schema():
    """运行时校验：显式 remap 时 target_db 生成 REMAP_SCHEMA，备份仍用 SCHEMAS。"""
    from core.engines.dameng import DamengEngine

    eng = DamengEngine.__new__(DamengEngine)
    extra = {"schemas": ["AIDBM_SRC"], "remap_schema": 1,
             "remap_source_schema": "AIDBM_SRC"}
    assert eng._scope_args(extra, target_db="AIDBM_UAT") == [
        "REMAP_SCHEMA=AIDBM_SRC:AIDBM_UAT"]
    assert eng._scope_args(extra) == ["SCHEMAS=AIDBM_SRC"]
    assert eng._scope_args({}) == ["FULL=Y"]
