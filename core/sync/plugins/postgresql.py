# -*- coding: utf-8 -*-
"""PostgreSQL 同步插件。"""
import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, List

from .base import (BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader,
                   SyncConfig, matrix_suggest)
from ..type_mapper import JavaType, db_type_to_java_type, to_db, to_java

logger = logging.getLogger(__name__)


class PostgreSQLSourceReader(SourceReader):
    def connect(self) -> Any:
        import psycopg2
        cfg = self.config
        port = cfg.src_port or 5432
        return psycopg2.connect(
            host=cfg.src_host,
            port=port,
            user=cfg.src_username,
            password=cfg.src_password,
            dbname=cfg.src_db_name,
            connect_timeout=8,
        )

    def list_tables(self) -> List[str]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                schema = self.config.src_schema or "public"
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s ORDER BY table_name",
                    (schema,),
                )
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    def list_columns(self, table: str) -> List[ColumnMeta]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                schema = self.config.src_schema or "public"
                cur.execute(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
                    "NOT a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
                    "FROM pg_attribute a LEFT JOIN pg_attrdef d ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
                    "WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped "
                    "ORDER BY a.attnum",
                    (f"{schema}.{table}",),
                )
                rows = cur.fetchall()
                # 主键
                cur.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid=%s::regclass AND i.indisprimary",
                    (f"{schema}.{table}",),
                )
                pk_set = {r[0] for r in cur.fetchall()}
                # 字符类型（max_length 用字符长度）与数值类型（precision/scale 用精度）的归一
                _CHAR_BASES = {"CHARACTER VARYING", "VARCHAR", "CHARACTER", "CHAR",
                               "NCHAR", "NVARCHAR", "NVARCHAR2", "BPCHAR"}
                _NUM_BASES = {"NUMERIC", "DECIMAL"}
                cols = []
                for row in rows:
                    raw = (row[1] or "").strip()
                    # 拆出精度括号与数组后缀（PG 列类型形如 'numeric(12,3)'/'integer[]'/'varchar(64)'）
                    m = re.match(r"^\s*([^(]+?)\s*(?:\(([^)]*)\))?\s*(\[\])?\s*$", raw)
                    if m:
                        base = m.group(1).strip().upper()
                        inner = (m.group(2) or "").strip()
                        arr_sfx = m.group(3) or ""
                    else:
                        base = raw.upper().strip()
                        inner = ""
                        arr_sfx = ""
                    ctype = base + arr_sfx
                    prec = scale = None
                    if inner and re.fullmatch(r"\s*\d+\s*(?:,\s*\d+)?\s*", inner):
                        parts = inner.split(",")
                        try: prec = int(parts[0])
                        except Exception: pass
                        if len(parts) > 1:
                            try: scale = int(parts[1])
                            except Exception: pass
                    cols.append(ColumnMeta(
                        name=row[0], type=ctype, nullable=row[2], default=row[3],
                        max_length=prec if base in _CHAR_BASES else None,
                        numeric_precision=prec if base in _NUM_BASES else None,
                        numeric_scale=scale if base in _NUM_BASES else None,
                    ))
                for c in cols:
                    c.is_primary = c.name in pk_set
                return cols
        finally:
            conn.close()

    def _build_select_sql(self, table: str, columns: List[str]) -> str:
        schema = self.config.src_schema or "public"
        table_ref = f'"{schema}"."{table}"'
        col_str = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        sql = f"SELECT {col_str} FROM {table_ref}"
        where_parts = []
        if self.config.source_where:
            where_parts.append(f"({self.config.source_where})")
        if self.config.sync_mode == "incremental" and self.config.incremental_column:
            where_parts.append(f'"{self.config.incremental_column}" > %s')
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if self.config.incremental_column:
            sql += f' ORDER BY "{self.config.incremental_column}"'
        sql += " LIMIT %s"
        return sql

    def read_batch(self, cursor: Any) -> ReadResult:
        cfg = self.config
        table = cfg.source_table
        mapping = cfg.column_mapping or []
        # 无 mapping 时不限定列（SELECT *）；空列表语义=全列
        source_cols = [m.get("source") for m in mapping if m.get("source")]

        sql = self._build_select_sql(table, source_cols)
        params = []
        if cfg.sync_mode == "incremental" and cfg.incremental_column and cfg.incremental_value:
            params.append(cfg.incremental_value)
        params.append(cfg.batch_size)

        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        desc = cursor.description
        if not desc:
            return ReadResult(has_more=False)
        columns = [d.name for d in desc]
        records = [[self.plugin.type_to_java(db_type_to_java_type(str(desc[i].type_code)), v)
                    for i, v in enumerate(row)]
                   for row in rows]

        next_value = None
        if cfg.incremental_column and rows:
            idx = columns.index(cfg.incremental_column)
            next_value = to_java(rows[-1][idx])
        return ReadResult(records=records, columns=columns, has_more=len(rows) >= cfg.batch_size,
                          next_value=next_value)


