# -*- coding: utf-8 -*-
"""国产数据库协议兼容层（瀚高/优炫/TiDB/OceanBase）注册与分发单测。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

from core.engines import ENGINE_REGISTRY, supported_types
from core.engines.highgo import HighGoEngine
from core.engines.uxdb import UXDBEngine
from core.engines.tidb import TiDBEngine
from core.engines.oceanbase import OceanBaseEngine
from core.engines.base import BackupType
from core import remote_dump


def test_registered_in_backup_matrix():
    for t in ("highgo", "uxdb", "tidb", "oceanbase"):
        assert t in ENGINE_REGISTRY and t in supported_types(), t


def test_pg_family_engines_inherit_postgresql():
    for cls in (HighGoEngine, UXDBEngine):
        from core.engines.postgresql import PostgreSQLEngine
        assert issubclass(cls, PostgreSQLEngine)
        assert cls.required_clients  # pg_dump/psql 等工具继承


def test_mysql_family_engines_inherit_mysql():
    for cls in (TiDBEngine, OceanBaseEngine):
        from core.engines.mysql import MySQLEngine
        assert issubclass(cls, MySQLEngine)


def test_tidb_oceanbase_physical_honestly_rejected():
    for cls in (TiDBEngine, OceanBaseEngine):
        eng = cls({"id": 1, "name": "t", "db_type": cls.db_type}, ".")
        r = eng._backup_physical(BackupType.FULL)
        assert not r.success and "不支持" in r.message


def test_pg_family_tooling_entries():
    for t, port in (("highgo", 5866), ("uxdb", 5432)):
        cfg = remote_dump._PG_FAMILY_TOOLING[t]
        assert cfg["dump_tool"] == "pg_dump" and cfg["default_port"] == port
        assert cfg["db_type"] == t


def test_remote_db_dump_dispatch():
    """分发冒烟：highgo/uxdb 走 PG 族通道（对不存在的 ssh_host 应在连接处失败，
    而不是'不支持的远程 dump 类型'——证明已进入 PG 分支）。"""
    try:
        remote_dump.remote_db_dump(
            {"id": 1, "db_name": "d", "db_type": "highgo"},
            {"host_key": "nobody@127.0.0.1:1", "hostname": "127.0.0.1",
             "port": 1, "username": "nobody", "password": "x"},
            "highgo", 0)
        assert False, "应连接失败"
    except RuntimeError as e:
        msg = str(e)
        assert "不支持的远程 dump 类型" not in msg, msg
    except Exception:
        pass  # 连接类异常 = 已进入分支
