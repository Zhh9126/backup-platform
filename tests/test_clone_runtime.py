"""隔离运行时的生命周期与归档、SQL 越界护栏。"""
import io
import json
import sqlite3
import tarfile
from unittest.mock import Mock
import pytest
from core import clone_runtime as runtime


def test_destroy_checks_container_owner(monkeypatch):
    state = {"kind": "redis", "owner": "clone_1", "container": "aidbm-clone_1"}
    monkeypatch.setattr(runtime, "_state", lambda _: state)
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "ps":
            return "container-id"
        return json.dumps([{"Config": {"Labels": {"aidbm.clone.owner": "someone_else"}}}])
    monkeypatch.setattr(runtime, "_run", docker)
    with pytest.raises(ValueError, match="所有权"):
        runtime.destroy(1)
    assert not any(c[0] == "rm" for c in calls)


def test_destroy_missing_container_is_idempotent(monkeypatch):
    monkeypatch.setattr(runtime, "_state", lambda _: {"kind": "redis", "container": "x"})
    run = Mock(return_value="")
    monkeypatch.setattr(runtime, "_run", run)
    assert runtime.destroy(1)["ok"]
    assert run.call_count == 1


@pytest.mark.parametrize("name,kind", [("../outside", "file"), ("/outside", "file"), ("link", "link")])
def test_chroma_archive_rejects_escape(tmp_path, name, kind):
    archive = tmp_path / "data.tar"
    with tarfile.open(archive, "w") as tf:
        member = tarfile.TarInfo(name)
        if kind == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/other"
            tf.addfile(member)
        else:
            member.size = 1
            tf.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="越界"):
        runtime._extract_chroma(archive, tmp_path / "out")


def test_chroma_archive_checks_sqlite(tmp_path):
    source = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE collections (id TEXT)")
    conn.close()
    archive = tmp_path / "data.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(source, arcname="chroma.sqlite3")
    output = tmp_path / "output"
    output.mkdir()
    runtime._extract_chroma(archive, output)
    assert (output / "chroma.sqlite3").exists()


def test_jdbc_artifact_plan_counts_rows_and_strips_schema(tmp_path):
    path = tmp_path / "backup.jsonl"
    rows = [{"type": "table", "name": "source.orders", "ddl": 'CREATE TABLE "orders" ("id" INTEGER)'},
            {"type": "row", "table": "source.orders", "values": [1]}]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert runtime._jdbc_plan(path)["source.orders"]["rows"] == 1


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE orders (id INTEGER); DROP DATABASE source",
    "CREATE TABLE source.orders (id INTEGER)",
    "CREATE TABLE orders (id INTEGER REFERENCES source.orders)",
])
def test_jdbc_rejects_ddl_outside_clone(tmp_path, ddl):
    path = tmp_path / "backup.jsonl"
    path.write_text(json.dumps({"type": "table", "name": "orders", "ddl": ddl}))
    with pytest.raises(ValueError, match="专用适配"):
        runtime._jdbc_plan(path)


def test_jdbc_checks_actual_namespace_before_write(monkeypatch):
    conn = Mock()
    conn.jconn.getCatalog.return_value = "production"
    conn.jconn.getSchema.return_value = "public"
    engine = Mock()
    engine._conn.return_value = conn
    monkeypatch.setattr(runtime, "_jdbc_engine", lambda _: engine)
    with pytest.raises(ValueError, match="命名空间"):
        runtime._jdbc_connection({"owner": "clone_1"})
    conn.cursor.assert_not_called()
    conn.close.assert_called_once()


def test_preflight_reports_missing_docker(monkeypatch):
    monkeypatch.setattr(runtime, "_run", Mock(side_effect=FileNotFoundError("docker")))
    with pytest.raises(FileNotFoundError):
        runtime.preflight({"db_type": "redis"})


def test_redis_launch_tracks_failure_for_cleanup(monkeypatch, tmp_path):
    source = tmp_path / "bad.rdb"
    source.write_bytes(b"invalid")
    states = {}
    monkeypatch.setattr(runtime, "preflight", lambda *a: "ok")
    monkeypatch.setattr(runtime, "_source_task", lambda *a: {})
    monkeypatch.setattr(runtime, "_save", lambda rid, state: states.update({rid: dict(state)}))
    monkeypatch.setattr(runtime, "_run", lambda *a, **k: "")
    register = Mock()
    with pytest.raises(ValueError, match="RDB"):
        runtime.launch({"id": 1}, {"db_type": "redis", "backup_path": str(source)}, register)
    register.assert_called_once()
    assert states[1]["container"] == "aidbm-clone_1"


