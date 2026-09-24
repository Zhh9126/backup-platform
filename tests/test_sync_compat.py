# -*- coding: utf-8 -*-
"""异构迁移兼容性顾问（compat_advisor）单测——全部离线可跑。

覆盖信创迁移暗坑的引擎内置防御：
- 零日期消毒（MySQL '0000-00-00' → NULL，仅严格日期语义目标库）；
- VARCHAR 字节语义长度放大（达梦 LENGTH_IN_CHAR=0 / Oracle NLS=BYTE）；
- 无符号整型升位（达梦 NUMBER(20,0) / Oracle NUMBER(20)）；
- 目标库参数探测顾问（CASE_SENSITIVE / COMPATIBLE_MODE / 保留字 /
  NLS_LENGTH_SEMANTICS / server_encoding），探测失败不阻断。
"""
from types import SimpleNamespace

import pytest

from core.sync import compat_advisor as ca
from core.sync.plugins.base import ColumnMeta
from core.sync.plugins.dameng import DamengPlugin, DamengSinkWriter
from core.sync.plugins.oracle import OraclePlugin, OracleSinkWriter


def _mk_cfg(src="mysql", tgt="dameng"):
    return SimpleNamespace(src_db_type=src, tgt_db_type=tgt)


# ------------------------- 零日期消毒 -------------------------

def test_sanitize_zero_dates_strict_targets():
    recs = [["2024-01-01", "0000-00-00", "0000-00-00 00:00:00", None, 5]]
    fixed = ca.sanitize_zero_dates(recs, ["a", "b", "c", "d", "e"], "dameng")
    assert fixed == 2
    assert recs[0][0] == "2024-01-01" and recs[0][1] is None and recs[0][2] is None


def test_sanitize_zero_dates_mysql_target_kept():
    recs = [["0000-00-00", "0000-00-00 00:00:00"]]
    assert ca.sanitize_zero_dates(recs, ["a", "b"], "mysql") == 0
    assert recs[0][0] == "0000-00-00"          # MySQL 合法，保持原值


def test_sanitize_zero_dates_partial_prefix_only():
    # 仅完整前缀 '0000-00-00' 命中；数字/None/普通日期不动
    recs = [[0, None, "0000-00-00", "0000-00-0"]]
    fixed = ca.sanitize_zero_dates(recs, list("abcd"), "postgresql")
    assert fixed == 1 and recs[0][2] is None
    assert recs[0][0] == 0 and recs[0][3] == "0000-00-0"


# ------------------------- 字符长度语义 -------------------------

def test_scale_char_length_byte_target_utf8():
    # MySQL→达梦(字节语义, UTF-8)：VARCHAR(100) 实际只存 33 个汉字 → 放大 3 倍
    assert ca.scale_char_length(100, "mysql", dst_by_bytes=True,
                                dst_charset_factor=3) == 300


def test_scale_char_length_byte_target_gb18030():
    assert ca.scale_char_length(100, "mysql", dst_by_bytes=True,
                                dst_charset_factor=2) == 200


def test_scale_char_length_overflow_passthrough():
    # 不截顶：超出目标上限的换算值由调用方降级（达梦>3900→TEXT）
    assert ca.scale_char_length(2000, "mysql", dst_by_bytes=True,
                                dst_charset_factor=3) == 6000


def test_scale_char_length_noop_cases():
    # Oracle 源本就是字节语义，不放大
    assert ca.scale_char_length(100, "oracle", dst_by_bytes=True) == 100
    # 字符语义目标（LENGTH_IN_CHAR=1）不放大
    assert ca.scale_char_length(100, "mysql", dst_by_bytes=False) == 100


# ------------------------- 达梦建表映射 -------------------------

def _dm_writer(src="mysql"):
    return DamengSinkWriter(_mk_cfg(src=src), DamengPlugin())


def test_dameng_unsigned_upscale():
    w = _dm_writer()

    def col(t, uns=False):
        c = ColumnMeta(name="c", type=t)
        c.unsigned = uns
        return c

    assert w._map_to_dameng_type(col("TINYINT UNSIGNED", True),
                                 "TINYINT UNSIGNED") == "SMALLINT"
    assert w._map_to_dameng_type(col("SMALLINT UNSIGNED", True),
                                 "SMALLINT UNSIGNED") == "INT"
    assert w._map_to_dameng_type(col("INT UNSIGNED", True),
                                 "INT UNSIGNED") == "BIGINT"
    assert w._map_to_dameng_type(col("BIGINT UNSIGNED", True),
                                 "BIGINT UNSIGNED") == "NUMBER(20,0)"
    # 有符号保持原样
    assert w._map_to_dameng_type(col("INT"), "INT") == "INT"


def test_dameng_varchar_scale_byte_semantics():
    w = _dm_writer()
    w._sem_probed, w._len_in_char, w._charset_factor = True, 0, 3
    c = ColumnMeta(name="n", type="VARCHAR", max_length=100)
    assert w._map_to_dameng_type(c, "VARCHAR(100)") == "VARCHAR(300)"


