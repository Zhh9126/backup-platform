# -*- coding: utf-8 -*-
"""异构数据库类型映射引擎（对标阿里 DTS《结构初始化涉及的数据类型映射关系》
与 AWS DMS datatype mapping 规则）。

功能：
1. 类型解析：把任意库型的列类型解析为规范结构
   （家族 + 基类型 + 精度/刻度 + unsigned 修饰）
2. 映射矩阵：源类型 → 目标库型的建议类型 + 风险级别 + 说明
3. 风险判定（影响迁移/同步成功率的关键点）：
   - ok    直接映射
   - warn  需升位/降精度/隐式转换（unsigned 升位、DECIMAL 精度、BIT/ENUM/SET/JSON 特殊处理）
   - fail  目标库不支持（如 TIME→Oracle）

风险级别直接影响 precheck 结论：fail 阻止迁移，warn 提示人工确认。
"""
import re
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 类型解析
# ---------------------------------------------------------------------------
_TYPE_RE = re.compile(r"^\s*([a-zA-Z_ ]+?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\))?\s*$")

# 多词类型名归一表（长的在前，用 startswith 精确匹配）。
# 只取"第一个单词"会把 PG format_type() 的输出判错，实测会丢语义：
#   'character varying(100)'   → base=character → char 家族（当定长字符处理，错误）
#   'timestamp with time zone' → base=timestamp → 丢时区语义
#   'long raw'                 → base=long → text 家族（应为二进制）
_MULTIWORD_BASE = (
    ("timestamp with local time zone", "timestamp with local time zone"),
    ("timestamp with time zone", "timestamptz"),
    ("time with time zone", "time with time zone"),
    ("character varying", "varchar"),
    ("double precision", "double"),
    ("bit varying", "bit"),
    ("interval year to month", "interval year to month"),
    ("interval day to second", "interval day to second"),
    ("long varchar", "long varchar"),
    ("long varbinary", "long varbinary"),
    ("long raw", "long raw"),
)


def parse_type(type_str) -> Dict[str, Any]:
    """解析列类型字符串 → {base, prec, scale, unsigned, array}。

    示例：
      'BIGINT UNSIGNED'      -> base=bigint, unsigned=True
      'DECIMAL(20,4)'        -> base=decimal, prec=20, scale=4
      "ENUM('a','b')"        -> base=enum（枚举值列表剥离）
      'VARCHAR2(255 CHAR)'   -> base=varchar2, prec=255
      'TIMESTAMP(6)'         -> base=timestamp, prec=6
      'INTEGER[]'            -> base=integer, array=True
      'TEXT[]'               -> base=text, array=True
    """
    if type_str is not None and not isinstance(type_str, str):
        type_str = str(type_str)          # JDBC java.lang.String 归一
    s = (type_str or "").strip().lower()
    unsigned = "unsigned" in s
    s = s.replace("unsigned", "").replace("signed", "").replace("zerofill", "").strip()
    # 基类型词（enum('a','b')/set('x') 的括号值列表随词干剥离）
    # 注意：词干含数字（varchar2/nvarchar2/raw...），字符类必须含 0-9，
    # 否则 'varchar2(64)' 被截成 'varchar' 且精度丢失（实测踩坑）。
    # 同时识别 PG 风格的 '[]' 数组后缀，单独标记 array=True（map_type 据此走 array 家族）。
    m = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)(\[\])?\s*(?:\(([^)]*)\))?", s)
    if not m:
        return {"base": s, "prec": None, "scale": None, "unsigned": unsigned, "array": False}
    base = re.sub(r"\s+", " ", m.group(1).strip())
    for phrase, norm in _MULTIWORD_BASE:      # 多词类型优先（见 _MULTIWORD_BASE 注释）
        if s.startswith(phrase):
            base = norm
            break
    is_array = bool(m.group(2))
    inner = (m.group(3) or "").strip()
    prec = scale = None
    digits = re.match(r"^\s*(\d+)\s*(?:,\s*(\d+))?\s*$", inner)
    if digits:
        prec = int(digits.group(1))
        scale = int(digits.group(2)) if digits.group(2) else None
    elif inner and re.match(r"^\s*\d+\s+(char|byte)\s*$", inner):
        prec = int(re.match(r"^\s*(\d+)", inner).group(1))   # '255 CHAR'
    return {"base": base, "prec": prec, "scale": scale, "unsigned": unsigned,
            "array": is_array, "raw_inner": inner}


