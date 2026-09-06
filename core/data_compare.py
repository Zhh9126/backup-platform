# -*- coding: utf-8 -*-
"""数据对比引擎：验证恢复后的备份数据与原生产库的一致性。

对比策略（逐表三级递进）：
1. 表清单比对：任务指定表清单，或默认取两端共有的基础表；
2. 行数比对：SELECT COUNT(*)；
3. 校验和比对（可选，enable_checksum）：对全表所有列做聚合哈希
   （MySQL=CRC32 / PostgreSQL=hashtext / Oracle=ORA_HASH，跨版本通用语法）；
4. 抽样行比对：按第一列排序取前 N 行，逐行逐列比对。

连接方式（与平台约束一致）：
- 优先原生直连（core/native_conn.py：pymysql/psycopg2/oracledb/dmPython，无 Java 依赖）；
- 原生驱动缺失时回退 JDBC 桥接（core/jdbc.py，可选：需本机 JRE 与 drivers/ 驱动）；
- 均不可用时任务失败并在报告中给出原因。

设计约束：
- 不做全表内存载入（区别于 sync/schema_compare.CrossDBVerifier），一切比对下推为 SQL；
- Oracle 兼容 11g：采样用 ROWNUM 子查询，不使用 12c+ 的 FETCH FIRST；
- 单表失败不中断整体，逐表落盘到报告。
"""
import re
import time
import logging
from typing import Optional

from core import db
from core import models

logger = logging.getLogger(__name__)

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

# 行值归一化时 None 的占位符（避免与空串混淆）
_NULL_MARK = "~N"


# ---------------------------------------------------------------------------
# 连接适配层
# ---------------------------------------------------------------------------
def _open_conn(cfg: dict):
    """按端配置打开数据库连接（DB-API），调用方负责 close。

    顺序：原生直连（core/native_conn.py，无 Java 依赖）→ JDBC 兜底。
    """
    db_type = (cfg.get("db_type") or "").lower()
    host = cfg.get("host") or ""
    port = int(cfg.get("port") or 0)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    database = cfg.get("database") or ""
    last_err = None

    # 原生直连优先（pymysql / psycopg2 / oracledb / dmPython）
    from core import native_conn
    try:
        return native_conn.connect(db_type, host, port, database, user, pwd)
    except native_conn.DriverUnavailable as e:
        last_err = e
    except Exception as e:
        raise ConnectionError(
            f"无法建立 {db_type} 连接 {host}:{port}/{database}: {e}") from e

    # JDBC 兜底（mysql/pg/oracle/kingbase/dameng，需本机 JRE + drivers/ jar）
    try:
        from core import jdbc
        return jdbc.connect(db_type, host, port, database, user, pwd)
    except Exception as e:
        last_err = last_err or e

    raise ConnectionError(
        f"无法建立 {db_type} 连接 {host}:{port}/{database}: {last_err}")


def _side_cfg(task: dict, side: str) -> dict:
    """从任务行提取某一端的连接配置。"""
    return {
        "db_type": task.get(side + "_db_type"),
        "host": task.get(side + "_host"),
        "port": task.get(side + "_port"),
        "username": task.get(side + "_username"),
        "password": task.get(side + "_password"),
        "database": task.get(side + "_database"),
        "schema": task.get(side + "_schema"),
    }


# ---------------------------------------------------------------------------
# 方言辅助（标识符 / 元数据 / 聚合 SQL）
# ---------------------------------------------------------------------------
def _qident(db_type: str, *parts: str) -> str:
    """生成带引用的限定表名。输入均来自库内元数据或白名单校验。"""
    db_type = (db_type or "").lower()
    # JDBC 通道标识符可能为 java.lang.String，统一归一为 python str
    parts = [str(p) for p in parts if p]
    if db_type in ("mysql", "mariadb"):
        return ".".join("`" + p.replace("`", "``") + "`" for p in parts)
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def _lit_ident(db_type: str, ident: str) -> str:
    """拼接进 SQL 字面量的标识符（仅允许出现在 Oracle 无绑定场景）。"""
    if not _SAFE_IDENT.match(ident or ""):
        raise ValueError(f"非法标识符: {ident!r}")
    return ident


