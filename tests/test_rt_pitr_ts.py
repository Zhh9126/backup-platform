# -*- coding: utf-8 -*-
"""PITR 目标时间点归一化回归。

2026-11-13 MySQL 真机暴露：前端/脚本常传 ``2026-11-13 21:07:56``（空格、无时区），
而 recovery_journal.pit_at 是 ``2026-11-13T21:07:16+08:00``。SQL 字符串比较时
``'T' > ' '`` 导致所有恢复点都被判成晚于目标时间，链头 db-full 被丢弃，
最终报「链头缺失基准全量…请先做一次全量备份」，与真实原因不符。
"""
import re

from core.rt_backup.pitr import normalize_ts


def test_space_separated_ts_is_normalized():
    out = normalize_ts("2026-11-13 21:07:56")
    assert out.startswith("2026-11-13T21:07:56"), out
    assert re.search(r"[+-]\d{2}:\d{2}$", out), f"未补时区：{out}"


def test_naive_iso_ts_gets_offset():
    out = normalize_ts("2026-11-13T21:07:56")
    assert re.search(r"[+-]\d{2}:\d{2}$", out), out


def test_ts_with_offset_passthrough():
    src = "2026-11-13T21:07:56+08:00"
    assert normalize_ts(src) == src


def test_empty_ts_defaults_to_now():
    out = normalize_ts("")
    assert out and re.match(r"^\d{4}-\d{2}-\d{2}T", out)


def test_normalized_ts_compares_correctly_with_journal_format():
    """归一后必须能按字符串正确比较（这是链头选择的实际判据）。"""
    pit_at = "2026-11-13T21:07:16+08:00"
    target = normalize_ts("2026-11-13 21:07:56")
    assert pit_at <= target, (pit_at, target)
    target_early = normalize_ts("2026-11-13 21:00:00")
    assert not (pit_at <= target_early)
