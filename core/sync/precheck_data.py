# -*- coding: utf-8 -*-
"""数据级预检增强（对标 DTS 预检查清单的深度项）：

1. check_data_sample  数据级试写：源端采样 N 行 → 目标端按映射建议 DDL 建
   临时表试写 → 对账 → 清理。真实暴露类型溢出/截断/字符集/约束问题
   （结构检查无法发现的数据级失败在这里拦截）。
2. check_capacity     大表容量预估：行数/平均行宽 → 总量、大表告警、
   按带宽预估迁移时长。
3. check_charset      字符集冲突检测（DTS 检查项）：源/目标字符集家族判定，
   utf8mb4(4字节) 源 vs gbk/latin1 目标 → fail；utf8(3字节) → warn。
4. check_fk_parents   外键父表完整性（DTS 检查项）：子表依赖的父表不在
   同步列表 → warn。
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("precheck")


# ---------------------------------------------------------------------------
# 1) 数据级试写
# ---------------------------------------------------------------------------
def _to_bindable(v, src_type: str = ""):
    """源行值 → 目标端可绑定值（按源类型语义归一）。

    - BIT 源（bytes）→ int（映射建议 NUMBER 承载，直接传 bytes 会转换失败）
    - datetime/date/time → 字符串（跨驱动最稳）
    - Decimal → 字符串（精度无损）
    """
    from datetime import date, datetime, time
    from decimal import Decimal
    if v is None:
        return None
    st = (src_type or "").lower()
    if st.startswith("bit") and isinstance(v, (bytes, bytearray)):
        return int.from_bytes(bytes(v), "big")
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)              # JDBC BINARY → setBytes
    if isinstance(v, (datetime, date, time)):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    return v


def build_sample_ddl(src_db: str, tgt_db: str, src_cols: list,
                     table: str, tmp_name: str,
                     pk_col: Optional[str] = None) -> str:
    """按映射引擎建议类型生成试写临时表 DDL（同时验证建表建议可执行）。"""
    from core.sync.type_matrix import map_type
    from core.sync.precheck import _norm_name
    qt = "`" if tgt_db in ("mysql", "mariadb") else '"'
    cols, pk_decl = [], ""
    for name, stype in src_cols:
        m = map_type(src_db, tgt_db, stype)
        tt = m.get("target_type")
        if not tt or m["level"] == "fail":
            tt = "CLOB" if tgt_db in ("oracle", "dameng") else "TEXT"
        col = _norm_name(name, "upper") if tgt_db not in ("mysql", "mariadb") \
            else _norm_name(name, "origin")
        if pk_col and name.lower() == pk_col.lower():
            nn = "NOT NULL" if tgt_db in ("mysql", "mariadb") else "NOT NULL"
            pk_decl = (f", PRIMARY KEY ({qt}{col}{qt})")
        else:
            nn = ""
        cols.append(f"{qt}{col}{qt} {tt} {nn}".strip())
    return f"CREATE TABLE {qt}{tmp_name}{qt} ({', '.join(cols)}{pk_decl})"


def check_data_sample(cfg, src_conn, tgt_conn, table: str,
                      sample_rows: int = 20) -> Dict[str, Any]:
    """数据级试写：源采样 → 目标临时表 → 对账 → 清理。"""
    from core.data_compare import _get_pk_column, _table_ref
    from core.sync.precheck import _get_cols_typed, _norm_name
    tgt = (cfg.tgt_db_type or "").lower()
    is_mysql_tgt = tgt in ("mysql", "mariadb")
    qt = "`" if is_mysql_tgt else '"'

    # 源端采样
    ref_s = _table_ref(cfg.src_db_type, cfg.src_db_name, cfg.src_schema, table)
    try:
        pk = _get_pk_column(src_conn, cfg.src_db_type, cfg.src_db_name,
                            cfg.src_schema, table)
        order = f" ORDER BY {pk}" if pk and cfg.src_db_type != "oracle" else ""
        top = f" WHERE ROWNUM <= {sample_rows}" if cfg.src_db_type == "oracle" \
            else f" LIMIT {sample_rows}"
        cur = src_conn.cursor()
        cur.execute(f"SELECT * FROM {ref_s}{order}{top}")
        rows = cur.fetchall()
        src_cols = _get_cols_typed(src_conn, cfg.src_db_type,
                                   cfg.src_db_name, cfg.src_schema, table)
        cur.close()
    except Exception as e:
        return {"status": "warn", "message": f"源端采样失败（跳过试写）: {e}",
                "detail": []}
    if not rows:
        return {"status": "pass", "message": "源表无数据，跳过试写", "detail": []}

    # 目标端临时表（按映射建议 DDL，同时验证 DDL 可执行性）
    tmp = ("_bkpc_" + table.lower())[:120]
    tmp_ident = tmp if is_mysql_tgt else tmp.upper()
    full = (f"{qt}{tmp_ident}{qt}" if not cfg.tgt_schema or is_mysql_tgt
            else f"{qt}{cfg.tgt_schema}{qt}.{qt}{tmp_ident}{qt}")
    cur = tgt_conn.cursor()
    try:
        try:
            cur.execute(f"DROP TABLE {full}")
            if is_mysql_tgt or tgt in ("postgresql", "kingbase", "oracle", "dameng"):
                _commit(tgt_conn)
        except Exception:
            _rollback(tgt_conn)
        try:
            cur.execute(build_sample_ddl(cfg.src_db_type, tgt, src_cols,
                                         table, tmp_ident, pk))
            _commit(tgt_conn)
        except Exception as e:
            return {"status": "fail",
                    "message": f"试写建表失败（映射建议 DDL 不可执行）: {str(e)[:200]}",
                    "detail": []}
        # 试写采样行
        col_names = [c[0] for c in src_cols]
        tgt_cols = ([_norm_name(n, "upper") if not is_mysql_tgt
                     else _norm_name(n, "origin") for n in col_names])
        ins = (f"INSERT INTO {full} ("
               + ", ".join(f"{qt}{c}{qt}" for c in tgt_cols) + ") VALUES ("
               + ", ".join("?" for _ in col_names) + ")")
        ok_rows, first_err = 0, None
        for r in rows:
            try:
                cur.execute(ins, [_to_bindable(v, st)
                                  for v, (_, st) in zip(r, src_cols)])
                ok_rows += 1
            except Exception as e:
                if first_err is None:
                    first_err = f"{type(e).__name__}: {str(e)[:180]}"
        _commit(tgt_conn)
        # 对账
        try:
            cur.execute(f"SELECT COUNT(*) FROM {full}")
            written = int(cur.fetchone()[0])
        except Exception:
            written = ok_rows
        detail = [{"table": table, "sampled": len(rows),
                   "written": ok_rows, "verified": written}]
        if ok_rows == len(rows) and written == len(rows):
            return {"status": "pass",
                    "message": f"数据级试写通过：采样 {len(rows)} 行全部写入"
                               f"（按映射建议建列）", "detail": detail}
        msg = (f"数据级试写失败：采样 {len(rows)} 行仅 {ok_rows} 行写入成功"
               f"（对账 {written}）。首错: {first_err}")
        # 附风险候选列（映射 warn/fail 的列）
        try:
            from core.sync.type_matrix import map_type as _mt
            risky = [c[0] for c in src_cols
                     if _mt(cfg.src_db_type, tgt, c[1])["level"] != "ok"]
            if risky:
                msg += f"；候选风险列: {', '.join(risky[:6])}"
        except Exception:
            pass
        return {"status": "fail", "message": msg, "detail": detail}
    finally:
        try:
            cur.execute(f"DROP TABLE {full}")
            _commit(tgt_conn)
        except Exception:
            _rollback(tgt_conn)
        try:
            cur.close()
        except Exception:
            pass


def _commit(conn):
    try:
        conn.commit()
    except Exception:
        pass


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 2) 大表容量预估
# ---------------------------------------------------------------------------
def check_capacity(cfg, src_conn, tables: List[str],
                   bandwidth_mb_s: float = 50.0) -> Dict[str, Any]:
    """行数/平均行宽 → 大表告警 + 迁移时长预估。"""
    dbt = (cfg.src_db_type or "").lower()
    detail, total_mb, big_tables = [], 0.0, []
    for t in tables:
        rows, size_mb = None, None
        try:
            cur = src_conn.cursor()
            if dbt in ("mysql", "mariadb"):
                cur.execute(
                    "SELECT table_rows, "
                    "(data_length+index_length)/1024/1024 "
                    "FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s",
                    (cfg.src_db_name, t))
                r = cur.fetchone()
                rows, size_mb = (int(r[0] or 0), float(r[1] or 0)) if r else (0, 0)
            elif dbt in ("postgresql", "kingbase"):
                cur.execute(
                    "SELECT reltuples::bigint, "
                    "pg_total_relation_size(%s)/1024.0/1024.0 "
                    f"FROM pg_class WHERE relname = '{t}'",
                    (f"{cfg.src_schema or 'public'}.{t}",))
                r = cur.fetchone()
                rows, size_mb = (int(r[0] or 0), float(r[1] or 0)) if r else (0, 0)
            elif dbt in ("oracle", "dameng"):
                owner = (cfg.src_schema or "").upper()
                cur.execute(
                    "SELECT num_rows, (blocks*8192)/1024.0/1024.0 "
                    "FROM all_tables WHERE owner='" + owner +
                    "' AND table_name='" + t.upper() + "'")
                r = cur.fetchone()
                rows = int(r[0]) if r and r[0] is not None else None
                size_mb = float(r[1]) if r and r[1] is not None else None
            cur.close()
        except Exception as e:
            detail.append({"table": t, "status": "warn",
                           "message": f"容量统计不可用: {str(e)[:80]}"})
            continue
        big = (rows is not None and rows >= 1e8) or \
              (size_mb is not None and size_mb >= 10240)
        if big:
            big_tables.append({"table": t, "rows": rows, "size_mb": size_mb})
        if size_mb:
            total_mb += size_mb
        detail.append({
            "table": t, "status": "pass",
            "rows": rows if rows is not None else "未统计（需 ANALYZE）",
            "size_mb": round(size_mb, 1) if size_mb else None,
            "big": big})
    eta_min = total_mb / bandwidth_mb_s / 60 if total_mb else None
    msg = (f"预估迁移总量 {total_mb:.1f} MB"
           + (f"，按 {bandwidth_mb_s:.0f} MB/s 约 {eta_min:.0f} 分钟" if eta_min else "")
           + (f"；大表 {len(big_tables)} 张（≥1 亿行或 ≥10GB）" if big_tables else ""))
    status = "warn" if big_tables else "pass"
    return {"status": status, "message": msg,
            "detail": detail + [{"big_tables": big_tables}]}


# ---------------------------------------------------------------------------
# 3) 字符集冲突检测
# ---------------------------------------------------------------------------
_4BYTE = {"utf8mb4", "utf8", "utf-8", "al32utf8", "al32utf 8"}
_3BYTE = {"utf8"}        # MySQL utf8 是 3 字节
_GB = {"gbk", "gb2312", "gb18030", "zhs16gbk", "zhs16gb18030"}
_LATIN = {"latin1", "cp1252", "we8iso8859p1", "ascii"}


def _charset_family(cs: str) -> str:
    cs = (cs or "").lower()
    if cs.startswith("utf8mb3"):      # probe_charset 已按库型区分（MySQL utf8）
        return "utf8_3"
    c = cs.replace("-", "").replace("_", "")
    if c in ("utf8mb4", "al32utf8", "utf8"):
        # 非 MySQL 库的 utf-8 是真 4 字节 UTF-8；MySQL 已被上面拦截
        return "utf8_4" if c != "utf8" else "utf8_3"
    if c.startswith("gb"):
        return "gb"
    if c.startswith("latin") or "ascii" in c or "8859" in c:
        return "latin"
    if "16" in c:
        return "utf16"
    return c or "unknown"


def probe_charset(conn, db_type: str, database: str) -> str:
    """探测服务端字符集（尽力而为，失败返回 unknown）。

    注意：MySQL 的 'utf8' 是 3 字节阉割版，与达梦/PG/Oracle 的真 UTF-8
    （4 字节）不同名同实——按库型区分家族。
    """
    dbt = (db_type or "").lower()
    try:
        cur = conn.cursor()
        if dbt in ("mysql", "mariadb"):
            cur.execute("SELECT default_character_set_name "
                        "FROM information_schema.SCHEMATA "
                        "WHERE schema_name=%s", (database,))
            r = cur.fetchone()
        elif dbt in ("postgresql", "kingbase"):
            cur.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database "
                        "WHERE datname = current_database()")
            r = cur.fetchone()
        elif dbt == "oracle":
            cur.execute("SELECT value FROM nls_database_parameters "
                        "WHERE parameter = 'NLS_CHARACTERSET'")
            r = cur.fetchone()
        elif dbt == "dameng":
            try:
                cur.execute("SELECT SF_GET_UNICODE_FLAG()")
                flag = cur.fetchone()
                # 达梦 UNICODE_FLAG：1=UTF-8（4字节），0=GB18030
                v = str(flag[0]) if flag else ""
                r = ("utf-8",) if v == "1" else \
                    (("gb18030",) if v == "0" else None)
            except Exception:
                r = None
        else:
            r = None
        cur.close()
        cs = str(r[0]) if r else "unknown"
        # 库型语义归一：MySQL utf8=3字节；其余库 UTF-8=4字节
        if cs.lower() == "utf8" and dbt in ("mysql", "mariadb"):
            return "utf8mb3(MySQL)"
        return cs
    except Exception:
        return "unknown"


def check_charset(cfg, src_conn, tgt_conn) -> Dict[str, Any]:
    """源/目标字符集家族判定（DTS 字符集检查项）。"""
    src_cs = probe_charset(src_conn, cfg.src_db_type, cfg.src_db_name)
    tgt_cs = probe_charset(tgt_conn, cfg.tgt_db_type, cfg.tgt_db_name)
    sf, tf = _charset_family(src_cs), _charset_family(tgt_cs)
    if "unknown" in (sf, tf):
        return {"status": "warn",
                "message": f"字符集探测不完整（源 {src_cs} / 目标 {tgt_cs}），"
                           f"无法自动判定，建议人工确认",
                "detail": [{"source": src_cs, "target": tgt_cs}]}
    if sf == "utf8_4" and tf in ("gb", "latin"):
        return {"status": "fail",
                "message": f"字符集冲突：源 {src_cs}（4 字节，含 emoji/生僻字）"
                           f"→ 目标 {tgt_cs}（无法承载 4 字节字符），写入将失败或丢数据",
                "detail": [{"source": src_cs, "target": tgt_cs}]}
    if sf == "utf8_4" and tf == "utf8_3":
        return {"status": "warn",
                "message": f"源 {src_cs}(4字节) → 目标 {tgt_cs}(3字节)："
                           f"emoji/部分生僻字将丢失，建议目标启用 utf8mb4",
                "detail": [{"source": src_cs, "target": tgt_cs}]}
    if sf in ("utf8_4", "utf8_3") and tf == "utf16":
        return {"status": "pass",
                "message": f"源 {src_cs} → 目标 {tgt_cs}（UTF-16 全兼容）",
                "detail": [{"source": src_cs, "target": tgt_cs}]}
    return {"status": "pass",
            "message": f"字符集兼容（源 {src_cs} → 目标 {tgt_cs}）",
            "detail": [{"source": src_cs, "target": tgt_cs}]}


# ---------------------------------------------------------------------------
# 4) 外键父表完整性（DTS 检查项）
# ---------------------------------------------------------------------------
def check_fk_parents(cfg, src_conn, tables: List[str]) -> Dict[str, Any]:
    """多表迁移时，子表依赖的父表不在同步列表 → warn（DTS 约束完整性检查）。"""
    if len(tables) < 2:
        return None
    dbt = (cfg.src_db_type or "").lower()
    tset = {t.lower() for t in tables}
    missing = []
    try:
        cur = src_conn.cursor()
        if dbt in ("mysql", "mariadb"):
            cur.execute(
                "SELECT table_name, referenced_table_name "
                "FROM information_schema.key_column_usage "
                "WHERE table_schema=%s AND referenced_table_name IS NOT NULL",
                (cfg.src_db_name,))
            rows = cur.fetchall()
            missing = [{"child": str(r[0]), "parent": str(r[1])}
                       for r in rows
                       if str(r[0]).lower() in tset
                       and str(r[1]).lower() not in tset]
        elif dbt in ("oracle", "dameng"):
            owner = (cfg.src_schema or "").upper()
            cur.execute(
                "SELECT a.table_name, c.table_name FROM all_constraints a "
                "JOIN all_constraints c ON a.r_constraint_name = c.constraint_name "
                f"AND c.owner = '{owner}' "
                f"WHERE a.constraint_type = 'R' AND a.owner = '{owner}'")
            rows = cur.fetchall()
            missing = [{"child": str(r[0]), "parent": str(r[1])}
                       for r in rows
                       if str(r[0]).lower() in tset
                       and str(r[1]).lower() not in tset]
        cur.close()
    except Exception:
        return None
    if missing:
        return {"status": "warn",
                "message": f"约束完整性：{len(missing)} 个外键依赖的父表不在同步列表"
                           f"（写入将违反外键约束）",
                "detail": missing}
    return {"status": "pass", "message": "外键父表完整性检查通过", "detail": []}
