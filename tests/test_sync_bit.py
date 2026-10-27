# -*- coding: utf-8 -*-
"""MySQL BIT 列同步回归：类型码 16 读取端转 int（防 bytea↔smallint 整批失败）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.sync.type_mapper import db_type_to_java_type, JavaType
from core.sync.plugins.mysql import MySQLPlugin


def test_pymysql_code_16_bit_maps_to_long():
    assert db_type_to_java_type("16") == JavaType.LONG


def test_mysql_type_to_java_bit_bytes_to_int():
    p = MySQLPlugin()
    assert p.type_to_java("16", b"\x01") == 1
    assert p.type_to_java("16", bytearray(b"\xff\xff")) == 65535
    assert p.type_to_java("16", None) is None
    # 常规整型不受影响
    assert p.type_to_java("3", 5) == 5
