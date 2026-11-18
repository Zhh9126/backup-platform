# -*- coding: utf-8 -*-
"""CDC 公共小工具（消除各 LogMiner 适配器里的重复实现）。"""
from __future__ import annotations


def cell_str(row, index: int) -> str:
    """安全取列并转字符串（LOB / datetime / None 全兼容）。

    此前 ``dameng_logmnr._cell_str`` 与 ``oracle_logminer._cell_str``
    是两份逐字相同的实现（cx_Oracle LOB 与 dmPython LOB 都是 .read() 语义）。
    """
    try:
        value = row[index]
    except (IndexError, TypeError):
        return ""
    if value is None:
        return ""
    reader = getattr(value, "read", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:
            return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return str(value)
    return str(value)
