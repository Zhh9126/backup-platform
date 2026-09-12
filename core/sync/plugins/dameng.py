# -*- coding: utf-8 -*-
"""达梦 DM8 同步插件（dmPython 驱动）。

离线环境说明：dmPython 不在 PyPI，随达梦安装介质提供
（<DM_HOME>/drivers/python/dmPython，pip install 或拷贝 .egg 到离线包）。
导入失败时给出明确指引，插件注册不受影响（运行时才报错）。
"""
import logging
import os
from typing import Any, List

from .base import (BasePlugin, ColumnMeta, ReadResult, SinkWriter, SourceReader,
                   SyncConfig, matrix_suggest)
from ..type_mapper import db_type_to_java_type, to_java

logger = logging.getLogger(__name__)


def _upper(name) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        name = str(name)  # JDBC 通道返回 java.lang.String（JPype 包装）
    return name.upper()


def _s(v):
    """JPype java.lang.String → python str（其余原样）。"""
    if v is not None and not isinstance(v, str):
        try:
            return str(v)
        except Exception:
            return v
    return v


def _import_dmpython():
    try:
        import dmPython  # noqa
        return dmPython
    except ImportError as e:
        raise RuntimeError(
            "达梦同步需要 dmPython 驱动（离线环境随 DM 客户端提供）："
            "从数据库服务器 <DM_HOME>/drivers/python/dmPython 安装，"
            "或将其加入 PYTHONPATH") from e


# ---------------------------------------------------------------- #
# JDBC fallback（dmPython 不可用时）：JayDeBeApi + DmJdbcDriver8.jar
# jar 位置：平台 drivers/jdbc/DmJdbcDriver8.jar（离线包随附）；
# JVM 要求 JDK11+（jpype 1.5+ 不支持 JDK8），自动探测常见路径。
_JVM_STARTED = False


def _ensure_jvm():
    """启动 JVM（全局一次）。优先 JDK11+。"""
    global _JVM_STARTED
    if _JVM_STARTED:
        return
    import jpype
    import glob
    candidates = (glob.glob("/usr/lib/jvm/java-11*/lib/server/libjvm.so")
                  + glob.glob("/usr/lib/jvm/java-1[1-9]*/lib/server/libjvm.so")
                  + glob.glob("/usr/lib/jvm/*/lib/server/libjvm.so")
                  + glob.glob("/usr/java/*/lib/server/libjvm.so"))
    # __file__ = core/sync/plugins/dameng.py → 项目根需向上 4 层
    _root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    jar = os.path.join(_root, "drivers", "jdbc", "DmJdbcDriver8.jar")
    if not os.path.exists(jar):
        # 容器/安装环境：driver.jar 与 jar 环境变量兜底
        jar = os.environ.get("DM_JDBC_JAR", "/opt/backup-platform/drivers/jdbc/DmJdbcDriver8.jar")
    for jvm in candidates:
        try:
            jpype.startJVM(jvm, classpath=[jar])
            _JVM_STARTED = True
            return
        except OSError:
            continue
    try:
        if jpype.isJVMStarted():
            _JVM_STARTED = True
            return
    except Exception:
        pass
    raise RuntimeError(
        "无法启动 JVM（JDBC fallback 需要 JDK11+，未找到 libjvm.so）。"
        "请安装 java-11-openjdk 或改用 dmPython 驱动")


def _jdbc_connect(host: str, port: int, user: str, password: str,
                  database: str = ""):
    """JayDeBeApi 连接达梦（DmJdbcDriver8）。"""
    _ensure_jvm()
    import jaydebeapi
    dsn = f"jdbc:dm://{host}:{port}"
    if database:
        dsn += f"?schema={database}"
    return jaydebeapi.connect("dm.jdbc.driver.DmDriver", dsn,
                              [user, password])


def _connect_dameng(host: str, port: int, user: str, password: str,
                    database: str = ""):
    """统一连接入口：优先 dmPython，不可用自动降级 JDBC。"""
    try:
        dm = _import_dmpython()
        return dm.connect(host=host, port=port, user=user,
                          password=password, database=database,
                          loginTimeout=15)
    except RuntimeError:
        logger.info("[dameng-plugin] dmPython 不可用，降级 JDBC 通道")
        return _jdbc_connect(host, port, user, password, database)


