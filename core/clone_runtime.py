"""管理服务器上的隔离克隆执行器。仅管理带所有权标签的专用容器。"""
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import config
from core import db, models

KINDS = {"redis", "neo4j", "chroma", "jdbc_generic"}
IMAGES = {"redis": "redis:7.4", "neo4j": "neo4j:5.26", "chroma": "chromadb/chroma:0.6.3"}
INTERNAL_PORTS = {"redis": 6379, "neo4j": 7687, "chroma": 8000}


def _run(*args, timeout=120):
    try:
        result = subprocess.run(["docker", *map(str, args)], capture_output=True,
                                text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ValueError("管理服务器未安装 Docker，隔离运行时暂不可执行") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker 隔离操作超时，请检查克隆资源状态后重试") from exc
    if result.returncode:
        raise ValueError("隔离克隆执行失败：" + result.stderr[-500:])
    return result.stdout.strip()


def _state(request_id):
    raw = db.get_system_config(f"clone_runtime_{int(request_id)}")
    if not raw:
        raise ValueError("隔离克隆资源台账缺失")
    return json.loads(raw)


def _save(request_id, state):
    db.set_system_config(f"clone_runtime_{int(request_id)}", json.dumps(state))


def _owned(state):
    raw = _run("inspect", state["container"])
    info = json.loads(raw)[0]
    labels = info.get("Config", {}).get("Labels") or {}
    if labels.get("aidbm.clone.owner") != state["owner"]:
        raise ValueError("容器所有权标签不匹配，终止操作")
    return info


def _source_task(rec):
    return models.get_task(rec.get("task_id"), include_secret=True) or {}


def preflight(rec, host="127.0.0.1"):
    kind = rec.get("db_type")
    if kind == "jdbc_generic":
        options = _extra(_source_task(rec)).get("clone_jdbc") or {}
        for key in ("target_task_id", "create_sql", "drop_sql", "exists_sql"):
            if not options.get(key):
                raise ValueError(f"通用 JDBC 需在源任务 extra_options.clone_jdbc 配置 {key}")
        for key in ("create_sql", "drop_sql", "exists_sql"):
            statement = options[key]
            if not isinstance(statement, str) or "{name}" not in statement:
                raise ValueError(f"JDBC {key} 必须包含系统分配的 {{name}} 占位符")
            if ";" in statement:
                raise ValueError("JDBC 生命周期 SQL 每项仅接受一条语句")
        target = models.get_task(int(options["target_task_id"]), include_secret=True)
        if not target:
            raise ValueError("JDBC 目标管理任务不存在")
        source = _source_task(rec)
        if target["id"] == source.get("id"):
            raise ValueError("JDBC 克隆需使用独立目标管理任务")
        return "JDBC 独立目标与厂商生命周期 SQL 已配置"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("容器克隆当前在管理服务器执行，目标主机请选 127.0.0.1")
    _run("info", "--format", "{{.ServerVersion}}", timeout=10)
    image = _extra(_source_task(rec)).get("clone_image") or IMAGES[kind]
    _run("image", "inspect", image, timeout=10)
    return f"Docker 可用，隔离镜像 {image} 已就绪"


def _extra(task):
    raw = task.get("extra_options") or {}
    return json.loads(raw) if isinstance(raw, str) else raw


def launch(req, rec, register):
    kind = rec["db_type"]
    preflight(rec, req.get("target_host") or "127.0.0.1")
    if kind == "jdbc_generic":
        return _jdbc_launch(req, rec, register)
    owner = f"clone_{int(req['id'])}"
    state = {"kind": kind, "owner": owner, "container": f"aidbm-{owner}",
             "request_id": req["id"]}
    # 创建前排除同名资源，台账在分配资源前持久化，以覆盖部分失败。
    existing = _run("ps", "-aq", "--filter", f"name=^/{state['container']}$")
    if existing:
        raise ValueError("同名克隆容器已存在，请先销毁旧克隆资源")
    _save(req["id"], state)
    image = _extra(_source_task(rec)).get("clone_image") or IMAGES[kind]
    args = ["create", "--name", state["container"], "--label", f"aidbm.clone.owner={owner}",
            "--memory", "2g", "--cpus", "2", "--security-opt", "no-new-privileges",
            "-p", f"127.0.0.1::{INTERNAL_PORTS[kind]}"]
    if kind == "neo4j":
        # 本机随机端口仅供平台访问，数据库不允许出站连接的网络沙箱需独立实现。
        args += ["-e", "NEO4J_AUTH=none"]
    if kind == "chroma":
        args += ["-e", "IS_PERSISTENT=TRUE", "-e", "PERSIST_DIRECTORY=/chroma/chroma"]
    if kind == "redis":
        args += [image, "redis-server", "--dir", "/data", "--dbfilename", "dump.rdb",
                 "--appendonly", "no"]
    else:
        args += [image]
    _run(*args)
    register(owner, "127.0.0.1", None, "")
    source = Path(rec["backup_path"])
    if kind == "redis":
        with tempfile.TemporaryDirectory(prefix="clone-rdb-") as directory:
            output = Path(directory) / "dump.rdb"
            opener = gzip.open if str(source).endswith(".gz") else open
            with opener(source, "rb") as src, output.open("wb") as dst:
                if src.read(5) != b"REDIS":
                    raise ValueError("Redis 克隆需要 RDB 或 gzip RDB 产物")
                dst.write(b"REDIS")
                shutil.copyfileobj(src, dst)
            _run("cp", output, f"{state['container']}:/data/dump.rdb")
            _run("start", state["container"])
    elif kind == "chroma":
        with tempfile.TemporaryDirectory(prefix="clone-chroma-") as directory:
            _extract_chroma(source, Path(directory))
            _run("cp", directory + "/.", f"{state['container']}:/chroma/chroma")
        _run("start", state["container"])
    else:
        if not str(source).endswith(".dump"):
            raise ValueError("Neo4j 隔离克隆当前接受 neo4j-admin 的 .dump 产物")
        _run("cp", source, f"{state['container']}:/data/neo4j.dump")
        _run("start", state["container"])
        # 容器首次启动后停默认库再加载，使用独立一次性工具容器共享数据卷。
        _run("stop", state["container"])
        _run("run", "--rm", "--volumes-from", state["container"],
             "--entrypoint", "neo4j-admin", image, "database", "load", "neo4j",
             "--from-path=/data", "--overwrite-destination=true", timeout=1800)
        _run("start", state["container"])
    info = _owned(state)
    bindings = info["NetworkSettings"]["Ports"][f"{INTERNAL_PORTS[kind]}/tcp"]
    state["port"] = int(bindings[0]["HostPort"])
    _save(req["id"], state)
    # 就绪等待有上限，容器异常保留资源台账供重试销毁。
    last = None
    for _ in range(30):
        try:
            result = verify(req["id"])
            if result["ok"]:
                return state
            last = result["message"]
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(1)
    raise ValueError(f"隔离克隆未就绪：{last}")


def _extract_chroma(source, directory):
    with tarfile.open(source, "r:*") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Chroma 归档包含越界路径或链接")
        for member in archive.getmembers():
            destination = directory / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, destination.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    database = directory / "chroma.sqlite3"
    if not database.is_file():
        raise ValueError("Chroma 归档缺少 chroma.sqlite3")
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Chroma SQLite 完整性校验失败")
    finally:
        conn.close()


def verify(request_id):
    state = _state(request_id)
    if state["kind"] == "jdbc_generic":
        return _jdbc_verify(state)
    info = _owned(state)
    if not info["State"]["Running"]:
        return {"ok": False, "message": "克隆容器未运行"}
    container = state["container"]
    if state["kind"] == "redis":
        pong = _run("exec", container, "redis-cli", "PING")
        keys = _run("exec", container, "redis-cli", "INFO", "keyspace")
        return {"ok": pong == "PONG", "message": keys}
    if state["kind"] == "neo4j":
        value = _run("exec", container, "cypher-shell", "-d", "neo4j",
                     "MATCH (n) RETURN count(n) AS nodes", timeout=15)
        return {"ok": True, "message": value}
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{state['port']}/api/v1/heartbeat",
                                timeout=5) as response:
        return {"ok": response.status == 200, "message": "Chroma 心跳正常，归档 SQLite 校验通过"}


