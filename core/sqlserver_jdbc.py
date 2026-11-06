# -*- coding: utf-8 -*-
"""SQL Server JDBC 逻辑备份/恢复（纯 1433 通道，免安装免 SSH）。

场景：Windows SQL Server 无法/不便开启 SSH 时，通过 JDBC（平台自带
mssql-jdbc jar + JRE，零客户端安装）把库表结构+数据拉取为 T-SQL 逻辑
产物（.sql.gz），恢复时经 JDBC 回放到目标库。

产物形态（SSMS 惯例）：
    CREATE TABLE [dbo].[t](...)        ← 主键/IDENTITY/默认值/可空
    GO
    INSERT INTO [dbo].[t] VALUES (...),(...)   ← 200 行/批
    GO
解析端用单引号状态机按 GO 分段（数据值中的 "GO" 不会误分段）。

诚实边界：仅表结构（列/主键/IDENTITY/默认值）+ 表数据；不包含存储过程、
触发器、视图、索引（主键除外）、外键——需要完整对象请使用物理备份
（BACKUP DATABASE，需 SSH 通道）。大库性能受批大小限制（200 行/批）。
"""
import gzip
import time


def _conn_kwargs(task):
    """从任务提取 JDBC 连接参数（密码解密）。"""
    import core.db as db
    host = (task.get("host") or "").strip()
    port = int(task.get("port") or 1433)
    user = (task.get("username") or "sa").strip()
    pw = task.get("password") or ""
    if isinstance(pw, str) and (pw.startswith("sm4v1:") or pw.startswith("enc:")):
        pw = db.decrypt_secret(pw)
    return host, port, user, pw


def _connect(task, database=None, timeout=30):
    """专用连接：socketTimeout 长超时（备份/恢复通道），不用 jdbc.py 的
    探测级短超时模板（15s 会掐断 CREATE DATABASE 与大数据拉取）。"""
    import jaydebeapi
    from core import jdbc
    host, port, user, pw = _conn_kwargs(task)
    jdbc._ensure_jvm()
    jar, driver_class, _, _, _, _ = jdbc._resolve_config("sqlserver")
    url = ("jdbc:sqlserver://%s:%s;databaseName=%s;encrypt=false;"
           "trustServerCertificate=true;loginTimeout=15;socketTimeout=600000"
           % (host, port, database or task.get("db_name") or ""))
    return jaydebeapi.connect(driver_class, url,
                              [user or "", pw or ""], jars=str(jar))


def _jconn(conn):
    """jaydebeapi Connection → 原生 JPype java.sql.Connection。"""
    return getattr(conn, "jconn", None) or conn._connection


