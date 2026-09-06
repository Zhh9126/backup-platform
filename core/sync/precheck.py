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
    """取列 (名称, 完整类型字符串) 列表（预校验专用，跨库型）。

    类型含精度/unsigned 修饰（如 'bigint unsigned'、'decimal(20,4)'），
    否则映射引擎无法判定升位/精度风险。
    """
    from core.data_compare import _user_of
    cur = conn.cursor()
    db_type = (db_type or "").lower()
    try:
        if db_type in ("mysql", "mariadb"):
            # COLUMN_TYPE 含精度与 unsigned（'bigint unsigned'/'decimal(20,4)'）
            cur.execute(
                "SELECT column_name, column_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (database, table))
        elif db_type in ("postgresql", "kingbase"):
            cur.execute(
                "SELECT column_name, "
                "CASE WHEN data_type IN ('character varying','char','character') "
                "  AND character_maximum_length IS NOT NULL "
                "  THEN data_type || '(' || character_maximum_length || ')' "
                "WHEN data_type IN ('numeric','decimal') "
                "  AND numeric_precision IS NOT NULL "
                "  THEN data_type || '(' || numeric_precision || ',' "
                "       || numeric_scale || ')' "
                "ELSE data_type END "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (schema or "public", table))
        elif db_type in ("oracle", "dameng"):
            owner = (schema or _user_of(conn) or "").upper()
            cur.execute(
                "SELECT column_name, CASE "
                "WHEN data_type IN ('VARCHAR2','NVARCHAR2','CHAR','NCHAR','RAW') "
                "  AND char_length > 0 THEN data_type || '(' || char_length || ')' "
                "WHEN data_type = 'NUMBER' AND data_precision IS NOT NULL "
                "  THEN 'NUMBER(' || data_precision || "
                "       CASE WHEN data_scale IS NOT NULL AND data_scale > 0 "
                "            THEN ',' || data_scale ELSE '' END || ')' "
                "WHEN data_type = 'TIMESTAMP' AND data_precision IS NOT NULL "
                "  THEN 'TIMESTAMP(' || data_precision || ')' "
                "ELSE data_type END "
                f"FROM all_tab_columns WHERE owner = '{owner}' "
                f"AND table_name = '{table}' ORDER BY column_id")
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

    # ---- 0) 概念区分（业界定义：迁移=一次性 / 同步=持续性）----
    kind = "migration" if cfg.sync_mode == "full" else "sync"
    if kind == "migration":
        _add(items, "task_kind", "pass",
             "任务性质：数据迁移（一次性任务，完成后即结束，支持覆盖写入）")
    else:
        if cfg.save_mode == "overwrite":
            _add(items, "task_kind", "fail",
                 "概念冲突：同步是持续性任务（保持两端一致），不支持覆盖写入"
                 "（overwrite 仅适用于一次性数据迁移）。请改用 upsert/append，"
                 "或将任务类型改为数据迁移（full）")
        else:
            _add(items, "task_kind", "pass",
                 "任务性质：数据同步（持续性任务，常驻运行保持两端一致）")

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
    # overwrite（迁移自动建表）时同样输出映射建议——对标 DTS 结构初始化：
    # 自动建表按映射引擎的建议类型建列，风险类型需人工确认。
    col_detail = []
    col_fail = col_warn = 0
    if True:
        for t in tables:
            tt = _tgt_table(t)
            tgt_cols = []
            try:
                src_cols = _get_cols_typed(src_conn, cfg.src_db_type,
                                           cfg.src_db_name, cfg.src_schema, t)
                # overwrite 自动建表时目标表可能不存在（tgt_cols 为空）
                if not (overwrite and tt in tgt_missing):
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
            from core.sync.type_matrix import map_type
            for c in src_cols:
                name, stype = c
                key = _norm_name(name, cfg.field_ide).upper()
                src_map.add(key)
                tc = tgt_map.get(key)
                if tc is None:
                    if overwrite:
                        continue  # 自动建表场景无目标列可缺（建表建议见下）
                    col_detail.append({"table": t, "column": name,
                                       "status": "fail",
                                       "message": "目标表缺少该列"})
                    col_fail += 1
                    continue
                # 类型映射引擎（对标 DTS 映射手册）：unsigned 升位/精度降级/
                # 特殊类型不支持等异构风险逐列判定
                m = map_type(cfg.src_db_type, cfg.tgt_db_type, stype)
                if m["level"] == "fail":
                    col_detail.append({
                        "table": t, "column": name, "status": "fail",
                        "message": f"类型不支持: 源 {stype} → 目标 {cfg.tgt_db_type}"
                                   f"（{m['reason']}）"})
                    col_fail += 1
                elif m["level"] == "warn":
                    col_detail.append({
                        "table": t, "column": name, "status": "warn",
                        "message": f"源 {stype} → 建议 {m['target_type'] or '人工确认'}"
                                   f"（实际目标 {tc[1]}）: {m['reason']}"})
                    col_warn += 1
            # 目标多列（源没有的）
            for c in tgt_cols:
                if _norm_name(c[0], cfg.field_ide).upper() not in src_map:
                    col_detail.append({
                        "table": t, "column": str(c[0]), "status": "warn",
                        "message": "目标列源端不存在（取默认值/空）"})
                    col_warn += 1
            # overwrite 自动建表：逐列输出建表类型建议（对标 DTS 结构初始化）
            if overwrite and tt in tgt_missing:
                from core.sync.type_matrix import map_type as _mt
                for c in src_cols:
                    m = _mt(cfg.src_db_type, cfg.tgt_db_type, c[1])
                    if m["level"] == "ok" and not m["reason"]:
                        continue          # 无风险类型不刷屏
                    lvl = "warn"          # 建议仅提示，不拦截（建表已按建议类型规避）
                    col_warn += 1
                    col_detail.append({
                        "table": t, "column": str(c[0]),
                        "status": lvl,
                        "message": (f"建表建议: {c[1]} → {m['target_type'] or '人工确认'}"
                                    + (f"（{m['reason']}）" if m["reason"] else ""))})
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

    # ---- 6) 数据级试写 / 容量预估 / 字符集 / 外键完整性 ----
    from core.sync import precheck_data as _pd
    sample_n = int(getattr(cfg, "precheck_sample_rows", 20) or 0)
    if sample_n > 0:
        for t in tables[:5]:                 # 最多试写 5 张表（控制预检耗时）
            try:
                r = _pd.check_data_sample(cfg, src_conn, tgt_conn, t, sample_n)
            except Exception as e:
                r = {"status": "warn", "message": f"数据级试写异常（跳过）: {e}",
                     "detail": []}
            if r:
                _add(items, f"data_sample[{t}]", r["status"],
                     r["message"], r.get("detail"))
    try:
        r = _pd.check_capacity(cfg, src_conn, tables)
        _add(items, "capacity", r["status"], r["message"], r.get("detail"))
    except Exception as e:
        _add(items, "capacity", "warn", f"容量预估不可用: {e}")
    try:
        r = _pd.check_charset(cfg, src_conn, tgt_conn)
        _add(items, "charset", r["status"], r["message"], r.get("detail"))
    except Exception as e:
        _add(items, "charset", "warn", f"字符集探测不可用: {e}")
    if len(tables) >= 2:
        r = _pd.check_fk_parents(cfg, src_conn, tables)
        if r:
            _add(items, "fk_integrity", r["status"], r["message"], r.get("detail"))

    return _summary(items)


def _summary(items: List[dict]) -> Dict[str, Any]:
    fail = sum(1 for i in items if i["status"] == "fail")
    warn = sum(1 for i in items if i["status"] == "warn")
    return {"passed": fail == 0, "fail": fail, "warn": warn, "items": items}
