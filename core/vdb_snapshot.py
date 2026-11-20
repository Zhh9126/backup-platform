# -*- coding: utf-8 -*-
"""VDB 逻辑快照：平台侧原生驱动导出 / 回滚，零客户端安装、完全离线可用。

参考 CDM「虚拟副本时间点」语义，但按 AIDBM 的硬约束落地：
- 平台侧直连 VDB（pymysql / psycopg2），不要求被管机安装任何工具或 agent；
- 不依赖 LVM/VG、存储快照或外部 mysqldump/pg_dump 二进制；
- 产物是自描述的 gzip JSON（表清单 + 列 + 数据），带 sha256 与行数摘要。

诚实边界（v1）：
- 这是**数据级快照**，不是 DDL 快照：表结构与快照点不一致时回滚如实失败，
  提示重新克隆，不静默丢列/丢表。
- **全量快照**：`parent_snapshot_id` 只记录版本链，不做增量链（避免假增量）。
- 仅支持 VDB 目前真实可拉起的库型 mysql/mariadb/postgresql/kingbase。
- 大库保护：表数与行数上限可配置，超限如实拒绝而非撑爆平台内存。

--------------------------------------------------------------------------
2026-11-20 增补：原生导出通道（snap_kind=native）
--------------------------------------------------------------------------
原通道（snap_kind=logical）是「平台用 Python 逐行 SELECT 出来的 gzip JSON」，
它的三个硬伤决定了它撑不起「快照备份」这个语义：

1. **不含 DDL** —— 表结构一变，回滚只能如实拒绝（"表结构已变更"）；
2. **不是备份产物** —— 平台自己的恢复引擎吃不下，只能回滚到同一个 VDB；
3. **没有一致性快照点** —— 逐表 SELECT，跨表不是一个时间点；且全量数据
   进内存，受 MAX_ROWS=20 万硬上限约束。

因此新增 **native 通道**：直接调用数据库**自带**的导出工具
（mysqldump / pg_dump / sys_dump）产出**标准备份产物**：
- 含 DDL（表结构随快照走，回滚不再受结构变更限制）；
- 单事务/一致性快照（--single-transaction、pg_dump 的一致性快照）；
- **流式落盘**，不进内存，因此不受 20 万行上限约束；
- 产物是平台恢复引擎**认识的格式**，可恢复到新库/新实例。

仍然遵守平台硬约束：工具全部在**平台侧**执行（mysqldump/pg_dump 是客户端，
连接远端库即可），**被管数据库服务器不安装任何东西**。
"""
import base64
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from decimal import Decimal

import config
import core.db as db

SNAP_VERSION = 1
MAX_TABLES = 200
MAX_ROWS = 200000
BATCH = 500
SUPPORTED = ("mysql", "mariadb", "postgresql", "kingbase")

