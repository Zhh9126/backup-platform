# -*- coding: utf-8 -*-
"""Oracle 同步插件（oracledb 瘦客户端，纯 Python 离线可用）。

- 源/目标均为 Oracle 时经 oracledb 直连（thin 模式，无需 Instant Client）；
- 模式（schema）即 Oracle user，大小写默认按大写处理（Oracle 惯例）；
- 增量同步：基于时间戳/数值增量列（incremental_column + incremental_value）。
"""
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List

from .base import (BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader,
                   SyncConfig, matrix_suggest)
from ..type_mapper import db_type_to_java_type, to_java, JavaType

logger = logging.getLogger(__name__)


def _upper(name: str) -> str:
    return (name or "").upper()


class OracleSourceReader(SourceReader):
    def connect(self) -> Any:
        import oracledb
        cfg = self.config
        port = cfg.src_port or 1521
        dsn = f"{cfg.src_host}:{port}/{cfg.src_db_name or 'ORCL'}"
        # oracledb 瘦客户端不支持 timeout 关键字（老版本 cx_Oracle 同样不支持）
        # 跨虚拟机 1521 偶发被网络重置（DPY-6005/12514）→ 自动重试
        last = None
        for attempt in range(3):
            try:
                return oracledb.connect(user=cfg.src_username,
                                        password=cfg.src_password, dsn=dsn,
                                        tcp_connect_timeout=15)
            except TypeError:
                return oracledb.connect(user=cfg.src_username,
                                        password=cfg.src_password, dsn=dsn)
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise last

    def list_tables(self) -> List[str]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                owner = _upper(self.config.src_schema or self.config.src_username)
                cur.execute(
                    "SELECT table_name FROM all_tables WHERE owner=:o "
                    "ORDER BY table_name", {"o": owner})
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    def list_columns(self, table: str) -> List[ColumnMeta]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                owner = _upper(self.config.src_schema or self.config.src_username)
                cur.execute(
                    "SELECT column_name, data_type, nullable, data_default, "
                    "data_length, data_precision, data_scale "
                    "FROM all_tab_columns WHERE owner=:o AND table_name=:t "
                    "ORDER BY column_id",
                    {"o": owner, "t": _upper(table)})
                rows = cur.fetchall()
                cur.execute(
                    "SELECT cc.column_name FROM all_constraints c "
                    "JOIN all_cons_columns cc ON c.owner=cc.owner "
                    "AND c.constraint_name=cc.constraint_name "
                    "WHERE c.owner=:o AND c.table_name=:t AND c.constraint_type='P'",
                    {"o": owner, "t": _upper(table)})
                pk_set = {r[0] for r in cur.fetchall()}
            cols = []
            for row in rows:
                c = ColumnMeta(name=row[0], type=(row[1] or "").upper(),
                               nullable=(row[2] == "Y"),
                               default=row[3], max_length=row[4],
                               numeric_precision=row[5], numeric_scale=row[6])
                c.is_primary = c.name in pk_set
                cols.append(c)
            return cols
        finally:
            conn.close()

    def _build_select_sql(self, table: str, columns: List[str]) -> str:
        owner = _upper(self.config.src_schema or self.config.src_username)
        table_ref = f'{owner}.{_upper(table)}'
        col_str = ", ".join(c for c in columns) if columns else "*"
        sql = f"SELECT {col_str} FROM {table_ref}"
        cfg = self.config
        conds = []
        if cfg.source_where:
            conds.append(f"({cfg.source_where})")
        if cfg.sync_mode == "incremental" and cfg.incremental_column:
            col = cfg.incremental_column
            if cfg.incremental_value:
                conds.append(f'{col} > TO_TIMESTAMP(:iv, '
                             f"'YYYY-MM-DD HH24:MI:SS.FF6')")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        # ORDER BY 仅增量模式需要（且必须有排序列）；full 模式/列未知时
        # 不加（空 ORDER BY → ORA-00936 missing expression，实测踩坑）
        order_col = cfg.incremental_column or (columns[0] if columns else "")
        if cfg.sync_mode == "incremental" and order_col:
            sql += " ORDER BY " + order_col
        return sql, ({"iv": cfg.incremental_value} if
                     (cfg.sync_mode == "incremental" and
                      cfg.incremental_column and cfg.incremental_value) else {})

    def read_batch(self, cursor: Any) -> ReadResult:
        cfg = self.config
        table = (cfg.source_tables_list or [cfg.source_table])[0] \
            if (cfg.source_tables_list or cfg.source_table) else ""
        sql, binds = self._build_select_sql(table, [])
        cursor.execute(sql, binds or {})
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(cfg.batch_size)
        # oracledb 的 description[i][1] 是 DbType 枚举（非 str）→ 取 .name 归一
        col_types = []
        for d in cursor.description:
            raw = d[1]
            col_types.append(raw if isinstance(raw, str)
                             else getattr(raw, "name", str(raw)))
        records = [[self.plugin.type_to_java(col_types[i], v)
                    for i, v in enumerate(row)] for row in rows]
        return ReadResult(records=records, columns=columns,
                          has_more=len(rows) >= cfg.batch_size)


