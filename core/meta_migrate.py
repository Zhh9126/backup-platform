# -*- coding: utf-8 -*-
"""元数据库搬移引擎：SQLite <-> PostgreSQL / MySQL 全量互拷。

设计要点：
- 表清单：sqlite 源取 sqlite_master；PG/MySQL 源取 information_schema。
- 建表：目标库先跑 db.init_schema(conn, backend)（幂等：CREATE IF NOT EXISTS
  + ALTER 补丁列 + 逐句容错）。
- 拷贝：逐表 SELECT * 批量 INSERT（占位符 ? 由适配层按目标后端翻译），
  自增主键显式带值拷贝，PG 拷完 setval 归位序列。
- 校验：逐表行数比对，不一致即报错并终止切换。
- 回切：反向执行同一引擎（PG/MySQL -> SQLite），SQLite 侧建表走 init_schema。
"""
import logging

from core import db

log = logging.getLogger("aidbm.meta_migrate")

BATCH_SIZE = 500
# 纯缓存/临时表：搬移时跳过（目标侧会自动重建）
SKIP_TABLES = set()


def _quote_ident(backend: str, name: str) -> str:
    if backend == "mysql" and name.lower() in db._MYSQL_RESERVED:
        return f"`{name}`"
    return name


def list_tables(conn, backend: str) -> list:
    if backend == "sqlite":
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
        return sorted(r[0] for r in cur.fetchall())
    if backend == "postgresql":
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'")
    elif backend == "mysql":
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_type='BASE TABLE'")
    else:
        raise ValueError(f"不支持的后端: {backend}")
    return sorted(r[0] for r in cur.fetchall())


def table_columns(conn, backend: str, table: str) -> list:
    """列清单（按 ordinal 顺序）。不依赖 cur.description（空表时 psycopg2 为 None）。"""
    if backend == "sqlite":
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        cur.close()
        return cols
    if backend == "postgresql":
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=? "
            "ORDER BY ordinal_position", (table,))
    else:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=? "
            "ORDER BY ordinal_position", (table,))
    cols = [r[0] for r in cur.fetchall()]
    cur.close()
    return cols


