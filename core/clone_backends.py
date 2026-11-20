"""无 Agent 克隆后端，复用已注册的数据库恢复引擎。"""
import json
import re

from core import db, models, native_conn

MYSQL_TYPES = {"tidb", "oceanbase", "greatdb", "goldendb"}
PG_TYPES = {"opengauss", "panwei", "highgo", "uxdb", "gaussdb", "vastbase"}
ENGINE_TYPES = MYSQL_TYPES | PG_TYPES | {"sqlserver", "mongodb", "qdrant"}
PORTS = dict.fromkeys(MYSQL_TYPES, 3306)
PORTS.update(dict.fromkeys(PG_TYPES, 5432))
PORTS.update(opengauss=26000, panwei=12800, sqlserver=1433, mongodb=27017, qdrant=6333)


def target_task(rec, host, port, password):
    source = models.get_task(rec.get("task_id"), include_secret=True) or {}
    task = dict(source, host=host, port=port, password=password, demo_only=False)
    # SSH 绑定属于源任务，不能用于跨主机恢复。
    if host != source.get("host"):
        raise ValueError("此库型跨主机克隆须先配置目标实例任务，当前仅接受源实例上的异名克隆")
    return task


def _sql_conn(kind, host, port, database, user, password):
    family = "mysql" if kind in MYSQL_TYPES else "postgresql"
    if kind == "sqlserver":
        import pymssql
        return pymssql.connect(server=host, port=str(port), user=user,
                               password=password, database=database, login_timeout=10)
    return native_conn.connect(family, host, port, database, user, password)


def _mongo(host, port, user, password):
    from pymongo import MongoClient
    return MongoClient(host=host, port=int(port), username=user or None,
                       password=password or None, serverSelectionTimeoutMS=10000)


def _qdrant(method, host, port, name, password):
    from urllib.parse import quote
    from core.engines.qdrant import _http
    return _http(method, f"http://{host}:{port}/collections/{quote(name, safe='')}",
                 headers={"api-key": password} if password else {})


def operate(kind, host, port, name, user, password, drop=False):
    if not re.fullmatch(r"clone_[0-9]+", name or ""):
        raise ValueError("克隆后端仅管理系统分配的 clone_N 名称")
    if kind == "mongodb":
        with _mongo(host, port, user, password) as client:
            if drop:
                client.drop_database(name)
                return {"ok": True, "message": "克隆数据库已删除"}
            collections = client[name].list_collection_names()
            return {"ok": bool(collections), "tables": len(collections),
                    "message": f"集合数量 {len(collections)}"}
    if kind == "qdrant":
        _, body = _qdrant("DELETE" if drop else "GET", host, port, name, password)
        result = json.loads(body)
        if drop:
            return {"ok": bool(result.get("result")), "message": "克隆 collection 删除结果"}
        info = result.get("result") or {}
        return {"ok": info.get("status") == "green",
                "message": f"status={info.get('status')} points={info.get('points_count')}"}
    database = ("master" if kind == "sqlserver" else
                ("mysql" if kind in MYSQL_TYPES else "postgres")) if drop else name
    conn = _sql_conn(kind, host, port, database, user, password)
    try:
        if drop and kind in PG_TYPES:
            conn.autocommit = True
        cur = conn.cursor()
        try:
            if drop:
                quoted = f"`{name}`" if kind in MYSQL_TYPES else (
                    f"[{name}]" if kind == "sqlserver" else f'"{name}"')
                cur.execute(f"DROP DATABASE {quoted}")
                if kind not in PG_TYPES:
                    conn.commit()
                return {"ok": True, "message": "克隆数据库已删除"}
            if kind in MYSQL_TYPES:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s",
                            (name,))
            else:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_type='BASE TABLE' AND table_schema NOT IN "
                            "('pg_catalog','information_schema') AND table_schema NOT LIKE 'sys%'")
            count = int(cur.fetchone()[0])
            return {"ok": count > 0, "tables": count, "message": f"用户表数量 {count}"}
        finally:
            cur.close()
    finally:
        conn.close()


def launch(rec, name, host, port, user, password, root, on_allocated):
    from core.engines import get_engine
    kind = rec["db_type"]
    task = target_task(rec, host, port, password)
    task["username"] = user
    # 恢复前确认目标不存在，重试也不能覆盖一个既有数据库。
    if kind == "qdrant":
        from core.engines.qdrant import QdrantEngine
        probe_task = dict(task, db_name="")
        existing = QdrantEngine(probe_task, root)._collections()
    elif kind == "mongodb":
        with _mongo(host, port, user, password) as client:
            existing = client.list_database_names()
    else:
        conn = _sql_conn(kind, host, port,
                         "master" if kind == "sqlserver" else
                         ("mysql" if kind in MYSQL_TYPES else "postgres"), user, password)
        try:
            cur = conn.cursor()
            try:
                cur.execute("SHOW DATABASES" if kind in MYSQL_TYPES else
                            ("SELECT name FROM sys.databases" if kind == "sqlserver" else
                             "SELECT datname FROM pg_database"))
                existing = [r[0] for r in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()
    if name in existing:
        raise ValueError(f"目标库已存在，克隆终止：{name}")
    on_allocated()
    if kind in PG_TYPES:
        conn = _sql_conn(kind, host, port, "postgres", user, password)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                cur.execute(f'CREATE DATABASE "{name}"')
            finally:
                cur.close()
        finally:
            conn.close()
    engine = get_engine(kind, task, root)
    result = engine.restore(rec["backup_path"], target_db=name)
    if not result.success or getattr(result, "simulated", False):
        raise RuntimeError(f"真实克隆失败：{result.message}")
    verified = operate(kind, host, port, name, user, password)
    if not verified["ok"]:
        raise RuntimeError(f"恢复后校验失败：{verified['message']}")
    return verified


def capabilities():
    from core.engines import ENGINE_REGISTRY
    from core.clone_runtime import KINDS
    legacy = {"mysql", "mariadb", "postgresql", "kingbase", "oracle", "dameng"}
    excluded = {"file", "object_storage", "synology", "vm"}
    result = []
    for kind, engine in sorted(ENGINE_REGISTRY.items()):
        if kind in excluded:
            continue
        supported = kind in legacy | ENGINE_TYPES | KINDS
        result.append({
            "db_type": kind, "name": engine.display_name,
            "clone": supported, "agentless": True,
            "isolation": ("container" if kind in {"redis", "neo4j", "chroma"} else
                          "schema" if kind in {"oracle", "dameng"} else "database"),
            "message": ("需管理服务器 Docker 和预置镜像，恢复到专用容器"
                        if kind in {"redis", "neo4j", "chroma"} else
                        "需目标管理任务和厂商命名空间 SQL，恢复后逐表核对行数"
                        if kind == "jdbc_generic" else
                        "备份恢复到独立命名空间，实际依赖由预检和恢复结果判定"
                        if supported else
                        "需要专用隔离实例和专用销毁适配器，当前入口暂未实现"),
            "live_mount": False, "network_sandbox": False,
        })
    return result