# 家族归一：不同库型的同义类型 → 规范名（覆盖所有偏门类型，含各数据库特有类型）
_FAMILY = {
    # 整数（宽度，bit）
    "tinyint": "int", "smallint": "int", "mediumint": "int",
    "int": "int", "integer": "int", "bigint": "int",
    "number": "number",            # Oracle/达梦 NUMBER(38,0) 可表达一切整数
    "smallint identity": "int",
    "binary_integer": "int", "pls_integer": "int",  # Oracle 整数别名
    "int2": "int", "int4": "int", "int8": "int",     # PG 别名（atttypid 友好）
    # PG/金仓/达梦的自增整数别名（DDL 里常见写法，等价 integer/bigint）
    "serial": "int", "bigserial": "int", "smallserial": "int",
    "serial2": "int", "serial4": "int", "serial8": "int",
    # 浮点/精确小数
    "decimal": "decimal", "numeric": "decimal", "dec": "decimal",
    "float": "float", "double": "float", "real": "float",
    "double precision": "float", "binary_float": "float", "binary_double": "float",
    # 定长/变长字符
    "char": "char", "varchar": "varchar", "varchar2": "varchar",
    "nchar": "char", "nvarchar": "varchar", "nvarchar2": "varchar",
    "character varying": "varchar", "character": "char",
    "long varchar": "text", "long varbinary": "blob",   # 达梦/老 Oracle 长类型
    # 大文本（含 PG XML/Oracle XMLType）
    "text": "text", "tinytext": "text", "mediumtext": "text", "longtext": "text",
    "clob": "text", "nclob": "text",
    "ntext": "text",                 # SQL Server 已弃用 NTEXT（UCS-2 大文本）
    "longvarchar": "text",           # 达梦/金仓 LONGVARCHAR
    "longnvarchar": "text",          # 达梦 LONGNVARCHAR
    "xml": "text",            # PG/Oracle/SQL Server 的 XML 字符串承载
    "xmltype": "text",        # Oracle XMLType
    "long": "text",           # Oracle LONG（已弃用）
    # 二进制
    "blob": "blob", "tinyblob": "blob", "mediumblob": "blob", "longblob": "blob",
    "bytea": "blob", "raw": "blob", "long raw": "blob", "binary": "blob",
    "varbinary": "blob", "image": "blob", "bfile": "blob",
    # 时间（含 SQL Server 多种 datetime）
    "date": "date", "datetime": "datetime", "timestamp": "timestamp",
    "timestamptz": "timestamptz", "time": "time", "year": "year",
    "timestamp with time zone": "timestamptz",
    "timestamp with local time zone": "timestamp",
    "datetime2": "datetime", "datetimeoffset": "timestamptz",
    "smalldatetime": "datetime", "time with time zone": "time",
    "bigdatetime": "datetime",       # 金仓/达梦 BIGDATETIME（微秒精度时间戳）
    "interval": "interval",
    "interval year to month": "interval", "interval day to second": "interval",
    # 特殊
    "json": "json", "jsonb": "json",
    # jsonpath 单独归 text：JSON 家族分支只能给出 JSON/JSONB，而 jsonpath 是
    # 路径表达式（'$.a.b'），存进 JSONB 会报类型错误；同库应保形 JSONPATH。
    "jsonpath": "text",
    "enum": "enum", "set": "set",
    "bit": "bit", "bool": "bool", "boolean": "bool",
    "uuid": "uuid",
    "uniqueidentifier": "uuid",      # SQL Server UUID 等价
    # PG 特有（数组/范围/全文/网络地址/扩展类型）
    "array": "array",                # PG 任意[]数组 → 家族 array
    "int4range": "range", "int8range": "range", "numrange": "range",
    "daterange": "range", "tsrange": "range", "tstzrange": "range",
    "tsvector": "text", "tsquery": "text",
    "hstore": "text",                # PG key-value KV
    "inet": "text", "cidr": "text",  # PG 网络地址 → 字符串承载
    "macaddr": "text", "macaddr8": "text",
    "citext": "text",                # PG 不区分大小写文本
    "ltree": "text",                 # PG 树结构标签
    "cube": "text",                  # PG 多维立方体
    # 空间类型（统一归到 geometry 家族；落库策略见 map_type）
    "geometry": "geometry", "point": "geometry",
    "linestring": "geometry", "polygon": "geometry",
    "multipoint": "geometry", "multilinestring": "geometry",
    "multipolygon": "geometry", "geometrycollection": "geometry",
    "geomcollection": "geometry",
    "geography": "geometry",         # SQL Server/PG 地理
    "st_geometry": "geometry",       # 达梦空间类型（DMGEO 包）
    "st_point": "geometry", "st_linestring": "geometry", "st_polygon": "geometry",
    # Oracle 特有
    "urowid": "text", "rowid": "text",
    "sdo_geometry": "geometry", "sdo_topo_geometry": "geometry",
    "sdo_georaster": "geometry",
    "anydata": "text", "anytype": "text", "anydataset": "text",
    "ref": "text", "mdsys.sdo_geometry": "geometry",
    # SQL Server 特有
    "hierarchyid": "text", "rowversion": "blob",
    # 注意：SQL Server 的 TIMESTAMP 是 ROWVERSION 别名（8 字节二进制），但其它
    # 库的 TIMESTAMP 是真时间戳。这里绝不能写 "timestamp": "blob"（会覆盖上面的
    # 时间类型），SQL Server 源的特殊语义在 map_type 里按 src_db 判定。
    "sql_variant": "text", "vector": "text",
    # 货币（家族归到 decimal；精度差异由 _decimal_target 处理）
    "money": "decimal", "smallmoney": "decimal",
}


# 必须走 _special_target 精细映射的偏门类型。
# 这些类型在 _FAMILY 里被归到 text/blob 等粗家族（仅为 family_of 归一），若直接按
# 粗家族映射会丢语义：ROWID→TEXT、BFILE→BLOB、XMLTYPE→TEXT、网络地址→TEXT，
# SQL Server 同库 HIERARCHYID/VECTOR/ROWVERSION 不保形等。map_type 命中本集合时
# 一律优先走 _special_target 的逐类型策略。
_SPECIAL_BASES = {
    # JSON / 枚举 / 位 / 布尔 / UUID
    "json", "jsonb", "jsonpath", "enum", "set", "bit", "bool", "boolean",
    "uuid", "uniqueidentifier",
    # PG 范围 / 全文 / KV / 网络地址 / 扩展类型
    "int4range", "int8range", "numrange", "daterange", "tsrange", "tstzrange",
    "tsvector", "tsquery", "hstore", "citext", "ltree", "cube",
    "inet", "cidr", "macaddr", "macaddr8",
    # 空间类型
    "geometry", "point", "linestring", "polygon", "multipoint",
    "multilinestring", "multipolygon", "geometrycollection", "geomcollection",
    "geography", "sdo_geometry", "sdo_topo_geometry", "sdo_georaster",
    # Oracle 特有
    "urowid", "rowid", "bfile", "long", "long raw",
    "anydata", "anytype", "anydataset", "ref", "xml", "xmltype",
    # SQL Server / 其它特有
    "hierarchyid", "sql_variant", "vector", "rowversion",
    "money", "smallmoney",       # 固定 4 位小数，按精度保形（否则退成无精度 DECIMAL）
}


