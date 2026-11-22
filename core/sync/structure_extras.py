# -*- coding: utf-8 -*-
"""表级完整性对象迁移（对标 DTS/KDTS 的结构完整性补齐）。

现有同步引擎只通过 ``ColumnMeta`` 还原列类型/长度/主键/自增，索引、唯一约束、
外键、CHECK 一律丢失。本模块独立承担「抽取 + DDL 生成 + 落地」三件事，不侵入
五个插件的建表逻辑（避免改动 ``_create_table_sql`` 带来的大范围回归）。

设计约束：
- 全变量化：上限、是否启用、表名/schema 全部由调用方传入，不写死业务值；
- 逐对象容错：单个索引/约束失败只记录不中断，保证数据迁移主结论不受影响；
- 诚实边界：不支持的库型返回 ``supported=False`` 和明确原因，绝不静默跳过。
"""
import re
from typing import Any, Dict, List, Optional

MYSQL_FAMILY = ("mysql", "mariadb")
PG_FAMILY = ("postgresql", "kingbase", "opengauss", "panwei")
ORA_FAMILY = ("oracle", "dameng")
MSSQL_FAMILY = ("sqlserver",)

DEFAULT_MAX_OBJECTS = 200


def _q(name: str, db_type: str) -> str:
    """按方言引用标识符。"""
    n = str(name or "").strip()
    if db_type in MYSQL_FAMILY:
        return "`" + n.replace("`", "``") + "`"
    return '"' + n.replace('"', '""') + '"'


def _fetchall(conn: Any, sql: str, params: tuple, style: str) -> list:
    cur = conn.cursor()
    try:
        if style == "qmark":
            cur.execute(sql.replace("?", "?"), params)
        elif style == "named":
            cur.execute(sql, {"p1": params[0], "p2": params[1]} if len(params) == 2
                        else {"p1": params[0]})
        else:
            cur.execute(sql, params)
        return list(cur.fetchall())
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _group_columns(rows: list, name_idx: int, col_idx: int,
                   extra_idx: Optional[int] = None) -> Dict[str, list]:
    grouped: Dict[str, list] = {}
    for r in rows:
        name = str(r[name_idx] or "")
        if not name:
            continue
        item = r[col_idx]
        if extra_idx is not None:
            item = (item, r[extra_idx])
        grouped.setdefault(name, []).append(item)
    return grouped


def _extract_mysql(conn, table, schema, max_objects) -> dict:
    out: Dict[str, Any] = {"indexes": [], "constraints": [],
                           "warnings": [], "supported": True}
    db = schema or ""
    idx_rows = _fetchall(conn, """
        SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
    """, (db, table), "format")
    by_idx: Dict[str, list] = {}
    uniq: Dict[str, bool] = {}
    for r in idx_rows:
        name = str(r[0] or "")
        if not name or name.upper() == "PRIMARY":
            continue
        by_idx.setdefault(name, []).append(str(r[2]))
        uniq[name] = (int(r[1] or 0) == 0)
    fk_rows = _fetchall(conn, """
        SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
               REFERENCED_COLUMN_NAME, ORDINAL_POSITION
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
    """, (db, table), "format")
    fk_map: Dict[str, dict] = {}
    for r in fk_rows:
        name = str(r[0] or "")
        e = fk_map.setdefault(name, {"name": name, "kind": "foreign",
                                     "columns": [], "ref_table": str(r[2] or ""),
                                     "ref_columns": []})
        e["columns"].append(str(r[1]))
        e["ref_columns"].append(str(r[3]))
    out["constraints"].extend(list(fk_map.values())[:max_objects])
    # MySQL 会为外键自动建同名支撑索引；目标端 ADD FOREIGN KEY 同样自动建，
    # 若再单独 CREATE INDEX 就是重复对象，必须跳过
    for name, cols in list(by_idx.items())[:max_objects]:
        if name in fk_map:
            continue
        out["indexes"].append({"name": name, "columns": cols,
                               "unique": uniq.get(name, False)})

    try:
        ck_rows = _fetchall(conn, """
            SELECT k.CONSTRAINT_NAME, c.CHECK_CLAUSE
            FROM information_schema.TABLE_CONSTRAINTS k
            JOIN information_schema.CHECK_CONSTRAINTS c
              ON c.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
             AND c.CONSTRAINT_NAME = k.CONSTRAINT_NAME
            WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME = %s
              AND k.CONSTRAINT_TYPE = 'CHECK'
        """, (db, table), "format")
        for r in ck_rows[:max_objects]:
            out["constraints"].append({"name": str(r[0]), "kind": "check",
                                       "check_clause": str(r[1] or "")})
    except Exception as e:
        # MySQL 5.7 无 CHECK_CONSTRAINTS 视图；不是缺陷，如实提示即可
        out["warnings"].append("CHECK 约束无法从当前 MySQL 版本抽取: %s" % str(e)[:160])
    return out


