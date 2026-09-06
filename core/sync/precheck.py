"""迁移/同步前预校验（对标阿里 DTS 预检查、AWS DMS 迁移前评估）。

检查项：
1. 源/目标连通性
2. 源表存在性 / 目标表存在性（overwrite 模式目标表可不存在，将自动重建）
3. 列兼容性（列数、列名匹配、类型兼容矩阵）——核心检查
4. 主键检查（upsert / realtime 需要）
5. 增量列检查（incremental / realtime 需要）

结论：任一 fail → 拒绝启动迁移；warn 允许继续但提示风险。
"""
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# 类型兼容矩阵（跨库通用：按"类别"归一）
# ---------------------------------------------------------------------------
_NUMERIC = {
    "int", "integer", "smallint", "bigint", "tinyint", "mediumint",
    "number", "decimal", "numeric", "float", "double", "real",
    "double precision", "bit", "money", "serial", "bigserial",
}
_CHAR = {
    "varchar", "varchar2", "char", "nchar", "nvarchar", "nvarchar2",
    "character varying", "character", "string", "text", "clob", "nclob",
    "longtext", "mediumtext", "tinytext", "longvarchar",
}
_DATE = {"date", "datetime", "timestamp", "timestamptz", "time",
         "time with time zone", "year", "interval"}
_BINARY = {"blob", "bytea", "binary", "varbinary", "image", "raw",
           "long raw", "bfile", "longblob", "mediumblob", "tinyblob"}

# 类别间兼容级别：ok / warn（有损或需隐式转换）/ fail
_COMPAT = {
    ("num", "num"): "ok",
    ("char", "char"): "ok",
    ("date", "date"): "ok",
    ("bin", "bin"): "ok",
    ("num", "char"): "warn",    # 数值→字符：隐式转换
    ("date", "char"): "warn",   # 日期→字符：格式化
    ("bin", "char"): "warn",
    ("char", "bin"): "warn",
    ("char", "num"): "fail",    # 字符→数值：数据可能非法
    ("date", "num"): "fail",
    ("bin", "num"): "fail",
    ("char", "date"): "warn",   # 取决于数据格式
    ("num", "date"): "fail",
    ("bin", "date"): "fail",
    ("date", "bin"): "fail",
    ("num", "bin"): "fail",
}


def _norm_type(t) -> str:
    """类型归一：去精度括号/unsigned/字符集修饰。"""
    if t is not None and not isinstance(t, str):
        t = str(t)  # JDBC 通道返回 java.lang.String
    t = (t or "").lower().strip()
    for sep in ("(", " "):
        if sep in t:
            t = t.split(sep, 1)[0]
    t = t.replace("unsigned", "").replace("signed", "").strip()
    return t or "unknown"


_SAFE_IDENT = None  # 延迟初始化（复用 data_compare 的白名单）


def _get_cols_typed(conn, db_type: str, database: str, schema: str,
                    table: str) -> List[tuple]:
    """取列 (名称, 数据类型) 列表（预校验专用，跨库型）。"""
    from core.data_compare import _SAFE_IDENT, _user_of
    cur = conn.cursor()
    db_type = (db_type or "").lower()
    try:
        if db_type in ("mysql", "mariadb"):
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (database, table))
        elif db_type in ("postgresql", "kingbase"):
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (schema or "public", table))
        elif db_type in ("oracle", "dameng"):
            owner = (schema or _user_of(conn) or "").upper()
            cur.execute(
                "SELECT column_name, data_type FROM all_tab_columns "
                f"WHERE owner = '{owner}' "
                f"AND table_name = '{table}' "
                "ORDER BY column_id")
        else:
            raise ValueError(f"暂不支持 {db_type} 取列类型")
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _category(t: str) -> str:
    t = _norm_type(t)
    if t in _NUMERIC:
        return "num"
    if t in _CHAR:
        return "char"
    if t in _DATE:
        return "date"
    if t in _BINARY:
        return "bin"
    return "char"          # 未识别类型按字符宽容处理


def _compat_level(src_t: str, dst_t: str) -> str:
    return _COMPAT.get((_category(src_t), _category(dst_t)), "warn")