def _list_tables(conn, db_type: str, database: str, schema: str) -> list:
    """列出库/模式下的基础表名（大写不敏感归一）。"""
    cur = conn.cursor()
    db_type = (db_type or "").lower()
    try:
        if db_type in ("mysql", "mariadb"):
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE' "
                "ORDER BY table_name", (database,))
        elif db_type in ("postgresql", "kingbase"):
            # schema 名来自用户配置，白名单校验后内联（避免 psycopg2 LIKE 字面量 % 转义问题）
            sch = schema or "public"
            if not _SAFE_IDENT.match(sch):
                raise ValueError(f"非法 schema: {sch!r}")
            cur.execute(
                "SELECT tablename FROM pg_tables "
                f"WHERE schemaname = '{sch}' "
                "AND tablename NOT LIKE 'pg\\_%' "
                "AND tablename NOT LIKE 'xx\\_%' "
                "ORDER BY tablename")
        elif db_type in ("oracle", "dameng"):
            owner = (schema or _user_of(conn) or "").upper()
            if not owner:
                return []
            if owner in ("SYS", "SYSTEM", "SYSMAN", "DBSNMP", "SYSAUDITOR",
                         "SYSSSO", "CTISYS") and not schema:
                return []
            cur.execute(
                "SELECT table_name FROM all_tables "
                f"WHERE owner = '{_lit_ident(db_type, owner)}' "
                "ORDER BY table_name")
        else:
            raise ValueError(f"暂不支持列出 {db_type} 的表清单")
        names = [str(r[0]) for r in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return names


def _user_of(conn) -> str:
    """Oracle 当前用户。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT USER FROM DUAL")
        return str(cur.fetchone()[0])   # JDBC 通道 java.lang.String 归一
    except Exception:
        return ""
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _table_ref(db_type: str, database: str, schema: str, table: str) -> str:
    db_type = (db_type or "").lower()
    if db_type in ("mysql", "mariadb"):
        return _qident(db_type, database, table)
    if db_type in ("postgresql", "kingbase"):
        return _qident(db_type, schema or "public", table)
    if db_type in ("oracle", "dameng"):
        owner = (schema or "").upper()
        return (_lit_ident(db_type, owner) + "." + _lit_ident(db_type, table)
                if owner else _lit_ident(db_type, table))
    raise ValueError(f"暂不支持 {db_type}")


def _get_columns(conn, db_type: str, database: str, schema: str, table: str) -> list:
    """取表的列名（有序）。"""
    cur = conn.cursor()
    db_type = (db_type or "").lower()
    try:
        if db_type in ("mysql", "mariadb"):
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (database, table))
        elif db_type in ("postgresql", "kingbase"):
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema or "public", table))
        elif db_type in ("oracle", "dameng"):
            owner = (schema or _user_of(conn) or "").upper()
            cur.execute(
                "SELECT column_name FROM all_tab_columns "
                f"WHERE owner = '{_lit_ident(db_type, owner)}' "
                f"AND table_name = '{_lit_ident(db_type, table)}' "
                "ORDER BY column_id")
        else:
            raise ValueError(f"暂不支持 {db_type} 取列")
        return [str(r[0]) for r in cur.fetchall()]   # JDBC java String 归一
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _row_count_sql(db_type: str, ref: str) -> str:
    return f"SELECT COUNT(*) FROM {ref}"


def _checksum_sql(db_type: str, ref: str, cols: list):
    """全表聚合校验和 SQL：返回 (sql, 结果列序 (count, checksum))。

    - MySQL: SUM(CRC32(CONCAT_WS('#', IFNULL(col,'~N'), ...)))
    - PostgreSQL: SUM(hashtext(...)::bigint)
    - Oracle: SUM(ORA_HASH(...))（ORA_HASH 10g+ 可用）
    """
    db_type = (db_type or "").lower()
    if db_type in ("mysql", "mariadb"):
        expr_cols = ", ".join(
            "IFNULL(`" + c.replace("`", "``") + "`,'" + _NULL_MARK + "')" for c in cols)
        sql = ("SELECT COUNT(*), COALESCE(SUM(CRC32(CONCAT_WS('#', {e}))), 0) "
               "FROM {r}").format(e=expr_cols, r=ref)
    elif db_type in ("postgresql", "kingbase"):
        expr_cols = " || '##' || ".join(
            'COALESCE("' + c.replace('"', '""') + '"::text,\'{}\')'.format(_NULL_MARK)
            for c in cols)
        sql = ("SELECT COUNT(*), COALESCE(SUM(hashtext({e})::bigint), 0) "
               "FROM {r}").format(e=expr_cols, r=ref)
    elif db_type == "oracle":
        # Oracle：ORA_HASH(拼接表达式)，NVL 处理 NULL；TO_CHAR 统一转文本
        def nvl(col: str) -> str:
            return "NVL(TO_CHAR(\"{}\"), '{}')".format(
                col.replace('"', '""'), _NULL_MARK)
        expr = " || '#' || ".join(nvl(c) for c in cols)
        sql = ("SELECT COUNT(*), COALESCE(SUM(ORA_HASH({e})), 0) "
               "FROM {r}").format(e=expr, r=ref)
    elif db_type == "dameng":
        # 达梦兼容 Oracle 语法（ORA_HASH 可用）
        def nvl_dm(col: str) -> str:
            return "NVL(TO_CHAR(\"{}\"), '{}')".format(
                col.replace('"', '""'), _NULL_MARK)
        expr = " || '#' || ".join(nvl_dm(c) for c in cols)
        sql = ("SELECT COUNT(*), COALESCE(SUM(ORA_HASH({e})), 0) "
               "FROM {r}").format(e=expr, r=ref)
    elif db_type in ("sqlserver", "mssql"):
        # SQL Server：CHECKSUM_AGG(BINARY_CHECKSUM(*)) 原生聚合
        sql = ("SELECT COUNT(*), COALESCE(CHECKSUM_AGG(BINARY_CHECKSUM(*)), 0) "
               "FROM {r}").format(r=ref)
    else:
        raise ValueError(f"暂不支持 {db_type} 校验和")
    return sql


def _sample_sql(db_type: str, ref: str, order_col: str, n: int) -> str:
    """按第一列排序取前 n 行。Oracle 用 ROWNUM 子查询（兼容 11g）。"""
    db_type = (db_type or "").lower()
    order = _qident(db_type, order_col) if db_type not in ("oracle",) \
        else '"' + order_col.replace('"', '""') + '"'
    if db_type == "oracle":
        return ("SELECT * FROM (SELECT * FROM {r} ORDER BY {o}) WHERE ROWNUM <= {n}"
                ).format(r=ref, o=order, n=int(n))
    return "SELECT * FROM {r} ORDER BY {o} LIMIT {n}".format(r=ref, o=order, n=int(n))


# ---------------------------------------------------------------------------
# 行值归一化与比对
# ---------------------------------------------------------------------------
def _norm_val(v) -> str:
    """将 DB-API 返回值归一化为可比对字符串。"""
    if v is None:
        return _NULL_MARK
    if isinstance(v, bytes):
        return "0x" + v[:32].hex() + ("…" if len(v) > 32 else "")
    if hasattr(v, "isoformat"):           # datetime.date / datetime / time
        return v.isoformat(sep="T") if hasattr(v, "hour") else v.isoformat()
    if isinstance(v, float):
        return repr(round(v, 6))
    return str(v)


def _norm_row(row) -> list:
    return [_norm_val(v) for v in row]


# ---------------------------------------------------------------------------
# 主键查询（分块对比依赖）
# ---------------------------------------------------------------------------
def _get_pk_column(conn, db_type: str, database: str, schema: str,
                   table: str) -> str:
    """返回表的单列主键列名（无主键/复合主键返回 None，退回抽样对比）。"""
    cur = conn.cursor()
    db_type = (db_type or "").lower()
    try:
        if db_type in ("mysql", "mariadb"):
            cur.execute(
                "SELECT column_name FROM information_schema.key_column_usage "
                "WHERE table_schema=%s AND table_name=%s "
                "AND constraint_name='PRIMARY' ORDER BY ordinal_position",
                (database, table))
        elif db_type in ("postgresql", "kingbase"):
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                "AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=%s::regclass AND i.indisprimary",
                (f"{schema or 'public'}.{table}",))
        elif db_type in ("oracle", "dameng"):
            owner = (schema or _user_of(conn) or "").upper()
            cur.execute(
                "SELECT cols.column_name FROM all_constraints c "
                "JOIN all_cons_columns cols ON c.owner=cols.owner "
                "AND c.constraint_name=cols.constraint_name "
                f"WHERE c.owner = '{_lit_ident(db_type, owner)}' "
                f"AND c.table_name = '{_lit_ident(db_type, table)}' "
                "AND c.constraint_type='P' ORDER BY cols.position")
        else:
            return None
        pks = [str(r[0]) for r in cur.fetchall()]
        return pks[0] if len(pks) == 1 else None
    except Exception:
        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 分块对比（对标 pt-table-checksum：主键有序分块 + 归并差异定位）
# ---------------------------------------------------------------------------
def _pk_of(cols: list) -> list:
    """取主键列名列表（按列序）。"""
    return [c.name for c in cols if getattr(c, "is_primary", False)]


def _page_sql(db_type: str, ref: str, pk: str, last, chunk: int) -> str:
    """keyset 分页 SQL：WHERE pk > last ORDER BY pk LIMIT chunk。"""
    where = f" WHERE {pk} > " + (
        str(int(last)) if str(last).lstrip('-').isdigit()
        else "'" + str(last).replace("'", "''") + "'") if last is not None else ""
    if (db_type or "").lower() == "oracle":
        sql = (f"SELECT * FROM (SELECT * FROM {ref}{where} ORDER BY {pk}) "
               f"WHERE ROWNUM <= {chunk}")
    else:
        sql = f"SELECT * FROM {ref}{where} ORDER BY {pk} LIMIT {chunk}"
    return sql


def _pk_chunk_compare(src, dst, src_type: str, dst_type: str, task: dict,
                      table: str, ref_s: str, ref_d: str,
                      pk: str, pk_idx: int, pk_idx_d: int, out: dict,
                      dst_table: str = None,
                      max_diffs: int = 20) -> dict:
    """主键 keyset 双指针归并对比（对标 pt-table-checksum 差异定位）。

    两侧按主键序流式拉取（keyset 分页，每页 chunk 行），逐行归并：
    - 仅源有   → missing_in_target（目标缺行，修复=INSERT）
    - 仅目标有 → extra_in_target（目标多行，修复=DELETE）
    - 双侧都有但值不同 → changed（修复=UPDATE）
    内存占用 O(单页)，可对比千万级行；差异行携带行级明细与修复 SQL 模板。
    """
    chunk = int(task.get("chunk_rows") or 5000)

    def _iter_side(cur, db_type, ref, idx):
        """按主键 keyset 流式产出原始行（pk 取第 idx 列）。"""
        last = None
        while True:
            cur.execute(_page_sql(db_type, ref, pk, last, chunk))
            rows = cur.fetchall()
            if not rows:
                return
            for r in rows:
                yield r
            last = _norm_val(rows[-1][idx])

    it_s = _iter_side(cur_s := src.cursor(), src_type, ref_s, pk_idx)
    it_d = _iter_side(cur_d := dst.cursor(), dst_type, ref_d, pk_idx_d)
    diffs = []
    compared = 0
    rs, rd = next(it_s, None), next(it_d, None)
    try:
        while rs is not None or rd is not None:
            compared += 1
            ks = _norm_val(rs[0]) if rs else None
            kd = _norm_val(rd[0]) if rd else None
            if rd is None or (rs is not None and ks < kd):
                if len(diffs) < max_diffs:
                    diffs.append({
                        "op": "missing_in_target", "pk": ks,
                        "source": _norm_row(rs),
                        "repair": f"-- 目标缺行：请从源库补插该行（pk={ks}）"})
                rs = next(it_s, None)
            elif rs is None or kd < ks:
                if len(diffs) < max_diffs:
                    diffs.append({
                        "op": "extra_in_target", "pk": kd,
                        "target": _norm_row(rd),
                        "repair": f"DELETE FROM {ref_d} WHERE {pk} = {kd if str(kd).lstrip('-').isdigit() else chr(39)+str(kd).replace(chr(39), chr(39)*2)+chr(39)};"})
                rd = next(it_d, None)
            else:
                ns, nd = _norm_row(rs), _norm_row(rd)
                if ns != nd and len(diffs) < max_diffs:
                    diffs.append({"op": "changed", "pk": ks,
                                  "source": ns, "target": nd,
                                  "repair": f"UPDATE {ref_d} SET ... WHERE {pk} = "
                                            f"{ks if str(ks).lstrip('-').isdigit() else chr(39)+str(ks)+chr(39)};"})
                rs = next(it_s, None)
                rd = next(it_d, None)
        out["compared_rows"] = compared
        out["diff_count"] = len(diffs)
        out["diffs"] = diffs
        return out
    finally:
        try:
            cur_s.close()
        except Exception:
            pass
        try:
            cur_d.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 逐表比对
# ---------------------------------------------------------------------------
def _compare_table(src, dst, src_type: str, dst_type: str, task: dict,
                   table: str, target_table: str = None) -> dict:
    """对比单张表，返回结果 dict（永抛不异常，失败记 status=failed）。"""
    src_db = task.get("source_database") or ""
    src_schema = task.get("source_schema") or ""
    dst_db = task.get("target_database") or ""
    dst_schema = task.get("target_schema") or ""
    # 跨名对比：目标侧独立表名（src_table 仍用于源端，不能覆盖共享变量
    # ——此前 table=target_table 导致源侧也查目标表，永远"一致"）
    dst_table = target_table or table
    sample_n = int(task.get("sample_rows") or 100)
    enable_checksum = bool(task.get("enable_checksum"))

    out = {
        "table": table,
        "status": "failed",
        "source_rows": None,
        "target_rows": None,
        "rows_match": None,
        "source_checksum": None,
        "target_checksum": None,
        "checksum_match": None,
        "sample_diff_count": 0,
        "sample_diffs": [],
        "message": "",
    }
    cur_s = src.cursor()
    cur_d = dst.cursor()
    try:
        # 行数
        ref_s = _table_ref(src_type, src_db, src_schema, table)
        ref_d = _table_ref(dst_type, dst_db, dst_schema, dst_table)
        cur_s.execute(_row_count_sql(src_type, ref_s))
        out["source_rows"] = int(cur_s.fetchone()[0])
        cur_d.execute(_row_count_sql(dst_type, ref_d))
        out["target_rows"] = int(cur_d.fetchone()[0])
        out["rows_match"] = out["source_rows"] == out["target_rows"]

        if not out["rows_match"]:
            out["status"] = "mismatch"
            out["message"] = "行数不一致"
            return out

        # ---- 分块对比（对标 pt-table-checksum）：大表或有主键时走
        #      keyset 归并，精确输出差异行（missing/extra/changed）；
        #      行数少且无主键时退回抽样比对。
        pk = _get_pk_column(src, src_type, src_db, src_schema, table)
        threshold = int(task.get("chunk_threshold") or 20000)
        if pk and (out["source_rows"] > threshold
                   or str(task.get("force_pk_compare") or "") == "1"):
            cols_s = _get_columns(src, src_type, src_db, src_schema, table)
            cols_d = _get_columns(dst, dst_type, dst_db, dst_schema, dst_table)
            pk_idx = next((i for i, c in enumerate(cols_s)
                           if c.lower() == pk.lower()), 0)
            pk_idx_d = next((i for i, c in enumerate(cols_d)
                             if c.lower() == pk.lower()), 0)
            # 归一化列序：按源列序对齐（源/目标列数一致前提下）
            out["mode"] = "pk_chunk"
            out["pk"] = pk
            _pk_chunk_compare(src, dst, src_type, dst_type, task, table,
                              ref_s, ref_d, pk, pk_idx, pk_idx_d, out,
                              dst_table=dst_table)
            if out.get("diff_count"):
                out["status"] = "mismatch"
                out["message"] = (f"主键归并发现 {out['diff_count']} 处差异"
                                  f"（共比对 {out.get('compared_rows')} 行）")
            else:
                out["status"] = "match"
                out["message"] = (f"主键归并比对 {out.get('compared_rows')} 行一致")
            return out
        out["mode"] = "checksum_sample"

        # 校验和（可选）
        if enable_checksum:
            cols_s = _get_columns(src, src_type, src_db, src_schema, table)
            if not cols_s:
                out["message"] = "源表无列可校验"
                out["status"] = "mismatch"
                return out
            cur_s.execute(_checksum_sql(src_type, ref_s, cols_s))
            row_s = cur_s.fetchone()
            cols_d = _get_columns(dst, dst_type, dst_db, dst_schema, dst_table)
            cur_d.execute(_checksum_sql(dst_type, ref_d, cols_d))
            row_d = cur_d.fetchone()
            out["source_checksum"] = str(row_s[1])
            out["target_checksum"] = str(row_d[1])
            out["checksum_match"] = str(row_s[1]) == str(row_d[1])
            if not out["checksum_match"]:
                out["status"] = "mismatch"
                out["message"] = "全表校验和不一致"
                return out

        # 抽样比对
        order_col = None
        cols = _get_columns(src, src_type, src_db, src_schema, table)
        if cols:
            order_col = cols[0]
        if order_col and sample_n > 0:
            cur_s.execute(_sample_sql(src_type, ref_s, order_col, sample_n))
            rows_s = [_norm_row(r) for r in cur_s.fetchall()]
            cur_d.execute(_sample_sql(dst_type, ref_d, order_col, sample_n))
            rows_d = [_norm_row(r) for r in cur_d.fetchall()]
            diffs = []
            for i in range(max(len(rows_s), len(rows_d))):
                rs = rows_s[i] if i < len(rows_s) else None
                rd = rows_d[i] if i < len(rows_d) else None
                if rs != rd:
                    diffs.append({"source": rs, "target": rd})
                if len(diffs) >= 20:
                    break
            out["sample_diff_count"] = len(diffs)
            out["sample_diffs"] = diffs
            if diffs:
                out["status"] = "mismatch"
                out["message"] = f"抽样比对发现 {len(diffs)}+ 处差异"
                return out

        out["status"] = "match"
        out["message"] = "一致"
        return out
    except Exception as e:
        import traceback as _tb
        out["message"] = f"{type(e).__name__}: {e}\n{_tb.format_exc()[-600:]}"
        out["status"] = "failed"
        return out
    finally:
        try:
            cur_s.close()
        except Exception:
            pass
        try:
            cur_d.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 任务执行入口
# ---------------------------------------------------------------------------
def run_data_compare_task(task_id: int) -> dict:
    """执行一次数据对比任务，生成报告。

    可由 API（手动）或调度器触发。返回 {report_id, success, message}。
    """
    task = models.get_data_compare_task(task_id, include_secret=True)
    if not task:
        raise ValueError(f"数据对比任务不存在: {task_id}")

    report_id = models.create_data_compare_report({
        "task_id": task_id,
        "status": "running",
        "message": "开始数据对比",
    })
    models.set_data_compare_status(task_id, db.now_iso(), "running", report_id)

    started = time.monotonic()
    src = dst = None
    try:
        src_cfg = _side_cfg(task, "source")
        dst_cfg = _side_cfg(task, "target")
        src_type = (src_cfg.get("db_type") or "").lower()
        dst_type = (dst_cfg.get("db_type") or "").lower()

        src = _open_conn(src_cfg)
        dst = _open_conn(dst_cfg)

        # 表清单：任务指定 → 否则两端交集（大小写不敏感）
        wanted = task.get("tables") or []
        table_pairs = []   # (源表名, 目标表名)：元素可为 "T1" 或
                           # {"source": "T1", "target": "T1_CMP"}（跨名对比）
        for t in wanted:
            if isinstance(t, dict) and t.get("source"):
                table_pairs.append((str(t["source"]),
                                    str(t.get("target") or t["source"])))
            elif isinstance(t, str) and t:
                table_pairs.append((t, t))
        if not table_pairs:
            # 未指定表清单：两端交集（同名表对比）
            src_tables = _list_tables(src, src_type, src_cfg.get("database"),
                                      src_cfg.get("schema"))
            dst_tables = _list_tables(dst, dst_type, dst_cfg.get("database"),
                                      dst_cfg.get("schema"))
            lower = {t.lower(): t for t in src_tables}
            table_pairs = [(lower[t.lower()], t) for t in dst_tables
                           if t.lower() in lower]
        if not table_pairs:
            raise ValueError("没有可对比的表（任务未指定且两端无共有表）")

        results = []
        for src_t, dst_t in table_pairs:
            r = _compare_table(src, dst, src_type, dst_type, task, src_t,
                               target_table=dst_t)
            results.append(r)
            logger.info("[data_compare] task=%s table=%s -> %s",
                        task_id, src_t, r["status"])

        duration = round(time.monotonic() - started, 3)
        matched = sum(1 for r in results if r["status"] == "match")
        mismatched = sum(1 for r in results if r["status"] == "mismatch")
        failed = sum(1 for r in results if r["status"] == "failed")
        summary = {
            "tables_total": len(results),
            "tables_matched": matched,
            "tables_mismatched": mismatched,
            "tables_failed": failed,
            "duration_sec": duration,
        }
        # 状态语义：failed 仅表示对比执行异常；发现差异是正常业务结果
        # （status=mismatch），不应与执行失败混为一谈
        if failed:
            status = "failed"
            message = ("差异表 {m} / 失败表 {f}（共 {n} 张）".format(
                m=mismatched, f=failed, n=len(results)))
        elif mismatched:
            status = "mismatch"
            message = "发现差异表 {m}（共 {n} 张，一致 {ok} 张）".format(
                m=mismatched, n=len(results), ok=matched)
        else:
            status = "success"
            message = "全部 {n} 张表比对一致".format(n=len(results))

        models.update_data_compare_report(report_id, {
            "status": status,
            "duration_sec": duration,
            "summary_json": _dumps(summary),
            "tables_json": _dumps(results),
            "message": message,
            "finished_at": db.now_iso(),
        })
        models.set_data_compare_status(task_id, db.now_iso(), status, report_id)
        return {"report_id": report_id, "success": not failed,
                "message": message,
                "summary": summary}
    except Exception as e:
        duration = round(time.monotonic() - started, 3)
        msg = f"数据对比异常: {e}"
        logger.exception("数据对比失败 task_id=%s", task_id)
        models.update_data_compare_report(report_id, {
            "status": "failed",
            "duration_sec": duration,
            "message": msg,
            "finished_at": db.now_iso(),
        })
        models.set_data_compare_status(task_id, db.now_iso(), "failed", report_id)
        return {"report_id": report_id, "success": False, "message": msg}
    finally:
        for c in (src, dst):
            if c:
                try:
                    c.close()
                except Exception:
                    pass


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