# 支持原生导出（标准备份产物）的库型
NATIVE_SUPPORTED = ("mysql", "mariadb", "postgresql", "kingbase")
# 各库型的导出工具候选（按优先级；同名多版本时按版本择优）
NATIVE_TOOLS = {
    "mysql": ("mysqldump",),
    "mariadb": ("mariadb-dump", "mysqldump"),
    "postgresql": ("pg_dump",),
    "kingbase": ("sys_dump", "pg_dump"),
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _ident(name, db_type):
    """标识符白名单 + 方言引用，杜绝快照内容拼进 SQL 造成注入。"""
    s = str(name or "")
    if not _IDENT.match(s):
        raise ValueError("非法对象名: %r" % s)
    return "`%s`" % s if db_type in ("mysql", "mariadb") else '"%s"' % s


def _enc(v):
    """DB-API 值 → JSON 可序列化（bytes 保留二进制，其余交给目标库隐式转换）。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (Decimal,)) or hasattr(v, "isoformat"):
        return str(v)  # Decimal / datetime / date / time：字符串由目标列类型转换
    return str(v)


def _dec(v):
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


def _collect_bin_candidates(*names):
    """枚举本机工具候选：PATH → 常见安装目录 glob。"""
    cands, seen = [], set()
    for n in names:
        p = shutil.which(n)
        if p and p not in seen:
            cands.append(p)
            seen.add(p)
    try:
        from core.engines.base import BackupEngine
        globs = BackupEngine._COMMON_TOOL_DIR_GLOBS
    except Exception:
        globs = ()
    for n in names:
        for d in globs:
            for p in glob.glob(os.path.join(d, n)):
                if os.path.isfile(p) and os.access(p, os.X_OK) and p not in seen:
                    cands.append(p)
                    seen.add(p)
    return cands


def _bin_version(path, names):
    """解析工具版本；**返回码非 0 或输出不含工具名 → 视为坏二进制**。

    坏二进制（缺动态库等）的 ``--version`` 报错文本里可能带版本样数字样
    （如 ``libcrypto.so.1.1`` → 被误认成 1.1），且报错文本含工具名路径，
    光靠"输出含工具名"判断不够——必须以**返回码**为准。
    """
    try:
        ret = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=10)
    except Exception:
        return None
    if ret.returncode != 0:
        return None
    out = (ret.stdout or "") + (ret.stderr or "")
    if not any(n in out for n in names):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _resolve_bin_for_server(server_ver, *names):
    """按**目标服务器版本**挑选客户端工具。

    真实踩坑（2026-11-20）：平台机上同时存在 PG 14 与 PG 18 两套工具链，
    「版本择优」会选中更高的 PG 18 —— 而 PG 18 的 ``pg_restore`` 会给会话发
    ``SET transaction_timeout``（PG 17+ 才有），**PG 14 服务器不认**，
    恢复直接失败（表现为 "unrecognized configuration parameter"）。

    规则：优先选与服务器**主版本一致**的客户端 → 其次选不高于服务器的最高版本
    → 服务器版本未知时才退回「版本择优」（取最高）。
    """
    cands = _collect_bin_candidates(*names)
    if not cands:
        return ""
    vers = {}
    for p in cands:
        v = _bin_version(p, names)
        if v:
            vers[p] = v
    if not vers:
        return cands[0]
    try:
        smaj = int(str(server_ver or "").split(".")[0] or 0)
    except (TypeError, ValueError):
        smaj = 0
    if smaj:
        exact = [p for p, v in vers.items() if v[0] == smaj]
        if exact:
            return max(exact, key=lambda p: vers[p])
        lower = [(p, v) for p, v in vers.items() if v[0] <= smaj]
        if lower:
            return max(lower, key=lambda x: x[1])[0]
    return max(vers.items(), key=lambda x: x[1])[0]


def _resolve_bin(*names):
    """本机工具解析（版本择优）。

    与引擎基类 ``_resolve_local_tool`` 同口径：PATH → 常见安装目录 glob，
    多个同名候选时按 ``--version`` 选版本最高者（低版本客户端无法操作
    新版本服务器，pg_dump 会报 server version mismatch）。
    """
    cands, seen = [], set()
    for n in names:
        p = shutil.which(n)
        if p and p not in seen:
            cands.append(p)
            seen.add(p)
    try:
        from core.engines.base import BackupEngine
        globs = BackupEngine._COMMON_TOOL_DIR_GLOBS
    except Exception:
        globs = ()
    for n in names:
        for d in globs:
            for p in glob.glob(os.path.join(d, n)):
                if os.path.isfile(p) and os.access(p, os.X_OK) and p not in seen:
                    cands.append(p)
                    seen.add(p)
    best, best_ver = None, None
    for p in cands:
        try:
            from core.engines.base import BackupEngine
            v = BackupEngine._bin_version_tuple(p)
        except Exception:
            v = None
        if v is None:
            continue
        if best_ver is None or v > best_ver:
            best, best_ver = p, v
    return best or (cands[0] if cands else "")


def _server_version(vdb, password, db_type):
    """服务端版本（字符串），用于判断客户端参数兼容性；取不到返回空串。"""
    try:
        conn = _connect(vdb, password)
        cur = conn.cursor()
        if db_type in ("mysql", "mariadb"):
            cur.execute("SELECT VERSION()")
        else:
            cur.execute("SHOW server_version")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return str((row or [""])[0] or "")
    except Exception:
        return ""


def _mysql_dump_cmd(bin_path, vdb, password, database, server_ver):
    """构造 mysqldump 命令（含一致性快照与 GTID 兼容处理）。"""
    cmd = [bin_path, "--no-defaults",
           "-h", str(vdb.get("host") or "127.0.0.1"),
           "-P", str(int(vdb.get("port") or 3306)),
           "-u", str(vdb.get("username") or "root"),
           "--single-transaction", "--routines", "--triggers",
           "--default-character-set=utf8mb4",
           "--databases", database]
    # mysqldump 8.0 连 5.7 服务端会报 "Unknown table 'COLUMN_STATISTICS'"
    # （rc=2）；5.7 客户端又不认识该参数，故按服务端版本决定是否加。
    try:
        major = int(str(server_ver).split(".")[0] or 0)
    except (TypeError, ValueError):
        major = 0
    if major and major < 8:
        cmd.insert(1, "--column-statistics=0")
    return cmd


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _connect(vdb, password, database=None):
    from core import native_conn
    return native_conn.connect(
        (vdb.get("db_type") or "mysql").lower(),
        vdb.get("host") or "127.0.0.1", vdb.get("port"),
        database or vdb.get("database_name"),
        vdb.get("username"), password)


def _tables(conn, db_type, database):
    cur = conn.cursor()
    if db_type in ("mysql", "mariadb"):
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' "
                    "ORDER BY table_name", (database,))
    else:
        cur.execute("SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' ORDER BY tablename")
    out = [r[0] for r in (cur.fetchall() or [])]
    cur.close()
    return out


def _columns(conn, db_type, database, table):
    cur = conn.cursor()
    if db_type in ("mysql", "mariadb"):
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s "
                    "ORDER BY ordinal_position", (database, table))
    else:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s "
                    "ORDER BY ordinal_position", (table,))
    out = [r[0] for r in (cur.fetchall() or [])]
    cur.close()
    return out


def snapshot_root():
    return os.path.join(config.BACKUP_ROOT, "vdb_snapshots")


def export_snapshot(vdb, password, name="", created_by="", out_root=None):
    """导出就绪 VDB 的数据级快照，返回元数据 dict（不含凭据）。

    产物：<root>/<vdb_id>/<ts>/snapshot.json.gz
    """
    db_type = (vdb.get("db_type") or "").lower()
    if db_type not in SUPPORTED:
        raise ValueError("VDB 快照暂不支持库型 %s（支持 %s）"
                         % (db_type or "-", "/".join(SUPPORTED)))
    database = vdb.get("database_name")
    if not database:
        raise ValueError("VDB 缺少 database_name，无法创建快照")

    conn = _connect(vdb, password)
    tables, total_rows = [], 0
    try:
        names = _tables(conn, db_type, database)
        if len(names) > MAX_TABLES:
            raise ValueError("VDB 表数 %d 超过快照上限 %d，请改用备份产物再克隆"
                             % (len(names), MAX_TABLES))
        for t in names:
            cols = _columns(conn, db_type, database, t)
            if not cols:
                continue
            for c in cols:
                _ident(c, db_type)
            sql = "SELECT %s FROM %s" % (
                ",".join(_ident(c, db_type) for c in cols), _ident(t, db_type))
            cur = conn.cursor()
            cur.execute(sql)
            rows = []
            while True:
                chunk = cur.fetchmany(BATCH)
                if not chunk:
                    break
                total_rows += len(chunk)
                if total_rows > MAX_ROWS:
                    raise ValueError("VDB 行数超过快照上限 %d，请改用备份产物再克隆"
                                     % MAX_ROWS)
                rows.append([[_enc(v) for v in r] for r in chunk])
            cur.close()
            tables.append({"name": t, "columns": cols,
                           "rows": [r for part in rows for r in part]})
    finally:
        conn.close()

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root or snapshot_root(),
                           str(vdb.get("id") or "unknown"), ts)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "snapshot.json.gz")
    payload = {"version": SNAP_VERSION, "db_type": db_type,
               "vdb_id": vdb.get("id"), "database": database,
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "name": name or ("snap-" + ts), "tables": tables,
               "table_count": len(tables), "row_count": total_rows}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return {"name": payload["name"], "backup_path": path,
            "size_bytes": os.path.getsize(path), "checksum": sha256_file(path),
            "table_count": len(tables), "row_count": total_rows,
            "snap_kind": "logical", "db_type": db_type, "database": database}


def read_snapshot(path):
    if not path or not os.path.isfile(path):
        raise ValueError("快照产物不存在: %s" % (path or "-"))
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != SNAP_VERSION:
        raise ValueError("快照格式版本不兼容: %s" % data.get("version"))
    return data


def restore_snapshot(vdb, password, path, database=None,
                     drop_new_tables=True):
    """把 VDB 数据回滚到快照点（或在指定目标库回放快照，用于再克隆）。

    - 快照内表：DELETE 后按列名批量插回（当前表若缺列 → 如实失败）。
    - 快照后新建的表：默认 DROP（保证「回到那个时间点」）。
    - 全部在一个事务内提交，失败 rollback 并抛出真实原因。
    """
    snap = read_snapshot(path)
    db_type = (vdb.get("db_type") or "").lower()
    if db_type not in SUPPORTED:
        raise ValueError("VDB 快照回滚暂不支持库型 %s" % db_type)
    database = database or vdb.get("database_name")
    if not database:
        raise ValueError("回滚目标库为空")

    conn = _connect(vdb, password, database)
    cur = conn.cursor()
    dropped, restored_rows, restored_tables = [], 0, 0
    try:
        current = set(_tables(conn, db_type, database))
        snap_names = {t["name"] for t in snap.get("tables") or []}
        if drop_new_tables:
            for t in sorted(current - snap_names):
                cur.execute("DROP TABLE %s" % _ident(t, db_type))
                dropped.append(t)
        for t in snap.get("tables") or []:
            tname = t["name"]
            if tname not in current:
                raise ValueError("快照表 %s 在当前库中不存在（表结构已变更），"
                                 "请重新克隆后再导入" % tname)
            cols = t["columns"]
            for c in cols:
                _ident(c, db_type)
            now_cols = _columns(conn, db_type, database, tname)
            missing = [c for c in cols if c not in now_cols]
            if missing:
                raise ValueError("表 %s 缺少快照列 %s（表结构已变更），"
                                 "拒绝不完整回滚" % (tname, ",".join(missing)))
            cur.execute("DELETE FROM %s" % _ident(tname, db_type))
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                _ident(tname, db_type),
                ",".join(_ident(c, db_type) for c in cols),
                ",".join(["%s"] * len(cols)))
            rows = [[_dec(v) for v in r] for r in (t.get("rows") or [])]
            for i in range(0, len(rows), BATCH):
                cur.executemany(sql, rows[i:i + BATCH])
            restored_rows += len(rows)
            restored_tables += 1
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()
    return {"ok": True, "tables": restored_tables, "rows": restored_rows,
            "dropped": dropped,
            "message": "已回滚到快照点：%d 表 / %d 行，清理新增表 %d 个"
                       % (restored_tables, restored_rows, len(dropped))}


# ======================================================================
# 原生导出通道（snap_kind=native）：标准备份产物
# ======================================================================

def _native_out_dir(vdb, ts, out_root=None):
    out_dir = os.path.join(out_root or snapshot_root(),
                           str(vdb.get("id") or "unknown"), ts)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _dump_stats(vdb, password, db_type, database):
    """表数与行数估算（information_schema 的 TABLE_ROWS 是估算值）。"""
    try:
        conn = _connect(vdb, password)
        cur = conn.cursor()
        if db_type in ("mysql", "mariadb"):
            cur.execute("SELECT COUNT(*), COALESCE(SUM(TABLE_ROWS),0) "
                        "FROM information_schema.tables WHERE table_schema=%s "
                        "AND table_type='BASE TABLE'", (database,))
        else:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(c.reltuples),0) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE c.relkind IN ('r','p') AND n.nspname='public'")
        row = cur.fetchone() or (0, 0)
        cur.close()
        conn.close()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return 0, 0


def export_native_snapshot(vdb, password, name="", created_by="",
                           out_root=None):
    """用数据库**自带**导出工具产出一份标准备份产物（含 DDL + 一致性快照）。

    产物：``<root>/<vdb_id>/<ts>/snapshot.sql``（MySQL 系）或 ``snapshot.dump``
    （PG 系），另附 ``manifest.json`` 记录库型/库名/工具与版本。

    返回元数据 dict（不含凭据）。失败一律抛真实原因，不产出半成品。
    """
    db_type = (vdb.get("db_type") or "").lower()
    if db_type not in NATIVE_SUPPORTED:
        raise ValueError("原生导出暂不支持库型 %s（支持 %s）"
                         % (db_type or "-", "/".join(NATIVE_SUPPORTED)))
    database = vdb.get("database_name")
    if not database:
        raise ValueError("VDB 缺少 database_name，无法创建快照")

    bin_path = _resolve_bin(*NATIVE_TOOLS.get(db_type, ()))
    if not bin_path:
        raise ValueError(
            "平台侧未找到 %s 导出工具（%s）——原生快照需要在**平台侧**具备该工具"
            "（被管库无需安装任何东西）。可改用「数据级快照」通道。"
            % (db_type, "/".join(NATIVE_TOOLS.get(db_type, ()))))

    server_ver = _server_version(vdb, password, db_type)
    # 客户端版本必须与服务器匹配：高版本客户端会向老服务器发新参数
    # （PG18 pg_dump 写出的产物含 PG17+ 的 SET transaction_timeout，
    #  PG14 服务器恢复时直接报 unrecognized configuration parameter）。
    bin_path = _resolve_bin_for_server(server_ver,
                                       *NATIVE_TOOLS.get(db_type, ())) or bin_path

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = _native_out_dir(vdb, ts, out_root)
    is_pg = db_type in ("postgresql", "kingbase")
    path = os.path.join(out_dir, "snapshot.dump" if is_pg else "snapshot.sql")

    env = os.environ.copy()
    if db_type in ("mysql", "mariadb"):
        if password:
            env["MYSQL_PWD"] = password        # 密码走环境变量，不进 argv
        cmd = _mysql_dump_cmd(bin_path, vdb, password, database,
                               _server_version(vdb, password, db_type))
    else:
        if password:
            env["PGPASSWORD"] = password
        cmd = [bin_path, "-h", str(vdb.get("host") or "127.0.0.1"),
               "-p", str(int(vdb.get("port") or 5432)),
               "-U", str(vdb.get("username") or "postgres"),
               "-Fc", "-f", path, database]

    # 流式落盘：stdout/文件直连，绝不把产物读进内存（大库会打爆平台内存）
    try:
        with open(path, "wb") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE,
                               timeout=7200, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError("导出超时（>2 小时）")
    except Exception as e:
        raise ValueError("导出失败: %s" % str(e)[:200])
    err = (p.stderr or b"").decode("utf-8", "replace")[:400]
    if p.returncode != 0:
        # 失败不留半成品产物
        try:
            os.remove(path)
        except Exception:
            pass
        raise ValueError("导出失败(rc=%s): %s" % (p.returncode, err.strip()[:300]))
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError("导出产物为空（rc=0 但无数据）: %s" % err.strip()[:200])

    table_count, row_estimate = _dump_stats(vdb, password, db_type, database)
    manifest = {
        "kind": "native", "db_type": db_type, "database": database,
        "vdb_id": vdb.get("id"), "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": os.path.basename(bin_path), "tool_path": bin_path,
        "table_count": table_count, "row_estimate": row_estimate,
        "size_bytes": os.path.getsize(path),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return {"name": name or ("snap-" + ts), "backup_path": path,
            "size_bytes": os.path.getsize(path), "checksum": sha256_file(path),
            "table_count": table_count, "row_count": row_estimate,
            "snap_kind": "native", "db_type": db_type, "database": database,
            "tool": manifest["tool"], "row_estimated": True,
            "note": "标准备份产物（含 DDL，一致性快照），可被恢复引擎直接消费"}


# ------------------------------------------------------------------ 恢复

def restore_native_snapshot(vdb, password, path, target_db=None,
                            drop_target=True):
    """把 **native 快照产物**恢复到目标库（默认恢复到同名库，覆盖式）。

    与 ``restore_snapshot``（JSON 数据级回滚）的区别：
    - native 产物**自带 DDL**，因此表结构变更不再是障碍；
    - 走数据库原生恢复通道（mysql 客户端导入 / pg_restore），产物即备份产物。

    诚实边界：目标库会被重建（``drop_target=True``），这是「恢复到快照点」
    的语义；如需保留原库请传 ``drop_target=False`` 并自行准备空库。
    """
    db_type = (vdb.get("db_type") or "").lower()
    if db_type not in NATIVE_SUPPORTED:
        raise ValueError("native 快照恢复暂不支持库型 %s" % db_type)
    if not path or not os.path.isfile(path):
        raise ValueError("快照产物不存在: %s" % (path or "-"))
    target = target_db or vdb.get("database_name")
    if not target:
        raise ValueError("恢复目标库为空")
    host = str(vdb.get("host") or "127.0.0.1")
    port = int(vdb.get("port") or (5432 if db_type in ("postgresql", "kingbase") else 3306))
    user = str(vdb.get("username") or "root")
    env = os.environ.copy()

    if db_type in ("mysql", "mariadb"):
        if password:
            env["MYSQL_PWD"] = password
        bin_path = _resolve_bin("mysql")
        if not bin_path:
            raise ValueError("平台侧未找到 mysql 客户端，无法导入 native 快照")
        base = [bin_path, "--no-defaults", "-h", host, "-P", str(port), "-u", user]
        if drop_target:
            r = subprocess.run(base + ["-e", "DROP DATABASE IF EXISTS `%s`" % target],
                               env=env, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise ValueError("删除目标库失败: %s" % (r.stderr or "")[:200])
        # mysqldump --databases 产物自带 CREATE DATABASE/USE，直接导入即可
        with open(path, "rb") as f:
            p = subprocess.run(base + [target], stdin=f, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=7200, env=env)
        if p.returncode != 0:
            raise ValueError("导入失败(rc=%s): %s"
                             % (p.returncode,
                                (p.stderr or b"").decode("utf-8", "replace")[:300]))
        return {"ok": True, "database": target,
                "message": "native 快照已恢复到库 %s" % target}

    if password:
        env["PGPASSWORD"] = password
    # 客户端版本必须与**服务器**匹配：平台机上可能同时存在 PG14 与 PG18
    # 工具链，用高版本 pg_restore 恢复老服务器会因其下发 PG17+ 的
    # SET transaction_timeout 而失败。
    server_ver = _server_version(vdb, password, db_type)
    # 目标库必须先存在：pg_restore 只往已有库里灌，不会建库。
    # （此前漏了这一步 → pg_restore 报 "database does not exist"，
    #   而致命判定又把它当可忽略 → **假成功**，实测克隆库根本没建出来。）
    psql = _resolve_bin_for_server(server_ver, "psql")
    if not psql:
        raise ValueError("平台侧未找到 psql，无法准备恢复目标库")
    if drop_target:
        subprocess.run([psql, "-h", host, "-p", str(port), "-U", user,
                        "-d", "postgres", "-c",
                        'DROP DATABASE IF EXISTS "%s"' % target],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=120, env=env)
    mk = subprocess.run([psql, "-h", host, "-p", str(port), "-U", user,
                         "-d", "postgres", "-c",
                         'CREATE DATABASE "%s"' % target],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=120, env=env)
    if mk.returncode != 0:
        mk_err = (mk.stderr or b"").decode("utf-8", "replace")
        if "already exists" not in mk_err.lower():
            raise ValueError("创建恢复目标库失败: %s" % mk_err.strip()[:200])
    bin_path = _resolve_bin_for_server(server_ver, "pg_restore")
    if not bin_path:
        raise ValueError("平台侧未找到 pg_restore，无法导入 native 快照")
    cmd = [bin_path, "-h", host, "-p", str(port), "-U", user,
           "-d", target, "--clean", "--if-exists", path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=7200, env=env)
    err = (p.stderr or b"").decode("utf-8", "replace")
    # pg_restore 对「对象已存在」「会话参数不被老服务器识别」类错误返回非 0，
    # 但数据可能已导入 —— 与平台既有 impdp 判定同口径：只要不致命就放行。
    # 致命判定的排除项：
    #   - already exists：对象已存在（--clean 已处理）
    #   - unrecognized configuration parameter：会话级参数不被老服务器支持，
    #     数据导入本身不受影响（PG18 客户端 → PG14 服务器的典型噪音）
    #   - does not exist **不放行**：目标对象/库根本不存在 = 真失败
    fatal = [ln for ln in err.splitlines()
             if ln.strip() and ("error" in ln.lower() or "fatal" in ln.lower())
             and "already exists" not in ln.lower()
             and "unrecognized configuration parameter" not in ln.lower()]
    if p.returncode != 0 and fatal:
        raise ValueError("pg_restore 失败: %s" % "; ".join(fatal[:3])[:300])
    # 结果校验：不能只看 rc——「报了错但数据其实导入了」与「真失败」必须区分开。
    # 连接目标库确认快照里的表确实在（表数为 0 且快照非空 → 视为失败）。
    try:
        names = _tables(_connect(vdb, password, target), db_type, target)
    except Exception:
        names = []
    if not names:
        raise ValueError("恢复后校验失败：目标库 %s 中没有查到任何表，"
                         "快照数据未真正导入" % target)
    return {"ok": True, "database": target, "tables": len(names),
            "message": "native 快照已恢复到库 %s（%d 张表）" % (target, len(names))}


# ------------------------------------------------------------------ 治理

def purge_snapshot_files(snapshot_ids, dry_run=False):
    """清理已删除/超限快照的**产物文件**（元数据保留，审计可追溯）。

    治理意义：快照产物是实打实的磁盘占用，此前删除快照只改 status，
    文件永远留在磁盘上——长期跑下来会把备份盘吃满。
    返回被清理的条目清单。
    """
    cleaned = []
    for sid in snapshot_ids or []:
        row = db.query_one("SELECT id, backup_path, status, size_bytes "
                           "FROM vdb_snapshots WHERE id=?", (int(sid),))
        if not row:
            continue
        path = row.get("backup_path") or ""
        entry = {"id": row.get("id"), "path": path,
                 "size_bytes": row.get("size_bytes") or 0, "removed": False}
        if path and os.path.isfile(path):
            entry["removed"] = True
            if not dry_run:
                try:
                    os.remove(path)
                    # 同目录的 manifest.json 一并清理
                    man = os.path.join(os.path.dirname(path), "manifest.json")
                    if os.path.isfile(man):
                        os.remove(man)
                    # 目录空了就删掉，避免留下空壳目录堆积
                    parent = os.path.dirname(path)
                    if parent and os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                except Exception:
                    entry["removed"] = False
        cleaned.append(entry)
    return cleaned


def purge_snapshot(snapshot_id: int, operator: str = "") -> dict:
    """物理清理某个快照：**删产物 + 置删除态**（元数据保留、审计可追溯）。

    两者必须同时做：只删产物不置删除态，会在列表里留下「有快照点但产物
    已不存在」的僵尸条目，点回滚只会报产物不存在。
    """
    row = db.query_one("SELECT id, backup_path, size_bytes, status "
                       "FROM vdb_snapshots WHERE id=?", (int(snapshot_id),))
    if not row:
        raise ValueError("快照不存在: %s" % snapshot_id)
    cleaned = purge_snapshot_files([snapshot_id])
    db.execute("UPDATE vdb_snapshots SET status='deleted', deleted_at=?, "
               "note=COALESCE(note,'')||' | 产物已清理' WHERE id=?",
               (db.now_iso(), int(snapshot_id)))
    db.add_log("INFO", "clone",
               "VDB 快照 %s 产物已清理（元数据保留，操作人 %s）"
               % (snapshot_id, operator or "unknown"))
    return {"ok": True, "snapshot_id": int(snapshot_id), "items": cleaned,
            "backup_path": row.get("backup_path") or ""}


def apply_retention(request_id=None, vdb_id=None, keep=10, dry_run=False):
    """保留策略：每个克隆请求 / VDB 最多保留 N 个就绪快照，超限清理最旧的。

    默认 keep=10（可用环境变量 ``AIDBM_VDB_SNAPSHOT_KEEP`` 调整）。
    返回被清理的快照 id 列表。
    """
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 10
    if keep <= 0:
        keep = 10
    stale = []
    if request_id:
        rows = db.query("SELECT id FROM vdb_snapshots WHERE clone_request_id=? "
                        "AND status='ready' ORDER BY id DESC", (int(request_id),))
    elif vdb_id:
        rows = db.query("SELECT id FROM vdb_snapshots WHERE vdb_instance_id=? "
                        "AND status='ready' ORDER BY id DESC", (int(vdb_id),))
    else:
        return stale
    over = [r["id"] for r in rows[keep:]]
    if not over:
        return stale
    if not dry_run:
        purge_snapshot_files(over)
        db.execute("UPDATE vdb_snapshots SET status='expired' WHERE id IN (%s)"
                   % ",".join("?" * len(over)), tuple(over))
    return over