def destroy(request_id):
    state = _state(request_id)
    if state["kind"] == "jdbc_generic":
        return _jdbc_destroy(state)
    # 删除可重试，Docker 查询失败与资源不存在分开处理。
    ids = _run("ps", "-aq", "--filter", f"name=^/{state['container']}$")
    if ids:
        _owned(state)
        _run("rm", "-fv", state["container"])
    return {"ok": True, "message": "专用克隆容器及匿名数据卷已回收"}


def _jdbc_engine(state):
    from core.engines.jdbc_logical import JDBCLogicalEngine
    task = json.loads(db.decrypt_secret(state["target_task"]))
    return JDBCLogicalEngine(task, config.BACKUP_ROOT)


def _jdbc_sql(state, key):
    engine = _jdbc_engine(state)
    conn = engine._conn()
    try:
        raw = getattr(conn, "jconn", None)
        if raw is not None:
            raw.setAutoCommit(True)
        cur = conn.cursor()
        try:
            cur.execute(state["sql"][key].format(name=state["owner"]))
            rows = cur.fetchall() if key == "exists_sql" else []
            conn.commit()
            return rows
        finally:
            cur.close()
    finally:
        conn.close()


def _jdbc_connection(state):
    engine = _jdbc_engine(state)
    conn = engine._conn(database=state["owner"])
    try:
        raw = getattr(conn, "jconn", None)
        catalog = str(raw.getCatalog() or "") if raw is not None else ""
        schema = (str(raw.getSchema() or "")
                  if raw is not None and catalog.lower() != state["owner"].lower() else "")
        if state["owner"].lower() not in {catalog.lower(), schema.lower()}:
            raise ValueError("JDBC 连接未切入本次克隆命名空间，拒绝执行恢复")
    except Exception:
        conn.close()
        raise
    return engine, conn


