# -*- coding: utf-8 -*-
"""恢复后数据量实测（表数 / 行数 / 占用体积）。

平台对外展示的「恢复数据量」有两个口径，页面同时给出并标注来源：

* **实测口径**（``restored_*``）：恢复成功后到**目标库实测**的表数 / 行数 /
  占用体积。由本模块采集；采集不到就如实留空并记录原因（不猜数、不填 0）。
* **产物口径**（``restore_size_*``）：本次恢复所用**备份产物的落盘体积**
  （压缩后），来自 ``backup_records.size_bytes``，无需额外采集。

实测优先用平台侧原生驱动直连目标库；目标库不可直连时，退到 SSH 通道在目标
主机上执行轻量查询（mysql/mariadb/postgresql 具备该能力）；仍不可达则如实
记录原因，由页面展示为「未采集」。

行数默认为**统计估算值**（MySQL information_schema.table_rows / PG reltuples /
Oracle·达梦 num_rows），避免大库扫描拖慢恢复。估算值为 0/负（恢复后统计信息尚未
刷新，常见于刚导入的 InnoDB 表）且表数在阈值内时，自动回退逐表 ``COUNT(*)``
精确统计，避免页面展示「N 张表 · 0 行」误导用户以为恢复失败。
"""

import logging
import shlex

_logger = logging.getLogger(__name__)

# 支持原生驱动直连实测的库型
NATIVE_TYPES = ("mysql", "mariadb", "postgresql", "kingbase", "opengauss",
                "panwei", "oracle", "dameng")
# 支持 SSH 通道实测的库型（在目标主机上跑客户端查询）
SSH_TYPES = ("mysql", "mariadb", "postgresql")

# Oracle / 达梦：未指定 schema 时排除的系统属主
_SYS_OWNERS = ("SYS", "SYSTEM", "SYSAUX", "OUTLN", "DBSNMP", "APPQOSSYS",
               "XDB", "AUDSYS", "CTXSYS", "MDSYS", "OLAPSYS", "ORDSYS",
               "LBACSYS", "DVSYS", "WMSYS", "GSMADMIN_INTERNAL", "OJVMSYS",
               "SI_INFORMTN_SCHEMA", "DVF", "REMOTE_SCHEDULER_AGENT",
               "SYSAUDITOR", "SYSSSO", "CTISYS", "CJISYS")

_MYSQL_FAMILY = ("mysql", "mariadb")
_PG_FAMILY = ("postgresql", "kingbase", "opengauss", "panwei")
_ORA_FAMILY = ("oracle", "dameng")


def _int(v):
    """宽松转整数（统计值可能是 Decimal / 字符串 / None）。"""
    try:
        return int(float(v))
    except Exception:
        return None


def _lit(s):
    """SQL 字面量转义（单引号加倍 + 反斜杠转义，MySQL/PG/Oracle 通用）。"""
    return "'" + str(s or "").replace("\\", "\\\\").replace("'", "''") + "'"


def _owner_clause(owner: str) -> str:
    """按属主过滤的 where 片段。"""
    if owner:
        return "owner=%s" % _lit(owner.upper())
    return "owner NOT IN (%s)" % ", ".join(_lit(o) for o in _SYS_OWNERS)


def _sql_for(db_type: str, database: str, schema: str) -> str:
    """返回「表数, 行数估算, 体积字节」三列的统计 SQL（单行结果）。"""
    db_type = (db_type or "").lower()
    if db_type in _MYSQL_FAMILY:
        return ("SELECT COUNT(*), COALESCE(SUM(table_rows),0), "
                "COALESCE(SUM(data_length+index_length),0) "
                "FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE'"
                % _lit(database))
    if db_type in _PG_FAMILY:
        return ("SELECT COUNT(*), COALESCE(SUM(c.reltuples),0)::bigint, "
                "COALESCE(SUM(pg_total_relation_size(c.oid)),0)::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p') AND n.nspname=%s"
                % _lit(schema or "public"))
    if db_type in _ORA_FAMILY:
        return ("SELECT COUNT(*), COALESCE(SUM(num_rows),0), 0 "
                "FROM all_tables WHERE %s" % _owner_clause(schema or ""))
    raise ValueError("暂不支持该库型的数据量实测: %s" % db_type)