def family_of(t) -> str:
    return _FAMILY.get(parse_type(t)["base"], "unknown")


# ---------------------------------------------------------------------------
# 目标类型映射矩阵（key = 目标库型；value = 家族 → (目标类型模板, 级别, 说明)）
# 模板中 {p}/{s} = 源精度/刻度。级别: ok / warn / fail
# 依据：阿里 DTS 官方映射手册 + AWS DMS mapping + ora2pg 实践
# ---------------------------------------------------------------------------
_INT_UPGRADE = {   # (base, unsigned) -> (宽度 bit)
    ("tinyint", False): 8, ("tinyint", True): 8,
    ("smallint", False): 16, ("smallint", True): 16,
    ("mediumint", False): 24, ("mediumint", True): 24,
    ("int", False): 32, ("int", True): 32,
    ("bigint", False): 64, ("bigint", True): 64,
    # 别名（Oracle/PG/金仓/达梦 整数与自增写法）：宽度对齐，避免落到 KeyError 兜底
    ("binary_integer", False): 32, ("pls_integer", False): 32,
    ("int2", False): 16, ("int4", False): 32, ("int8", False): 64,
    ("smallserial", False): 16, ("serial2", False): 16,
    ("serial", False): 32, ("serial4", False): 32,
    ("bigserial", False): 64, ("serial8", False): 64,
}


def _int_target(tgt: str, t: dict):
    """整数家族映射：核心是 unsigned 升位（DTS 规则：INT UNSIGNED→BIGINT、
    BIGINT UNSIGNED→DECIMAL(20,0)）。"""
    base, uns = t["base"], t["unsigned"]
    if uns and base == "bigint":
        # BIGINT UNSIGNED 最大 18446744073709551615，任何 64 位有符号都装不下
        if tgt in ("oracle", "dameng", "kingbase"):
            return ("NUMBER(20,0)", "warn",
                    "BIGINT UNSIGNED → NUMBER(20,0)（64位无符号超有符号范围，需 20 位十进制）")
        return ("DECIMAL(20,0)", "warn",
                "BIGINT UNSIGNED → DECIMAL(20,0)（超出 BIGINT 上限 9223372036854775807，"
                "映射为 BIGINT 会静默降级/溢出）")
    if uns and base == "int":
        # INT UNSIGNED (0~4294967295)：有符号 32 位装不下 → 升位 64 位
        up = ("BIGINT" if tgt in ("mysql", "mariadb", "postgresql",
                                  "kingbase", "sqlserver")
              else "NUMBER(10,0)")
        return (up, "warn",
                f"INT UNSIGNED → {up}（无符号 32 位超有符号范围，升位 64 位）")
    if uns:
        # TINYINT/SMALLINT UNSIGNED 升一级
        up = {"tinyint": ("SMALLINT", "MEDIUMINT"), "smallint": ("INT", "BIGINT"),
              "mediumint": ("INT", "BIGINT")}[base]
        return (up[0] if tgt in ("mysql", "mariadb") else up[1], "warn",
                f"{base.upper()} UNSIGNED → {up[0]}（无符号升位）")
    # 有符号整数：宽度对齐
    width = _INT_UPGRADE.get((base, False), 32)
    if tgt in ("oracle", "dameng"):
        # Oracle/达梦整数族统一 NUMBER(p,0) 或 INTEGER
        if base == "bigint":
            return ("NUMBER(19,0)", "ok", "BIGINT → NUMBER(19,0)")
        return ("INTEGER", "ok", "整数 → INTEGER")
    if tgt in ("postgresql", "kingbase"):
        # 宽位兜底：pls_integer/int8/serial 等别名不在白名单里时按位宽归一，
        # 直接按 base 查字典会 KeyError（实测 oracle PLS_INTEGER / kingbase int8 命中）
        if width <= 16:
            return ("SMALLINT", "ok", "TINYINT/SMALLINT → SMALLINT")
        if width <= 32:
            return ("INTEGER", "ok", "")
        return ("BIGINT", "ok", "")
    # mysql/mariadb/sqlserver：按位宽归一到各库整数名（避免别名原样输出）
    if tgt in ("mysql", "mariadb"):
        return ({8: "TINYINT", 16: "SMALLINT", 24: "MEDIUMINT",
                 32: "INT", 64: "BIGINT"}.get(width, "INT"), "ok", "")
    return ({8: "TINYINT", 16: "SMALLINT", 24: "INT",
             32: "INT", 64: "BIGINT"}.get(width, "INT"), "ok", "")


def _decimal_target(tgt: str, t: dict):
    p, s = t["prec"], t["scale"]
    if tgt in ("oracle", "dameng"):
        if p is None:
            return ("NUMBER", "warn", "DECIMAL 无精度 → NUMBER（DTS：精度缺失降为 NUMBER）")
        return (f"NUMBER({p},{s or 0})", "ok", "")
    if tgt in ("postgresql", "kingbase"):
        return (f"NUMERIC({p},{s or 0})" if p else "NUMERIC", "ok", "")
    if p is None:
        # 源 NUMBER 无精度（Oracle 任意精度）→ 目标 DECIMAL 默认 (10,0)，
        # 超范围数据会降级/溢出（DTS：静默精度降低）
        return ("DECIMAL", "warn",
                "源无精度约束（任意精度），目标 DECIMAL 默认 (10,0) 存在溢出/降级风险，"
                "建议按业务实际精度建列")
    return ("DECIMAL" + f"({p},{s or 0})" if p else "DECIMAL"), "ok", ""


