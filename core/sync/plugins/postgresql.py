# -*- coding: utf-8 -*-
"""PostgreSQL 同步插件。"""
import logging
from typing import Any, List

from .base import BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader, SyncConfig
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
                cols = []
                for row in rows:
                    ctype = row[1].upper().split("(")[0]
                    cols.append(ColumnMeta(name=row[0], type=ctype, nullable=row[2],
                                           default=row[3]))
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
        source_cols = [m.get("source") for m in mapping if m.get("source")]
        if not source_cols:
            source_cols = ["*"]

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

    def _map_to_pg_type(self, col: ColumnMeta) -> str:
        t = (col.type or "VARCHAR").upper()
        if t in ("VARCHAR", "CHAR"):
            return f"{t}({col.max_length or 255})"
        if t in ("DECIMAL", "NUMERIC"):
            return f"{t}({col.numeric_precision or 10},{col.numeric_scale or 0})"
        if t in ("INT", "BIGINT", "SMALLINT", "INTEGER", "SERIAL", "BIGSERIAL"):
            return t
        if t in ("TEXT", "DATE", "TIMESTAMP", "TIMESTAMPTZ", "TIME", "TIMETZ",
                 "BOOLEAN", "BOOL", "FLOAT", "DOUBLE", "REAL", "JSON", "JSONB",
                 "BYTEA"):
            return t
        return "VARCHAR(255)"

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        table_ref = self._table_ref(table)
        lines = []
        pks = []
        for c in columns:
            ctype = self._map_to_pg_type(c)
            null_str = "NULL" if c.nullable else "NOT NULL"
            default_str = f" DEFAULT {c.default}" if c.default is not None else ""
            lines.append(f'    "{c.name}" {ctype} {null_str}{default_str}')
            if getattr(c, "is_primary", False):
                pks.append(c.name)
        if pks:
            lines.append(f"    PRIMARY KEY ({', '.join(f'\"{k}\"' for k in pks)})")
        return f"CREATE TABLE IF NOT EXISTS {table_ref} (\n" + ",\n".join(lines) + "\n)"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        with conn.cursor() as cur:
            if cfg.save_mode == "overwrite":
                cur.execute(f"TRUNCATE TABLE {self._table_ref(table)}")
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
                java_val = self.plugin.type_to_java(target_type, row[i])
                out.append(to_db(java_val, target_type))
            return tuple(out)

        col_str = ", ".join(f'"{c}"' for c in target_cols)
        placeholders = ", ".join(["%s"] * len(target_cols))
        values = [convert_row(r) for r in records]

        with conn.cursor() as cur:
            if cfg.save_mode == "upsert":
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