def test_dameng_varchar_scale_overflow_to_text():
    w = _dm_writer()
    w._sem_probed, w._len_in_char, w._charset_factor = True, 0, 3
    c = ColumnMeta(name="n", type="VARCHAR", max_length=2000)
    assert w._map_to_dameng_type(c, "VARCHAR(2000)") == "TEXT"


def test_dameng_varchar_char_semantics_no_scale():
    w = _dm_writer()
    w._sem_probed, w._len_in_char, w._charset_factor = True, 1, 3
    c = ColumnMeta(name="n", type="VARCHAR", max_length=100)
    assert w._map_to_dameng_type(c, "VARCHAR(100)") == "VARCHAR(100)"


# ------------------------- Oracle 建表映射 -------------------------

def _ora_writer(src="mysql"):
    return OracleSinkWriter(_mk_cfg(src=src, tgt="oracle"), OraclePlugin())


def test_oracle_bigint_unsigned_upscale():
    w = _ora_writer()
    c = ColumnMeta(name="c", type="BIGINT UNSIGNED")
    c.unsigned = True
    assert w._map_to_oracle_type(c, "BIGINT UNSIGNED") == "NUMBER(20)"
    assert w._map_to_oracle_type(ColumnMeta(name="c", type="BIGINT"),
                                 "BIGINT") == "NUMBER(19)"


def test_oracle_varchar_scale_byte_semantics():
    w = _ora_writer()
    w._ora_byte_semantics = True
    c = ColumnMeta(name="n", type="VARCHAR", max_length=100)
    assert w._map_to_oracle_type(c, "VARCHAR(100)") == "VARCHAR2(300)"


# ------------------------- 参数探测顾问（stub 连接） -------------------------

class _StubCursor:
    """按 SQL 关键字返回脚本化结果。"""

    def __init__(self, script: dict):
        self._script = script
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql or ""
        return self

    def fetchone(self):
        for key, val in self._script.items():
            if key.lower() in self._last.lower():
                return val
        return None

    def fetchall(self):
        v = self.fetchone()
        return [v] if v else []

    def close(self):
        pass


class _StubConn:
    def __init__(self, script: dict):
        self._script = script

    def cursor(self):
        return _StubCursor(self._script)


def test_advisor_dameng_default_library():
    """默认初始化的达梦库（敏感库+字节语义+非兼容模式）：全部给出提示。"""
    cfg = _mk_cfg()
    conn = _StubConn({
        "CASE_SENSITIVE": (1,), "UNICODE": (0,),
        "LENGTH_IN_CHAR": (0,), "COMPATIBLE_MODE": (0,),
    })
    out = ca.run_compat_advisory(cfg, conn, ["t1", "order"])
    codes = {f["code"]: f["level"] for f in out}
    assert codes.get("dm_case_sensitive") == "warn"
    assert codes.get("dm_length_in_char") == "warn"
    assert codes.get("dm_charset_gb18030") == "warn"
    assert codes.get("dm_reserved_words") == "warn"   # order 命中兜底保留字


def test_advisor_dameng_optimized_library_quiet():
    """优化过的目标库（不敏感+按字符+MySQL 兼容模式）：无 warn/fail。"""
    cfg = _mk_cfg()
    conn = _StubConn({
        "CASE_SENSITIVE": (0,), "UNICODE": (1,),
        "LENGTH_IN_CHAR": (1,), "COMPATIBLE_MODE": (4,),
    })
    out = ca.run_compat_advisory(cfg, conn, ["t1"])
    assert [f for f in out if f["level"] in ("warn", "fail")] == []


def test_advisor_probe_failure_never_blocks():
    cfg = _mk_cfg()
    conn = _StubConn({})          # 无脚本 → 全部探测失败
    assert ca.run_compat_advisory(cfg, conn, ["t1"]) == []


def test_advisor_oracle_identifier_30():
    cfg = _mk_cfg(src="mysql", tgt="oracle")
    conn = _StubConn({"NLS_LENGTH_SEMANTICS": ("BYTE",),
                      "product_component_version": ("11.2.0.4.0",)})
    out = ca.run_compat_advisory(cfg, conn, ["t_ok", "x" * 31])
    codes = {f["code"]: f["level"] for f in out}
    assert codes.get("ora_identifier_30") == "fail"   # 建表必失败 → 阻断
    assert codes.get("ora_byte_semantics") == "warn"
    assert codes.get("ora_empty_string") == "info"


def test_advisor_pg_encoding():
    cfg = _mk_cfg(src="mysql", tgt="postgresql")
    conn = _StubConn({"server_encoding": ("LATIN1",)})
    out = ca.run_compat_advisory(cfg, conn, ["t1"])
    codes = {f["code"]: f["level"] for f in out}
    assert codes.get("pg_encoding") == "warn"
    assert codes.get("pg_zero_date") == "info"
