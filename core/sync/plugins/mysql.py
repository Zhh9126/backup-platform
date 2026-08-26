# -*- coding: utf-8 -*-
"""MySQL / MariaDB 同步插件。"""
import logging
from typing import Any, List

from .base import BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader, SyncConfig
from ..type_mapper import JavaType, db_type_to_java_type, to_db, to_java

logger = logging.getLogger(__name__)


class MySQLSourceReader(SourceReader):
    def connect(self) -> Any:
        import pymysql
        cfg = self.config
        port = cfg.src_port or 3306
        return pymysql.connect(
            host=cfg.src_host,
            port=port,
            user=cfg.src_username,
            password=cfg.src_password,
            database=cfg.src_db_name or cfg.src_schema,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=8,
            read_timeout=30,
            write_timeout=30,
            cursorclass=pymysql.cursors.Cursor,
        )

    def list_tables(self) -> List[str]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                db = self.config.src_db_name or self.config.src_schema
                if db:
                    cur.execute("SHOW TABLES FROM `{}`".format(db))
                else:
                    cur.execute("SHOW TABLES")
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_columns(self, table: str) -> List[ColumnMeta]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                db = self.config.src_db_name or self.config.src_schema
                if not db:
                    raise ValueError("MySQL 需要指定源 database/schema")
                # 主键
                cur.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY'",
                    (db, table),
                )
                pk_set = {r[0] for r in cur.fetchall()}
                # 列信息
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                    "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                    (db, table),
                )
                cols = []
                for row in cur.fetchall():
                    cols.append(ColumnMeta(
                        name=row[0],
                        type=row[1].upper(),
                        nullable=row[2] == "YES",
                        default=row[3],
                        max_length=row[4],
                        numeric_precision=row[5],
                        numeric_scale=row[6],
                    ))
                for c in cols:
                    c.is_primary = c.name in pk_set
                return cols
        finally:
            conn.close()

    def _build_select_sql(self, table: str, columns: List[str]) -> str:
        db = self.config.src_db_name or self.config.src_schema
        table_ref = f"`{db}`.`{table}`" if db else f"`{table}`"
        if columns and columns != ["*"]:
            col_str = ", ".join(f"`{c}`" for c in columns if c and c != "*")
        else:
            col_str = "*"
        sql = f"SELECT {col_str} FROM {table_ref}"
        where_parts = []
        if self.config.source_where:
            where_parts.append(f"({self.config.source_where})")
        if self.config.sync_mode == "incremental" and self.config.incremental_column:
            where_parts.append(f"`{self.config.incremental_column}` > %s")
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if self.config.incremental_column:
            sql += f" ORDER BY `{self.config.incremental_column}`"
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
        columns = [d[0] for d in desc]
        records = [[self.plugin.type_to_java(db_type_to_java_type(str(desc[i][1])), v)
                    for i, v in enumerate(row)]
                   for row in rows]

        next_value = None
        if cfg.incremental_column and rows:
            idx = columns.index(cfg.incremental_column)
            next_value = rows[-1][idx]
            next_value = to_java(next_value)
        return ReadResult(records=records, columns=columns, has_more=len(rows) >= cfg.batch_size,
                          next_value=next_value)