def _parse_row(line: str):
    """把一行「表数 行数 体积」解析成三元组。"""
    parts = [p.strip() for p in (line or "").replace("|", "\t").split("\t")]
    parts = [p for p in parts if p != ""]
    if len(parts) < 2:
        return None, None, None
    tables = _int(parts[0])
    rows = _int(parts[1])
    size = _int(parts[2]) if len(parts) > 2 else None
    return tables, rows, size


# 精确回退阈值：估算行数为 0 且表数不超过该值时，逐表 COUNT(*) 精确统计
_EXACT_FALLBACK_MAX_TABLES = 200


def _quote_ident(db_type: str, name: str) -> str:
    """按方言引用标识符。"""
    if db_type in _MYSQL_FAMILY:
        return "`%s`" % str(name).replace("`", "``")
    return '"%s"' % str(name).replace('"', '""')


def _table_list_sql(db_type: str, database: str, schema: str) -> str:
    """返回表清单 SQL（最多取阈值条）。Oracle/达梦额外返回属主列。"""
    db_type = (db_type or "").lower()
    if db_type in _MYSQL_FAMILY:
        return ("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE' "
                "ORDER BY table_name LIMIT %d"
                % (_lit(database), _EXACT_FALLBACK_MAX_TABLES))
    if db_type in _PG_FAMILY:
        return ("SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p') AND n.nspname=%s "
                "ORDER BY c.relname LIMIT %d"
                % (_lit(schema or "public"), _EXACT_FALLBACK_MAX_TABLES))
    return ("SELECT owner, table_name FROM all_tables WHERE %s "
            "AND ROWNUM <= %d" % (_owner_clause(schema or ""),
                                  _EXACT_FALLBACK_MAX_TABLES))


def _exact_rows(db_type: str, conn, database: str, schema: str):
    """逐表 COUNT(*) 精确统计，返回 ``(行数合计, 成功统计表数)``。

    单表失败（权限/视图/统计期间锁表）跳过并以已统计表数如实返回；
    一张也没统计成功时返回 ``(None, 0)``。
    """
    db_type = (db_type or "").lower()
    cur = conn.cursor()
    cur.execute(_table_list_sql(db_type, database, schema))
    raw = cur.fetchall() or []
    total, counted = 0, 0
    for item in raw:
        if db_type in _ORA_FAMILY:
            owner, name = (item[0], item[1]) if len(item) > 1 else ((schema or "").upper(), item[0])
            if not owner:
                continue
            full = "%s.%s" % (_quote_ident(db_type, owner), _quote_ident(db_type, name))
        elif db_type in _PG_FAMILY:
            full = "%s.%s" % (_quote_ident(db_type, schema or "public"),
                              _quote_ident(db_type, item[0]))
        else:
            full = "%s.%s" % (_quote_ident(db_type, database),
                              _quote_ident(db_type, item[0]))
        try:
            cur.execute("SELECT COUNT(*) FROM %s" % full)
            total += _int((cur.fetchone() or (0,))[0]) or 0
            counted += 1
        except Exception:
            continue
    return (total, counted) if counted else (None, 0)


def _query_metrics(db_type: str, conn, database: str, schema: str) -> dict:
    """在已建立的连接上执行统计查询。"""
    db_type = (db_type or "").lower()
    cur = conn.cursor()
    cur.execute(_sql_for(db_type, database, schema))
    row = cur.fetchone() or ()
    tables, rows, size = _int(row[0] if len(row) > 0 else None), \
        _int(row[1] if len(row) > 1 else None), \
        _int(row[2] if len(row) > 2 else None)
    note = ""
    if db_type in _MYSQL_FAMILY:
        note = "行数为统计估算值（information_schema.table_rows）"
    elif db_type in _PG_FAMILY:
        note = "行数为统计估算值（pg_class.reltuples）"
        if not size:
            try:
                cur.execute("SELECT pg_database_size(current_database())")
                size = _int((cur.fetchone() or (None,))[0])
            except Exception:
                pass
    else:
        note = "行数为统计信息估算值（all_tables.num_rows）"
        try:
            owner = (schema or "").upper()
            if owner:
                cur.execute("SELECT COALESCE(SUM(bytes),0) FROM dba_segments "
                            "WHERE owner=%s" % _lit(owner))
            else:
                cur.execute("SELECT COALESCE(SUM(bytes),0) FROM dba_segments "
                            "WHERE owner NOT IN (%s)"
                            % ", ".join(_lit(o) for o in _SYS_OWNERS))
            size = _int((cur.fetchone() or (None,))[0])
        except Exception:
            size = None
    # 估算行数为 0/负且确有表 → 恢复后统计信息可能尚未刷新，回退精确统计
    if (rows or 0) <= 0 and (tables or 0) > 0 \
            and (tables or 0) <= _EXACT_FALLBACK_MAX_TABLES:
        exact, counted = _exact_rows(db_type, conn, database, schema)
        if exact is not None:
            rows = exact
            note = ("行数为精确统计（逐表 COUNT(*)，%d 张表）" % counted
                    if counted >= (tables or 0) else
                    "行数为精确统计（逐表 COUNT(*)，仅 %d/%d 张表成功）"
                    % (counted, tables))
    return {"tables": tables, "rows": rows, "size_bytes": size, "note": note}


