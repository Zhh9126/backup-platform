# -*- coding: utf-8 -*-
"""元数据库后端管理 API：状态 / 测试连接 / 预检 / 迁移切换 / 回切。

页面入口：系统设置 -> 元数据库。
切换语义：加全局写锁 -> 目标库幂等建表 -> 全量搬移+逐表行数校验 ->
热切换连接工厂 + 持久化 instance/meta_backend.json（重启自动生效）。
SQLite 文件保留，可一键回切（回切前先把当前后端数据搬回 SQLite）。
"""
from flask import jsonify, request

from core import db, meta_migrate
from . import api_bp


def _err(msg, code=400):
    return jsonify({"success": False, "error": str(msg)}), code


def _cfg_from_request() -> dict:
    data = request.get_json(force=True, silent=True) or {}
    backend = (data.get("backend") or "").strip().lower()
    if backend not in ("postgresql", "mysql"):
        raise ValueError("backend 仅支持 postgresql / mysql")
    cfg = {
        "backend": backend,
        "host": (data.get("host") or "127.0.0.1").strip(),
        "port": str(data.get("port") or "").strip(),
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "name": (data.get("name") or "").strip(),
        "allow_missing": bool(data.get("allow_missing")),
    }
    if not cfg["port"]:
        cfg["port"] = "5432" if backend == "postgresql" else "3306"
    if not cfg["user"] or not cfg["name"]:
        raise ValueError("用户名与数据库名不能为空")
    return cfg


def _ensure_pg_database(cfg: dict) -> None:
    """PG：库不存在时自动创建（勾选 allow_missing 时）。"""
    if not cfg.get("allow_missing"):
        return
    try:
        conn = db.open_backend_conn(cfg)
        conn.close()
        return  # 库已存在
    except Exception as e:
        if "does not exist" not in str(e) and "3D000" not in str(e):
            raise
    import psycopg2
    maint = dict(cfg, name="postgres")
    conn = psycopg2.connect(host=maint["host"], port=int(maint["port"]),
                            user=maint["user"], password=maint["password"],
                            dbname="postgres", connect_timeout=10)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute('CREATE DATABASE "%s"' % cfg["name"].replace('"', ""))
        cur.close()
    finally:
        conn.close()


def _driver_status() -> dict:
    out = {}
    for name, mod in (("postgresql", "psycopg2"), ("mysql", "pymysql")):
        try:
            __import__(mod)
            out[name] = True
        except ImportError:
            out[name] = False
    return out


@api_bp.get("/meta-db/status")
def meta_db_status():
    st = db.backend_status()
    st["drivers"] = _driver_status()
    return jsonify({"success": True, **st})


@api_bp.post("/meta-db/test")
def meta_db_test():
    """测试目标库连通性（不建表、不写数据）。"""
    try:
        cfg = _cfg_from_request()
    except ValueError as e:
        return _err(e)
    try:
        _ensure_pg_database(cfg)
        conn = db.open_backend_conn(cfg)
        try:
            if cfg["backend"] == "postgresql":
                row = conn.execute("SELECT version()").fetchone()
            else:
                row = conn.execute("SELECT VERSION()").fetchone()
            ver = (row[0] or "")[:60] if row else ""
        finally:
            conn.close()
        return jsonify({"success": True, "message": f"连接成功：{ver}"})
    except Exception as e:
        return _err(f"连接失败：{e}")


@api_bp.post("/meta-db/plan")
def meta_db_plan():
    """预检：目标库建表（幂等）+ 逐表源行数 vs 目标行数预览，不切换。"""
    try:
        cfg = _cfg_from_request()
    except ValueError as e:
        return _err(e)
    try:
        _ensure_pg_database(cfg)
        dst = db.open_backend_conn(cfg)
        try:
            db.init_schema(conn=dst, backend=cfg["backend"])
            src = db.get_conn()
            src_backend = db.current_backend()
            src_tables = [t for t in meta_migrate.list_tables(src, src_backend)
                          if t not in meta_migrate.SKIP_TABLES]
            dst_tables = set(meta_migrate.list_tables(dst, cfg["backend"]))
            rows = []
            for t in src_tables:
                try:
                    c = meta_migrate.count_rows(src, src_backend, t)
                except Exception:
                    c = -1
                rows.append({"table": t, "source": c,
                             "target_exists": t in dst_tables})
            return jsonify({"success": True, "tables": rows,
                            "table_count": len(rows)})
        finally:
            dst.close()
    except Exception as e:
        return _err(f"预检失败：{e}")


@api_bp.post("/meta-db/migrate")
def meta_db_migrate():
    """执行切换（同步、持全局写锁）。元库数据量大时请求可能耗时较长。"""
    try:
        cfg = _cfg_from_request()
    except ValueError as e:
        return _err(e)
    events = []

    def progress(done, total, table, cnt):
        events.append(f"{done}/{total} {table}({cnt}行)")

    try:
        _ensure_pg_database(cfg)
        with db._write_lock:
            report = meta_migrate.switch_flow(cfg, progress=progress)
        report["progress"] = events
        return jsonify({"success": True, **report})
    except Exception as e:
        return _err(f"切换失败：{e}", 500)


@api_bp.post("/meta-db/rollback")
def meta_db_rollback():
    """回切到 SQLite：先把当前后端数据全量搬回，再热切换。"""
    events = []

    def progress(done, total, table, cnt):
        events.append(f"{done}/{total} {table}({cnt}行)")

    try:
        with db._write_lock:
            report = meta_migrate.rollback_flow(progress=progress)
        report["progress"] = events
        return jsonify({"success": True, **report})
    except Exception as e:
        return _err(f"回切失败：{e}", 500)
