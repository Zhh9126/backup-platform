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


def parse_type(type_str) -> Dict[str, Any]:
    """解析列类型字符串 → {base, prec, scale, unsigned}。

    示例：
      'BIGINT UNSIGNED'      -> base=bigint, unsigned=True
      'DECIMAL(20,4)'        -> base=decimal, prec=20, scale=4
      "ENUM('a','b')"        -> base=enum（枚举值列表剥离）
      'VARCHAR2(255 CHAR)'   -> base=varchar2, prec=255
      'TIMESTAMP(6)'         -> base=timestamp, prec=6
    """
    if type_str is not None and not isinstance(type_str, str):
        type_str = str(type_str)          # JDBC java.lang.String 归一
    s = (type_str or "").strip().lower()
    unsigned = "unsigned" in s
    s = s.replace("unsigned", "").replace("signed", "").replace("zerofill", "").strip()
    # 基类型词（enum('a','b')/set('x') 的括号值列表随词干剥离）
    m = re.match(r"^\s*([a-zA-Z_]+)\s*(?:\(([^)]*)\))?", s)
    if not m:
        return {"base": s, "prec": None, "scale": None, "unsigned": unsigned}
    base = re.sub(r"\s+", " ", m.group(1).strip())
    inner = (m.group(2) or "").strip()
    prec = scale = None
    digits = re.match(r"^\s*(\d+)\s*(?:,\s*(\d+))?\s*$", inner)
    if digits:
        prec = int(digits.group(1))
        scale = int(digits.group(2)) if digits.group(2) else None
    elif inner and re.match(r"^\s*\d+\s+(char|byte)\s*$", inner):
        prec = int(re.match(r"^\s*(\d+)", inner).group(1))   # '255 CHAR'
    return {"base": base, "prec": prec, "scale": scale, "unsigned": unsigned}


