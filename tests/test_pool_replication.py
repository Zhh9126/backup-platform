# -*- coding: utf-8 -*-
"""P1-3 池复制回归：链 CRUD / serial 级联与中断 / parallel 扇出 / 副本分布 / 异地取回。

用 local 存储后端真读真写（三站点=三个不同目录池），备份产物为真实文件。
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="pool_e2e_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "bk")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import core.db as db
db.init_schema()

import pytest

from core import pool_replication as pr


def _mk_target(name, root):
    d = os.path.join(_TMP, root)
    os.makedirs(d, exist_ok=True)
    return db.execute(
        "INSERT INTO storage_targets(name, type, endpoint, enabled, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?)",
        (name, "local", d, 1, db.now_iso(), db.now_iso()))


def _mk_record(task_id=1):
    tid = db.execute(
        "INSERT INTO backup_tasks(db_type, name, host, port, username, "
        "db_name, backup_type, backup_mode, enabled, created_at, updated_at, "
        "last_status) VALUES ('mysql','pool-tk','127.0.0.1',3306,'root','db1',"
        "'FULL','logical',0,?,?, 'never')", (db.now_iso(), db.now_iso()))
    rid = db.execute(
        "INSERT INTO backup_records(task_id, backup_path, status, started_at, "
        "size_bytes) VALUES (?,?,?,?,?)",
        (tid, "/tmp/pool_src.tar.gz", "success", db.now_iso(), 1024))
    return rid


@pytest.fixture()
def env():
    # 三个站点池
    ta = _mk_target("池A-生产", "poolA")
    tb = _mk_target("池B-同城", "poolB")
    tc = _mk_target("池C-异地", "poolC")
    rid = _mk_record()
    src = os.path.join(_TMP, "src_backup.tar.gz")
    with open(src, "wb") as f:
        f.write(os.urandom(4096))
    yield {"targets": [ta, tb, tc], "rid": rid, "src": src}
    db.execute("DELETE FROM pool_replica_logs")
    db.execute("DELETE FROM pool_chains")
    db.execute("DELETE FROM storage_targets")
    db.execute("DELETE FROM backup_records")
    db.execute("DELETE FROM backup_tasks")


def _chain(env, mode="serial", sites=None, enabled=1, policy=None):
    sites = sites or [
        {"target_id": env["targets"][0], "site_role": "production", "name": "北京生产"},
        {"target_id": env["targets"][1], "site_role": "same_city", "name": "上海同城"},
        {"target_id": env["targets"][2], "site_role": "remote", "name": "广州异地"},
    ]
    return pr.create_chain({"name": "两地三中心", "mode": mode, "sites": sites,
                            "policy": policy or {}, "enabled": enabled})


def test_chain_crud_and_site_normalize(env):
    cid = _chain(env)
    ch = pr.get_chain(cid)
    assert ch["mode"] == "serial"
    assert [s["seq"] for s in ch["sites"]] == [1, 2, 3]
    assert ch["sites"][0]["site_role_cn"] == "生产中心"
    assert ch["sites"][2]["site_role_cn"] == "异地灾备"
    # 目标不存在 → 诚实拒绝
    with pytest.raises(pr.PoolError, match="存储目标不存在"):
        pr.create_chain({"name": "bad", "sites": [{"target_id": 99999}]})


def test_serial_chain_replicates_all_pools(env):
    cid = _chain(env)
    r = pr.run_chain(cid, env["rid"], env["src"])
    assert r["ok"] is True
    assert [s["status"] for s in r["sites"]] == ["success"] * 3
    # 三个池真实落盘
    for t in env["targets"]:
        row = db.query_one("SELECT endpoint FROM storage_targets WHERE id=?", (t,))
        files = os.listdir(row["endpoint"])
        assert files, f"池 {row['endpoint']} 未落盘"
    # last_result 更新
    ch = pr.get_chain(cid)
    assert ch["last_result"]["ok"] is True


def test_serial_chain_stops_on_failure(env):
    # 中间站点目标失效（endpoint 指向文件而非目录 → 后端报错）
    bad = os.path.join(_TMP, "not_a_dir")
    with open(bad, "w") as f:
        f.write("x")
    db.execute("UPDATE storage_targets SET endpoint=? WHERE id=?",
               (bad, env["targets"][1]))
    cid = _chain(env, policy={"max_retries": 1, "retry_interval": 1})
    r = pr.run_chain(cid, env["rid"], env["src"])
    assert r["ok"] is False
    assert r["sites"][0]["status"] == "success"     # A 成功
    assert r["sites"][1]["status"] == "failed"      # B 失败
    assert r["sites"][2]["status"] == "skipped"     # C 级联中断
    assert "级联中断" in r["sites"][2]["error"]


def test_parallel_chain_fanout(env):
    cid = _chain(env, mode="parallel")
    r = pr.run_chain(cid, env["rid"], env["src"])
    assert r["ok"] is True
    assert all(s["status"] == "success" for s in r["sites"])


def test_replica_view_and_fetch_back(env):
    cid = _chain(env)
    pr.run_chain(cid, env["rid"], env["src"])
    view = pr.replica_view(env["rid"])
    assert view["n_ok"] == 3
    assert len(view["replicas"]) == 3
    # 异地取回（站点3）
    remote = [r for r in view["replicas"] if r["seq"] == 3][0]
    assert remote["fetchable"] is True
    out = pr.fetch_back(env["rid"], remote["id"])
    assert out["ok"] is True and os.path.isfile(out["dest"])
    with open(out["dest"], "rb") as f:
        assert f.read() == open(env["src"], "rb").read()   # 逐字节一致


def test_fetch_back_rejects_failed_replica(env):
    cid = _chain(env, policy={"max_retries": 1, "retry_interval": 1})
    bad_dir = os.path.join(_TMP, "not_a_dir_c")
    with open(bad_dir, "w") as f:
        f.write("x")   # endpoint 指向文件 → local 后端保存失败
    db.execute("UPDATE storage_targets SET endpoint=? WHERE id=?",
               (bad_dir, env["targets"][2]))
    pr.run_chain(cid, env["rid"], env["src"])
    view = pr.replica_view(env["rid"])
    bad = [r for r in view["replicas"] if r["seq"] == 3][0]
    assert bad["status"] == "failed"
    assert bad["fetchable"] is False
    with pytest.raises(pr.PoolError, match="不可取回"):
        pr.fetch_back(env["rid"], bad["id"])
