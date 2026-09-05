# -*- coding: utf-8 -*-
"""Oracle 同步插件（oracledb 瘦客户端，纯 Python 离线可用）。

- 源/目标均为 Oracle 时经 oracledb 直连（thin 模式，无需 Instant Client）；
- 模式（schema）即 Oracle user，大小写默认按大写处理（Oracle 惯例）；
- 增量同步：基于时间戳/数值增量列（incremental_column + incremental_value）。
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List

from .base import BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader, SyncConfig
from ..type_mapper import db_type_to_java_type, to_java

logger = logging.getLogger(__name__)


def _upper(name: str) -> str:
    return (name or "").upper()


class OracleSourceReader(SourceReader):
    def connect(self) -> Any:
        import oracledb
        cfg = self.config
        port = cfg.src_port or 1521
        dsn = f"{cfg.src_host}:{port}/{cfg.src_db_name or 'ORCL'}"
        return oracledb.connect(user=cfg.src_username,
                                password=cfg.src_password, dsn=dsn,
                                timeout=15)

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
        sql += " ORDER BY " + (cfg.incremental_column or columns[0] if columns else "")
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
        records = [[self.plugin.type_to_java(
            (cursor.description[i][1] or str), v)
            for i, v in enumerate(row)] for row in rows]
        return ReadResult(records=records, columns=columns,
                          has_more=len(rows) >= cfg.batch_size)


class OracleSinkWriter(SinkWriter):
    def connect(self) -> Any:
        import oracledb
        cfg = self.config
        port = cfg.tgt_port or 1521
        dsn = f"{cfg.tgt_host}:{port}/{cfg.tgt_db_name or 'ORCL'}"
        return oracledb.connect(user=cfg.tgt_username,
                                password=cfg.tgt_password, dsn=dsn,
                                timeout=15)

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
            ora_t = "VARCHAR2(4000)"
            if t in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
                ora_t = "NUMBER(19)"
            elif t in ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
                ora_t = "NUMBER"
            elif t.startswith("DATETIME") or t in ("TIMESTAMP", "DATE"):
                ora_t = "DATE" if t == "DATE" else "TIMESTAMP"
            elif t.startswith("VARCHAR"):
                ora_t = "VARCHAR2(4000)"
            elif "TEXT" in t or "BLOB" in t or "BYTEA" in t:
                ora_t = "CLOB"
            defs.append(f"{name} {ora_t}"
                        + ("" if c.nullable else " NOT NULL"))
        pk = [c.name for c in columns if getattr(c, "is_primary", False)]
        if pk:
            defs.append("PRIMARY KEY (" + ", ".join(_upper(p) for p in pk) + ")")
        return f'CREATE TABLE {schema}.{tbl} ({", ".join(defs)})'

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
        return to_java(db_type_to_java_type((db_type or "").upper()), value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return java_value

    def quote_identifier(self, name: str) -> str:
        return f'"{_upper(name)}"'