# 家族归一：不同库型的同义类型 → 规范名
_FAMILY = {
    # 整数（宽度，bit）
    "tinyint": "int", "smallint": "int", "mediumint": "int",
    "int": "int", "integer": "int", "bigint": "int",
    "number": "number",            # Oracle/达梦 NUMBER(38,0) 可表达一切整数
    "smallint identity": "int",
    # 浮点/精确小数
    "decimal": "decimal", "numeric": "decimal", "dec": "decimal",
    "float": "float", "double": "float", "real": "float",
    "double precision": "float", "binary_float": "float", "binary_double": "float",
    # 定长/变长字符
    "char": "char", "varchar": "varchar", "varchar2": "varchar",
    "nchar": "char", "nvarchar": "varchar", "nvarchar2": "varchar",
    "character varying": "varchar", "character": "char",
    # 大文本
    "text": "text", "tinytext": "text", "mediumtext": "text", "longtext": "text",
    "clob": "text", "nclob": "text", "longtext": "text",
    # 二进制
    "blob": "blob", "tinyblob": "blob", "mediumblob": "blob", "longblob": "blob",
    "bytea": "blob", "raw": "blob", "long raw": "blob", "binary": "blob",
    "varbinary": "blob", "image": "blob", "bfile": "blob",
    # 时间
    "date": "date", "datetime": "datetime", "timestamp": "timestamp",
    "timestamptz": "timestamptz", "time": "time", "year": "year",
    "timestamp with time zone": "timestamptz",
    "timestamp with local time zone": "timestamp",
    "interval": "interval",
    # 特殊
    "json": "json", "jsonb": "json",
    "enum": "enum", "set": "set",
    "bit": "bit", "bool": "bool", "boolean": "bool",
    "uuid": "uuid", "geometry": "geometry", "point": "geometry",
    "linestring": "geometry", "polygon": "geometry",
    "money": "decimal",
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
        return ("BIGINT" if tgt in ("mysql", "mariadb") else "NUMBER(10,0)",
                "warn", "INT UNSIGNED → BIGINT/NUMBER(10,0)（无符号 32 位超有符号范围）")
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
        return {"tinyint": ("SMALLINT", "ok", "TINYINT → SMALLINT"),
                "smallint": ("SMALLINT", "ok", ""),
                "mediumint": ("INTEGER", "ok", ""),
                "int": ("INTEGER", "ok", ""),
                "bigint": ("BIGINT", "ok", "")}[base]
    # mysql/mariadb/sqlserver
    return (base.upper(), "ok", "")


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
        return (None, "fail", "Oracle 无 TIME 类型（DTS 规则：MySQL TIME→Oracle 不支持）")
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
    return (base.upper(), "ok", "")


def _char_target(tgt: str, t: dict):
    base, p = t["base"], t["prec"]
    if base in ("char", "nchar"):
        if tgt in ("oracle", "dameng") and p is None:
            return ("CHAR(1)", "warn", "CHAR 长度缺失 → CHAR(1)（DTS 规则）")
        return ("CHAR" + (f"({p})" if p else ""), "ok", "")
    if base == "varchar":
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


def _special_target(tgt: str, t: dict):
    """特殊类型：JSON / ENUM / SET / BIT / BOOL / UUID / geometry。"""
    fam = _FAMILY.get(t["base"], t["base"])
    if fam == "json":
        if tgt in ("mysql", "mariadb", "postgresql", "kingbase"):
            return ("JSON", "ok", "")
        if tgt in ("oracle", "dameng"):
            return ("CLOB", "warn", "JSON → CLOB（目标无原生 JSON，字符串承载）")
        return ("NVARCHAR(MAX)", "warn", "JSON → 字符串承载")
    if fam in ("enum", "set"):
        if tgt in ("mysql", "mariadb"):
            return (t["base"].upper(), "ok", "")
        if tgt in ("oracle",):
            return (None, "fail", "Oracle 不支持 ENUM/SET（DTS 规则）→ 建议 VARCHAR/CLOB")
        return ("VARCHAR(128)", "warn", "ENUM/SET → VARCHAR（枚举约束丢失，应用层校验）")
    if fam == "bit":
        if tgt in ("oracle", "dameng"):
            p = t["prec"] or 1
            return (f"NUMBER({max(p, 2)},0)", "warn", f"BIT({p}) → NUMBER（DTS 规则）")
        return ("BIT", "ok", "")
    if fam == "bool":
        if tgt in ("oracle", "dameng"):
            return ("NUMBER(1,0)", "warn", "BOOLEAN → NUMBER(1,0)（0/1 承载）")
        if tgt in ("mysql", "mariadb"):
            return ("TINYINT(1)", "ok", "BOOLEAN 即 TINYINT(1)")
        return ("BOOLEAN", "ok", "")
    if fam == "uuid":
        if tgt in ("postgresql", "kingbase"):
            return ("UUID", "ok", "")
        return ("CHAR(36)" if tgt not in ("oracle",) else "VARCHAR2(36)",
                "warn", "UUID → 字符串承载（目标无原生 UUID）")
    if fam == "geometry":
        if tgt in ("mysql", "mariadb"):
            return ("GEOMETRY", "ok", "")
        if tgt in ("oracle",):
            return ("MDSYS.SDO_GEOMETRY", "warn", "空间类型需 SDO 结构转换，非直接迁移")
        return (None, "fail", "目标库无对应空间类型（DTS：Oracle 目标空间类型不支持）")
    return (None, "warn", f"未识别类型 {t['base']} → 按字符串承载（需人工确认）")


def map_type(src_db: str, tgt_db: str, type_str) -> Dict[str, Any]:
    """源类型 → 目标库型映射判定。

    返回 {source, target_type, level: ok|warn|fail, reason}
    target_type 为 None 表示目标不支持。
    """
    t = parse_type(type_str)
    fam = _FAMILY.get(t["base"])
    tgt = (tgt_db or "").lower()
    if fam is None:
        return {"source": type_str, "target_type": None, "level": "warn",
                "reason": f"自定义/未识别类型 '{t['base']}' → 建议按文本承载，需人工确认映射"}
    try:
        if fam == "int":
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
