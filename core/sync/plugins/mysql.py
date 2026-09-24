# -*- coding: utf-8 -*-
"""MySQL / MariaDB 同步插件。"""
import logging
import re
from typing import Any, List

from .base import (BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader,
                   SyncConfig, matrix_suggest)
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
                # COLUMN_TYPE 含完整修饰（'int unsigned'/'decimal(20,4)'），
                # 仅 DATA_TYPE 会丢 unsigned 导致升位映射失效
                cur.execute(
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                    "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, EXTRA "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                    (db, table),
                )
                cols = []
                for row in cur.fetchall():
                    extra = (row[7] or "").lower()
                    col_type = (row[1] or "").upper()
                    cols.append(ColumnMeta(
                        name=row[0],
                        type=col_type,
                        nullable=row[2] == "YES",
                        default=row[3],
                        max_length=row[4],
                        numeric_precision=row[5],
                        numeric_scale=row[6],
                        # 自增列在 INFORMATION_SCHEMA 里没有默认值（COLUMN_DEFAULT 为 NULL），
                        # 只有 EXTRA 带 auto_increment。不读 EXTRA 会导致目标表丢自增，
                        # 后续插入数据主键冲突。
                        auto_increment=("auto_increment" in extra),
                        # 'int unsigned'/'tinyint unsigned'：目标端无符号整型升位依赖此标记
                        unsigned="UNSIGNED" in col_type,
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
        records = [[self.plugin.type_to_java(str(desc[i][1]), v)
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
        auto_cols = []
        for c in columns:
            ctype = self._map_to_mysql_type(c)
            null_str = "NULL" if c.nullable else "NOT NULL"
            default_str, auto_inc = self._mysql_default(c.default, ctype)
            # 源端显式自增标记（MySQL/MariaDB 的 EXTRA、PG/金仓序列默认值）优先
            if getattr(c, "auto_increment", False) and self._is_int_type(ctype):
                auto_inc = True
            if auto_inc:
                lines.append(f"    `{c.name}` {ctype} {null_str} AUTO_INCREMENT")
                auto_cols.append(c.name)
            else:
                lines.append(f"    `{c.name}` {ctype} {null_str}{default_str}")
            if getattr(c, "is_primary", False):
                pks.append(c.name)
        if pks:
            lines.append(f"    PRIMARY KEY ({', '.join(f'`{k}`' for k in pks)})")
        # MySQL 要求 AUTO_INCREMENT 列必须是键：源端自增列若非主键，必须补唯一键，
        # 否则整张表建不起来（Incorrect table definition; there can be only one
        # auto column and it must be defined as a key）。
        for name in auto_cols:
            if name not in pks:
                lines.append(f"    UNIQUE KEY `uk_{name}` (`{name}`)")
        return f"CREATE TABLE IF NOT EXISTS {table_ref} (\n" + ",\n".join(lines) + "\n)"

    # 源端原生函数默认值 -> MySQL 等价写法（跨库型迁移时源端可能是 PG/金仓/达梦）
    _FUNC_DEFAULTS = {
        "CURRENT_TIMESTAMP": "CURRENT_TIMESTAMP",
        "CURRENT_TIMESTAMP()": "CURRENT_TIMESTAMP",
        "NOW()": "CURRENT_TIMESTAMP",
        "LOCALTIMESTAMP": "CURRENT_TIMESTAMP",
        "CURRENT_DATE": "CURRENT_DATE",
        "CURRENT_DATE()": "CURRENT_DATE",
        "CURRENT_TIME": "CURRENT_TIME",
        "CURRENT_TIME()": "CURRENT_TIME",
    }
    _INT_BASES = ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT")

    # 异源多词/异名类型 -> MySQL 基类型（源端可能是 PG/达梦/金仓/Oracle）
    _TYPE_ALIAS = {
        "CHARACTER VARYING": "VARCHAR",
        "CHAR VARYING": "VARCHAR",
        "NATIONAL CHARACTER VARYING": "VARCHAR",
        "NATIONAL CHARACTER": "CHAR",
        "CHARACTER": "CHAR",
        "NCHAR": "CHAR",
        "NVARCHAR": "VARCHAR",
        "DOUBLE PRECISION": "DOUBLE",
        "TIMESTAMP WITHOUT TIME ZONE": "DATETIME",
        "TIMESTAMP WITH TIME ZONE": "DATETIME",
        "TIMESTAMPTZ": "DATETIME",
        "TIME WITHOUT TIME ZONE": "TIME",
        "TIME WITH TIME ZONE": "TIME",
        "DEC": "DECIMAL",
        "NUMERIC": "DECIMAL",
        "INTEGER": "INT",
        "INT2": "SMALLINT",
        "INT4": "INT",
        "INT8": "BIGINT",
        "FLOAT4": "FLOAT",
        "FLOAT8": "DOUBLE",
        "SERIAL": "INT",
        "BIGSERIAL": "BIGINT",
        "SMALLSERIAL": "SMALLINT",
        "BOOLEAN": "TINYINT",
        "BYTEA": "BLOB",
        "UUID": "CHAR",
        "INTERVAL": "VARCHAR",
        "MONEY": "DECIMAL",
        "CLOB": "LONGTEXT",
    }

    @classmethod
    def _is_int_type(cls, ctype: str) -> bool:
        return (ctype or "").upper().split("(")[0].strip() in cls._INT_BASES

    @classmethod
    def _mysql_default(cls, value: Any, ctype: str):
        """源端列默认值 -> (MySQL DEFAULT 子句, 是否 AUTO_INCREMENT)。

        跨库型迁移时源端默认值不一定是 MySQL 字面量：
        - PG/金仓 serial(identity) 列的默认值是 nextval('seq'::regclass)，直接拼进
          目标 DDL 会报 1064 语法错误 → 整表建表失败（且读 0 写 0，看状态像"成功"）；
          这类列应转为 AUTO_INCREMENT，由 MySQL 自增接管，不依赖源端序列名。
        - PG 的函数默认值（now()/CURRENT_TIMESTAMP）需要转成 MySQL 写法。
        - PG 的字面量默认值带类型转换（'x'::character varying、0::numeric）需剥掉 ::type。
        - 其余无法换算的原生表达式一律**不生成 DEFAULT**：宁可不带默认值，
          也不能让整张表建不起来（缺默认值只影响新插入行，丢表影响全量数据）。
        """
        if value is None:
            return "", False
        s = str(value).strip()
        if not s:
            return "", False
        up = s.upper()

        # ① 序列自增 / identity -> AUTO_INCREMENT
        if re.match(r"^(NEXT VALUE FOR\s+|NEXTVAL\s*\()", up) or "::REGCLASS" in up:
            return "", cls._is_int_type(ctype)
        # ② 原生函数默认值
        if up in cls._FUNC_DEFAULTS:
            expr = cls._FUNC_DEFAULTS[up]
            # 时间函数默认值必须与列精度一致：DATETIME(6) 配 CURRENT_TIMESTAMP 会报
            # 1067 Invalid default value（MySQL 8.0 实测命中）→ 需写成 CURRENT_TIMESTAMP(6)。
            m_fsp = re.search(r"\((\d+)\)", ctype or "")
            if m_fsp and expr in ("CURRENT_TIMESTAMP", "CURRENT_TIME"):
                expr = "%s(%s)" % (expr, m_fsp.group(1))
            return " DEFAULT " + expr, False
        # ③ 带类型转换的字面量
        m = re.match(r"^'(.*)'::[\w\s\[\].]+$", s, re.S)
        if m:
            return " DEFAULT '%s'" % m.group(1).replace("'", "''"), False
        m = re.match(r"^([-\d.]+)::[\w\s\[\].]+$", s)
        if m:
            return " DEFAULT " + m.group(1), False
        # ④ 其它原生表达式（含 :: 或函数调用）——不生成 DEFAULT，避免建表失败
        if "::" in s or re.search(r"[A-Za-z_]\w*\s*\(", s):
            return "", False
        # ⑤ 普通字面量
        if up == "NULL":
            return " DEFAULT NULL", False
        if re.match(r"^[+-]?\d+(\.\d+)?$", s):
            return " DEFAULT " + s, False
        return " DEFAULT '%s'" % s.replace("'", "''"), False

    def _map_to_mysql_type(self, col: ColumnMeta) -> str:
        t = (col.type or "VARCHAR").upper().strip()
        # COLUMN_TYPE 携带完整修饰（'decimal(12,3)' / 'varchar(100)' / 'int unsigned'），
        # 必须先拆出基类型再比较：否则带修饰的类型名与裸名精确比较不相等，
        # 会全部落到末尾兜底 VARCHAR(255)，导致长度/精度丢失、数值列退化为字符串列
        # （同构 MySQL/MariaDB 迁移同样必须保真）。参考 postgresql.py 的同款处理。
        head = t.split("(")[0].strip()
        # 去掉 unsigned/signed/zerofill 修饰后再取首词：'BIGINT(20) UNSIGNED' → bigint；
        # 否则 head.split()[0] 会取到 'unsigned' 而落到兜底 VARCHAR(255)（实测命中）
        head = re.sub(r"\b(unsigned|signed|zerofill)\b", " ", head).strip()
        base = head.split()[0] if head else t
        # 异源类型名归一化，必须先于下面的标准类型分支：PG 的 'CHARACTER VARYING'
        # 取首词是 'CHARACTER'，匹配不到任何分支而落到兜底 TEXT —— 列长度
        # (varchar(50)/(100)) 与数值精度全部丢失，且 TEXT 列不允许默认值、不能建前缀索引。
        norm = self._TYPE_ALIAS.get(" ".join(head.split()))
        t_out = t
        if norm:
            head = base = t_out = norm
        # 偏门类型优先：ENUM/SET/JSON/YEAR/SPATIAL/UUID/VECTOR 等需要保留精确写法
        if base in ("ENUM", "SET"):
            # ENUM/SET 关键字大写，括号内枚举值原大小写（MySQL 关键字大小写
            # 不敏感但官方推荐大写；枚举值大小写敏感必须原样保留）
            src = (col.type or "").strip()
            m = re.match(r"^\s*\w+\s*(\(.*\))\s*$", src, re.DOTALL)
            return f"{base}{m.group(1)}" if m else "VARCHAR(128)"
        if base == "JSON":
            return "JSON"
        if base == "YEAR":
            # MySQL YEAR[(2|4)]：原写法需保留 (4)/(2)，不能丢精度修饰
            return t if "(" in t else "YEAR"
        if base in ("GEOMETRY", "POINT", "LINESTRING", "POLYGON",
                    "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON",
                    "GEOMETRYCOLLECTION", "GEOMCOLLECTION"):
            return base
        if base == "VECTOR":
            # MySQL 8.0.28+ VECTOR 类型，原写法形如 'VECTOR(384)'
            return t if "(" in t else "VECTOR"
        # 标准类型
        if base in ("VARCHAR", "CHAR"):
            return f"{base}({col.max_length or 255})"
        if base in ("DECIMAL", "NUMERIC"):
            return f"{base}({col.numeric_precision or 10},{col.numeric_scale or 0})"
        if base in ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT"):
            return t_out
        if base in ("TEXT", "LONGTEXT", "MEDIUMTEXT", "TINYTEXT", "BLOB", "LONGBLOB",
                    "MEDIUMBLOB", "TINYBLOB", "DATE", "DATETIME", "TIMESTAMP", "TIME",
                    "FLOAT", "DOUBLE", "REAL", "BIT", "JSON", "BINARY", "VARBINARY"):
            # MySQL 没有带时区的时间戳：'TIMESTAMP WITH TIME ZONE'/'TIMESTAMPTZ'
            # 原样输出会建表报错。按 UTC 归一为 DATETIME(6)——保留微秒精度且
            # 无 TIMESTAMP 的 2038 上限（与 type_matrix 的映射建议保持一致）
            if base == "TIMESTAMP" and "WITH" in t:
                return "DATETIME(6)"
            return t_out
        # 跨源兜底：源端列类型来自别的库（JSONB/XMLTYPE/HIERARCHYID/INET/ROWID/
        # TSVECTOR...）时用统一类型矩阵翻译，避免无脑落 VARCHAR(255) 丢语义
        sug = matrix_suggest(getattr(self, "config", None), "mysql", col.type)
        if sug:
            if sug in ("VARCHAR", "CHAR", "NVARCHAR"):
                return f"VARCHAR({col.max_length or 255})"
            if sug in ("DECIMAL", "NUMERIC"):
                return f"DECIMAL({col.numeric_precision or 10},{col.numeric_scale or 0})"
            return sug
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
                    try:
                        cur.execute(f"TRUNCATE TABLE {self._table_ref(table)}")
                    except Exception:
                        # MySQL 下被外键引用的表 TRUNCATE 会失败(1701)，
                        # 即使 FOREIGN_KEY_CHECKS=0；降级为 DELETE 清空
                        cur.execute(f"DELETE FROM {self._table_ref(table)}")
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
                # bytes 原样透传（BLOB/VARBINARY 等二进制列）：默认 target_type
                # 为 STRING 时 to_db 会 str() 成 "b'...'" 字面量损坏二进制数据
                # （与 postgresql.py 的同款处理保持一致）
                if isinstance(row[i], (bytes, bytearray)):
                    out.append(bytes(row[i]))
                    continue
                java_val = self.plugin.type_to_java(target_type, row[i])
                out.append(to_db(java_val, target_type))
            return tuple(out)

        placeholders = ", ".join(["%s"] * len(target_cols))
        col_str = ", ".join(f"`{c}`" for c in target_cols)

        with conn.cursor() as cur:
            # save_mode 为空时与 prepare_table 保持同一默认（upsert），
            # 避免"默认按 upsert 建表/不清空、写入却走纯 INSERT"的
            # 语义错位导致主键冲突全量失败
            if (cfg.save_mode or "upsert") == "upsert":
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
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            # 值已是 str：MySQL 协议中 TEXT 与 BLOB 共用类型码 252（TEXT 靠字段
            # 字符集区分，pymysql 的 description 不暴露 charsetnr），按类型码判定
            # 会把 TEXT 误判为 BYTES。pymysql 已按列字符集把 TEXT 解码为 str、
            # 二进制列保持 bytes，故以实际值类型为准：str 原样保留。
            # 否则 str 会被 encode 成 bytes，目标端再 str() 成 "b'\xe6\x96\x87...'"
            # 字面量，导致中文文本损坏。
            return value
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
