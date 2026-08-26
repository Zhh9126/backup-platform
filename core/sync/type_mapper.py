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


def db_type_to_java_type(db_type: str) -> str:
    """根据源库列类型名推断平台中间类型。"""
    t = (db_type or "").upper()
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
    return JavaType.STRING