def _extract_pg(conn, table, schema, max_objects) -> dict:
    out: Dict[str, Any] = {"indexes": [], "constraints": [],
                           "warnings": [], "supported": True}
    sch = schema or "public"
    con_rows = _fetchall(conn, """
        SELECT conname, contype, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = (quote_ident(%s) || '.' || quote_ident(%s))::regclass
          AND contype IN ('u', 'f', 'c')
        ORDER BY conname
    """, (sch, table), "format")
    kind_map = {"u": "unique", "f": "foreign", "c": "check"}
    for r in con_rows[:max_objects]:
        out["constraints"].append({"name": str(r[0]),
                                   "kind": kind_map.get(str(r[1]), str(r[1])),
                                   "definition": str(r[2] or "")})
    idx_rows = _fetchall(conn, """
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
        ORDER BY indexname
    """, (sch, table), "format")
    con_names = {c["name"] for c in out["constraints"]}
    # 主键索引由 ColumnMeta.is_primary 在建表时已还原，此处必须排除，
    # 否则目标端重复建主键索引（且名字常与源端约束名冲突）
    try:
        pk_rows = _fetchall(conn, """
            SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
            WHERE x.indrelid = (quote_ident(%s) || '.' || quote_ident(%s))::regclass
              AND x.indisprimary
        """, (sch, table), "format")
        con_names.update(str(r[0]) for r in pk_rows if r and r[0])
    except Exception as e:
        out["warnings"].append("主键索引清单获取失败: %s" % str(e)[:160])
    for r in idx_rows:
        name = str(r[0] or "")
        definition = str(r[1] or "")
        if not name or name in con_names or "PRIMARY KEY" in definition.upper():
            continue
        if not re.match(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", definition, re.I):
            continue
        if len(out["indexes"]) >= max_objects:
            break
        out["indexes"].append({"name": name, "definition": definition})
    return out


def _extract_ora(conn, table, schema, max_objects) -> dict:
    out: Dict[str, Any] = {"indexes": [], "constraints": [],
                           "warnings": [], "supported": True}
    owner = (schema or "").upper()
    tbl = (table or "").upper()
    style = "named"
    params = (owner, tbl) if owner else (tbl,)
    where_owner = "AND c.owner = :p1" if owner else ""
    where_idx_owner = "AND i.table_owner = :p1" if owner else ""
    if style == "named":
        where_owner = "AND c.owner = :p1"
        where_idx_owner = "AND i.table_owner = :p1"
    sql_con = """
        SELECT c.constraint_name, c.constraint_type
        FROM all_constraints c
        WHERE c.table_name = :p2 %s
          AND c.constraint_type IN ('U', 'R', 'C')
        ORDER BY c.constraint_name
    """ % where_owner
    # 无 owner 时表名是唯一占位符，改用 :p1
    if not owner:
        sql_con = sql_con.replace(":p2", ":p1")
    rows = _fetchall(conn, sql_con, params, style)
    kinds = {"U": "unique", "R": "foreign", "C": "check"}
    for r in rows[:max_objects]:
        out["constraints"].append({"name": str(r[0]),
                                   "kind": kinds.get(str(r[1]), str(r[1]))})
    sql_idx = """
        SELECT i.index_name, i.uniqueness
        FROM all_indexes i
        WHERE i.table_name = :p2 %s
          AND NOT EXISTS (SELECT 1 FROM all_constraints c
                          WHERE c.owner = i.owner AND c.index_name = i.index_name
                            AND c.constraint_type = 'P')
        ORDER BY i.index_name
    """ % where_idx_owner
    if not owner:
        sql_idx = sql_idx.replace(":p2", ":p1")
    idx_rows = _fetchall(conn, sql_idx, params, style)
    for r in idx_rows[:max_objects]:
        out["indexes"].append({"name": str(r[0]), "unique": str(r[1] or "").upper() == "UNIQUE"})
    out["warnings"].append(
        "Oracle/达梦仅抽取对象清单；约束列/表达式需目标端 DDL 复核")
    return out


def extract_structure(conn: Any, db_type: str, table: str,
                      schema: Optional[str] = None,
                      max_objects: int = DEFAULT_MAX_OBJECTS) -> dict:
    """从源连接抽取表级索引/约束对象；失败只回 warnings，不抛异常。"""
    dt = (db_type or "").lower()
    try:
        limit = max(1, min(int(max_objects or 0) or DEFAULT_MAX_OBJECTS, 2000))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_OBJECTS
    try:
        if dt in MYSQL_FAMILY:
            return _extract_mysql(conn, table, schema, limit)
        if dt in PG_FAMILY:
            return _extract_pg(conn, table, schema, limit)
        if dt in ORA_FAMILY:
            return _extract_ora(conn, table, schema, limit)
        return {"indexes": [], "constraints": [], "supported": False,
                "warnings": ["结构对象抽取暂不支持库型: %s" % (dt or "未知")]}
    except Exception as e:
        return {"indexes": [], "constraints": [], "supported": False,
                "warnings": ["结构对象抽取失败: %s" % str(e)[:240]]}


def build_ddl(db_type: str, table: str, extras: dict,
              name_prefix: Optional[str] = None) -> List[str]:
    """为目标库生成索引/约束 DDL；前缀用于规避跨表同名对象冲突。"""
    if not extras or not extras.get("supported", True):
        return []
    dt = (db_type or "").lower()
    prefix = re.sub(r"[^0-9A-Za-z_]", "_", str(name_prefix or table or "idx"))[:24]
    qt = _q(table, dt)
    ddl: List[str] = []
    for idx in extras.get("indexes") or []:
        name = "%s_%s" % (prefix, str(idx.get("name") or "idx"))
        uniq = "UNIQUE " if idx.get("unique") else ""
        if idx.get("definition") and dt in PG_FAMILY:
            # PG 索引定义含方法/表达式/偏索引，直接复用并替换归属表
            ident = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
            stmt = re.sub(r"(\bON\s+)" + ident + r"(?:\." + ident + r")?",
                          lambda m: m.group(1) + qt, idx["definition"],
                          count=1, flags=re.I)
            stmt = re.sub(r"^(\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+)\S+",
                          r"\g<1>" + _q(name, dt), stmt, count=1, flags=re.I)
            ddl.append(stmt.rstrip().rstrip(";"))
        elif idx.get("columns"):
            cols = ", ".join(_q(c, dt) for c in idx["columns"])
            ddl.append("CREATE %sINDEX %s ON %s (%s)" %
                       (uniq, _q(name, dt), qt, cols))
    for con in extras.get("constraints") or []:
        name = _q("%s_%s" % (prefix, str(con.get("name") or "con")), dt)
        kind = con.get("kind")
        if con.get("definition") and dt in PG_FAMILY:
            ddl.append("ALTER TABLE %s ADD CONSTRAINT %s %s" %
                       (qt, name, con["definition"]))
        elif kind == "unique" and con.get("columns"):
            cols = ", ".join(_q(c, dt) for c in con["columns"])
            ddl.append("ALTER TABLE %s ADD CONSTRAINT %s UNIQUE (%s)" %
                       (qt, name, cols))
        elif kind == "foreign" and con.get("columns") and con.get("ref_table"):
            cols = ", ".join(_q(c, dt) for c in con["columns"])
            rcols = ", ".join(_q(c, dt) for c in (con.get("ref_columns") or []))
            stmt = "ALTER TABLE %s ADD CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s)" % (
                qt, name, cols, _q(con["ref_table"], dt), rcols or cols)
            for opt in ("on_delete", "on_update"):
                if con.get(opt):
                    stmt += " %s %s" % (opt.replace("_", " ").upper(), con[opt])
            ddl.append(stmt)
        elif kind == "check" and con.get("check_clause"):
            ddl.append("ALTER TABLE %s ADD CONSTRAINT %s CHECK (%s)" %
                       (qt, name, con["check_clause"]))
    return ddl


def apply_structure(conn: Any, db_type: str, table: str, extras: dict,
                    name_prefix: Optional[str] = None) -> dict:
    """逐条执行 DDL；单条失败仅记录，不回滚已成功对象、不影响数据迁移结论。"""
    result: Dict[str, Any] = {"created": [], "failed": [], "skipped": 0,
                              "warnings": list(extras.get("warnings") or []),
                              "supported": bool(extras.get("supported", True))}
    stmts = build_ddl(db_type, table, extras, name_prefix)
    result["total"] = len(stmts)
    for stmt in stmts:
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(stmt)
            result["created"].append(stmt[:240])
        except Exception as e:
            result["failed"].append({"ddl": stmt[:240], "error": str(e)[:240]})
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
    try:
        conn.commit()
    except Exception as e:
        result["failed"].append({"ddl": "COMMIT", "error": str(e)[:240]})
    return result