def _measure_direct(db_type, host, port, database, user, password,
                    schema, timeout) -> dict:
    """平台侧原生驱动直连目标库实测。"""
    from core import native_conn
    conn = native_conn.connect(db_type, host, port, database, user, password,
                               timeout=timeout)
    try:
        return _query_metrics(db_type, conn, database, schema)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ssh_cmd(db_type, bin_path, port, user, database, sql) -> str:
    """在目标主机上执行单条 SQL 的命令行（密码用 ``__PW__`` 占位待替换）。"""
    if db_type in _MYSQL_FAMILY:
        return ("MYSQL_PWD=%s %s --no-defaults -h 127.0.0.1 -P %s -u %s -N -B -e %s"
                % ("__PW__", shlex.quote(bin_path), shlex.quote(str(port or 3306)),
                   shlex.quote(user or ""), shlex.quote(sql)))
    return ("PGPASSWORD=%s %s -h 127.0.0.1 -p %s -U %s -d %s -At -c %s"
            % ("__PW__", shlex.quote(bin_path), shlex.quote(str(port or 5432)),
               shlex.quote(user or ""), shlex.quote(database or "postgres"),
               shlex.quote(sql)))


def _measure_ssh(db_type, ssh_host, port, database, user, password,
                 schema, timeout) -> dict:
    """SSH 通道：在目标主机上用数据库客户端执行同一条统计查询。"""
    from core import remote_dump as rd
    tool = "mysql" if db_type in _MYSQL_FAMILY else "psql"
    bin_path = rd.resolve_remote_tool(ssh_host, tool)
    if not bin_path:
        return {"tables": None, "rows": None, "size_bytes": None,
                "note": "目标主机上未找到 %s 客户端，无法采集" % tool}

    def _run(sql):
        cmd = _ssh_cmd(db_type, bin_path, port, user, database, sql).replace(
            "__PW__", shlex.quote(password or ""), 1)
        return rd.remote_exec_capture(ssh_host, cmd, timeout=timeout)

    res = _run(_sql_for(db_type, database, schema))
    if res.get("returncode") != 0:
        err = (res.get("stderr") or res.get("stdout") or "").strip()[:160]
        return {"tables": None, "rows": None, "size_bytes": None,
                "note": "SSH 采集失败：%s" % (err or "命令返回码 %s"
                                             % res.get("returncode"))}
    line = ""
    for ln in (res.get("stdout") or "").splitlines():
        if ln.strip():
            line = ln.strip()
            break
    tables, rows, size = _parse_row(line)
    if tables is None:
        return {"tables": None, "rows": None, "size_bytes": None,
                "note": "SSH 采集未返回可解析结果"}
    note = ("行数为统计估算值（information_schema.table_rows）"
            if db_type in _MYSQL_FAMILY
            else "行数为统计估算值（pg_class.reltuples）")
    # 估算为 0/负 → 再发一条「表清单」+「UNION 求和」SQL 做精确统计
    if (rows or 0) <= 0 and (tables or 0) > 0 \
            and (tables or 0) <= _EXACT_FALLBACK_MAX_TABLES:
        lst = _run(_table_list_sql(db_type, database, schema))
        names = []
        if lst.get("returncode") == 0:
            names = [ln.strip() for ln in (lst.get("stdout") or "").splitlines()
                     if ln.strip()]
        if names:
            if db_type in _MYSQL_FAMILY:
                subs = ["(SELECT COUNT(*) FROM %s.%s)"
                        % (_quote_ident(db_type, database), _quote_ident(db_type, n))
                        for n in names]
            else:
                subs = ["(SELECT COUNT(*) FROM %s.%s)"
                        % (_quote_ident(db_type, schema or "public"),
                           _quote_ident(db_type, n)) for n in names]
            res2 = _run("SELECT " + "+".join(subs))
            if res2.get("returncode") == 0:
                exact = None
                for ln in (res2.get("stdout") or "").splitlines():
                    if ln.strip():
                        exact = _int(ln.strip())
                        break
                if exact is not None:
                    rows = exact
                    note = "行数为精确统计（逐表 COUNT(*)，%d 张表）" % len(names)
    return {"tables": tables, "rows": rows, "size_bytes": size, "note": note}