def count_rows(conn, backend: str, table: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    row = cur.fetchone()
    cur.close()
    return int(row[0])


def copy_table(src, src_backend: str, dst, dst_backend: str,
               table: str, batch: int = BATCH_SIZE) -> dict:
    """单表全量拷贝。返回 {rows, }；表在源不存在时返回 rows=0。"""
    tables = set(list_tables(src, src_backend))
    if table not in tables:
        return {"rows": 0, "skipped": "source_missing"}
    cols = table_columns(src, src_backend, table)
    col_list = ",".join(_quote_ident(dst_backend, c) for c in cols)
    ph = ",".join("?" * len(cols))
    ins = f"INSERT INTO {_quote_ident(dst_backend, table)} ({col_list}) VALUES ({ph})"
    cur = src.execute(f"SELECT * FROM {table}")
    total = 0
    try:
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for r in rows:
                vals = [r[i] for i in range(len(cols))]
                dst.execute(ins, vals)
            dst.commit()
            total += len(rows)
    finally:
        cur.close()
    return {"rows": total}


def reset_pg_sequences(dst, dst_backend: str, tables: list) -> list:
    """PG：把各表 SERIAL 序列归位到 MAX(id)+1，避免后续插入主键冲突。"""
    done = []
    if dst_backend != "postgresql":
        return done
    for t in tables:
        pk = (db._TABLE_PK or {}).get(t)
        if not pk:
            continue
        try:
            cur = dst.execute(
                "SELECT COALESCE(MAX(%s),0)+1 FROM %s" % (pk, t))
            nxt = cur.fetchone()[0]
            cur.close()
            dst.execute(
                "SELECT setval(pg_get_serial_sequence('%s','%s'), %s, false)"
                % (t, pk, int(nxt)))
            done.append(t)
        except Exception as e:  # 表无自增序列等，忽略
            log.debug("[meta_migrate] setval %s 失败: %s", t, e)
    dst.commit()
    return done


def fk_references(conn, backend: str, table: str) -> set:
    """返回 table 引用（FOREIGN KEY）的父表集合。"""
    try:
        if backend == "sqlite":
            cur = conn.execute(f"PRAGMA foreign_key_list({table})")
            return {r[2] for r in cur.fetchall()}
        if backend == "postgresql":
            cur = conn.execute(
                "SELECT DISTINCT ccu.table_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.table_name = ? AND tc.constraint_type = 'FOREIGN KEY'",
                (table,))
        else:  # mysql
            cur = conn.execute(
                "SELECT DISTINCT REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_NAME = ? AND REFERENCED_TABLE_NAME IS NOT NULL "
                "AND TABLE_SCHEMA = DATABASE()", (table,))
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def topo_sort_tables(conn, backend: str, tables: list) -> list:
    """按外键依赖拓扑排序（父表先拷）。有环时剩余表按原序追加。"""
    tables = list(tables)
    refs = {t: (fk_references(conn, backend, t) & set(tables)) for t in tables}
    ordered, done = [], set()
    pending = list(tables)
    while pending:
        progressed = False
        for t in list(pending):
            if refs[t] <= done:
                ordered.append(t)
                done.add(t)
                pending.remove(t)
                progressed = True
        if not progressed:  # 环：剩余按原序
            ordered.extend(pending)
            break
    return ordered


def table_columns_typed(conn, backend: str, table: str) -> dict:
    """返回 {列名: 声明类型}；sqlite 用 PRAGMA，PG/MySQL 用 information_schema。"""
    out = {}
    try:
        if backend == "sqlite":
            cur = conn.execute(f"PRAGMA table_info({table})")
            for r in cur.fetchall():
                out[r[1]] = (r[2] or "TEXT").upper()
            return out
        if backend == "postgresql":
            cur = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=? "
                "ORDER BY ordinal_position", (table,))
        else:
            cur = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=? "
                "ORDER BY ordinal_position", (table,))
        m = {"character varying": "TEXT", "text": "TEXT", "integer": "INTEGER",
             "bigint": "INTEGER", "numeric": "REAL", "real": "REAL",
             "double precision": "REAL", "int": "INTEGER", "datetime": "TEXT",
             "timestamp": "TEXT", "varchar": "TEXT"}
        for r in cur.fetchall():
            out[r[0]] = m.get(str(r[1]).lower(), "TEXT")
        return out
    except Exception:
        return out


def sync_columns(src, src_backend: str, dst, dst_backend: str, table: str) -> list:
    """把源表有而目标表缺的列补上（ALTER ADD COLUMN），返回补列清单。"""
    src_cols = table_columns_typed(src, src_backend, table)
    dst_cols = set(table_columns_typed(dst, dst_backend, table))
    added = []
    for col, typ in src_cols.items():
        if col in dst_cols:
            continue
        try:
            dst.execute(f"ALTER TABLE {table} ADD COLUMN "
                        f"{_quote_ident(dst_backend, col)} {typ}")
            added.append(col)
        except Exception as e:
            log.warning("[meta_migrate] %s.%s 补列失败: %s", table, col, e)
    if added:
        dst.commit()
    return added


def clear_target(dst, dst_backend: str, tables: list) -> None:
    """清空目标表（目标库视为影子副本，源为权威）。支持重复切换。"""
    if dst_backend == "postgresql":
        if tables:
            dst.execute("TRUNCATE TABLE " + ",".join(tables) + " CASCADE")
    elif dst_backend == "mysql":
        dst.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            dst.execute(f"TRUNCATE TABLE {t}")
        dst.execute("SET FOREIGN_KEY_CHECKS=1")
    else:  # sqlite
        for t in tables:
            try:
                dst.execute(f"DELETE FROM {t}")
            except Exception as e:
                log.debug("[meta_migrate] 清空 %s: %s", t, e)
    dst.commit()