def _datetime_target(tgt: str, t: dict):
    base = t["base"]
    if base == "year":
        return ("INTEGER" if tgt not in ("mysql", "mariadb") else "YEAR",
                "warn", "YEAR → INTEGER（目标无 YEAR 类型）")
    if base == "time" and tgt in ("oracle",):
        # Oracle 无纯 TIME 类型：按 'HH:MM:SS[.ffffff]' 字符串承载。落 DATE 会引入
        # 无意义的日期部分，且无法表达 MySQL TIME 的超 24h / 负值语义，字符串最保真。
        return ("VARCHAR2(16)", "warn",
                "Oracle 无 TIME 类型：按字符串承载 'HH:MM:SS'（保留超 24h/负值语义，"
                "需人工确认是否改按 DATE 处理）")
    if base == "datetime":
        if tgt in ("oracle", "dameng"):
            p = t["prec"]
            return (f"TIMESTAMP({p})" if p else "TIMESTAMP(0)", "warn",
                    "DATETIME → TIMESTAMP（精度缺失时降为 TIMESTAMP(0)，"
                    "DTS 规则）" if not p else "DATETIME → TIMESTAMP")
        if tgt in ("postgresql", "kingbase"):
            return ("TIMESTAMP", "warn",
                    "DATETIME → TIMESTAMP（无时区语义；源若涉多时区需审查）")
        return ("DATETIME" if tgt in ("mysql", "mariadb") else "DATETIME2", "ok", "")
    if base == "timestamp":
        if tgt in ("oracle",):
            return ("TIMESTAMP WITH LOCAL TIME ZONE", "warn",
                    "TIMESTAMP → TIMESTAMP WITH LOCAL TIME ZONE（时区语义变化，"
                    "跨时区部署需审查）")
        return ("TIMESTAMP", "ok", "")
    if base == "date":
        if tgt in ("mysql", "mariadb"):
            return ("DATE", "ok", "")
        return ("DATE", "ok", "")
    # 带时区的时间戳：TIMESTAMP WITH TIME ZONE / SQL Server DATETIMEOFFSET
    # 注意 base 可能是 timestamptz / datetimeoffset（SQL Server 源码），两者同族
    if base in ("timestamptz", "datetimeoffset"):
        if tgt in ("postgresql", "kingbase"):
            return ("TIMESTAMP WITH TIME ZONE", "ok", "")
        if tgt in ("oracle", "dameng"):
            return ("TIMESTAMP WITH TIME ZONE", "ok", "")
        if tgt in ("sqlserver",):
            return ("DATETIMEOFFSET", "ok",
                    "TIMESTAMPTZ → DATETIMEOFFSET（精度按源端保留）")
        if tgt in ("mysql", "mariadb"):
            # DTS 规则：按 UTC 归一为 DATETIME(6)。选 DATETIME 而非 TIMESTAMP：
            # TIMESTAMP 有 2038 上限且受会话时区影响；DATETIME(6) 保留微秒精度、
            # 覆盖全时间范围。原始时区偏移丢失，跨时区业务需人工确认。
            return ("DATETIME(6)", "warn",
                    "MySQL/MariaDB 无带时区时间戳：按 UTC 归一为 DATETIME(6)"
                    "（微秒精度保留、时区偏移丢失，跨时区业务需人工确认）")
        return ("VARCHAR(64)", "warn", "UNKNOWN 目标库：带时区时间戳按字符串承载")
    # SQL Server DATETIME2/DATETIMEOFFSET/SMALLDATETIME 跨库
    if base == "datetime2":
        return ("DATETIME" if tgt in ("mysql", "mariadb") else
                "TIMESTAMP" if tgt in ("postgresql", "kingbase") else
                "TIMESTAMP" if tgt in ("oracle", "dameng") else
                "DATETIME2", "ok", "")
    if base == "smalldatetime":
        return ("DATETIME" if tgt in ("mysql", "mariadb") else
                "TIMESTAMP" if tgt in ("postgresql", "kingbase") else
                "TIMESTAMP" if tgt in ("oracle", "dameng") else
                "SMALLDATETIME", "ok", "")
    if base == "bigdatetime":
        # 金仓/达梦 BIGDATETIME：微秒级时间戳，按目标方言归一到标准时间类型
        if tgt in ("oracle", "dameng"):
            return ("TIMESTAMP(6)", "ok", "")
        if tgt in ("postgresql", "kingbase"):
            return ("TIMESTAMP", "ok", "")
        if tgt == "sqlserver":
            return ("DATETIME2(6)", "ok", "")
        return ("DATETIME(6)", "ok", "")
    # INTERVAL 家族
    if base == "interval" or base.startswith("interval"):
        if tgt in ("oracle", "dameng"):
            return ("INTERVAL DAY TO SECOND", "warn",
                    "INTERVAL 落 DAY TO SECOND（年度/月度精度降级）" if "year" in base
                    else "INTERVAL 跨库保精度")
        if tgt in ("postgresql", "kingbase"):
            return ("INTERVAL", "ok", "")
        if tgt == "sqlserver":
            # SQL Server 无原生 INTERVAL：字符串承载（与插件层 DDL 保持一致）
            return ("VARCHAR(64)", "warn",
                    "INTERVAL → VARCHAR(64)（SQL Server 无原生 INTERVAL 类型）")
        if tgt in ("mysql", "mariadb"):
            # MySQL/MariaDB 无 INTERVAL：按文本承载（'1-2' / '3 04:05:06'）。
            # 拆成 年/月/日/秒 多字段需业务侧决策，故默认字符串 + 人工确认。
            return ("VARCHAR(64)", "warn",
                    "MySQL/MariaDB 无原生 INTERVAL：按字符串承载"
                    "（'1-2' / '3 04:05:06'，需人工确认是否拆字段）")
        return ("VARCHAR(64)", "warn", "UNKNOWN 目标库：INTERVAL 按字符串承载")
    # 其它库的时间类型别名：按目标方言归一为标准时间类型。
    # 绝不能原样输出源类型名（BIGDATETIME 等曾让目标库建表直接失败）
    if tgt in ("oracle", "dameng"):
        return ("TIMESTAMP", "warn", f"{base.upper()} → TIMESTAMP（按标准时间类型承载）")
    if tgt in ("postgresql", "kingbase"):
        return ("TIMESTAMP", "warn", f"{base.upper()} → TIMESTAMP（按标准时间类型承载）")
    if tgt == "sqlserver":
        return ("DATETIME2", "warn", f"{base.upper()} → DATETIME2（按标准时间类型承载）")
    return ("DATETIME", "warn", f"{base.upper()} → DATETIME（按标准时间类型承载）")


