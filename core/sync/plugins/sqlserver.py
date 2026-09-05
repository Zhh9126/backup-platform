# -*- coding: utf-8 -*-
"""SQL Server 同步插件（pymssql 驱动，离线包随附 wheel）。"""
import logging
from typing import Any, List

from .base import BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader, SyncConfig
from ..type_mapper import db_type_to_java_type, to_java

logger = logging.getLogger(__name__)


class SQLServerSourceReader(SourceReader):
    def connect(self) -> Any:
        import pymssql
        cfg = self.config
        return pymssql.connect(server=cfg.src_host, port=str(cfg.src_port or 1433),
                               user=cfg.src_username, password=cfg.src_password,
                               database=cfg.src_db_name, timeout=15,
                               login_timeout=15, as_dict=False)

    def list_tables(self) -> List[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            schema = self.config.src_schema or "dbo"
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s ORDER BY table_name", (schema,))
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    def list_columns(self, table: str) -> List[ColumnMeta]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            schema = self.config.src_schema or "dbo"
            cur.execute(
                "SELECT c.column_name, c.data_type, CASE WHEN c.is_nullable='YES' "
                "THEN 1 ELSE 0 END, c.column_default, c.character_maximum_length, "
                "c.numeric_precision, c.numeric_scale, "
                "CASE WHEN kcu.column_name IS NOT NULL THEN 1 ELSE 0 END "
                "FROM information_schema.columns c "
                "LEFT JOIN information_schema.table_constraints tc "
                "ON tc.table_schema=c.table_schema AND tc.table_name=c.table_name "
                "AND tc.constraint_type='PRIMARY KEY' "
                "LEFT JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_name=tc.constraint_name "
                "AND kcu.column_name=c.column_name "
                "WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position",
                (schema, table))
            cols = []
            for row in cur.fetchall():
                c = ColumnMeta(name=row[0], type=(row[1] or "").upper(),
                               nullable=bool(row[2]), default=row[3],
                               max_length=row[4], numeric_precision=row[5],
                               numeric_scale=row[6])
                c.is_primary = bool(row[7])
                cols.append(c)
            return cols
        finally:
            conn.close()

    def _build_select_sql(self, table: str, columns: List[str]) -> str:
        schema = self.config.src_schema or "dbo"
        table_ref = f"[{schema}].[{table}]"
        col_str = ", ".join(f"[{c}]" for c in columns) if columns else "*"
        sql = f"SELECT {col_str} FROM {table_ref}"
        cfg = self.config
        params = []
        if cfg.source_where:
            sql += f" WHERE ({cfg.source_where})"
        if cfg.sync_mode == "incremental" and cfg.incremental_column \
                and cfg.incremental_value:
            sql += (" WHERE " if "WHERE" not in sql else " AND ") + \
                f"[{cfg.incremental_column}] > %s"
            params.append(cfg.incremental_value)
        if cfg.incremental_column:
            sql += f" ORDER BY [{cfg.incremental_column}]"
        return sql, params

    def read_batch(self, cursor: Any) -> ReadResult:
        cfg = self.config
        table = (cfg.source_tables_list or [cfg.source_table])[0] \
            if (cfg.source_tables_list or cfg.source_table) else ""
        sql, params = self._build_select_sql(table, [])
        cursor.execute(sql, tuple(params))
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(cfg.batch_size)
        records = [[self.plugin.type_to_java(
            (cursor.description[i][1] or str), v)
            for i, v in enumerate(row)] for row in rows]
        return ReadResult(records=records, columns=columns,
                          has_more=len(rows) >= cfg.batch_size)


class SQLServerSinkWriter(SinkWriter):
    def connect(self) -> Any:
        import pymssql
        cfg = self.config
        return pymssql.connect(server=cfg.tgt_host, port=str(cfg.tgt_port or 1433),
                               user=cfg.tgt_username, password=cfg.tgt_password,
                               database=cfg.tgt_db_name, timeout=15,
                               login_timeout=15, as_dict=False)

    def _table_ref(self, table: str = None) -> str:
        cfg = self.config
        schema = cfg.tgt_schema or "dbo"
        tbl = table or cfg.target_table or ""
        return f"[{schema}].[{tbl}]"

    def _get_primary_keys(self, conn: Any, table: str) -> List[str]:
        cur = conn.cursor()
        schema = self.config.tgt_schema or "dbo"
        cur.execute(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_name=tc.constraint_name "
            "WHERE tc.table_schema=%s AND tc.table_name=%s "
            "AND tc.constraint_type='PRIMARY KEY'", (schema, table))
        return [r[0] for r in cur.fetchall()]

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        cfg = self.config
        schema = cfg.tgt_schema or "dbo"
        defs = []
        for c in columns:
            name, t = c.name, c.type.upper()
            ss_t = "NVARCHAR(4000)"
            if t in ("INT", "INTEGER"):
                ss_t = "INT"
            elif t == "BIGINT":
                ss_t = "BIGINT"
            elif t in ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
                ss_t = "FLOAT"
            elif t.startswith("DATETIME") or t in ("TIMESTAMP", "DATE"):
                ss_t = "DATETIME2" if t != "DATE" else "DATE"
            elif "TEXT" in t:
                ss_t = "NVARCHAR(MAX)"
            defs.append(f"[{name}] {ss_t}" + ("" if c.nullable else " NOT NULL"))
        pk = [c.name for c in columns if getattr(c, "is_primary", False)]
        if pk:
            defs.append("PRIMARY KEY (" + ", ".join(f"[{p}]" for p in pk) + ")")
        return f"CREATE TABLE [{schema}].[{table}] ({', '.join(defs)})"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table
        cur = conn.cursor()
        schema = cfg.tgt_schema or "dbo"
        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s",
                    (schema, table))
        exists = cur.fetchone()[0] > 0
        if cfg.save_mode == "overwrite" and exists:
            cur.execute(f"DROP TABLE {self._table_ref(table)}")
            exists = False
        if not exists:
            cur.execute(self._create_table_sql(table, columns))
        conn.commit()

    def write_batch(self, conn: Any, records: List[List[Any]],
                    columns: List[str]) -> int:
        cfg = self.config
        table = cfg.target_table
        cur = conn.cursor()
        pks = self._get_primary_keys(conn, table)
        ncols = [self.plugin.normalize_identifier(
            c, cfg.field_ide) for c in columns]
        if cfg.save_mode == "upsert" and pks:
            upd = [c for c in ncols if c.lower() not in
                   {p.lower() for p in pks}]
            set_sql = ", ".join(f"[{c}] = %s" for c in upd)
            on_sql = " AND ".join(f"T.[{p}] = S.[{p}]" for p in pks)
            src_cols = ", ".join(f"S.[{p}]" for p in pks) + ", " + \
                ", ".join(f"S.[{c}]" for c in upd)
            ins_cols = ", ".join(f"[{c}]" for c in ncols)
            ins_vals = ", ".join(f"S.[{c}]" for c in ncols)
            sql = (f"MERGE INTO {self._table_ref(table)} AS T USING "
                   f"(VALUES ({', '.join(['%s'] * len(ncols))})) AS S "
                   f"({', '.join(f'[{c}]' for c in ncols)}) "
                   f"ON {on_sql} "
                   f"WHEN MATCHED THEN UPDATE SET {set_sql} "
                   f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) "
                   f"VALUES ({ins_vals})")
            cur.executemany(sql, records)
        else:
            cols_sql = ", ".join(f"[{c}]" for c in ncols)
            binds = ", ".join(["%s"] * len(ncols))
            sql = (f"INSERT INTO {self._table_ref(table)} "
                   f"({cols_sql}) VALUES ({binds})")
            cur.executemany(sql, records)
        conn.commit()
        return len(records)


class SQLServerPlugin(BasePlugin):
    db_type = "sqlserver"
    default_ports = {"sync": 1433}

    def create_reader(self, config: SyncConfig) -> SourceReader:
        return SQLServerSourceReader(config, self)

    def create_writer(self, config: SyncConfig) -> SinkWriter:
        return SQLServerSinkWriter(config, self)

    def type_to_java(self, db_type: str, value: Any) -> Any:
        return to_java(db_type_to_java_type((db_type or "").upper()), value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return java_value

    def quote_identifier(self, name: str) -> str:
        return f"[{name}]"
