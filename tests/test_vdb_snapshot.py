# -*- coding: utf-8 -*-
"""VDB 逻辑快照回归：表契约 / 值编码 / 完整性 / 状态闸 / API 路由。"""
import gzip
import json
import os
import sys
import tempfile
from decimal import Decimal

_TMP = tempfile.mkdtemp(prefix="vdb_snapshot_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import core.db as db  # noqa: E402
db.init_schema()

import core.models as models  # noqa: E402
from core import vdb_snapshot as vs  # noqa: E402
from core.clone_service import CloneService  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM vdb_snapshots")
    db.execute("DELETE FROM clone_requests")
    db.execute("DELETE FROM vdb_instances")
    yield


def test_snapshot_table_and_service_column_exist():
    cols = {r["name"] for r in db.query("PRAGMA table_info(vdb_snapshots)")}
    assert {"clone_request_id", "parent_snapshot_id", "backup_path",
            "checksum", "table_count", "row_count", "status"} <= cols
    vcols = {r["name"] for r in db.query("PRAGMA table_info(vdb_instances)")}
    assert "service" in vcols, "Oracle 服务名必须可持久化"


def test_enc_dec_roundtrip():
    assert vs._dec(vs._enc(b"\x00\xff")) == b"\x00\xff"
    assert vs._enc(Decimal("111.50")) == "111.50"
    assert vs._enc("订单-甲") == "订单-甲"
    assert vs._enc(None) is None
    assert vs._enc(3) == 3


def test_ident_rejects_injection():
    assert vs._ident("t1", "mysql") == "`t1`"
    assert vs._ident("t1", "postgresql") == '"t1"'
    with pytest.raises(ValueError):
        vs._ident("t1; DROP TABLE x", "mysql")


def test_unsupported_db_type_rejected_without_connect():
    with pytest.raises(ValueError):
        vs.export_snapshot({"db_type": "oracle", "database_name": "X"},
                           "pw", out_root=_TMP)


def test_read_snapshot_version_and_missing(tmp_path):
    p = tmp_path / "s.json.gz"
    with gzip.open(str(p), "wt", encoding="utf-8") as f:
        json.dump({"version": vs.SNAP_VERSION, "tables": []}, f)
    assert vs.read_snapshot(str(p))["version"] == vs.SNAP_VERSION
    with pytest.raises(ValueError):
        vs.read_snapshot(str(tmp_path / "nope.json.gz"))


def test_sha256_mismatch_blocks_rollback():
    cs = CloneService()
    rid = models.create_clone_request({
        "source_record_id": 1, "target_env": "test", "status": "pending",
        "requested_by": "pytest"})
    with pytest.raises(ValueError, match="未就绪"):
        cs.create_snapshot(rid)
    # 构造 ready 克隆 + VDB + 快照行，篡改产物应被拒绝
    vid = models.create_vdb({"name": "clone_1", "db_type": "mysql",
                             "database_name": "clone_1", "host": "127.0.0.1",
                             "port": 3306, "username": "root", "password": "",
                             "status": "ready"})
    models.update_clone_request(rid, {"status": "ready", "vdb_instance_id": vid})
    p = os.path.join(_TMP, "snap.json.gz")
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({"version": vs.SNAP_VERSION, "tables": []}, f)
    sid = db.execute(
        "INSERT INTO vdb_snapshots(clone_request_id, vdb_instance_id, name,"
        " backup_path, checksum, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (rid, 1, "s1", p, "0" * 64, "ready", db.now_iso()))
    with pytest.raises(ValueError, match="校验和不匹配"):
        cs.rollback_snapshot(rid, sid)


def test_snapshot_lifecycle_rows():
    cs = CloneService()
    rid = models.create_clone_request({
        "source_record_id": 1, "target_env": "test", "status": "ready",
        "requested_by": "pytest"})
    sid = db.execute(
        "INSERT INTO vdb_snapshots(clone_request_id, name, status, created_at)"
        " VALUES (?,?,?,?)", (rid, "s1", "ready", db.now_iso()))
    assert len(cs.list_snapshots(rid)) == 1
    cs.delete_snapshot(rid, sid, operator="pytest")
    assert cs.list_snapshots(rid) == []
    assert db.query_one("SELECT status FROM vdb_snapshots WHERE id=?",
                        (sid,))["status"] == "deleted"


def test_api_routes_contract():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "api", "clone.py"), encoding="utf-8").read()
    for frag in ('"/clone/<int:request_id>/snapshots"',
                 'snapshots/<int:snapshot_id>/rollback',
                 'snapshots/<int:snapshot_id>/reclone',
                 'api_delete_vdb_snapshot'):
        assert frag in src, "缺快照路由: %s" % frag