def _char_target(tgt: str, t: dict):
    base, p = t["base"], t["prec"]
    is_n = base.startswith("n")           # NCHAR/NVARCHAR/NVARCHAR2 为 Unicode 字符族
    if base in ("char", "nchar"):
        if tgt in ("oracle", "dameng"):
            name = "NCHAR" if is_n else "CHAR"
            if p is None:
                return (f"{name}(1)", "warn", "CHAR 长度缺失 → CHAR(1)（DTS 规则）")
            # Oracle CHAR 上限 2000 / NCHAR 上限 1000
            if is_n and p > 1000:
                return ("NVARCHAR2(1000)", "warn", "NCHAR 超 1000 → NVARCHAR2(1000)")
            if not is_n and p > 2000:
                return ("VARCHAR2(2000)", "warn", "CHAR 超 2000 → VARCHAR2(2000)")
            return (f"{name}({p})", "ok", "")
        if tgt == "sqlserver":
            name = "NCHAR" if is_n else "CHAR"
            if p is None:
                return (f"{name}(1)", "warn", "CHAR 长度缺失 → 默认长度 1")
            if p > 4000:
                return ("NVARCHAR(MAX)" if is_n else "VARCHAR(MAX)", "warn",
                        f"{name} 超 4000 → MAX")
            return (f"{name}({p})", "ok", "")
        return ("CHAR" + (f"({p})" if p else ""), "ok", "")
    if base in ("varchar", "varchar2", "nvarchar", "nvarchar2"):
        # MySQL 的 VARCHAR 必须带长度；源无长度/超长时退 TEXT
        if tgt in ("mysql", "mariadb") and p is None:
            return ("TEXT", "warn", "VARCHAR 无长度 → TEXT（MySQL VARCHAR 必须带长度）")
        if tgt == "sqlserver":
            name = "NVARCHAR" if is_n else "VARCHAR"
            if p is None:
                return (f"{name}(MAX)", "warn",
                        "SQL Server 变长字符长度缺失 → MAX（避免默认长度 1 丢数据）")
            if p > 4000:
                return (f"{name}(MAX)", "warn", f"{name} 超 4000 → {name}(MAX)")
            return (f"{name}({p})", "ok", "")
        if tgt in ("oracle", "dameng"):
            # Oracle/达梦没有 VARCHAR/NVARCHAR 标准类型，统一转 VARCHAR2/NVARCHAR2
            name = "NVARCHAR2" if is_n else "VARCHAR2"
            if p is None:
                return (f"{name}(4000)", "warn",
                        "变长字符长度缺失 → 按 VARCHAR2 上限 4000 兜底，建议人工确认")
            if tgt == "oracle":
                if is_n and p > 2000:
                    return ("NCLOB", "warn", "NVARCHAR2 超 2000 → NCLOB")
                if not is_n and p > 4000:
                    return ("CLOB", "warn", "VARCHAR2 超 4000 → CLOB")
            if tgt == "dameng" and p > 3900:
                return ("TEXT", "warn", "达梦 VARCHAR 上限 3900 → TEXT")
            return (f"{name}({p})", "ok", "")
        return ("VARCHAR" + (f"({p})" if p else ""), "ok", "")
    # text 族
    if tgt in ("oracle", "dameng"):
        return ("CLOB", "ok", "TEXT → CLOB")
    if tgt in ("postgresql", "kingbase"):
        return ("TEXT", "ok", "")
    return ("TEXT", "ok", "")


def _blob_target(tgt: str, t: dict):
    if tgt in ("oracle",):
        return ("BLOB", "ok", "")
    if tgt in ("postgresql", "kingbase"):
        return ("BYTEA", "ok", "")
    return ("BLOB" if tgt in ("mysql", "mariadb", "dameng") else "VARBINARY(MAX)", "ok", "")


def _enum_value_max_len(raw_inner: str) -> int:
    """从 ENUM/SET 括号内原文（如 "'a','bb','ccc'"）取最长取值长度。

    跨库落 VARCHAR2(n)/VARCHAR(n) 时长度必须按最长枚举值，固定 4000/255 会
    与源语义脱节（过短截断、过长浪费），故统一按值长计算。
    """
    raw = str(raw_inner or "").strip()
    if not raw:
        return 0
    best = 0
    for part in raw.split(","):
        best = max(best, len(part.strip().strip("'\"")))
    return best


