# -*- coding: utf-8 -*-
"""实时备份公共小工具（消除 db_rt / file_rt 各自重复实现）。"""
from __future__ import annotations


def epoch_of(iso: str) -> float:
    """ISO8601 → epoch 秒；不可解析返回 0.0。

    此前 ``db_rt._epoch`` 与 ``file_rt._epoch`` 是两份逐字相同的实现。
    """
    if not iso:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0