class PostgreSQLSinkWriter(SinkWriter):
    def connect(self) -> Any:
        import psycopg2
        cfg = self.config
        port = cfg.tgt_port or 5432
        return psycopg2.connect(
            host=cfg.tgt_host,
            port=port,
            user=cfg.tgt_username,
            password=cfg.tgt_password,
            dbname=cfg.tgt_db_name,
            connect_timeout=8,
        )

    def _table_ref(self, table: str = None) -> str:
        cfg = self.config
        schema = cfg.tgt_schema or "public"
        t = table or cfg.target_table or cfg.source_table
        return f'"{schema}"."{t}"'

    def _get_primary_keys(self, conn: Any, table: str) -> List[str]:
        schema = self.config.tgt_schema or "public"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=%s::regclass AND i.indisprimary",
                (f"{schema}.{table}",),
            )
            return [r[0] for r in cur.fetchall()]

    # MySQL/通用 DATA_TYPE（无长度后缀） -> PostgreSQL 类型
    _MYSQL_TYPE_MAP = {
        "TINYINT": "SMALLINT",      # MySQL BOOL/TINYINT(1) 落 SMALLINT，避免 int->bool 适配问题
        "SMALLINT": "SMALLINT",
        "MEDIUMINT": "INTEGER",
        "INT": "INTEGER",
        "INTEGER": "INTEGER",
        "BIGINT": "BIGINT",
        "YEAR": "SMALLINT",
        "FLOAT": "REAL",
        "DOUBLE": "DOUBLE PRECISION",
        "REAL": "REAL",
        "BOOL": "BOOLEAN",
        "BOOLEAN": "BOOLEAN",
        "DATE": "DATE",
        "DATETIME": "TIMESTAMP",
        "TIMESTAMP": "TIMESTAMP",
        "TIME": "TIME",
        "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
        "CHAR": "CHAR",
        "VARCHAR": "VARCHAR",
        "TINYTEXT": "TEXT",
        "TEXT": "TEXT",
        "MEDIUMTEXT": "TEXT",
        "LONGTEXT": "TEXT",
        "BINARY": "BYTEA",
        "VARBINARY": "BYTEA",
        "TINYBLOB": "BYTEA",
        "BLOB": "BYTEA",
        "MEDIUMBLOB": "BYTEA",
        "LONGBLOB": "BYTEA",
        "JSON": "JSONB",            # MySQL JSON → PG JSONB（性能与索引更优）
        "ENUM": "TEXT",             # 跨库枚举约束丢失，DTS 规则
        "SET": "TEXT",
        "BIT": "SMALLINT",          # MySQL BIT(n) → PG SMALLINT（位串类型绑定兼容问题）
        "DECIMAL": "NUMERIC",
        "NUMERIC": "NUMERIC",
        # 各数据库偏门类型（跨库映射）
        "GEOMETRY": "TEXT",         # PG 需 PostGIS 扩展，否则降 TEXT
        "POINT": "TEXT",
        "LINESTRING": "TEXT",
        "POLYGON": "TEXT",
        "MULTIPOINT": "TEXT",
        "MULTILINESTRING": "TEXT",
        "MULTIPOLYGON": "TEXT",
        "GEOMETRYCOLLECTION": "TEXT",
        "GEOMCOLLECTION": "TEXT",
        "GEOGRAPHY": "TEXT",        # SQL Server 地理 → 字符串
        "UNIQUEIDENTIFIER": "UUID", # SQL Server UUID → PG 原生 UUID
        "HIERARCHYID": "TEXT",      # SQL Server 层次路径 → 字符串
        "ROWVERSION": "BYTEA",      # SQL Server 8 字节二进制
        "SQL_VARIANT": "TEXT",
        "VECTOR": "TEXT",
        "MONEY": "NUMERIC(19,4)",
        "SMALLMONEY": "NUMERIC(10,4)",
        "XML": "XML",               # PG XML 类型
        "XMLTYPE": "XML",
        "UROWID": "VARCHAR(40)",
        "ROWID": "VARCHAR(40)",
        "BFILE": "TEXT",            # 外部文件路径
        "LONG": "TEXT",             # Oracle 已弃用 LONG
        "LONG RAW": "BYTEA",
        "LONG VARCHAR": "TEXT",
        "LONG VARBINARY": "BYTEA",
        "IMAGE": "BYTEA",
        "ANYDATA": "TEXT",
        "ANYTYPE": "TEXT",
        "ANYDATASET": "TEXT",
        "REF": "TEXT",
        "NVARCHAR": "VARCHAR",
        "NVARCHAR2": "VARCHAR",
        "NCHAR": "CHAR",
        "NCHAR2": "CHAR",
        "RAW": "BYTEA",
        "NCLOB": "TEXT",
        "INTERVAL": "INTERVAL",
        "DATETIME2": "TIMESTAMP",
        "DATETIMEOFFSET": "TIMESTAMP WITH TIME ZONE",
        "SMALLDATETIME": "TIMESTAMP",
        "BINARY_DOUBLE": "DOUBLE PRECISION",
        "BINARY_FLOAT": "REAL",
        "PLS_INTEGER": "INTEGER",
        "BINARY_INTEGER": "INTEGER",
        "JSONB": "JSONB",           # PG 同库保形
        "JSONPATH": "TEXT",
        "UUID": "UUID",             # PG 原生 UUID
        "ARRAY": "JSONB",           # PG 数组 → JSONB（应用层适配）
        "INET": "TEXT",
        "CIDR": "TEXT",
        "MACADDR": "TEXT",
        "MACADDR8": "TEXT",
        "HSTORE": "TEXT",
        "TSVECTOR": "TEXT",
        "TSQUERY": "TEXT",
        "INT4RANGE": "TEXT",
        "INT8RANGE": "TEXT",
        "NUMRANGE": "TEXT",
        "DATERANGE": "TEXT",
        "TSRANGE": "TEXT",
        "TSTZRANGE": "TEXT",
        "CITEXT": "TEXT",
        "LTREE": "TEXT",
        "CUBE": "TEXT",
    }

    _PG_FUNC_DEFAULTS = {
        "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()", "CURRENT_DATE", "CURRENT_TIME",
        "NOW()", "LOCALTIME", "LOCALTIMESTAMP", "CURRENT_USER", "NULL", "DEFAULT",
    }

    def _map_to_pg_type(self, col: ColumnMeta) -> str:
        t = (col.type or "VARCHAR").upper()
        base = t.split("(")[0].strip() if "(" in t else t
        # MySQL 无符号大整数超出 PG BIGINT 范围，落 NUMERIC(20,0)
        if "UNSIGNED" in t and base in ("BIGINT", "INT", "INTEGER", "MEDIUMINT", "TINYINT", "SMALLINT"):
            return "NUMERIC(20,0)"
        pg = self._MYSQL_TYPE_MAP.get(base)
        if pg is None:
            # 跨源兜底：源端类型是其它库特有/偏门类型（XMLTYPE/ROWID/BFILE/INET/
            # HIERARCHYID/VECTOR/ST_GEOMETRY...）时用统一类型矩阵翻译，避免一律落 TEXT
            sug = matrix_suggest(getattr(self, "config", None), "postgresql", col.type)
            if sug:
                if sug.startswith("VARCHAR") or sug.startswith("CHAR"):
                    return f"{sug.split('(')[0]}({col.max_length or 255})"
                if sug in ("NUMERIC", "DECIMAL"):
                    return f"NUMERIC({col.numeric_precision or 10},{col.numeric_scale or 0})"
                return sug
            return "TEXT"
        if pg in ("VARCHAR", "CHAR"):
            return f"{pg}({col.max_length or 255})"
        if pg == "NUMERIC":
            return f"NUMERIC({col.numeric_precision or 10},{col.numeric_scale or 0})"
        return pg

    @staticmethod
    def _pg_default(value: Any, ctype: str) -> str:
        """MySQL 列默认值 -> PG 合法 DEFAULT 表达式。"""
        if value is None:
            return ""
        s = str(value).strip()
        upper = s.upper()
        if upper in PostgreSQLSinkWriter._PG_FUNC_DEFAULTS:
            return f" DEFAULT {upper.replace('CURRENT_TIMESTAMP()', 'CURRENT_TIMESTAMP')}"
        if s == "":
            return ""
        if re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
            return f" DEFAULT {s}"
        if ctype in ("BOOLEAN", "BOOL") and upper in ("TRUE", "FALSE", "1", "0", "'1'", "'0'"):
            return " DEFAULT TRUE" if s.strip("'") in ("1", "TRUE") else " DEFAULT FALSE"
        return f" DEFAULT '{s.replace(chr(39), chr(39) * 2)}'"

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        table_ref = self._table_ref(table)
        lines = []
        pks = []
        for c in columns:
            ctype = self._map_to_pg_type(c)
            null_str = "NULL" if c.nullable else "NOT NULL"
            default_str = self._pg_default(c.default, ctype)
            # 列名按 field_ide 归一（与 write_batch 一致；否则建表用源库
            # 大写列名而写入按 lower，列名错位导致 UndefinedColumn）
            cname = self.plugin.normalize_identifier(
                c.name, self.config.field_ide)
            lines.append(f'    "{cname}" {ctype} {null_str}{default_str}')
            if getattr(c, "is_primary", False):
                pks.append(cname)
        if pks:
            pk_cols = ", ".join('"' + k + '"' for k in pks)
            lines.append(f"    PRIMARY KEY ({pk_cols})")
        return f"CREATE TABLE IF NOT EXISTS {table_ref} (\n" + ",\n".join(lines) + "\n)"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        with conn.cursor() as cur:
            if cfg.save_mode == "overwrite":
                # overwrite = 按当前字段映射重建表（与达梦 writer 一致）：
                # DROP + CREATE，保证列名归一/类型映射后结构仍与源对齐
                cur.execute(f"DROP TABLE IF EXISTS {self._table_ref(table)}")
                cur.execute(self._create_table_sql(table, columns))
                conn.commit()
            elif cfg.save_mode in ("create_if_not_exists", "upsert"):
                cur.execute(self._create_table_sql(table, columns))
                conn.commit()

    def write_batch(self, conn: Any, records: List[List[Any]], columns: List[str]) -> int:
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        table_ref = self._table_ref(table)
        mapping = cfg.column_mapping or []

        target_cols = []
        for src_col in columns:
            mapped = next((m for m in mapping if m.get("source") == src_col), None)
            target_name = mapped.get("target", src_col) if mapped else src_col
            target_name = self.plugin.normalize_identifier(target_name, cfg.field_ide)
            target_cols.append(target_name)

        def convert_row(row):
            out = []
            for i, src_col in enumerate(columns):
                mapped = next((m for m in mapping if m.get("source") == src_col), None)
                target_type = JavaType.STRING
                if mapped and mapped.get("target_type"):
                    target_type = mapped.get("target_type")
                # bytes 原样透传（bytea/bytea 兼容列），不做 STRING 字符串化
                # （此前 bytes → "b'...'" 字符串导致 bytea 列写入失败）
                if isinstance(row[i], (bytes, bytearray)):
                    out.append(bytes(row[i]))
                    continue
                java_val = self.plugin.type_to_java(target_type, row[i])
                out.append(to_db(java_val, target_type))
            return tuple(out)

        col_str = ", ".join(f'"{c}"' for c in target_cols)
        placeholders = ", ".join(["%s"] * len(target_cols))
        values = [convert_row(r) for r in records]

        with conn.cursor() as cur:
            # save_mode 为空时与 prepare_table 保持同一默认（upsert），
            # 避免默认语义错位导致主键冲突全量失败（与 MySQL writer 一致）
            if (cfg.save_mode or "upsert") == "upsert":
                pks = self._get_primary_keys(conn, table)
                if pks:
                    conflict_keys = ", ".join(f'"{k}"' for k in pks)
                    updates = ", ".join(
                        f'"{c}" = EXCLUDED."{c}"' for c in target_cols if c not in pks
                    )
                    sql = (
                        f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({conflict_keys}) DO UPDATE SET {updates}"
                    )
                else:
                    sql = f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            else:
                sql = f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders})"
            cur.executemany(sql, values)
            conn.commit()
            return cur.rowcount

    # ---- 实时同步（Binlog CDC）----

    def _pg_value(self, v: Any) -> Any:
        """binlog 值转为 psycopg2 可写值。"""
        if v is None:
            return None
        if isinstance(v, (datetime, date, time, Decimal, bool)):
            return v
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        # MySQL TIME -> timedelta；PG time 列不认 timedelta，转 "HH:MM:SS"
        if isinstance(v, timedelta):
            total = int(v.total_seconds())
            return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
        return v

    def apply_binlog_row(self, conn: Any, op: str, schema: str, table: str,
                         before: dict, after: dict) -> None:
        """把一条 Binlog 行事件应用到 PG（实时同步，参考 Flink CDC 语义）。

        op: insert | update | delete
        before/after: 列名 -> 值 字典（binlog_row_image=FULL 时为整行快照）
        """
        cfg = self.config
        t = cfg.target_table or table
        table_ref = self._table_ref(t)

        def q(name: str) -> str:
            return self.plugin.normalize_identifier(name, cfg.field_ide)

        pks = self._get_primary_keys(conn, t)

        def build_where(old: dict):
            parts, vals = [], []
            for pk in pks:
                if pk in old:
                    parts.append(f'"{q(pk)}" = %s')
                    vals.append(self._pg_value(old[pk]))
            if not parts:
                for c in old:
                    parts.append(f'"{q(c)}" = %s')
                    vals.append(self._pg_value(old[c]))
            return parts, vals

        with conn.cursor() as cur:
            if op == "insert":
                row = after or before or {}
                if not row:
                    return
                cols = list(row.keys())
                placeholders = ", ".join(["%s"] * len(cols))
                col_str = ", ".join(f'"{q(c)}"' for c in cols)
                vals = tuple(self._pg_value(row[c]) for c in cols)
                sql = f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders})"
                if pks:
                    conflict = ", ".join(f'"{q(k)}"' for k in pks)
                    updates = ", ".join(
                        f'"{q(c)}" = EXCLUDED."{q(c)}"' for c in cols if c not in pks
                    )
                    if updates:
                        sql += f" ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
                    else:
                        sql += " ON CONFLICT DO NOTHING"
                cur.execute(sql, vals)
                conn.commit()
            elif op == "update":
                if not after:
                    return
                old = before or after
                cols = list(after.keys())
                set_str = ", ".join(f'"{q(c)}" = %s' for c in cols)
                vals = [self._pg_value(after[c]) for c in cols]
                where_parts, where_vals = build_where(old)
                if not where_parts:
                    return
                sql = f"UPDATE {table_ref} SET {set_str} WHERE {' AND '.join(where_parts)}"
                cur.execute(sql, vals + where_vals)
                conn.commit()
            elif op == "delete":
                if not before:
                    return
                where_parts, where_vals = build_where(before)
                if not where_parts:
                    return
                sql = f"DELETE FROM {table_ref} WHERE {' AND '.join(where_parts)}"
                cur.execute(sql, where_vals)
                conn.commit()


