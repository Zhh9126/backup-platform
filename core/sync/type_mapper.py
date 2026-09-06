# -*- coding: utf-8 -*-
"""
统一类型映射层：把各数据库原生类型映射为平台中间类型（Java 风格），
再在写入目标端时映射回目标数据库类型。

中间类型：STRING, LONG, DOUBLE, DECIMAL, BOOLEAN, DATE, TIME, DATETIME, BYTES, NULL
"""
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


class JavaType:
    STRING = "STRING"
    LONG = "LONG"
    DOUBLE = "DOUBLE"
    DECIMAL = "DECIMAL"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIME = "TIME"
    DATETIME = "DATETIME"
    BYTES = "BYTES"
    NULL = "NULL"


def to_java(value: Any) -> Any:
    """把 Python 值规范化为平台统一表示。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, Decimal):
        return float(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    return str(value)


def java_type_name(value: Any) -> str:
    if value is None:
        return JavaType.NULL
    if isinstance(value, bool):
        return JavaType.BOOLEAN
    if isinstance(value, int):
        return JavaType.LONG
    if isinstance(value, float):
        return JavaType.DOUBLE
    if isinstance(value, Decimal):
        return JavaType.DECIMAL
    if isinstance(value, (bytes, bytearray)):
        return JavaType.BYTES
    return JavaType.STRING


def to_db(value: Any, target_type: str = "STRING") -> Any:
    """把平台统一值转换回目标数据库可接受的 Python 值。"""
    if value is None:
        return None
    if target_type == JavaType.BOOLEAN:
        return bool(value)
    if target_type == JavaType.LONG:
        return int(value)
    if target_type == JavaType.DOUBLE:
        return float(value)
    if target_type == JavaType.DECIMAL:
        return str(value) if not isinstance(value, Decimal) else value
    if target_type == JavaType.BYTES:
        return bytes(value) if not isinstance(value, bytes) else value
    return str(value)


# pymysql 数字类型码 → 平台中间类型（driver 返回 description 类型码为数字）
_PYMYSQL_CODE_MAP = {
    0: JavaType.DECIMAL,     # DECIMAL
    1: JavaType.LONG,        # TINYINT
    2: JavaType.LONG,        # SMALLINT
    3: JavaType.LONG,        # INT
    4: JavaType.DOUBLE,      # FLOAT
    5: JavaType.DOUBLE,      # DOUBLE
    7: JavaType.DATETIME,    # TIMESTAMP
    8: JavaType.LONG,        # BIGINT
    9: JavaType.LONG,        # INT24
    10: JavaType.DATE,       # DATE
    11: JavaType.TIME,       # TIME
    12: JavaType.DATETIME,   # DATETIME
    13: JavaType.LONG,       # YEAR
    15: JavaType.STRING,     # VARCHAR
    16: JavaType.BYTES,      # BIT（bytes 原样透传）
    245: JavaType.STRING,    # JSON
    246: JavaType.DECIMAL,   # NEWDECIMAL
    247: JavaType.STRING,    # ENUM
    248: JavaType.STRING,    # SET
    249: JavaType.BYTES,     # TINY_BLOB
    250: JavaType.BYTES,     # MEDIUM_BLOB
    251: JavaType.BYTES,     # LONG_BLOB
    252: JavaType.BYTES,     # BLOB
    253: JavaType.STRING,    # VAR_STRING
    254: JavaType.STRING,    # STRING（含 CHAR/ENUM 字符串形态）
    255: JavaType.BYTES,     # GEOMETRY
}


def db_type_to_java_type(db_type: str) -> str:
    """根据源库列类型名（或 pymysql 数字类型码）推断平台中间类型。

    幂等：入参已是平台中间类型（STRING/LONG/BYTES 等）时原样返回——
    防御调用方把转换结果再次传入导致二次映射（BYTES→STRING 事故）。
    """
    t = (db_type or "").upper()
    if t in (JavaType.STRING, JavaType.LONG, JavaType.DOUBLE, JavaType.DECIMAL,
             JavaType.BOOLEAN, JavaType.DATE, JavaType.TIME, JavaType.DATETIME,
             JavaType.BYTES, JavaType.NULL):
        return t
    if any(x in t for x in ["INT", "SERIAL", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT"]):
        return JavaType.LONG
    if any(x in t for x in ["FLOAT", "DOUBLE", "REAL"]):
        return JavaType.DOUBLE
    if "DECIMAL" in t or "NUMERIC" in t:
        return JavaType.DECIMAL
    if "BOOL" in t:
        return JavaType.BOOLEAN
    if "DATE" in t and "TIME" in t:
        return JavaType.DATETIME
    if "DATE" in t:
        return JavaType.DATE
    if "TIME" in t:
        return JavaType.TIME
    if any(x in t for x in ["BLOB", "BINARY", "BYTEA"]):
        return JavaType.BYTES
    # pymysql 数字类型码（'252'/'16'/'246' 等）
    if t.isdigit() and int(t) in _PYMYSQL_CODE_MAP:
        return _PYMYSQL_CODE_MAP[int(t)]
    return JavaType.STRING