def _special_target(tgt: str, t: dict):
    """特殊类型：JSON / ENUM / SET / BIT / BOOL / UUID / geometry 以及各数据库偏门类型。"""
    # 数组修饰必须在最前面判定：'integer[]' 的 base 是 integer，直接查 _FAMILY 会得到
    # 'int' 家族，从而漏掉 array 分支（实测 integer[] 曾因此返回未识别 → target_type=None）。
    fam = "array" if t.get("array") else _FAMILY.get(t["base"], t["base"])
    # JSON / JSONB 家族
    if fam == "json":
        if tgt in ("mysql", "mariadb"):
            return ("JSON", "ok", "")
        if tgt in ("postgresql", "kingbase"):
            # 源若是 JSONB → JSONB；源若是 JSON（MySQL/SQL Server/Oracle）→ JSON
            return ("JSONB" if t["base"] == "jsonb" else "JSON", "ok", "")
        if tgt in ("oracle", "dameng"):
            return ("CLOB", "warn", "JSON → CLOB（目标无原生 JSON，字符串承载）")
        if tgt == "sqlserver":
            return ("NVARCHAR(MAX)", "warn", "JSON → NVARCHAR(MAX)（SQL Server 无原生 JSON）")
        return ("NVARCHAR(MAX)", "warn", "JSON → 字符串承载")
    if fam in ("enum", "set"):
        if tgt in ("mysql", "mariadb"):
            # 同构 MySQL/MariaDB：保留 ENUM('a','b','c') / SET('x','y','z') 完整写法
            # （枚举值列表不可丢失；丢失会导致 MySQL 语法错误或应用层枚举值识别失败）
            raw = t.get("raw_inner") or ""
            if raw:
                return (f"{t['base'].upper()}({raw})", "ok",
                        "ENUM/SET 同库原写法透传")
            return ("VARCHAR(128)", "warn",
                    "ENUM/SET 枚举值列表缺失，VARCHAR 兜底（建议人工补全）")
        if tgt == "oracle":
            # Oracle 无 ENUM/SET：按最长枚举值长度落 VARCHAR2（取值约束由应用层
            # 保证）。此前判 fail 会整条链路阻断，实际 VARCHAR2(n) 即可承载。
            n = _enum_value_max_len(t.get("raw_inner") or "") or 255
            return (f"VARCHAR2({n})", "warn",
                    f"Oracle 无 ENUM/SET：按 VARCHAR2({n}) 承载（长度取最长枚举值，"
                    "枚举约束与应用层校验需人工确认）")
        return ("VARCHAR(128)", "warn", "ENUM/SET → VARCHAR（枚举约束丢失，应用层校验）")
    if fam == "bit":
        p = t.get("prec") or 1
        if tgt in ("oracle", "dameng"):
            return (f"NUMBER({max(p, 2)},0)", "warn", f"BIT({p}) → NUMBER（DTS 规则）")
        if tgt in ("postgresql", "kingbase"):
            return ("SMALLINT", "warn",
                    "BIT → SMALLINT（整型 0/1 承载，规避位串类型绑定兼容问题）")
        if tgt == "sqlserver":
            return ("BIT", "ok", "SQL Server BIT 单位长度")
        # MySQL/MariaDB BIT 必须保留 (n) 长度，否则默认 BIT(1) 写 8 位字节会 1406 超长
        return (f"BIT({p})", "ok", f"BIT({p}) 长度保留")
    if fam == "bool":
        if tgt in ("oracle", "dameng"):
            return ("NUMBER(1,0)", "warn", "BOOLEAN → NUMBER(1,0)（0/1 承载）")
        if tgt in ("mysql", "mariadb"):
            return ("TINYINT(1)", "ok", "BOOLEAN 即 TINYINT(1)")
        if tgt == "sqlserver":
            return ("BIT", "ok", "")
        return ("BOOLEAN", "ok", "")
    if fam == "uuid":
        if tgt in ("postgresql", "kingbase"):
            return ("UUID", "ok", "")
        if tgt == "sqlserver":
            return ("UNIQUEIDENTIFIER", "ok", "")
        return ("CHAR(36)" if tgt not in ("oracle",) else "VARCHAR2(36)",
                "warn", "UUID → 字符串承载（目标无原生 UUID）")
    if fam == "geometry":
        # 空间类型跨库：目标库需具备空间能力（PG 需 PostGIS、达梦需 DMGEO、
        # Oracle 需 Spatial），且 SRID / 坐标系 / 几何存储结构并非直接兼容，
        # 统一按"需人工确认"的 warn 处理（此前 fail 会整条链路阻断）。
        if tgt in ("mysql", "mariadb"):
            return ("GEOMETRY", "warn",
                    "MySQL 空间类型内置（无需扩展）；SRID 与几何结构（WKB）需人工确认")
        if tgt == "oracle":
            return ("MDSYS.SDO_GEOMETRY", "warn",
                    "Oracle 需 Spatial 组件：WKT/WKB → MDSYS.SDO_GEOMETRY 结构转换"
                    "与 SRID 需人工确认")
        if tgt in ("postgresql", "kingbase"):
            return ("GEOMETRY", "warn",
                    "PG/金仓需 PostGIS 扩展（缺失时降 TEXT）："
                    "请先执行 CREATE EXTENSION IF NOT EXISTS postgis;，SRID 需人工确认")
        if tgt == "sqlserver":
            # 只有 geography 是地理坐标系类型；geometry / st_geometry / sdo_geometry
            # 等均为平面几何 → GEOMETRY（否则达梦 ST_GEOMETRY 会被误判为 GEOGRAPHY）
            return ("GEOGRAPHY" if t["base"] == "geography" else "GEOMETRY", "warn",
                    "SQL Server 空间类型内置（无需扩展）；"
                    "SRID（geometry 默认 0）与几何结构需人工确认")
        if tgt == "dameng":
            return ("ST_GEOMETRY", "warn",
                    "达梦需 DMGEO 包（ST_GEOMETRY）；SRID 与几何结构需人工确认")
        return ("CLOB", "warn",
                "目标库无对应空间类型：按 WKT 文本承载（应用层转几何，需人工确认）")
    # PG 数组家族：跨库一律转 JSON 字符串承载（数组语义跨库不通用）
    if fam == "array":
        if tgt in ("mysql", "mariadb"):
            return ("JSON", "warn", "PG 数组 → MySQL JSON（数组结构由应用层适配）")
        if tgt in ("postgresql", "kingbase"):
            return (t["base"].upper(), "ok", "同库原样保留")
        return ("CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(MAX)",
                "warn", "数组 → 字符串承载（应用层解析）")
    # PG 范围家族（int4/8range, numrange, daterange, tsrange, tstzrange）
    if fam == "range":
        if tgt in ("postgresql", "kingbase"):
            return (t["base"].upper(), "ok", "同库原样保留")
        return ("VARCHAR(64)" if tgt in ("mysql", "mariadb") else
                "CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(MAX)",
                "warn", "PG 范围类型 → 字符串承载（应用层解析 '[a,b)' 文本）")
    # PG 全文搜索 / 扩展类型（TSVECTOR/TSQUERY/HSTORE/CITEXT/LTREE/CUBE）
    if t["base"] in ("tsvector", "tsquery", "hstore", "citext",
                     "ltree", "cube", "jsonpath"):
        if tgt in ("postgresql", "kingbase"):
            return (t["base"].upper(), "ok", "同库原样保留")
        return ("TEXT" if tgt in ("mysql", "mariadb", "postgresql", "kingbase")
                else "CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(MAX)",
                "warn", f"{t['base'].upper()} → 字符串承载（应用层处理）")
    # PG 网络地址 / MAC
    if t["base"] in ("inet", "cidr", "macaddr", "macaddr8"):
        if tgt in ("postgresql", "kingbase"):
            return (t["base"].upper(), "ok", "同库原样保留")
        return ("VARCHAR(45)" if tgt in ("mysql", "mariadb") else
                "VARCHAR2(45)" if tgt in ("oracle",) else
                "NVARCHAR(45)" if tgt == "sqlserver" else "VARCHAR(45)",
                "warn", "网络地址 → 字符串承载")
    # Oracle 特有
    if t["base"] in ("urowid", "rowid"):
        return ("VARCHAR2(18)" if tgt in ("oracle", "dameng") else
                "VARCHAR(40)" if tgt in ("mysql", "mariadb") else
                "VARCHAR(40)" if tgt in ("postgresql", "kingbase") else
                "NVARCHAR(40)", "warn", "ROWID/UROWID → 字符串承载")
    if t["base"] in ("bfile",):
        return ("VARCHAR(1024)" if tgt not in ("oracle", "dameng") else "BFILE",
                "warn", "BFILE（外部文件） → 路径字符串承载")
    if t["base"] in ("long",):
        # Oracle 已弃用 LONG，跨库一律转 CLOB/TEXT
        return ("CLOB" if tgt in ("oracle", "dameng") else
                "TEXT" if tgt in ("mysql", "mariadb", "postgresql", "kingbase")
                else "NVARCHAR(MAX)", "warn", "LONG → 大文本")
    if t["base"] in ("long raw",):
        return ("BLOB" if tgt in ("oracle", "dameng", "mysql", "mariadb")
                else "BYTEA" if tgt in ("postgresql", "kingbase") else
                "VARBINARY(MAX)", "warn", "LONG RAW → 二进制承载")
    if t["base"] in ("anydata", "anytype", "anydataset", "ref"):
        return ("CLOB" if tgt in ("oracle", "dameng") else
                "TEXT" if tgt in ("mysql", "mariadb", "postgresql", "kingbase")
                else "NVARCHAR(MAX)",
                "warn", "Oracle ANY 族/REF → 字符串承载（异构需对象类型适配）")
    if t["base"] in ("xmltype", "xml"):
        return ("XMLTYPE" if tgt == "oracle" else
                "CLOB" if tgt == "dameng" else
                "XML" if tgt in ("postgresql", "kingbase", "sqlserver") else
                "TEXT", "ok" if tgt in ("oracle", "postgresql", "kingbase", "sqlserver")
                else "warn", "XMLType/XML 跨库")
    # SQL Server 特有
    if t["base"] in ("money", "smallmoney"):
        # MONEY/SMALLMONEY 固定 4 位小数：跨库带精度落 DECIMAL/NUMBER/NUMERIC，
        # 同库保形（否则会被 decimal 家族退成无精度 DECIMAL）
        p, s = (19, 4) if t["base"] == "money" else (10, 4)
        if tgt == "sqlserver":
            return (t["base"].upper(), "ok", "同库原样保留")
        if tgt in ("oracle", "dameng"):
            return (f"NUMBER({p},{s})", "ok", f"{t['base'].upper()} → NUMBER({p},{s})")
        if tgt in ("postgresql", "kingbase"):
            return (f"NUMERIC({p},{s})", "ok", f"{t['base'].upper()} → NUMERIC({p},{s})")
        return (f"DECIMAL({p},{s})", "ok", f"{t['base'].upper()} → DECIMAL({p},{s})")
    if t["base"] == "hierarchyid":
        return ("HIERARCHYID" if tgt == "sqlserver" else
                "TEXT" if tgt in ("mysql", "mariadb") else
                "VARCHAR(4000)" if tgt in ("postgresql", "kingbase") else
                "CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(4000)",
                "warn", "HIERARCHYID → 字符串承载")
    if t["base"] == "sql_variant":
        return ("SQL_VARIANT" if tgt == "sqlserver" else
                "TEXT" if tgt in ("mysql", "mariadb") else
                "TEXT" if tgt in ("postgresql", "kingbase") else
                "CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(MAX)",
                "warn", "SQL_VARIANT → 字符串承载（类型在同步中无法保持）")
    if t["base"] == "vector":
        return ("VECTOR" if tgt == "sqlserver" else
                "TEXT" if tgt in ("mysql", "mariadb") else
                "TEXT" if tgt in ("postgresql", "kingbase") else
                "CLOB" if tgt in ("oracle", "dameng") else "NVARCHAR(MAX)",
                "warn", "VECTOR 类型跨库保形困难 → 字符串承载")
    # ROWVERSION（SQL Server 8 字节二进制，跨库用 BLOB/BYTEA）
    if t["base"] in ("rowversion",):
        return ("ROWVERSION" if tgt == "sqlserver" else
                "BLOB" if tgt in ("mysql", "mariadb", "dameng") else
                "BYTEA" if tgt in ("postgresql", "kingbase") else
                "RAW(8)" if tgt == "oracle" else "VARBINARY(8)",
                "ok", "ROWVERSION → 二进制承载")
    return (None, "warn", f"未识别类型 {t['base']} → 按字符串承载（需人工确认）")