def test_jdbc_restore_verify_destroy_flow_with_real_sqlite_rows(tmp_path, monkeypatch):
    """以 SQLite 执行真实建表、参数化插入和行数核对，替代外部 JDBC 驱动。"""
    path = tmp_path / "backup.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in [
        {"type": "table", "name": "src.orders", "ddl": 'CREATE TABLE "orders" ("id" INTEGER)'},
        {"type": "row", "table": "src.orders", "values": [1]},
        {"type": "row", "table": "src.orders", "values": [2]},
    ]))
    target = {"id": 2, "db_type": "jdbc_generic", "host": "target", "port": 5432,
              "extra_options": {"jdbc_db_type": "postgresql"}}
    source = {"id": 1, "extra_options": {"jdbc_db_type": "postgresql", "clone_jdbc": {
        "target_task_id": 2, "create_sql": "create {name}", "drop_sql": "drop {name}",
        "exists_sql": "exists {name}"}}}
    monkeypatch.setattr(runtime, "_source_task", lambda _: source)
    monkeypatch.setattr(runtime.models, "get_task", lambda *a, **k: target)
    states = {}
    exists = []
    monkeypatch.setattr(runtime, "_save", lambda rid, state: states.update({rid: state}))
    monkeypatch.setattr(runtime, "_state", lambda rid: states[rid])
    def lifecycle(state, key):
        if key == "exists_sql":
            return list(exists)
        if key == "create_sql":
            exists.append(("clone_1",))
        else:
            exists.clear()
        return []
    monkeypatch.setattr(runtime, "_jdbc_sql", lifecycle)
    engine = Mock()
    engine._qi.side_effect = lambda name: '"' + name + '"'
    def connection(state):
        return engine, sqlite3.connect(tmp_path / "clone.db")
    monkeypatch.setattr(runtime, "_jdbc_connection", connection)
    register = Mock()
    result = runtime._jdbc_launch({"id": 1}, {"backup_path": str(path)}, register)
    assert result["host"] == "target"
    assert runtime.verify(1)["ok"]
    conn = sqlite3.connect(tmp_path / "clone.db")
    try:
        assert conn.execute("SELECT id FROM orders ORDER BY id").fetchall() == [(1,), (2,)]
        conn.execute("DELETE FROM orders WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    assert not runtime.verify(1)["ok"]
    assert runtime.destroy(1)["ok"]
    assert runtime.destroy(1)["ok"]
    register.assert_called_once()


def test_redis_container_lifecycle_uses_dedicated_volume_and_loopback(tmp_path, monkeypatch):
    source = tmp_path / "dump.rdb"
    source.write_bytes(b"REDIS-test-fixture")
    states = {}
    calls = []
    monkeypatch.setattr(runtime, "preflight", lambda *a: "ok")
    monkeypatch.setattr(runtime, "_source_task", lambda *a: {})
    monkeypatch.setattr(runtime, "_save", lambda rid, state: states.update({rid: dict(state)}))
    monkeypatch.setattr(runtime, "_state", lambda rid: states[rid])
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([{
                "Config": {"Labels": {"aidbm.clone.owner": "clone_1"}},
                "State": {"Running": True},
                "NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": "32100"}]}}}])
        if args[0] == "exec":
            return "PONG" if args[-1] == "PING" else "# Keyspace\ndb0:keys=2"
        return ""
    monkeypatch.setattr(runtime, "_run", docker)
    register = Mock()
    result = runtime.launch({"id": 1}, {"db_type": "redis", "backup_path": str(source)}, register)
    assert result["port"] == 32100
    assert runtime.verify(1)["ok"]
    create = next(c for c in calls if c[0] == "create")
    assert "127.0.0.1::6379" in create
    assert "aidbm.clone.owner=clone_1" in create
    assert any(c[0] == "cp" and c[-1] == "aidbm-clone_1:/data/dump.rdb" for c in calls)


def test_docker_missing_is_actionable(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    with pytest.raises(ValueError, match="未安装 Docker"):
        runtime._run("info")


def test_service_routes_runtime_and_updates_endpoint(monkeypatch):
    from core import clone_service as service
    monkeypatch.setattr(service.models, "get_record", lambda _: {
        "db_type": "redis", "backup_path": "/fixture.rdb", "task_id": 2})
    monkeypatch.setattr(service.models, "update_clone_request", Mock())
    execute = Mock()
    monkeypatch.setattr(service.db, "execute", execute)
    svc = service.CloneService()
    register = Mock(return_value=71)
    monkeypatch.setattr(svc, "_register_vdb", register)
    def launch(request, record, allocated):
        allocated("clone_1", "127.0.0.1", None, "")
        return {"port": 31001}
    monkeypatch.setattr(runtime, "launch", launch)
    assert svc._launch_vdb({"id": 1, "source_record_id": 5}) == 71
    assert execute.call_args.args[1] == ("127.0.0.1", 31001, 71)


def test_runtime_preflight_is_visible_as_failed_check(monkeypatch, tmp_path):
    from core import clone_preflight
    path = tmp_path / "source.rdb"
    path.write_bytes(b"REDIS-fixture")
    monkeypatch.setattr(runtime, "preflight", Mock(side_effect=ValueError("需要 Docker 镜像")))
    result = clone_preflight.preflight(
        {"db_type": "redis", "status": "success", "backup_path": str(path)})
    assert not result["ok"]
    assert any(c["item"] == "isolated_runtime" and c["level"] == "fail" for c in result["checks"])