class MySQLSinkWriter(SinkWriter):
    def connect(self) -> Any:
        import pymysql
        cfg = self.config
        port = cfg.tgt_port or 3306
        return pymysql.connect(
            host=cfg.tgt_host,
            port=port,
            user=cfg.tgt_username,
            password=cfg.tgt_password,
            database=cfg.tgt_db_name or cfg.tgt_schema,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=8,
            read_timeout=30,
            write_timeout=30,
        )

    def _table_ref(self, table: str = None) -> str:
        cfg = self.config
        db = cfg.tgt_db_name or cfg.tgt_schema
        t = table or cfg.target_table or cfg.source_table
        return f"`{db}`.`{t}`" if db else f"`{t}`"

    def _get_target_columns(self, conn: Any, table: str) -> List[str]:
        db = self.config.tgt_db_name or self.config.tgt_schema
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (db, table),
            )
            return [r[0] for r in cur.fetchall()]

    def _get_primary_keys(self, conn: Any, table: str) -> List[str]:
        db = self.config.tgt_db_name or self.config.tgt_schema
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY'",
                (db, table),
            )
            return [r[0] for r in cur.fetchall()]

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        db = self.config.tgt_db_name or self.config.tgt_schema
        table_ref = f"`{db}`.`{table}`" if db else f"`{table}`"
        lines = []
        pks = []
        for c in columns:
            ctype = self._map_to_mysql_type(c)
            null_str = "NULL" if c.nullable else "NOT NULL"
            default_str = ""
            if c.default is not None:
                default_str = f" DEFAULT {c.default}"
            lines.append(f"    `{c.name}` {ctype} {null_str}{default_str}")
            if getattr(c, "is_primary", False):
                pks.append(c.name)
        if pks:
            lines.append(f"    PRIMARY KEY ({', '.join(f'`{k}`' for k in pks)})")
        return f"CREATE TABLE IF NOT EXISTS {table_ref} (\n" + ",\n".join(lines) + "\n)"

    def _map_to_mysql_type(self, col: ColumnMeta) -> str:
        t = (col.type or "VARCHAR").upper()
        # 尽量保留原类型，若跨库差异大再做映射
        if t in ("VARCHAR", "CHAR"):
            return f"{t}({col.max_length or 255})"
        if t in ("DECIMAL", "NUMERIC"):
            return f"{t}({col.numeric_precision or 10},{col.numeric_scale or 0})"
        if "INT" in t:
            return t
        if t in ("TEXT", "LONGTEXT", "MEDIUMTEXT", "TINYTEXT", "BLOB", "LONGBLOB",
                 "MEDIUMBLOB", "TINYBLOB", "DATE", "DATETIME", "TIMESTAMP", "TIME",
                 "FLOAT", "DOUBLE", "REAL", "BIT", "JSON", "BINARY", "VARBINARY"):
            return t
        return "VARCHAR(255)"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        with conn.cursor() as cur:
            mode = cfg.save_mode or "upsert"
            db = cfg.tgt_db_name or cfg.tgt_schema
            cur.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (db, table),
            )
            exists = bool(cur.fetchone())
            if mode == "overwrite":
                if exists:
                    cur.execute(f"TRUNCATE TABLE {self._table_ref(table)}")
                    conn.commit()
                else:
                    cur.execute(self._create_table_sql(table, columns))
                    conn.commit()
            elif not exists:
                # create_if_not_exists / upsert / 默认：目标表不存在则自动建表
                cur.execute(self._create_table_sql(table, columns))
                conn.commit()

    def write_batch(self, conn: Any, records: List[List[Any]], columns: List[str]) -> int:
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        table_ref = self._table_ref(table)
        mapping = cfg.column_mapping or []

        # 字段 ide 转换
        target_cols = []
        for src_col in columns:
            mapped = next((m for m in mapping if m.get("source") == src_col), None)
            target_name = mapped.get("target", src_col) if mapped else src_col
            target_name = self.plugin.normalize_identifier(target_name, cfg.field_ide)
            target_cols.append(target_name)

        # 写入类型转换：按 mapping 中 target_type 或默认推断
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

        placeholders = ", ".join(["%s"] * len(target_cols))
        col_str = ", ".join(f"`{c}`" for c in target_cols)

        with conn.cursor() as cur:
            if cfg.save_mode == "upsert":
                pks = self._get_primary_keys(conn, table)
                if pks:
                    updates = ", ".join(
                        f"`{c}` = VALUES(`{c}`)" for c in target_cols if c not in pks
                    )
                    sql = (
                        f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders}) "
                        f"ON DUPLICATE KEY UPDATE {updates}"
                    )
                else:
                    sql = f"REPLACE INTO {table_ref} ({col_str}) VALUES ({placeholders})"
            else:
                sql = f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders})"

            values = [convert_row(r) for r in records]
            cur.executemany(sql, values)
            conn.commit()
            return cur.rowcount


    def apply_binlog_row(self, conn: Any, op: str, schema: str, table: str,
                         before: dict, after: dict) -> None:
        """把一条 Binlog 行事件应用到目标端（实时同步，参考 Flink CDC 语义）。

        op: insert | update | delete
        before/after: 列名 -> 值 字典（binlog_row_image=FULL 时为整行快照）
        """
        cfg = self.config
        t = cfg.target_table or table
        tgt_db = cfg.tgt_db_name or cfg.tgt_schema or schema
        table_ref = f"`{tgt_db}`.`{t}`"

        def tcol(name: str) -> str:
            return self.plugin.normalize_identifier(name, cfg.field_ide)

        pks = self._get_primary_keys(conn, t)

        def build_where(old: dict):
            parts, vals = [], []
            for pk in pks:
                if pk in old:
                    parts.append(f"`{tcol(pk)}` = %s")
                    vals.append(self._binlog_value(old[pk]))
            if not parts:
                for c in old:
                    parts.append(f"`{tcol(c)}` = %s")
                    vals.append(self._binlog_value(old[c]))
            return parts, vals

        with conn.cursor() as cur:
            if op == "insert":
                row = after or before or {}
                if not row:
                    return
                cols = list(row.keys())
                placeholders = ", ".join(["%s"] * len(cols))
                col_str = ", ".join(f"`{tcol(c)}`" for c in cols)
                vals = tuple(self._binlog_value(row[c]) for c in cols)
                if pks:
                    updates = ", ".join(
                        f"`{tcol(c)}` = VALUES(`{tcol(c)}`)"
                        for c in cols if c not in pks
                    )
                    if updates:
                        sql = (f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders}) "
                               f"ON DUPLICATE KEY UPDATE {updates}")
                    else:
                        sql = f"INSERT IGNORE INTO {table_ref} ({col_str}) VALUES ({placeholders})"
                else:
                    sql = f"INSERT INTO {table_ref} ({col_str}) VALUES ({placeholders})"
                cur.execute(sql, vals)
                conn.commit()
            elif op == "update":
                if not after:
                    return
                old = before or after
                cols = list(after.keys())
                set_str = ", ".join(f"`{tcol(c)}` = %s" for c in cols)
                vals = [self._binlog_value(after[c]) for c in cols]
                where_parts, where_vals = build_where(old)
                sql = f"UPDATE {table_ref} SET {set_str} WHERE {' AND '.join(where_parts)}"
                cur.execute(sql, vals + where_vals)
                conn.commit()
            elif op == "delete":
                if not before:
                    return
                where_parts, where_vals = build_where(before)
                sql = f"DELETE FROM {table_ref} WHERE {' AND '.join(where_parts)}"
                cur.execute(sql, where_vals)
                conn.commit()

    @staticmethod
    def _binlog_value(v: Any) -> Any:
        """binlog 值转为 pymysql 可写值。"""
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        return v


class MySQLPlugin(BasePlugin):
    db_type = "mysql"
    default_ports = {"mysql": 3306, "mariadb": 3306}

    def create_reader(self, config: SyncConfig) -> SourceReader:
        return MySQLSourceReader(config, self)

    def create_writer(self, config: SyncConfig) -> SinkWriter:
        return MySQLSinkWriter(config, self)

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
            return bytes(value)
        if jt in (JavaType.DATE, JavaType.TIME, JavaType.DATETIME):
            return str(value)
        return str(value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return to_db(java_value, target_type)

    def quote_identifier(self, name: str) -> str:
        return f"`{name}`"

    def disable_constraints(self, conn: Any) -> None:
        """禁用外键检查（参考 pg2mysql SetDefaultConnectionConfigs）。"""
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            conn.commit()
            logger.info("MySQL FOREIGN_KEY_CHECKS = 0")
        except Exception as e:
            logger.warning("禁用外键检查失败：%s", e)

    def enable_constraints(self, conn: Any) -> None:
        """恢复外键检查。"""
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
            logger.info("MySQL FOREIGN_KEY_CHECKS = 1")
        except Exception as e:
            logger.warning("恢复外键检查失败：%s", e)