def map_type(src_db: str, tgt_db: str, type_str) -> Dict[str, Any]:
    """源类型 → 目标库型映射判定。

    返回 {source, target_type, level: ok|warn|fail, reason}
    target_type 为 None 表示目标不支持。
    """
    t = parse_type(type_str)
    # SQL Server 的 TIMESTAMP 是 ROWVERSION 的别名（8 字节二进制），与其它库的
    # 时间戳语义完全不同，必须按源库判定。
    if (src_db or "").lower() == "sqlserver" and t["base"] == "timestamp":
        t["base"] = "rowversion"
    # 数组修饰优先：'integer[]' 应走 array 家族而非 int 家族
    fam = "array" if t.get("array") else _FAMILY.get(t["base"])
    tgt = (tgt_db or "").lower()
    if fam is None:
        return {"source": type_str, "target_type": None, "level": "warn",
                "reason": f"自定义/未识别类型 '{t['base']}' → 建议按文本承载，需人工确认映射"}
    try:
        if (t.get("array") or t["base"] in _SPECIAL_BASES
                or fam in ("json", "enum", "set", "bit", "bool", "uuid",
                           "geometry", "array", "range")):
            # 偏门/特殊类型优先走精细策略，避免被粗家族（text/blob）吞掉语义
            tt, lvl, reason = _special_target(tgt, t)
        elif fam == "int":
            tt, lvl, reason = _int_target(tgt, t)
        elif fam == "number":
            # Oracle/达梦 NUMBER：按精度区分整型/小数
            if t["scale"] in (None, 0) and (t["prec"] or 38) <= 19:
                tt, lvl, reason = _int_target(tgt, {"base": "bigint" if (t["prec"] or 0) > 9 else "int",
                                                    "unsigned": False, "prec": None, "scale": None})
            else:
                tt, lvl, reason = _decimal_target(tgt, t)
        elif fam in ("decimal",):
            tt, lvl, reason = _decimal_target(tgt, t)
        elif fam == "float":
            tt = "BINARY_DOUBLE" if tgt == "oracle" else ("DOUBLE PRECISION" if tgt in ("postgresql", "kingbase") else "DOUBLE")
            lvl, reason = "ok", ""
        elif fam in ("char", "varchar", "text"):
            tt, lvl, reason = _char_target(tgt, t)
        elif fam == "blob":
            tt, lvl, reason = _blob_target(tgt, t)
        elif fam in ("date", "datetime", "timestamp", "time", "year", "timestamptz",
                     "interval"):
            tt, lvl, reason = _datetime_target(tgt, t)
        else:
            tt, lvl, reason = _special_target(tgt, t)
    except Exception as e:                      # 矩阵未覆盖 → 人工确认
        return {"source": type_str, "target_type": None, "level": "warn",
                "reason": f"映射规则异常({e})，需人工确认"}
    return {"source": type_str, "target_type": tt, "level": lvl, "reason": reason}