class DamengSourceReader(SourceReader):
    def connect(self) -> Any:
        cfg = self.config
        return _connect_dameng(cfg.src_host, cfg.src_port or 5236,
                               cfg.src_username, cfg.src_password,
                               cfg.src_db_name or cfg.src_schema or "")

    def list_tables(self) -> List[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            schema = _upper(self.config.src_schema or self.config.src_username
                            or "SYSDBA")
            cur.execute(
                "SELECT table_name FROM dba_tables WHERE owner=? "
                "ORDER BY table_name", (schema,))
            return [_s(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_columns(self, table: str) -> List[ColumnMeta]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            schema = _upper(self.config.src_schema or self.config.src_username
                            or "SYSDBA")
            cur.execute(
                "SELECT column_name, data_type, nullable, data_default, "
                "data_length, data_precision, data_scale "
                "FROM dba_tab_columns WHERE owner=? AND table_name=? "
                "ORDER BY column_id", (schema, _upper(table)))
            rows = cur.fetchall()
            cur.execute(
                "SELECT cols.column_name FROM dba_constraints c "
                "JOIN dba_cons_columns cols ON c.owner=cols.owner "
                "AND c.constraint_name=cols.constraint_name "
                "WHERE c.owner=? AND c.table_name=? AND c.constraint_type='P'",
                (schema, _upper(table)))
            pk_set = {_s(r[0]) for r in cur.fetchall()}
            cols = []
            for row in rows:
                base = _s(row[1] or "").upper()
                # 字符/数值类型归一：达梦 data_length 用于字符长度；data_precision/scale 用于数值
                mlen = row[4] if base in ("CHAR", "VARCHAR", "VARCHAR2",
                                          "NCHAR", "NVARCHAR", "NVARCHAR2",
                                          "CLOB", "NCLOB", "TEXT", "LONG") else None
                nprec = row[5] if base in ("NUMBER", "DECIMAL", "NUMERIC",
                                           "TINYINT", "SMALLINT",
                                           "INTEGER", "BIGINT",
                                           "BINARY_FLOAT", "BINARY_DOUBLE",
                                           "FLOAT", "DOUBLE", "REAL") else None
                nscale = row[6] if nprec is not None else None
                c = ColumnMeta(name=_s(row[0]), type=base,
                               nullable=(_s(row[2]) == "Y" or row[2] == 1 or
                                         _s(row[2]).upper() == "Y"),
                               default=row[3],
                               max_length=mlen,
                               numeric_precision=nprec,
                               numeric_scale=nscale)
                c.is_primary = c.name in pk_set
                cols.append(c)
            return cols
        finally:
            conn.close()

    def _build_select_sql(self, table: str, columns: List[str]) -> tuple:
        schema = _upper(self.config.src_schema or self.config.src_username
                        or "SYSDBA")
        table_ref = f'{schema}.{_upper(table)}'
        col_str = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        sql = f"SELECT {col_str} FROM {table_ref}"
        cfg = self.config
        binds = {}
        if cfg.sync_mode == "incremental" and cfg.incremental_column \
                and cfg.incremental_value:
            sql += (f" WHERE {cfg.incremental_column} > ?")
            binds["1"] = cfg.incremental_value
        if cfg.incremental_column:
            sql += f" ORDER BY {cfg.incremental_column}"
        return sql, binds

    def read_batch(self, cursor: Any) -> ReadResult:
        cfg = self.config
        table = (cfg.source_tables_list or [cfg.source_table])[0] \
            if (cfg.source_tables_list or cfg.source_table) else ""
        sql, binds = self._build_select_sql(table, [])
        cursor.execute(sql, binds or {})
        columns = [_s(d[0]) for d in cursor.description]
        rows = cursor.fetchmany(cfg.batch_size)
        records = [[self.plugin.type_to_java(
            str(cursor.description[i][1] or ''), _s(v))
            for i, v in enumerate(row)] for row in rows]
        return ReadResult(records=records, columns=columns,
                          has_more=len(rows) >= cfg.batch_size)


class DamengSinkWriter(SinkWriter):
    def connect(self) -> Any:
        cfg = self.config
        return _connect_dameng(cfg.tgt_host, cfg.tgt_port or 5236,
                               cfg.tgt_username, cfg.tgt_password,
                               cfg.tgt_db_name or cfg.tgt_schema or "")

    def _table_ref(self, table: str = None) -> str:
        cfg = self.config
        schema = _upper(cfg.tgt_schema or cfg.tgt_username or "SYSDBA")
        tbl = _upper(table or cfg.target_table or "")
        return f'{schema}.{tbl}'

    def _get_target_columns(self, conn: Any, table: str) -> List[str]:
        cur = conn.cursor()
        schema = _upper(self.config.tgt_schema or self.config.tgt_username
                        or "SYSDBA")
        cur.execute("SELECT column_name FROM dba_tab_columns "
                    "WHERE owner=? AND table_name=? ORDER BY column_id",
                    (schema, _upper(table)))
        return [_s(r[0]) for r in cur.fetchall()]

    def _get_primary_keys(self, conn: Any, table: str) -> List[str]:
        cur = conn.cursor()
        schema = _upper(self.config.tgt_schema or self.config.tgt_username
                        or "SYSDBA")
        cur.execute(
            "SELECT cols.column_name FROM dba_constraints c "
            "JOIN dba_cons_columns cols ON c.owner=cols.owner "
            "AND c.constraint_name=cols.constraint_name "
            "WHERE c.owner=? AND c.table_name=? AND c.constraint_type='P'",
            (schema, _upper(table)))
        return [_s(r[0]) for r in cur.fetchall()]

    def _create_table_sql(self, table: str, columns: List[ColumnMeta]) -> str:
        cfg = self.config
        schema = _upper(cfg.tgt_schema or cfg.tgt_username or "SYSDBA")
        tbl = _upper(table)
        defs = []
        for c in columns:
            name, t = _upper(c.name), c.type.upper()
            dm_t = self._map_to_dameng_type(c, t)
            defs.append(f'"{name}" {dm_t}' + ("" if c.nullable else " NOT NULL"))
        pk = [c.name for c in columns if getattr(c, "is_primary", False)]
        if pk:
            defs.append("PRIMARY KEY (" + ", ".join(f'"{_upper(p)}"' for p in pk) + ")")
        return f'CREATE TABLE {schema}.{tbl} ({", ".join(defs)})'

    def _map_to_dameng_type(self, c: ColumnMeta, t: str) -> str:
        """达梦建表类型映射：覆盖所有偏门类型，避免落 VARCHAR(4000) 兜底。

        优先级：精确基类型 → 模糊匹配 → 兜底。
        字符类型用 c.max_length（列解析时填）；数值类型用 c.numeric_precision/scale。
        """
        base = t.split("(")[0].strip()
        # 整数
        if base in ("TINYINT",):
            return "TINYINT"
        if base == "SMALLINT":
            return "SMALLINT"
        if base in ("INT", "INTEGER"):
            return "INT"
        if base == "BIGINT":
            return "BIGINT"
        # 浮点/精确小数
        if base in ("DECIMAL", "NUMERIC", "NUMBER"):
            p = c.numeric_precision or 18
            s = c.numeric_scale if c.numeric_scale is not None else 0
            return f"DECIMAL({p},{s})" if s else f"NUMBER({p})"
        if base in ("FLOAT", "DOUBLE", "REAL", "BINARY_FLOAT", "BINARY_DOUBLE", "DOUBLE PRECISION"):
            return "DOUBLE"
        # 字符
        if base in ("CHAR", "NCHAR"):
            return f"{base}({c.max_length or 1})"
        if base in ("VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2"):
            ln = c.max_length or 4000
            if ln > 3900:                       # 达梦 VARCHAR 上限 3900
                return "TEXT"
            return f"VARCHAR2({ln})" if base == "VARCHAR2" else f"{base}({ln})"
        # 大对象
        if "TEXT" in base or base in ("CLOB", "NCLOB"):
            return "TEXT"
        if "BLOB" in base or base in ("BYTEA", "IMAGE", "LONG RAW"):
            return "BLOB"
        # 时间
        if base == "DATE":
            return "DATE"
        if base in ("TIMESTAMPTZ",):
            return "TIMESTAMP WITH TIME ZONE"
        if base.startswith("DATETIME") or base in ("TIMESTAMP", "DATETIME2", "SMALLDATETIME"):
            return "TIMESTAMP"
        if base == "DATETIMEOFFSET":
            return "TIMESTAMP WITH TIME ZONE"
        if base == "TIME":
            return "TIME"
        if base == "INTERVAL" or base.startswith("INTERVAL"):
            return "INTERVAL DAY TO SECOND"
        # YEAR（MySQL 1901-2155 → SMALLINT 足够）
        if base == "YEAR":
            return "SMALLINT"
        # 布尔
        if base in ("BOOLEAN", "BOOL"):
            return "TINYINT"
        # 字节串
        if base == "BIT":
            return "BIT"
        # 特殊
        if base in ("JSON", "JSONB", "JSONPATH"):
            return "TEXT"                        # 达梦无原生 JSON
        if base == "UUID" or base == "UNIQUEIDENTIFIER":
            return "VARCHAR2(36)"
        if base in ("XML", "XMLTYPE"):
            return "TEXT"                        # 达梦无原生 XML
        if base in ("ENUM", "SET"):
            return "VARCHAR(128)"
        if base in ("UROWID", "ROWID"):
            return "VARCHAR(18)"
        if base == "BFILE":
            return "VARCHAR(1024)"
        if base == "LONG":
            return "TEXT"
        if base == "ROWVERSION":
            return "BLOB"
        # 几何（达梦需 ST_GEOMETRY，需 spatial 扩展；无扩展时降 TEXT）
        if base in ("GEOMETRY", "POINT", "LINESTRING", "POLYGON",
                    "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON",
                    "GEOMETRYCOLLECTION", "GEOMCOLLECTION", "GEOGRAPHY",
                    "SDO_GEOMETRY", "SDO_TOPO_GEOMETRY", "SDO_GEORASTER"):
            return "ST_GEOMETRY" if base == "GEOMETRY" else "TEXT"
        # 跨源兜底：源端类型来自其它库（JSONB/INET/TSVECTOR/HIERARCHYID/VECTOR/
        # SDO_GEOMETRY...）时用统一矩阵翻译，避免一律落 VARCHAR(4000)
        sug = matrix_suggest(getattr(self, "config", None), "dameng", t)
        if sug:
            if sug in ("VARCHAR", "CHAR", "NVARCHAR"):
                return f"VARCHAR({min(c.max_length or 3900, 3900)})"
            if sug in ("NUMERIC", "DECIMAL", "NUMBER"):
                return f"DECIMAL({c.numeric_precision or 18},{c.numeric_scale or 0})"
            return sug
        # 其他
        return "VARCHAR(4000)"

    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        cfg = self.config
        table = cfg.target_table
        cur = conn.cursor()
        schema = _upper(cfg.tgt_schema or cfg.tgt_username or "SYSDBA")
        cur.execute("SELECT COUNT(*) FROM dba_tables WHERE owner=? "
                    "AND table_name=?", (schema, _upper(table)))
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
        cur = conn.cursor()
        pks = self._get_primary_keys(conn, table)
        ncols = [self.plugin.normalize_identifier(
            c, cfg.field_ide) for c in columns]
        if cfg.save_mode == "upsert" and pks:
            # 达梦 MERGE INTO（UPSERT）
            upd = [c for c in ncols if _upper(c) not in
                   {_upper(p) for p in pks}]
            # JDBC/dmPython 均为 qmark（?）参数风格；参数顺序 =
            # USING 子句的 pk 列 → SET 的 upd 列（ 达梦 MERGE ... USING
            # (SELECT ? AS pk ...) 逐行提供比对值）
            set_sql = ", ".join(f'"{_upper(c)}" = ?' for c in upd)
            on_sql = " AND ".join(f'T."{_upper(p)}" = S."{_upper(p)}"'
                                  for p in pks)
            ins_cols = ", ".join(f'"{_upper(c)}"' for c in ncols)
            ins_vals = ", ".join("?" for _ in ncols)
            using_cols = ", ".join(f'? AS "{_upper(p)}"' for p in pks)
            sql = (f"MERGE INTO {self._table_ref(table)} T USING "
                   f"(SELECT {using_cols} FROM DUAL) S ON ({on_sql}) "
                   f"WHEN MATCHED THEN UPDATE SET {set_sql} "
                   f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) "
                   f"VALUES ({ins_vals})")
            rows_data = []
            for rec in records:
                # 统一大写键（源列名与主键名大小写可能不一致）
                d = {_upper(k): v for k, v in zip(ncols, rec)}
                # 参数顺序：USING 的 pk → SET 的 upd → INSERT VALUES 的全列
                rows_data.append([d.get(_upper(p)) for p in pks]
                                 + [d.get(_upper(c)) for c in upd]
                                 + [d.get(_upper(c)) for c in ncols])
            cur.executemany(sql, rows_data)
        else:
            cols_sql = ", ".join(f'"{_upper(c)}"' for c in ncols)
            binds = ", ".join("?" for _ in ncols)
            sql = (f"INSERT INTO {self._table_ref(table)} "
                   f"({cols_sql}) VALUES ({binds})")
            cur.executemany(sql, records)
        conn.commit()
        return len(records)


class DamengPlugin(BasePlugin):
    db_type = "dameng"
    default_ports = {"sync": 5236}

    def create_reader(self, config: SyncConfig) -> SourceReader:
        return DamengSourceReader(config, self)

    def create_writer(self, config: SyncConfig) -> SinkWriter:
        return DamengSinkWriter(config, self)

    def type_to_java(self, db_type: str, value: Any) -> Any:
        # to_java(value) 单参（type_mapper 统一值归一）；
        # 此前误传两参导致轮询实时同步 "takes 1 positional argument but 2" 异常
        return to_java(value)

    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        return java_value

    def quote_identifier(self, name: str) -> str:
        return f'"{_upper(name)}"'
