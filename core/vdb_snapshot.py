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
"""
import base64
import gzip
import hashlib
import json
import os
import re
import time
from decimal import Decimal

import config

SNAP_VERSION = 1
MAX_TABLES = 200
MAX_ROWS = 200000
BATCH = 500
SUPPORTED = ("mysql", "mariadb", "postgresql", "kingbase")
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