def measure(db_type, host, port, database, user, password, schema=None,
            ssh_host=None, timeout=20) -> dict:
    """实测目标库数据量。

    返回 ``{"ok": bool, "tables": int|None, "rows": int|None,
    "size_bytes": int|None, "mode": "direct"/"ssh"/"", "note": str}``。

    ``ok=False`` 表示未采集到（note 给出真实原因）；任何情况下都不返回
    猜测值——采不到就是采不到。
    """
    db_type = (db_type or "").lower()
    out = {"ok": False, "tables": None, "rows": None, "size_bytes": None,
           "mode": "", "note": ""}
    if db_type not in NATIVE_TYPES:
        out["note"] = "该库型暂不支持数据量实测"
        return out
    if db_type not in _MYSQL_FAMILY and db_type not in _PG_FAMILY \
            and db_type not in _ORA_FAMILY:
        out["note"] = "该库型暂不支持数据量实测"
        return out

    # ① 直连优先
    if host:
        try:
            m = _measure_direct(db_type, host, port, database, user, password,
                                 schema, timeout)
            if m.get("tables") is not None:
                out.update(m)
                out["ok"] = True
                out["mode"] = "direct"
                return out
            out["note"] = m.get("note") or "直连采集未返回结果"
        except Exception as e:
            out["note"] = "直连采集失败：%s" % str(e)[:160]
    else:
        out["note"] = "未确定目标地址"

    # ② 退到 SSH 通道
    if ssh_host and db_type in SSH_TYPES:
        try:
            m = _measure_ssh(db_type, ssh_host, port, database, user, password,
                              schema, timeout)
            if m.get("tables") is not None:
                out.update(m)
                out["ok"] = True
                out["mode"] = "ssh"
                return out
            out["note"] = m.get("note") or out["note"]
        except Exception as e:
            out["note"] = "%s；SSH 采集失败：%s" % (out["note"], str(e)[:160])
    elif ssh_host and db_type not in SSH_TYPES:
        out["note"] = "%s；SSH 通道暂不支持该库型采集" % out["note"]
    return out


def _host_from_target(target_host: str, target_host_info: dict,
                      task: dict) -> str:
    """解析实测目标地址：恢复目标主机优先，其次任务源实例地址。"""
    if target_host_info:
        hk = (target_host_info.get("host_key") or "").strip()
        if hk:
            host = hk.split("@")[-1].split(":")[0]
            if host:
                return host
        if target_host_info.get("hostname"):
            return target_host_info["hostname"]
    if target_host:
        h = str(target_host).strip()
        if h and h not in ("local", "127.0.0.1"):
            return h.split(":")[0]
    return (task.get("host") or "").strip()


def measure_from_task(task: dict, target_host=None, target_host_info=None,
                       target_db=None, target_port=None,
                       target_schema=None, timeout=20) -> dict:
    """从恢复上下文推导目标库连接信息并实测。

    ``task`` 需是 include_secret=True 的任务字典（含明文口令）。
    """
    task = task or {}
    db_type = (task.get("db_type") or "").lower()
    host = _host_from_target(target_host, target_host_info, task)
    port = target_port or task.get("port")
    database = target_db or task.get("db_name")
    schema = target_schema
    if not schema:
        extra = task.get("extra_options")
        if isinstance(extra, dict):
            schema = extra.get("schema") or extra.get("db_schema")
    return measure(db_type=db_type, host=host, port=port, database=database,
                   user=task.get("username"), password=task.get("password"),
                   schema=schema, ssh_host=target_host_info, timeout=timeout)
