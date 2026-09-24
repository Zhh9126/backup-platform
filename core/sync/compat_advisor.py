# -*- coding: utf-8 -*-
"""异构迁移兼容性顾问：把业界反复踩的「暗坑」内置到迁移/同步引擎。

来源：信创改造实战与达梦/Oracle 官方 FAQ 的共性结论——
  坑1 达梦大小写敏感 CASE_SENSITIVE 建库后不可改（默认 Y）；
  坑2 达梦 VARCHAR 按字节计长（LENGTH_IN_CHAR=0），UTF-8 汉字 3 字节，
      MySQL 语义的 VARCHAR(100) 实际只能存 33 个汉字，-6169 截断错；
  坑3 COMPATIBLE_MODE 兼容模式（MySQL=4 / Oracle=2），建议初始化就规划；
  坑4 保留字（SECTION/LEVEL/TYPE/KEY/USER...）当列名，运行时随机炸；
  坑5 字符集（UNICODE(): 0=GB18030, 1=UTF-8）不匹配放大坑2；
  坑6 零日期 '0000-00-00'：MySQL 合法、达梦/Oracle/PG 全部非法；
  坑7 Oracle 空串=NULL、VARCHAR2 按字节（NLS_LENGTH_SEMANTICS）、
      12.1 以下标识符 30 字节上限、无符号整型缺失（各目标库通用）。

本模块分两部分：
1) ``run_compat_advisory``——预检阶段连上目标库做参数探测，输出
   findings（含修复建议），由 sync precheck 采纳为 warn/info 项；
2) ``sanitize_zero_dates`` / ``scale_char_length``——写入与建表阶段的
   自动防御（零日期转 NULL、字符长度按字节语义放大），不改变行数语义。
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 零日期一律视为 NULL（MySQL 特有的'无日期'语义，目标库全部非法）
_ZERO_DATE_PREFIX = "0000-00-00"

# 源端 VARCHAR(N) 以「字符」计长的数据库（MySQL/PG/SQLServer/Kingbase/达梦LENGTH_IN_CHAR=1）
_CHAR_SEMANTIC_SOURCES = {"mysql", "mariadb", "postgresql", "kingbase",
                          "sqlserver", "sqlite", "opengauss"}

# 达梦保留字兜底清单（V$RESERVED_WORDS 不可读时使用；引擎已全部双引号引用，
# 此清单用于预检提示应用层 SQL 风险）
_DM_RESERVED_FALLBACK = {
    "SECTION", "ORDER", "LEVEL", "SIZE", "TYPE", "STATUS", "VALUE", "KEY",
    "USER", "COMMENT", "FUNCTION", "DOMAIN", "INDEX", "MODE", "NAME",
    "PASSWORD", "PERCENT", "TYPE", "PACKAGE", "PURGE", "RECORD", "ROWS",
}


def sanitize_zero_dates(records: List[List[Any]], columns: List[str],
                        tgt_db_type: str) -> int:
    """把零日期字符串值改为 None（就地修改），返回修复个数。

    MySQL 的 '0000-00-00[ 00:00:00]' 在达梦/Oracle/PG/Kingbase/SQL Server
    插入直接报错（invalid date），转为 NULL 保持行语义；MySQL/MariaDB 目标
    本身接受零日期，保持原值不做转换。
    """
    if tgt_db_type not in ("dameng", "oracle", "postgresql", "kingbase",
                           "sqlserver"):
        return 0
    fixed = 0
    for rec in records:
        for i, v in enumerate(rec):
            if isinstance(v, str) and v.startswith(_ZERO_DATE_PREFIX):
                rec[i] = None
                fixed += 1
    return fixed


def scale_char_length(n: int, src_db_type: str, dst_by_bytes: bool,
                      dst_charset_factor: int = 3) -> int:
    """按目标端长度语义换算 VARCHAR/CHAR 长度（不截顶）。

    - 目标按字节计长（达梦 LENGTH_IN_CHAR=0 / Oracle NLS_LENGTH_SEMANTICS=BYTE）
      且源按字符计长（MySQL 等）时，UTF-8 汉字占 3 字节（GB18030 占 2），
      必须乘 factor；调用方按目标上限自行降级（达梦>3900→TEXT，
      Oracle>4000→CLOB）；
    - 其余情况原样返回。
    """
    if not dst_by_bytes or src_db_type not in _CHAR_SEMANTIC_SOURCES:
        return n
    factor = dst_charset_factor if dst_charset_factor in (2, 3) else 3
    return n * factor


# ---------------------------------------------------------------- #
# 目标库参数探测

def _probe_scalar(conn: Any, sql: str):
    """执行返回单值的探测 SQL，失败返回 None（老版本/权限不足不阻断）。"""
    try:
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        try:
            cur.close()
        except Exception:
            pass
        if row is None:
            return None
        v = row[0]
        if v is not None and not isinstance(v, (int, float, str)):
            try:
                return str(v)
            except Exception:
                return v
        return v
    except Exception as e:  # noqa: BLE001 - 探测失败=拿不到该参数
        logger.debug("[compat] 探测失败 %s: %s", sql, e)
        return None


def _s(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            return str(v)
        except Exception:
            return ""
    return v


def _dameng_findings(conn, cfg, tables: List[str]) -> List[Dict[str, str]]:
    out = []
    case_s = _probe_scalar(conn, "SELECT CASE_SENSITIVE()")
    unicode_f = _probe_scalar(conn, "SELECT UNICODE()")
    len_char = _probe_scalar(conn, "SELECT SF_GET_PARA_VALUE(2, 'LENGTH_IN_CHAR')")
    compat = _probe_scalar(conn, "SELECT SF_GET_PARA_VALUE(2, 'COMPATIBLE_MODE')")

    if case_s is not None and int(case_s or 0) == 1 \
            and cfg.src_db_type in ("mysql", "mariadb", "postgresql", "kingbase"):
        out.append({
            "code": "dm_case_sensitive", "level": "warn",
            "message": "目标达梦库为大小写敏感库（CASE_SENSITIVE=1，建库后不可改）。"
                       "引擎建表/写入已统一双引号大写标识符（数据可正常落库），"
                       "但应用侧用小写不加引号查询会报「对象不存在」。"
                       "建议：新建库时初始化 CASE_SENSITIVE=N，"
                       "或应用 SQL 显式使用双引号大写标识符。",
        })
    if compat is not None and str(compat) not in ("4",) \
            and cfg.src_db_type in ("mysql", "mariadb"):
        out.append({
            "code": "dm_compatible_mode", "level": "info",
            "message": f"目标达梦 COMPATIBLE_MODE={compat}（非 MySQL 兼容模式）。"
                       "若源端为 MySQL 且应用依赖其函数/语法习惯，建议初始化时设"
                       " SP_SET_PARA_VALUE(2,'COMPATIBLE_MODE',4)（静态参数，需重启实例）。",
        })
    if unicode_f is not None and int(unicode_f or 0) == 0 \
            and cfg.src_db_type in ("mysql", "mariadb"):
        out.append({
            "code": "dm_charset_gb18030", "level": "warn",
            "message": "目标达梦字符集为 GB18030（UNICODE()=0），一个汉字占 2 字节；"
                       "若含生僻字/emoji 等 GB18030 外字符将无法存储。"
                       "国际字符场景建议初始化时选 UTF-8。",
        })
    if len_char is not None and int(len_char or 0) == 0 \
            and cfg.src_db_type in _CHAR_SEMANTIC_SOURCES:
        factor = 2 if (unicode_f is not None and int(unicode_f or 0) == 0) else 3
        out.append({
            "code": "dm_length_in_char", "level": "warn",
            "message": f"目标达梦 VARCHAR 按字节计长（LENGTH_IN_CHAR=0），"
                       f"而源库按字符计长；UTF-8 汉字占 {factor} 字节，"
                       f"VARCHAR(N) 实际只能存 N/{factor} 个汉字。"
                       f"引擎已自动将 VARCHAR 长度放大 {factor} 倍（上限 3900，"
                       f"超出转 TEXT）。根治方案：初始化达梦库时设 LENGTH_IN_CHAR=1。",
        })
    # 保留字检查（目标表名）
    reserved = _dm_reserved_words(conn)
    hit = sorted({t.upper() for t in tables if t and t.upper() in reserved})
    if hit:
        out.append({
            "code": "dm_reserved_words", "level": "warn",
            "message": f"以下表名命中达梦保留字（引擎已双引号引用可正常建表，"
                       f"但应用 SQL 需始终带双引号）：{', '.join(hit)}。"
                       f"全量清单：SELECT * FROM V$RESERVED_WORDS WHERE RESERVED='Y'。",
        })
    return out


def _dm_reserved_words(conn) -> set:
    rows = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM V$RESERVED_WORDS WHERE RESERVED='Y'")
        rows = cur.fetchall()
        try:
            cur.close()
        except Exception:
            pass
    except Exception:
        rows = None
    if rows:
        return {_s(r[0]).upper() for r in rows if _s(r[0])}
    return set(_DM_RESERVED_FALLBACK)


def _oracle_findings(conn, cfg, tables: List[str]) -> List[Dict[str, str]]:
    out = []
    nls_sem = _probe_scalar(
        conn, "SELECT value FROM nls_database_parameters "
              "WHERE parameter='NLS_LENGTH_SEMANTICS'")
    if nls_sem and _s(nls_sem).upper() == "BYTE" \
            and cfg.src_db_type in _CHAR_SEMANTIC_SOURCES:
        out.append({
            "code": "ora_byte_semantics", "level": "warn",
            "message": "目标 Oracle VARCHAR2 按字节计长（NLS_LENGTH_SEMANTICS=BYTE），"
                       "而源库按字符计长，UTF-8 汉字占 3 字节会触发 ORA-12899 截断。"
                       "引擎已自动放大 VARCHAR2 长度；根治方案："
                       "ALTER SYSTEM SET NLS_LENGTH_SEMANTICS=CHAR。",
        })
    if cfg.src_db_type in ("mysql", "mariadb", "postgresql", "kingbase",
                           "sqlserver"):
        out.append({
            "code": "ora_empty_string", "level": "info",
            "message": "Oracle 语义：空字符串 '' 即 NULL（与 MySQL 不同）。"
                       "源库中 '' 与 NULL 有区分的字段迁移后统一为 NULL，属 Oracle 固有行为。",
        })
    # 12.1 及以下标识符上限 30 字节
    ver = _s(_probe_scalar(conn, "SELECT VERSION FROM product_component_version "
                                "WHERE ROWNUM=1"))
    major = 0
    for part in ver.replace("-", ".").split("."):
        if part.isdigit():
            major = int(part)
            break
    if 0 < major <= 12:
        long_names = [t for t in tables if len((t or "").encode("utf-8")) > 30]
        if long_names:
            out.append({
                "code": "ora_identifier_30", "level": "fail",
                "message": f"目标 Oracle 版本 {ver.split('-')[0]} 标识符上限 30 字节，"
                           f"以下表名超限将建表失败（ORA-00972）：{', '.join(long_names)}。",
            })
    return out


def _pg_findings(conn, cfg, tables: List[str]) -> List[Dict[str, str]]:
    out = []
    enc = _probe_scalar(conn, "SHOW server_encoding")
    if enc and _s(enc).upper() not in ("UTF8", "UNICODE") \
            and cfg.src_db_type in ("mysql", "mariadb"):
        out.append({
            "code": "pg_encoding", "level": "warn",
            "message": f"目标 PostgreSQL 服务器编码为 {_s(enc)}（非 UTF8），"
                       "含中文/多字节字符可能写入失败。建议以 UTF8 重建目标库。",
        })
    if cfg.src_db_type in ("mysql", "mariadb"):
        out.append({
            "code": "pg_zero_date", "level": "info",
            "message": "引擎已自动将源库零日期（'0000-00-00'）转为 NULL "
                       "（PostgreSQL 不接受该值）。",
        })
    return out


def _mysql_findings(conn, cfg, tables: List[str]) -> List[Dict[str, str]]:
    out = []
    mode = _probe_scalar(conn, "SELECT @@sql_mode")
    if mode and "STRICT_TRANS_TABLES" not in _s(mode).upper():
        out.append({
            "code": "mysql_no_strict", "level": "warn",
            "message": "目标 MySQL 未启用 STRICT_TRANS_TABLES：超长/非法值会被"
                       "静默截断或转 0，迁移结果可能失真。建议目标库开启严格模式。",
        })
    return out


def run_compat_advisory(cfg, tgt_conn, tables: Optional[List[str]] = None
                        ) -> List[Dict[str, str]]:
    """连接目标库做兼容性参数探测，返回 findings 列表。

    任何探测失败都只降级为「少一条提示」，绝不抛异常阻断迁移。
    """
    tgt = (cfg.tgt_db_type or "").lower()
    tables = tables or []
    try:
        if tgt == "dameng":
            return _dameng_findings(tgt_conn, cfg, tables)
        if tgt == "oracle":
            return _oracle_findings(tgt_conn, cfg, tables)
        if tgt in ("postgresql", "kingbase", "opengauss"):
            return _pg_findings(tgt_conn, cfg, tables)
        if tgt in ("mysql", "mariadb"):
            return _mysql_findings(tgt_conn, cfg, tables)
    except Exception as e:  # noqa: BLE001 - 顾问绝不阻断
        logger.warning("[compat] 兼容性探测异常（忽略）: %s", e)
    return []