def _jdbc_plan(path):
    """只接受单表 CREATE TABLE 产物，隔离恢复不执行任意 SQL。"""
    tables = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("type") == "table":
                source = item["name"]
                name = source.rsplit(".", 1)[-1]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    raise ValueError("JDBC 克隆表名含不支持的字符")
                ddl = item.get("ddl") or ""
                match = re.fullmatch(
                    r'\s*CREATE\s+TABLE\s+[`"]?[A-Za-z_][A-Za-z0-9_]*[`"]?\s*(\(.*\))\s*',
                    ddl, re.I | re.S)
                if not match or re.search(r";|--|/\*|\.|\bREFERENCES\b", match[1], re.I):
                    raise ValueError(f"JDBC 表 {source} 的 DDL 需要专用适配，已停止隔离恢复")
                if name in {t["name"] for t in tables.values()}:
                    raise ValueError("JDBC 不同 schema 存在同名表，无法合并克隆")
                tables[source] = {"name": name, "columns_ddl": match[1], "rows": 0}
            elif item.get("type") == "row":
                if item["table"] not in tables:
                    raise ValueError("JDBC 产物数据行缺少表定义")
                tables[item["table"]]["rows"] += 1
    if not tables:
        raise ValueError("JDBC 产物不含表定义")
    return tables


def _jdbc_launch(req, rec, register):
    from core.engines.jdbc_logical import _decode
    options = _extra(_source_task(rec))["clone_jdbc"]
    target = models.get_task(int(options["target_task_id"]), include_secret=True)
    if target.get("db_type") != "jdbc_generic":
        raise ValueError("JDBC 克隆目标任务需使用 jdbc_generic 引擎")
    source = _source_task(rec)
    if (not _extra(source).get("jdbc_db_type") or
            _extra(source).get("jdbc_db_type") != _extra(target).get("jdbc_db_type")):
        raise ValueError("JDBC 源和目标的 jdbc_db_type 必须一致")
    plan = _jdbc_plan(rec["backup_path"])
    state = {"kind": "jdbc_generic", "owner": f"clone_{req['id']}",
             "request_id": req["id"], "target_task": db.encrypt_secret(json.dumps(target)),
             "sql": {k: options[k] for k in ("create_sql", "drop_sql", "exists_sql")},
             "tables": plan}
    if _jdbc_sql(state, "exists_sql"):
        raise ValueError("JDBC 目标命名空间已存在")
    _save(req["id"], state)
    register(state["owner"], target["host"], target.get("port"), target.get("username") or "")
    _jdbc_sql(state, "create_sql")
    engine, conn = _jdbc_connection(state)
    try:
        cur = conn.cursor()
        try:
            for table in plan.values():
                cur.execute(f"CREATE TABLE {engine._qi(table['name'])} {table['columns_ddl']}")
            with open(rec["backup_path"], encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    if item.get("type") == "row":
                        table = plan[item["table"]]["name"]
                        values = [_decode(v) for v in item["values"]]
                        placeholders = ",".join("?" for _ in values)
                        cur.execute(f"INSERT INTO {engine._qi(table)} VALUES ({placeholders})", values)
            conn.commit()
        finally:
            cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _jdbc_verify(state)
    if not result["ok"]:
        raise ValueError(result["message"])
    return {**state, "host": target["host"], "port": target.get("port")}


def _jdbc_verify(state):
    engine, conn = _jdbc_connection(state)
    try:
        cur = conn.cursor()
        try:
            for table in state["tables"].values():
                cur.execute(f"SELECT COUNT(*) FROM {engine._qi(table['name'])}")
                if int(cur.fetchone()[0]) != table["rows"]:
                    return {"ok": False, "message": f"表 {table['name']} 行数与备份不一致"}
            return {"ok": True, "message": f"JDBC {len(state['tables'])} 张表行数与备份一致"}
        finally:
            cur.close()
    finally:
        conn.close()


def _jdbc_destroy(state):
    if _jdbc_sql(state, "exists_sql"):
        _jdbc_sql(state, "drop_sql")
    if _jdbc_sql(state, "exists_sql"):
        raise ValueError("JDBC 克隆命名空间删除后仍存在")
    return {"ok": True, "message": "JDBC 克隆命名空间已回收"}