class OracleSinkWriter(SinkWriter):
    def connect(self) -> Any:
        import oracledb
        cfg = self.config
        port = cfg.tgt_port or 1521
        dsn = f"{cfg.tgt_host}:{port}/{cfg.tgt_db_name or 'ORCL'}"
        # oracledb 瘦客户端不支持 timeout 关键字（老版本 cx_Oracle 同样不支持）
        # 跨虚拟机 1521 偶发被网络重置（DPY-6005/12514）→ 自动重试
        conn = None
        for attempt in range(3):
            try:
                conn = oracledb.connect(user=cfg.tgt_username,
                                        password=cfg.tgt_password, dsn=dsn,
                                        tcp_connect_timeout=15)
                break
            except TypeError:
                conn = oracledb.connect(user=cfg.tgt_username,
                                        password=cfg.tgt_password, dsn=dsn)
                break
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        if conn is None:
            raise last
        # 源端 DATETIME 以字符串绑定（"YYYY-MM-DD HH:MI:SS"），Oracle 默认
        # NLS_DATE_FORMAT 无法隐式转换（ORA-01843）→ 会话级对齐（业界标准做法）
        try:
            cur = conn.cursor()
            cur.execute("ALTER SESSION SET NLS_DATE_FORMAT="
                        "'YYYY-MM-DD HH24:MI:SS'")
            cur.close()
        except Exception:
            pass
        return conn

    def _table_ref(self, table: str = None) -> str:
        cfg = self.config
        schema = _upper(cfg.tgt_schema or cfg.tgt_username)
        tbl = _upper(table or cfg.target_table or "")
        return f"{schema}.{tbl}" if schema else tbl

    def _get_target_columns(self, conn: Any, table: str) -> List[str]:
        with conn.cursor() as cur:
            owner = _upper(self.config.tgt_schema or self.config.tgt_username)
            cur.execute(
                "SELECT column_name FROM all_tab_columns "
                "WHERE owner=:o AND table_name=:t ORDER BY column_id",
                {"o": owner, "t": _upper(table)})
            return [r[0] for r in cur.fetchall()]

    def _get_primary_keys(self, conn: Any, table: str) -> List[str]:
        with conn.cursor() as cur:
            owner = _upper(self.config.tgt_schema or self.config.tgt_username)
            cur.execute(
                "SELECT cc.column_name FROM all_constraints c "
                "JOIN all_cons_columns cc ON c.owner=cc.owner "
                "AND c.constraint_name=cc.constraint_name "
                "WHERE c.owner=:o AND c.table_name=:t AND c.constraint_type='P'",
                {"o": owner, "t": _upper(table)})
            return [r[0] for r in cur.fetchall()]

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        cfg = self.config
        schema = _upper(cfg.tgt_schema or cfg.tgt_username)
        tbl = _upper(table)
        defs = []
        for c in columns:
            name, t = _upper(c.name), c.type.upper()
            ora_t = self._map_to_oracle_type(c, t)
            defs.append(f"{name} {ora_t}"
                        + ("" if c.nullable else " NOT NULL"))
        pk = [c.name for c in columns if getattr(c, "is_primary", False)]
        if pk:
            defs.append("PRIMARY KEY (" + ", ".join(_upper(p) for p in pk) + ")")
        return f'CREATE TABLE {schema}.{tbl} ({", ".join(defs)})'

    def _map_to_oracle_type(self, c: ColumnMeta, t: str) -> str:
        """Oracle 建表类型映射：覆盖所有偏门类型，避免落 VARCHAR2(4000) 兜底。"""
        base = t.split("(")[0].strip()
        # 整数（Oracle 用 NUMBER(p) 表达）
        if base == "TINYINT":
            return "NUMBER(3)"
        if base == "SMALLINT":
            return "NUMBER(5)"
        if base in ("INT", "INTEGER"):
            return "NUMBER(10)"
        if base == "BIGINT":
            return "NUMBER(19)"
        if base in ("BINARY_INTEGER", "PLS_INTEGER"):
            return "NUMBER(10)"
        # 浮点/精确小数
        if base in ("DECIMAL", "NUMERIC", "NUMBER"):
            p = c.numeric_precision or 38
            s = c.numeric_scale if c.numeric_scale is not None else 0
            if p > 38:                            # Oracle NUMBER 上限 38
                p = 38
            return f"NUMBER({p},{s})" if s else f"NUMBER({p})"
        if base in ("FLOAT", "DOUBLE", "REAL", "BINARY_FLOAT", "BINARY_DOUBLE",
                    "DOUBLE PRECISION"):
            return "BINARY_DOUBLE"
        # 字符
        if base in ("CHAR", "NCHAR"):
            ln = c.max_length or 1
            if ln > 2000:                          # Oracle CHAR 上限 2000
                return f"{base}(2000)"
            return f"{base}({ln})"
        if base in ("VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2"):
            ln = c.max_length or 4000
            if ln > 4000:                          # Oracle VARCHAR2 上限 4000
                return "CLOB" if base not in ("NVARCHAR", "NVARCHAR2") else "NCLOB"
            # Oracle 没有 VARCHAR/NVARCHAR 标准类型，统一转 VARCHAR2/NVARCHAR2
            if base == "VARCHAR":
                base = "VARCHAR2"
            elif base == "NVARCHAR":
                base = "NVARCHAR2"
            return f"{base}({ln})"
        # 大对象
        if base in ("TEXT", "CLOB"):
            return "CLOB"
        if base == "NCLOB":
            return "NCLOB"
        if base in ("BLOB", "BYTEA", "IMAGE", "LONG RAW"):
            return "BLOB"
        if base == "LONG":
            return "CLOB"
        # 时间（DATETIME/TIMESTAMP 统一 DATE：避免 NLS_TIMESTAMP_FORMAT
        # 小数秒缺失问题 ORA-01843）
        if base == "DATE":
            return "DATE"
        if base.startswith("DATETIME") or base in ("TIMESTAMP", "DATETIME2", "SMALLDATETIME"):
            return "DATE"
        if base == "TIMESTAMPTZ" or base == "DATETIMEOFFSET":
            return "TIMESTAMP WITH TIME ZONE"
        if base == "TIME":
            return "VARCHAR(20)"                   # Oracle 无原生 TIME
        if base == "YEAR":
            return "NUMBER(4)"
        if base == "INTERVAL" or base.startswith("INTERVAL"):
            return "INTERVAL DAY TO SECOND"
        # 布尔
        if base in ("BOOLEAN", "BOOL"):
            return "NUMBER(1,0)"
        # 位串
        if base == "BIT":
            return "NUMBER(1,0)"
        # 特殊
        if base in ("JSON", "JSONB", "JSONPATH"):
            return "CLOB"                          # Oracle 无原生 JSON
        if base == "UUID" or base == "UNIQUEIDENTIFIER":
            return "VARCHAR2(36)"
        if base == "XML" or base == "XMLTYPE":
            return "XMLTYPE"
        if base in ("ENUM", "SET"):
            # Oracle 不支持 ENUM/SET（DTS 规则）；建表时只能兜底为 VARCHAR，
            # precheck/type_matrix 应在更早阶段拦截此组合，避免无效建表
            return "VARCHAR2(4000)"
        if base in ("UROWID", "ROWID"):
            return "UROWID" if base == "UROWID" else "ROWID"
        if base == "BFILE":
            return "BFILE"
        if base == "RAW":
            return f"RAW({min(c.max_length or 2000, 2000)})"
        if base in ("ROWVERSION",):
            return "RAW(8)"
        # 几何（Oracle 需 MDSYS.SDO_GEOMETRY + Oracle Spatial）
        if base in ("GEOMETRY", "POINT", "LINESTRING", "POLYGON",
                    "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON",
                    "GEOMETRYCOLLECTION", "GEOMCOLLECTION", "GEOGRAPHY",
                    "SDO_GEOMETRY", "SDO_TOPO_GEOMETRY", "SDO_GEORASTER"):
            return "MDSYS.SDO_GEOMETRY" if base in ("GEOMETRY", "SDO_GEOMETRY") else "CLOB"
        if base in ("ANYDATA", "ANYTYPE", "ANYDATASET", "REF"):
            return "CLOB"
        # 跨源兜底：源端类型来自其它库（JSONB/INET/TSVECTOR/HSTORE/HIERARCHYID/
        # SQL_VARIANT/VECTOR/ST_GEOMETRY...）时用统一矩阵翻译，避免一律落 VARCHAR2(4000)
        sug = matrix_suggest(getattr(self, "config", None), "oracle", t)
        if sug:
            if sug in ("VARCHAR", "CHAR", "NVARCHAR", "NVARCHAR2", "VARCHAR2"):
                name = "NVARCHAR2" if sug.startswith("N") else "VARCHAR2"
                return f"{name}({min(c.max_length or 4000, 4000)})"
            if sug in ("NUMERIC", "DECIMAL", "NUMBER"):
                p = min(c.numeric_precision or 38, 38)
                return f"NUMBER({p},{c.numeric_scale or 0})"
            return sug
        return "VARCHAR2(4000)"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table
        with conn.cursor() as cur:
            owner = _upper(cfg.tgt_schema or cfg.tgt_username)
            cur.execute(
                "SELECT COUNT(*) FROM all_tables WHERE owner=:o "
                "AND table_name=:t", {"o": owner, "t": _upper(table)})
            exists = cur.fetchone()[0] > 0
            mode = cfg.save_mode
            if mode == "overwrite" and exists:
                cur.execute(f"DROP TABLE {self._table_ref(table)}")
                exists = False
            if not exists:
                cur.execute(self._create_table_sql(table, columns))
            conn.commit()

    def write_batch(self, conn: Any, records: List[List[Any]],
                    columns: List[str]) -> int:
        cfg = self.config
        table = cfg.target_table
        with conn.cursor() as cur:
            pks = self._get_primary_keys(conn, table)
            ncols = [self.plugin.normalize_identifier(
                c, cfg.field_ide) for c in columns]
            if cfg.save_mode == "upsert" and pks:
                # MERGE INTO（UPSERT）
                upd = [c for c in ncols if _upper(c) not in
                       {_upper(p) for p in pks}]
                set_sql = ", ".join(
                    f"{_upper(c)} = :{i + len(pks) + 1}"
                    for i, c in enumerate(upd)) or f"{_upper(ncols[0])}={_upper(ncols[0])}"
                on_sql = " AND ".join(
                    f"T.{_upper(p)} = :{i + 1}"
                    for i, p in enumerate(pks))
                ins_cols = ", ".join(_upper(c) for c in ncols)
                ins_vals = ", ".join(
                    f":{i + 1}" for i in range(len(ncols)))
                sql = (f"MERGE INTO {self._table_ref(table)} T USING "
                       f"(SELECT 1 AS ONE FROM DUAL) S ON ({on_sql}) "
                       f"WHEN MATCHED THEN UPDATE SET {set_sql} "
                       f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) "
                       f"VALUES ({ins_vals})")
                rows_data = []
                for rec in records:
                    d = dict(zip(ncols, rec))
                    rows_data.append([d.get(_upper(p)) for p in pks]
                                     + [d.get(_upper(c)) for c in upd])
                cur.executemany(sql, rows_data)
            else:
                cols_sql = ", ".join(_upper(c) for c in ncols)
                binds = ", ".join(f":{i + 1}" for i in range(len(ncols)))
                sql = (f"INSERT INTO {self._table_ref(table)} "
                       f"({cols_sql}) VALUES ({binds})")
                cur.executemany(sql, records)
            conn.commit()
            return len(records)


class OraclePlugin(BasePlugin):
    db_type = "oracle"
    default_ports = {"sync": 1521}

    def create_reader(self, config: SyncConfig) -> SourceReader:
        return OracleSourceReader(config, self)

    def create_writer(self, config: SyncConfig) -> SinkWriter:
        return OracleSinkWriter(config, self)

    def type_to_java(self, db_type: str, value: Any) -> Any:
        """按目标 Java 类型归一化（对齐 MySQL 插件实现；原实现误把
        两个参数传给单参 to_java → TypeError）。"""
        if value is None:
            return None
        jt = db_type_to_java_type((db_type or "").upper())
        if jt == JavaType.BOOLEAN:
            return bool(value)
        if jt == JavaType.LONG:
            return int(value)
        if jt == JavaType.DOUBLE:
            return float(value)
        if jt == JavaType.DECIMAL:
            return str(value)
        if jt == JavaType.BYTES:
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            return str(value).encode("utf-8")
        return to_java(value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return java_value

    def quote_identifier(self, name: str) -> str:
        return f'"{_upper(name)}"'
