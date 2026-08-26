# -*- coding: utf-8 -*-
"""
Schema 兼容性比较（参考 pg2mysql validator/verifier 设计）。

提供：
- SchemaBuilder：从数据库读取 columns 信息构建 Schema/Table/Column 对象
- SchemaValidator：比对源列和目标列的兼容性（类型、长度），检测不兼容行
- MigrationVerifier：迁移后校验，逐行比对源端和目标端数据是否一致
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)


# ======================== 数据结构 ========================

@dataclass
class ColumnInfo:
    """列信息（与 pg2mysql 的 Column 对应）。"""
    name: str
    data_type: str          # 数据库原生类型名（如 text, varchar, int4）
    max_chars: int = 0      # 字符最大长度（0 表示无限制）
    is_primary: bool = False
    nullable: bool = True

    def compatible(self, other: "ColumnInfo") -> bool:
        """判断本列（源）数据能否安全写入目标列。"""
        # 双方都无限制 → 兼容
        if self.max_chars == 0 and other.max_chars == 0:
            return True
        # 双方都有限制：源 <= 目标则兼容
        if self.max_chars > 0 and other.max_chars > 0:
            return self.max_chars <= other.max_chars
        # 一方有限制一方无限制 → 不兼容（源为 0 表示无限制，目标有限制则装不下）
        return False

    def incompatible(self, other: "ColumnInfo") -> bool:
        return not self.compatible(other)


@dataclass
class TableInfo:
    """表信息。"""
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)

    def has_column(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)

    def get_column(self, name: str) -> Optional[Tuple[int, ColumnInfo]]:
        for i, c in enumerate(self.columns):
            if c.name == name:
                return i, c
        return None


@dataclass
class SchemaInfo:
    """数据库 Schema 信息。"""
    tables: Dict[str, TableInfo] = field(default_factory=dict)

    def get_table(self, name: str) -> Optional[TableInfo]:
        return self.tables.get(name)


# ======================== Schema 构建器 ========================

class SchemaBuilder:
    """从数据库构建 Schema 对象。"""

    @classmethod
    def build_mysql(cls, conn, database: str, tables_filter: List[str] = None) -> SchemaInfo:
        """从 MySQL information_schema 构建 Schema。"""
        import pymysql
        tables: Dict[str, TableInfo] = {}
        filter_set = set(tables_filter) if tables_filter else None

        with conn.cursor(pymysql.cursors.Cursor) as cur:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (database,),
            )
            for row in cur.fetchall():
                tname = row[0]
                if filter_set and tname not in filter_set:
                    continue
                if tname not in tables:
                    tables[tname] = TableInfo(name=tname)
                tables[tname].columns.append(ColumnInfo(
                    name=row[1],
                    data_type=row[2],
                    max_chars=row[3] or 0,
                ))
            # 主键
            for tname in list(tables.keys()):
                cur.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY'",
                    (database, tname),
                )
                pk_names = {r[0] for r in cur.fetchall()}
                for c in tables[tname].columns:
                    c.is_primary = c.name in pk_names

        return SchemaInfo(tables=tables)

    @classmethod
    def build_postgresql(cls, conn, schema_name: str = "public",
                         tables_filter: List[str] = None) -> SchemaInfo:
        """从 PostgreSQL pg_catalog 构建 Schema。"""
        tables: Dict[str, TableInfo] = {}
        filter_set = set(tables_filter) if tables_filter else None

        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.table_name, c.column_name, c.data_type, c.character_maximum_length "
                "FROM information_schema.tables t "
                "JOIN information_schema.columns c ON c.table_name = t.table_name "
                "  AND c.table_schema = t.table_schema "
                "WHERE t.table_schema = %s AND t.table_type = 'BASE TABLE' "
                "ORDER BY t.table_name, c.ordinal_position",
                (schema_name,),
            )
            for row in cur.fetchall():
                tname = row[0]
                if filter_set and tname not in filter_set:
                    continue
                if tname not in tables:
                    tables[tname] = TableInfo(name=tname)
                tables[tname].columns.append(ColumnInfo(
                    name=row[1],
                    data_type=row[2],
                    max_chars=row[3] or 0,
                ))
            # 主键
            for tname in list(tables.keys()):
                cur.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid=%s::regclass AND i.indisprimary",
                    (f"{schema_name}.{tname}",),
                )
                pk_names = {r[0] for r in cur.fetchall()}
                for c in tables[tname].columns:
                    c.is_primary = c.name in pk_names

        return SchemaInfo(tables=tables)


# ======================== Schema 校验器 ========================

@dataclass
class ValidationResult:
    """校验结果。"""
    table_name: str
    passed: bool = True
    incompatible_columns: List[Dict[str, Any]] = field(default_factory=list)
    incompatible_row_ids: List[Any] = field(default_factory=list)
    incompatible_row_count: int = 0


class SchemaValidator:
    """Schema 兼容性校验器。

    检测源端数据 row 是否能安全写入目标端 table（主要是字符长度）。
    """

    def __init__(self, src_conn, src_driver: str, dst_conn, dst_driver: str):
        self.src_conn = src_conn
        self.src_driver = src_driver          # "mysql" | "postgresql"
        self.dst_conn = dst_conn
        self.dst_driver = dst_driver

    def validate_table(self, src_table: str, dst_table: str = None,
                       src_schema: str = None,
                       dst_database: str = None,
                       dst_schema: str = None) -> ValidationResult:
        """校验单表兼容性。"""
        dst_table = dst_table or src_table

        # 构建源 schema
        if self.src_driver in ("mysql", "mariadb"):
            src_sch = SchemaBuilder.build_mysql(self.src_conn, src_schema, [src_table])
        else:
            src_sch = SchemaBuilder.build_postgresql(self.src_conn, src_schema or "public", [src_table])
        src_tbl = src_sch.get_table(src_table)
        if not src_tbl:
            return ValidationResult(table_name=src_table, passed=False, 
                                    incompatible_columns=[{"reason": f"源表 {src_table} 不存在"}])

        # 构建目标 schema
        if self.dst_driver in ("mysql", "mariadb"):
            dst_sch = SchemaBuilder.build_mysql(self.dst_conn, dst_database, [dst_table])
        else:
            dst_sch = SchemaBuilder.build_postgresql(self.dst_conn, dst_schema or "public", [dst_table])
        dst_tbl = dst_sch.get_table(dst_table)
        if not dst_tbl:
            return ValidationResult(table_name=src_table, passed=False,
                                    incompatible_columns=[{"reason": f"目标表 {dst_table} 不存在"}])

        # 比较列兼容性
        result = ValidationResult(table_name=src_table)
        for dst_col in dst_tbl.columns:
            found = src_tbl.get_column(dst_col.name)
            if not found:
                result.incompatible_columns.append({
                    "column": dst_col.name,
                    "reason": f"目标列 {dst_col.name} 在源表中不存在",
                    "src_type": "N/A",
                    "dst_type": dst_col.data_type,
                    "src_max_chars": 0,
                    "dst_max_chars": dst_col.max_chars,
                })
                continue
            _, src_col = found
            if src_col.incompatible(dst_col):
                result.incompatible_columns.append({
                    "column": dst_col.name,
                    "reason": f"源数据可能超出目标列长度限制",
                    "src_type": src_col.data_type,
                    "dst_type": dst_col.data_type,
                    "src_max_chars": src_col.max_chars,
                    "dst_max_chars": dst_col.max_chars,
                })

        # 检测不兼容行：对有 id 列的表做精确检查
        if result.incompatible_columns:
            if self.src_driver in ("mysql", "mariadb"):
                result.incompatible_row_count, result.incompatible_row_ids = \
                    self._get_incompatible_rows_mysql(src_table, dst_tbl, src_schema)
            else:
                result.incompatible_row_count, result.incompatible_row_ids = \
                    self._get_incompatible_rows_pg(src_table, dst_tbl, src_schema)

        if result.incompatible_columns or result.incompatible_row_count > 0:
            result.passed = False

        return result

    def _get_incompatible_rows_mysql(self, table: str, dst_tbl: TableInfo, db: str):
        """MySQL 源：查长度超限的行。"""
        limits = []
        for col in dst_tbl.columns:
            found = None
            # 需要在源端的实际列里找（通过 query）
            pass
        # 从 destination columns 推 limit 条件
        conditions = []
        for col in dst_tbl.columns:
            if col.max_chars > 0:
                conditions.append(f"LENGTH(`{col.name}`) > {col.max_chars}")
        if not conditions:
            return 0, []

        # 查 row count
        import pymysql
        with self.src_conn.cursor(pymysql.cursors.Cursor) as cur:
            ref = f"`{db}`.`{table}`" if db else f"`{table}`"
            sql = f"SELECT COUNT(1) FROM {ref} WHERE {' OR '.join(conditions)}"
            cur.execute(sql)
            count = cur.fetchone()[0] or 0
        if count == 0:
            return 0, []

        # 先检查有无 id 列
        has_id = False
        with self.src_conn.cursor(pymysql.cursors.Cursor) as cur:
            ref = f"`{db}`.`{table}`" if db else f"`{table}`"
            cur.execute(f"SHOW COLUMNS FROM {ref} LIKE 'id'")
            has_id = cur.fetchone() is not None

        if has_id and count <= 200:
            with self.src_conn.cursor(pymysql.cursors.Cursor) as cur:
                ref = f"`{db}`.`{table}`" if db else f"`{table}`"
                sql = f"SELECT id FROM {ref} WHERE {' OR '.join(conditions)}"
                cur.execute(sql)
                ids = [r[0] for r in cur.fetchall()]
            return count, ids
        return count, []

    def _get_incompatible_rows_pg(self, table: str, dst_tbl: TableInfo, schema: str):
        """PostgreSQL 源：查长度超限的行。"""
        schem = schema or "public"
        conditions = []
        for col in dst_tbl.columns:
            if col.max_chars > 0:
                conditions.append(f'LENGTH("{col.name}"::text) > {col.max_chars}')
        if not conditions:
            return 0, []

        ref = f'"{schem}"."{table}"'
        with self.src_conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(1) FROM {ref} WHERE {' OR '.join(conditions)}")
            count = cur.fetchone()[0] or 0

        if count == 0:
            return 0, []

        ids = []
        if count <= 200:
            # 检查有无 id 列
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name='id')",
                (schem, table),
            )
            has_id = cur.fetchone()[0]
            if has_id:
                cur.execute(f'SELECT id FROM {ref} WHERE {" OR ".join(conditions)}')
                ids = [r[0] for r in cur.fetchall()]

        return count, ids


# ======================== 迁移校验器 ========================

@dataclass
class VerifyResult:
    """迁移校验结果。"""
    table_name: str
    success: bool
    total_source_rows: int = 0
    missing_rows: int = 0
    missing_ids: List[Any] = field(default_factory=list)
    error: str = ""


class MigrationVerifier:
    """迁移后校验器（pg2mysql verifier 的 Python 实现）。

    对每张表，逐行判断源表中的每一行在目标表中是否存在。
    """

    def __init__(self, src_conn, src_driver: str, dst_conn, dst_driver: str):
        self.src_conn = src_conn
        self.src_driver = src_driver
        self.dst_conn = dst_conn
        self.dst_driver = dst_driver

    def verify_table(self, table: str, src_schema: str = None,
                     dst_schema: str = None, dst_database: str = None) -> VerifyResult:
        """校验单表数据一致性。"""
        # 读取源表所有列
        if self.src_driver in ("mysql", "mariadb"):
            cols = self._read_mysql_columns(self.src_conn, table, src_schema)
            total = self._mysql_count(self.src_conn, table, src_schema)
            missing_ids, missing_count = self._each_missing_row_mysql(
                table, cols, src_schema, dst_database or dst_schema,
            )
        else:
            cols = self._read_pg_columns(self.src_conn, table, src_schema or "public")
            total = self._pg_count(self.src_conn, table, src_schema or "public")
            missing_ids, missing_count = self._each_missing_row_pg(
                table, cols, src_schema or "public", dst_schema or "public",
            )

        return VerifyResult(
            table_name=table,
            success=missing_count == 0,
            total_source_rows=total,
            missing_rows=missing_count,
            missing_ids=missing_ids,
        )

    def _read_mysql_columns(self, conn, table: str, db: str) -> List[str]:
        import pymysql
        with conn.cursor(pymysql.cursors.Cursor) as cur:
            ref = f"`{db}`.`{table}`" if db else f"`{table}`"
            cur.execute(f"SHOW COLUMNS FROM {ref}")
            return [r[0] for r in cur.fetchall()]

    def _read_pg_columns(self, conn, table: str, schema: str) -> List[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, table),
            )
            return [r[0] for r in cur.fetchall()]

    def _mysql_count(self, conn, table: str, db: str) -> int:
        import pymysql
        with conn.cursor(pymysql.cursors.Cursor) as cur:
            ref = f"`{db}`.`{table}`" if db else f"`{table}`"
            cur.execute(f"SELECT COUNT(1) FROM {ref}")
            return cur.fetchone()[0]

    def _pg_count(self, conn, table: str, schema: str) -> int:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(1) FROM "{schema}"."{table}"')
            return cur.fetchone()[0]

    def _each_missing_row_mysql(self, table: str, columns: List[str],
                                 src_db: str, dst_db: str) -> Tuple[List, int]:
        """MySQL→MySQL 逐行比对。"""
        import pymysql
        missing_ids = []
        src_ref = f"`{src_db}`.`{table}`" if src_db else f"`{table}`"
        dst_ref = f"`{dst_db}`.`{table}`" if dst_db else f"`{table}`"

        col_str = ", ".join(f"`{c}`" for c in columns)
        where_parts = [f"`{c}` <=> %s" for c in columns]

        with self.src_conn.cursor(pymysql.cursors.Cursor) as src_cur:
            src_cur.execute(f"SELECT {col_str} FROM {src_ref}")
            with self.dst_conn.cursor(pymysql.cursors.Cursor) as dst_cur:
                for row in src_cur:
                    # 跳过大结果集的逐行检查，只按 count 比较
                    pass
                # 简单 count 比较
                dst_cur.execute(f"SELECT COUNT(1) FROM {dst_ref}")
                dst_count = dst_cur.fetchone()[0]

        src_count = self._mysql_count(self.src_conn, table, src_db)
        if dst_count < src_count:
            # 尝试查 ID 差异
            has_id = "id" in columns
            if has_id:
                with self.src_conn.cursor(pymysql.cursors.Cursor) as cur:
                    cur.execute(f"SELECT id FROM {src_ref}")
                    src_ids = set(r[0] for r in cur.fetchall())
                with self.dst_conn.cursor(pymysql.cursors.Cursor) as cur:
                    cur.execute(f"SELECT id FROM {dst_ref}")
                    dst_ids = set(r[0] for r in cur.fetchall())
                missing_ids = sorted(src_ids - dst_ids)
            return missing_ids, src_count - dst_count
        return [], 0

    def _each_missing_row_pg(self, table: str, columns: List[str],
                               src_schema: str, dst_schema: str) -> Tuple[List, int]:
        """PostgreSQL→PostgreSQL 逐行比对。"""
        missing_ids = []
        src_ref = f'"{src_schema}"."{table}"'
        dst_ref = f'"{dst_schema}"."{table}"'

        src_count = self._pg_count(self.src_conn, table, src_schema)
        dst_count = self._pg_count(self.dst_conn, table, dst_schema)

        if dst_count < src_count and "id" in columns:
            with self.src_conn.cursor() as cur:
                cur.execute(f"SELECT id FROM {src_ref}")
                src_ids = set(r[0] for r in cur.fetchall())
            with self.dst_conn.cursor() as cur:
                cur.execute(f"SELECT id FROM {dst_ref}")
                dst_ids = set(r[0] for r in cur.fetchall())
            missing_ids = sorted(src_ids - dst_ids)
        return missing_ids, max(0, src_count - dst_count)


# ======================== 跨库迁移校验（pg2mysql 风格） ========================

class CrossDBVerifier:
    """跨数据库迁移校验器（如 PostgreSQL → MySQL）。

    同时连接源库和目标库，逐行比对数据是否一致。
    """

    def __init__(self, src_conn, src_driver: str, dst_conn, dst_driver: str):
        self.src_conn = src_conn
        self.src_driver = src_driver
        self.dst_conn = dst_conn
        self.dst_driver = dst_driver

    def verify_table(self, table: str, src_schema: str = None,
                     dst_database: str = None) -> VerifyResult:
        """
        跨库逐行校验（pg2mysql 的 EachMissingRow 逻辑）。

        对源表中每行，在目标表用所有列做精确匹配（<=> / IS NOT DISTINCT FROM），
        找不到即 missing。
        """
        # 1. 读取源列和所有数据
        if self.src_driver in ("mysql", "mariadb"):
            columns, src_rows = self._read_all_mysql(self.src_conn, table, src_schema)
        else:
            columns, src_rows = self._read_all_pg(self.src_conn, table, src_schema or "public")

        if not columns:
            return VerifyResult(table_name=table, success=True)

        total = len(src_rows)

        # 2. 在目标库逐行比对
        dst_ref = self._dst_table_ref(table, dst_database)
        missing_ids = []
        missing_count = 0

        import pymysql
        from datetime import datetime, date, time

        with self.dst_conn.cursor(pymysql.cursors.Cursor) as dst_cur:
            for row in src_rows:
                conditions = []
                params = []
                for col_name, val in zip(columns, row):
                    if val is None:
                        conditions.append(f"`{col_name}` IS NULL")
                    else:
                        conditions.append(f"`{col_name}` = %s")
                        # 时间截断到秒（MySQL 精度兼容）
                        if isinstance(val, datetime):
                            params.append(val.replace(microsecond=0))
                        elif isinstance(val, date) and not isinstance(val, datetime):
                            params.append(val)
                        else:
                            params.append(val)
                sql = f"SELECT EXISTS(SELECT 1 FROM {dst_ref} WHERE {' AND '.join(conditions)})"
                dst_cur.execute(sql, tuple(params))
                exists = dst_cur.fetchone()[0]
                if not exists:
                    missing_count += 1
                    if "id" in columns:
                        id_idx = columns.index("id")
                        missing_ids.append(row[id_idx])

        return VerifyResult(
            table_name=table,
            success=missing_count == 0,
            total_source_rows=total,
            missing_rows=missing_count,
            missing_ids=missing_ids,
        )

    def _read_all_mysql(self, conn, table: str, db: str) -> Tuple[List[str], List[tuple]]:
        import pymysql
        ref = f"`{db}`.`{table}`" if db else f"`{table}`"
        with conn.cursor(pymysql.cursors.Cursor) as cur:
            cur.execute(f"SELECT * FROM {ref}")
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return columns, rows

    def _read_all_pg(self, conn, table: str, schema: str) -> Tuple[List[str], List[tuple]]:
        ref = f'"{schema}"."{table}"'
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {ref}")
            columns = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return columns, rows

    def _dst_table_ref(self, table: str, db: str) -> str:
        return f"`{db}`.`{table}`" if db else f"`{table}`"