def _norm_name(name, ide: str = "origin") -> str:
    if name is not None and not isinstance(name, str):
        name = str(name)  # JDBC 通道返回 java.lang.String
    name = name or ""
    if ide == "upper":
        return name.upper()
    if ide == "lower":
        return name.lower()
    return name


def _add(items: List[dict], check: str, status: str,
         message: str = "", detail: List[dict] = None):
    items.append({"check": check, "status": status,
                  "message": message, "detail": detail or []})


def run_precheck(cfg) -> Dict[str, Any]:
    """执行预校验。返回 {passed, fail, warn, items:[...]}。"""
    from core.data_compare import _get_columns, _get_pk_column, _table_ref
    from core.sync.plugins import registry
    items: List[dict] = []

    tables = list(cfg.source_tables_list or []) or (
        [cfg.source_table] if cfg.source_table else [])
    if not tables:
        _add(items, "table_list", "fail", "未指定要同步的表")
        return _summary(items)

    reader = registry.create_reader(cfg.src_db_type, cfg)
    writer = registry.create_writer(cfg.tgt_db_type, cfg)

    # ---- 1) 连通性 ----
    src_conn = tgt_conn = None
    try:
        src_conn = reader.connect()
        _add(items, "source_connectivity", "pass",
             f"源 {cfg.src_db_type} {cfg.src_host}:{cfg.src_port} 连接正常")
    except Exception as e:
        _add(items, "source_connectivity", "fail", f"源库连接失败: {e}")
        return _summary(items)
    try:
        tgt_conn = writer.connect()
        _add(items, "target_connectivity", "pass",
             f"目标 {cfg.tgt_db_type} {cfg.tgt_host}:{cfg.tgt_port} 连接正常")
    except Exception as e:
        _add(items, "target_connectivity", "fail", f"目标库连接失败: {e}")
        return _summary(items)

    overwrite = cfg.save_mode == "overwrite"
    create_if_ne = cfg.save_mode == "create_if_not_exists"

    def _tgt_table(t: str) -> str:
        return cfg.target_table if (len(tables) == 1 and cfg.target_table) else t

    # ---- 2) 表存在性 ----
    missing_src = []
    for t in tables:
        try:
            _table_ref(cfg.src_db_type, cfg.src_db_name, cfg.src_schema, t)
            if not _get_columns(src_conn, cfg.src_db_type, cfg.src_db_name,
                                cfg.src_schema, t):
                missing_src.append({"table": t, "error": "无列元数据"})
        except Exception as e:
            missing_src.append({"table": t, "error": str(e)})
    if missing_src:
        _add(items, "source_table_exists", "fail",
             "源表不存在或不可读", missing_src)
        return _summary(items)
    _add(items, "source_table_exists", "pass", f"{len(tables)} 张源表均可读")

    tgt_missing = []
    for t in tables:
        tt = _tgt_table(t)
        try:
            _table_ref(cfg.tgt_db_type, cfg.tgt_db_name, cfg.tgt_schema, tt)
            if not _get_columns(tgt_conn, cfg.tgt_db_type, cfg.tgt_db_name,
                                cfg.tgt_schema, tt):
                tgt_missing.append(tt)
        except Exception:
            tgt_missing.append(tt)
    if tgt_missing and not (overwrite or create_if_ne):
        _add(items, "target_table_exists", "fail",
             f"目标表不存在（当前模式 {cfg.save_mode} 要求目标表已存在，"
             f"或改用 overwrite 自动建表）", tgt_missing)
        return _summary(items)
    if tgt_missing:
        _add(items, "target_table_exists", "warn",
             f"{len(tgt_missing)} 张目标表不存在，overwrite 模式将自动建表")
    else:
        _add(items, "target_table_exists", "pass", "目标表均存在")

    # ---- 3) 列兼容性（核心）----
    col_detail = []
    col_fail = col_warn = 0
    if not overwrite:   # overwrite 重建表，列检查无意义
        for t in tables:
            tt = _tgt_table(t)
            try:
                src_cols = _get_cols_typed(src_conn, cfg.src_db_type,
                                           cfg.src_db_name, cfg.src_schema, t)
                tgt_cols = _get_cols_typed(tgt_conn, cfg.tgt_db_type,
                                           cfg.tgt_db_name, cfg.tgt_schema, tt)
            except Exception as e:
                col_detail.append({"table": t, "status": "fail",
                                   "message": f"读取列元数据失败: {e}"})
                col_fail += 1
                continue
            tgt_map = {_norm_name(c[0], cfg.field_ide).upper(): c
                       for c in tgt_cols}
            src_map = set()
            for c in src_cols:
                name, stype = c
                key = _norm_name(name, cfg.field_ide).upper()
                src_map.add(key)
                tc = tgt_map.get(key)
                if tc is None:
                    col_detail.append({"table": t, "column": name,
                                       "status": "fail",
                                       "message": "目标表缺少该列"})
                    col_fail += 1
                    continue
                lvl = _compat_level(stype, tc[1])
                if lvl == "fail":
                    col_detail.append({
                        "table": t, "column": name, "status": "fail",
                        "message": f"类型不兼容: 源 {stype} → 目标 {tc[1]}"})
                    col_fail += 1
                elif lvl == "warn":
                    col_detail.append({
                        "table": t, "column": name, "status": "warn",
                        "message": f"类型需隐式转换: 源 {stype} → 目标 {tc[1]}"})
                    col_warn += 1
            # 目标多列（源没有的）
            for c in tgt_cols:
                if _norm_name(c[0], cfg.field_ide).upper() not in src_map:
                    col_detail.append({
                        "table": t, "column": c, "status": "warn",
                        "message": "目标列源端不存在（取默认值/空）"})
                    col_warn += 1
    if col_fail:
        _add(items, "column_compat", "fail",
             f"{col_fail} 列不兼容（字段类型/缺失），请先修正表结构",
             col_detail)
    elif col_warn:
        _add(items, "column_compat", "warn",
             f"{col_warn} 列需隐式转换（可能截断/精度损失）", col_detail)
    else:
        _add(items, "column_compat", "pass", "列名与类型全部兼容")

    # ---- 4) 主键（upsert / realtime 必需）----
    if cfg.save_mode == "upsert" or cfg.sync_mode == "realtime":
        pk_detail = []
        for t in tables:
            tt = _tgt_table(t)
            try:
                pk = _get_pk_column(tgt_conn, cfg.tgt_db_type,
                                    cfg.tgt_db_name, cfg.tgt_schema, tt)
            except Exception:
                pk = None
            if not pk:
                pk_detail.append({
                    "table": tt,
                    "message": "目标表无单列主键，upsert/实时无法定位行"})
        if pk_detail:
            _add(items, "primary_key", "fail",
                 "upsert/实时同步要求目标表有单列主键", pk_detail)
        else:
            _add(items, "primary_key", "pass", "目标表主键满足")

    # ---- 5) 增量列 ----
    if cfg.sync_mode in ("incremental", "realtime"):
        if not cfg.incremental_column:
            _add(items, "incremental_column", "fail",
                 "增量/实时同步未指定增量列")
        else:
            try:
                src_cols = _get_columns(src_conn, cfg.src_db_type,
                                        cfg.src_db_name, cfg.src_schema,
                                        tables[0])
                hit = [c for c in src_cols
                       if str(c).lower() == cfg.incremental_column.lower()]
                if not hit:
                    _add(items, "incremental_column", "fail",
                         f"增量列 {cfg.incremental_column} 不存在于源表")
                elif _category(hit[0]) not in ("num", "date"):
                    _add(items, "incremental_column", "fail",
                         f"增量列 {cfg.incremental_column} 类型 "
                         f"{hit[0]} 不是数值/时间类型")
                else:
                    _add(items, "incremental_column", "pass",
                         f"增量列 {cfg.incremental_column} ({hit[0]})")
            except Exception as e:
                _add(items, "incremental_column", "warn",
                     f"增量列检查失败: {e}")

    return _summary(items)


def _summary(items: List[dict]) -> Dict[str, Any]:
    fail = sum(1 for i in items if i["status"] == "fail")
    warn = sum(1 for i in items if i["status"] == "warn")
    return {"passed": fail == 0, "fail": fail, "warn": warn, "items": items}