class PostgreSQLPlugin(BasePlugin):
    db_type = "postgresql"
    default_ports = {"postgresql": 5432}

    def create_reader(self, config: SyncConfig) -> SourceReader:
        return PostgreSQLSourceReader(config, self)

    def create_writer(self, config: SyncConfig) -> SinkWriter:
        return PostgreSQLSinkWriter(config, self)

    def type_to_java(self, db_type: str, value: Any) -> Any:
        jt = db_type_to_java_type(db_type)
        if value is None:
            return None
        if jt == JavaType.BOOLEAN:
            return bool(value)
        if jt == JavaType.LONG:
            return int(value)
        if jt == JavaType.DOUBLE:
            return float(value)
        if jt == JavaType.DECIMAL:
            return str(value)
        if jt == JavaType.BYTES:
            return bytes(value) if not isinstance(value, (bytes, bytearray)) else value
        if jt in (JavaType.DATE, JavaType.TIME, JavaType.DATETIME):
            return str(value)
        return str(value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return to_db(java_value, target_type)

    def quote_identifier(self, name: str) -> str:
        return f'"{name}"'

    def disable_constraints(self, conn: Any) -> None:
        """禁用约束（参考 pg2mysql 优化，用 session_replication_role 跳过 FK 检查）。"""
        try:
            with conn.cursor() as cur:
                cur.execute("SET session_replication_role = 'replica'")
            conn.commit()
            logger.info("PG session_replication_role = replica (constraints disabled)")
        except Exception as e:
            logger.warning("禁用约束失败：%s", e)

    def enable_constraints(self, conn: Any) -> None:
        """恢复约束检查。"""
        try:
            with conn.cursor() as cur:
                cur.execute("SET session_replication_role = 'origin'")
            conn.commit()
            logger.info("PG session_replication_role = origin (constraints enabled)")
        except Exception as e:
            logger.warning("恢复约束失败：%s", e)