# ---------------------------------------------------------------------------
# precheck 集成入口
# ---------------------------------------------------------------------------
def check_column_mapping(src_db: str, tgt_db: str,
                         src_cols: list, tgt_cols: list,
                         field_ide: str = "origin") -> Dict[str, Any]:
    """逐列生成映射报告（预校验/迁移前评审用）。

    src_cols/tgt_cols: [(列名, 类型字符串), ...]
    返回 {summary, columns:[{column, source_type, target_type, level, reason, action}]}
    """
    from core.sync.precheck import _norm_name   # 复用命名归一
    tgt_map = {_norm_name(c[0], field_ide).upper(): c[1] for c in tgt_cols}
    columns = []
    n_fail = n_warn = 0
    for name, stype in src_cols:
        key = _norm_name(name, field_ide).upper()
        r = map_type(src_db, tgt_db, stype)
        ttype = tgt_map.get(key)
        row = {"column": name, "source_type": stype,
               "target_type": r["target_type"], "level": r["level"],
               "reason": r["reason"]}
        if key not in tgt_map:
            row.update({"level": "warn",
                        "reason": "目标表无此列（overwrite 自动建表将采用建议类型）"})
            n_warn += 1
        elif ttype is not None:
            row["actual_target_type"] = ttype
            # 实际目标类型 vs 建议类型的一致性提示
            if r["level"] == "fail":
                n_fail += 1
            elif r["level"] == "warn":
                n_warn += 1
        columns.append(row)
    return {"summary": {"total": len(columns), "fail": n_fail, "warn": n_warn},
            "columns": columns}