def list_tables(conn, db_name=None):
    """列出业务表 [(schema, table)]（BASE TABLE，系统表除外）。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' "
            "AND TABLE_SCHEMA NOT IN ('sys','INFORMATION_SCHEMA','guest') "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME")
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _table_ddl(jconn, catalog, schema, table):
    """经 DatabaseMetaData 生成 CREATE TABLE（列/类型/可空/默认/IDENTITY/主键）。"""
    md = jconn.getMetaData()
    cols = []
    pk_cols = []
    rs = md.getColumns(catalog, schema, table, "%")
    try:
        while rs.next():
            name = rs.getString("COLUMN_NAME")
            tname = (rs.getString("TYPE_NAME") or "nvarchar").upper()
            # SQL Server JDBC 对 IDENTITY 列 TYPE_NAME 返回 "int identity"
            #（自带 identity 字样）——剥掉后缀，IDENTITY(1,1) 统一由
            # IS_AUTOINCREMENT 分支补一次，避免"多个 IDENTITY 约束"。
            autoinc = False
            if tname.endswith(" IDENTITY"):
                tname = tname[:-len(" IDENTITY")].strip()
                autoinc = True
            size = rs.getInt("COLUMN_SIZE")
            size = None if rs.wasNull() else size
            scale = rs.getInt("DECIMAL_DIGITS")
            scale = None if rs.wasNull() else scale
            nullable = (rs.getString("IS_NULLABLE") or "YES").upper() != "NO"
            dflt = rs.getString("COLUMN_DEF")
            if (rs.getString("IS_AUTOINCREMENT") or "NO").upper() == "YES":
                autoinc = True
            # 精度拼接：数值/时间类带 scale，字符/二进制类带 size
            if tname in ("DECIMAL", "NUMERIC") and size is not None:
                t = f"{tname}({size},{scale if scale is not None else 0})"
            elif tname in ("DATETIME2", "TIME", "DATETIMEOFFSET") and scale is not None and scale != 6:
                t = f"{tname}({scale})"
            elif size is not None and tname in (
                    "CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "BINARY",
                    "VARBINARY", "VARCHAR2"):
                t = f"{tname}({'MAX' if size >= 8000 and 'VAR' in tname else size})"
            else:
                t = tname
            parts = f"[{name}] {t}"
            if autoinc:
                parts += " IDENTITY(1,1)"
            if not nullable:
                parts += " NOT NULL"
            if dflt:
                parts += f" DEFAULT {dflt}"
            cols.append(parts)
    finally:
        rs.close()
    rs = md.getPrimaryKeys(catalog, schema, table)
    try:
        pks = []
        while rs.next():
            pks.append((rs.getInt("KEY_SEQ"), rs.getString("COLUMN_NAME")))
        pk_cols = [c for _seq, c in sorted(pks)]
    finally:
        rs.close()
    if pk_cols:
        cols.append("PRIMARY KEY (" + ", ".join(f"[{c}]" for c in pk_cols) + ")")
    has_identity = " IDENTITY(1,1)" in "\n  ".join(cols)
    return ("CREATE TABLE [{}].[{}] (\n  {}\n)".format(
        schema, table, ",\n  ".join(cols)), has_identity)


def _literal(v):
    """Python 值 → T-SQL 字面量。"""
    if v is None:
        return "NULL"
    import datetime
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex()
    if isinstance(v, str):
        return "N'" + v.replace("'", "''") + "'"
    if isinstance(v, datetime.datetime):
        return "N'" + v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "'"
    if isinstance(v, datetime.date):
        return "N'" + v.strftime("%Y-%m-%d") + "'"
    if isinstance(v, datetime.time):
        return "N'" + v.strftime("%H:%M:%S.%f")[:-3] + "'"
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


_INSERT_BATCH = 200


def export_db(task, out_path: str, tables=None) -> dict:
    """导出整库（或指定表）为 T-SQL 逻辑产物 .sql.gz。

    Returns: {"tables": N, "rows": M, "size_bytes": X, "duration_sec": s}
    """
    import os
    t0 = time.time()
    conn = _connect(task)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    n_rows = 0
    try:
        jconn = _jconn(conn)
        catalog = (task.get("db_name") or "").strip()
        if tables:
            tlist = [(t.split(".")[0] if "." in t else "dbo", t.split(".")[-1])
                     for t in tables]
        else:
            tlist = list_tables(conn, catalog)
        with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as f:
            f.write("-- AIDBM SQL Server JDBC 逻辑备份\n")
            f.write("-- database: %s | at: %s\n" % (catalog, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write("-- 诚实边界: 含表结构(列/主键/IDENTITY/默认值)+表数据；"
                    "不含存储过程/触发器/视图/索引/外键\n\n")
            for schema, table in tlist:
                ddl, has_identity = _table_ddl(jconn, catalog, schema, table)
                full = f"[{schema}].[{table}]"
                f.write(ddl + "\nGO\n")
                # IDENTITY 列数据保真：显式插入原自增值需会话级 IDENTITY_INSERT
                if has_identity:
                    f.write(f"SET IDENTITY_INSERT {full} ON\nGO\n")
                cur = conn.cursor()
                try:
                    cur.execute(f"SELECT * FROM {full}")
                    col_names = [d[0] for d in cur.description]
                    col_list = ", ".join(f"[{c}]" for c in col_names)
                    buf, batch = [], 0
                    while True:
                        rows = cur.fetchmany(_INSERT_BATCH)
                        if not rows:
                            break
                        for r in rows:
                            buf.append("(" + ", ".join(_literal(v) for v in r) + ")")
                        f.write(f"INSERT INTO {full} ({col_list}) VALUES\n"
                                + ",\n".join(buf) + "\nGO\n")
                        n_rows += len(rows)
                        buf, batch = [], batch + 1
                    if has_identity:
                        f.write(f"SET IDENTITY_INSERT {full} OFF\nGO\n")
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
        return {"tables": len(tlist), "rows": n_rows,
                "size_bytes": os.path.getsize(out_path),
                "duration_sec": round(time.time() - t0, 2)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _split_go_statements(text: str):
    """SSMS 语义分段：单引号状态机逐行扫描，独立行 GO 为段分隔。

    字符串值内的 "GO"（处于引号内，含跨行字符串）不会被误判为分隔。
    """
    stmts, buf, in_str = [], [], False
    for line in text.split("\n"):
        if not in_str and line.strip().upper() == "GO":
            stmts.append("\n".join(buf))
            buf = []
            continue
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "'":
                    if i + 1 < len(line) and line[i + 1] == "'":
                        i += 1  # '' 转义，仍在字符串内
                    else:
                        in_str = False
            elif ch == "'":
                in_str = True
            i += 1
        buf.append(line)
    if buf and "\n".join(buf).strip():
        stmts.append("\n".join(buf))
    out = []
    for s in stmts:
        s = s.strip()
        if not s:
            continue
        if all((not ln.strip()) or ln.strip().startswith("--")
               for ln in s.split("\n")):
            continue
        out.append(s)
    return out


def replay_artifact(task, artifact_path: str, target_db: str) -> dict:
    """回放 .sql/.sql.gz 逻辑产物到 target_db（目标库不存在自动创建）。

    Returns: {"stmts": N, "tables": N, "duration_sec": s}
    """
    import os
    import re as _re
    t0 = time.time()
    if not artifact_path or not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"备份文件不存在: {artifact_path}")
    opener = gzip.open if artifact_path.endswith(".gz") else open
    with opener(artifact_path, "rt", encoding="utf-8", errors="replace") as f:
        text = f.read()
    stmts = _split_go_statements(text)
    creates = [s for s in stmts if _re.search(r"\bCREATE TABLE\s+\[", s)]
    inserts = [s for s in stmts if _re.search(r"\bINSERT INTO\s+\[", s)]

    conn = _connect(task, database="master")
    try:
        cur = conn.cursor()
        cur.execute("IF DB_ID(N'%s') IS NULL CREATE DATABASE [%s]"
                    % (target_db.replace("'", "''"), target_db))
        conn.commit()
        try:
            cur.close()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 重恢复语义：CREATE TABLE 前先 DROP 同名旧表（对齐 dimp REPLACE；
    # 上次恢复半途失败残留的对象不阻断重跑）
    prepared = []
    for s in stmts:
        prepared.append(s)
        m = _re.search(r"CREATE TABLE\s+\[([^\]]+)\]\.\[([^\]]+)\]", s)
        if m:
                prepared.insert(-1, "IF OBJECT_ID(N'[%s].[%s]', N'U') IS NOT NULL "
                                    "DROP TABLE [%s].[%s]" % (m.group(1), m.group(2),
                                                              m.group(1), m.group(2)))
    stmts = prepared

    conn = _connect(task, database=target_db)
    executed = 0
    try:
        # 事务控制必须走底层 jconn（jaydebeapi 包装对象无 setAutoCommit）；
        # 语句用原生 Statement.execute（非 PreparedStatement）——SET
        # IDENTITY_INSERT 经 sp_prepexec 执行会被限制（实测报"标识列插入
        # 显式值"被拒），sqlcmd 式文本执行语义才正确。
        jconn = _jconn(conn)
        jconn.setAutoCommit(False)
        st = jconn.createStatement()
        try:
            for s in stmts:
                st.execute(s)
                executed += 1
            jconn.commit()
        except Exception:
            try:
                jconn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                st.close()
            except Exception:
                pass
        return {"stmts": executed, "tables": len(creates),
                "inserts": len(inserts),
                "duration_sec": round(time.time() - t0, 2)}
    finally:
        try:
            conn.close()
        except Exception:
            pass