def migrate(src, src_backend: str, dst, dst_backend: str,
            tables: list = None, progress=None) -> dict:
    """全量搬移 + 逐表行数校验。任一表校验不一致抛 RuntimeError。

    src/dst 均为连接（sqlite 原生连接或 _MetaConn 适配器）。
    """
    dst_tables = set(list_tables(dst, dst_backend))
    src_tables = [t for t in list_tables(src, src_backend)
                  if t not in SKIP_TABLES]
    if tables:
        src_tables = [t for t in src_tables if t in set(tables)]
    missing = [t for t in src_tables if t not in dst_tables]
    if missing:
        raise RuntimeError(f"目标库缺表（建表未完成？）: {', '.join(missing[:10])}")

    report = {"tables": [], "total_rows": 0}
    # 外键拓扑排序：父表先拷，避免 PG/MySQL 的 FK 约束报错
    src_tables = topo_sort_tables(dst, dst_backend, src_tables)
    # 清空目标（支持重复切换：目标库是源库的影子副本）
    clear_target(dst, dst_backend, src_tables)
    for i, t in enumerate(src_tables, 1):
        # 列同步：源库上历史 ALTER 出的列，目标库一并补齐
        report.setdefault("added_columns", {})
        added = sync_columns(src, src_backend, dst, dst_backend, t)
        if added:
            report["added_columns"][t] = added
        src_cnt = count_rows(src, src_backend, t)
        info = copy_table(src, src_backend, dst, dst_backend, t)
        dst_cnt = count_rows(dst, dst_backend, t)
        ok = dst_cnt == src_cnt
        report["tables"].append({"table": t, "source": src_cnt,
                                 "target": dst_cnt, "ok": ok,
                                 "copied": info.get("rows", 0)})
        report["total_rows"] += info.get("rows", 0)
        if not ok:
            raise RuntimeError(
                f"表 {t} 搬移后行数不一致：源 {src_cnt} / 目标 {dst_cnt}，"
                f"已中止切换（目标库可删除后重试）")
        if progress:
            progress(i, len(src_tables), t, src_cnt)
    reset_pg_sequences(dst, dst_backend, src_tables)
    return report


def switch_flow(target_cfg: dict, progress=None) -> dict:
    """完整切换流程（在 _write_lock 内执行，调用方持锁）：
    目标建表 -> 全量搬移+校验 -> 热切换 -> 持久化。
    当前后端即目标后端且连接信息相同则拒绝重复切换。
    """
    cur_backend = db.current_backend()
    new_backend = target_cfg["backend"]
    if new_backend == cur_backend:
        raise ValueError(f"当前已是 {new_backend}，无需切换")
    # 目标库建表（幂等）
    dst = db.open_backend_conn(target_cfg)
    try:
        db.init_schema(conn=dst, backend=new_backend)
        src = db.get_conn()
        report = migrate(src, cur_backend, dst, new_backend, progress=progress)
    except Exception:
        try:
            dst.close()
        except Exception:
            pass
        raise
    # 持久化配置（密码加密落盘）+ 热切换
    db.switch_backend(target_cfg, persist=True)
    try:
        dst.close()
    except Exception:
        pass
    log.info("[meta_migrate] 元数据库已切换 %s -> %s，%d 表 %d 行",
             cur_backend, new_backend, len(report["tables"]),
             report["total_rows"])
    return {"from": cur_backend, "to": new_backend, **report}


def rollback_flow(progress=None) -> dict:
    """回切到 SQLite：先把当前后端数据搬回 SQLite（覆盖式），再热切换。

    注意：自上次切换以来 SQLite 侧不会有新写入（写都落在当前后端），
    回切前必须先把当前后端数据完整搬回，否则丢失。
    """
    cur_backend = db.current_backend()
    if cur_backend == "sqlite":
        raise ValueError("当前后端已是 SQLite，无需回切")
    target_cfg = {"backend": "sqlite"}
    # 目标 SQLite：显式连本机 meta.db（活动后端此时仍是旧后端），清空业务表后重搬
    dst = db.open_sqlite_conn()
    db.init_schema(conn=dst, backend="sqlite")
    src = db.get_conn()
    tables = [t for t in list_tables(src, cur_backend) if t not in SKIP_TABLES]
    for t in tables:
        try:
            dst.execute(f"DELETE FROM {t}")
            dst.commit()
        except Exception as e:
            log.debug("[meta_migrate] 清空 %s: %s", t, e)
    report = migrate(src, cur_backend, dst, "sqlite", progress=progress)
    db.switch_backend(target_cfg, persist=True)
    log.info("[meta_migrate] 元数据库已回切 %s -> sqlite，%d 表 %d 行",
             cur_backend, len(report["tables"]), report["total_rows"])
    return {"from": cur_backend, "to": "sqlite", **report}
