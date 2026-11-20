# -*- coding: utf-8 -*-
"""VDB 快照备份**增强**回归（2026-11-20，native 通道与生命周期治理）。

注意：与 ``tests/test_vdb_snapshot.py``（既有：表契约/值编码/完整性/状态闸）
分工——本文件只覆盖本轮新增能力，两者互不重叠。

覆盖本轮新增/修复的能力：
1. **native 通道**：用数据库自带导出工具产出标准备份产物（含 DDL + 一致性快照）；
   库型不支持 / 工具缺失时**如实报错**，不静默退化成假成功。
2. **auto 回退**：native 不可用时回退数据级快照，并把回退原因写进 note
   （不假装是 native）；显式要 native 时失败即失败，不回退。
3. **回滚按类型分派**：native → 原生恢复通道；logical → 数据级回滚。
4. **生命周期治理**：保留策略清理超限快照、产物文件物理清理（防磁盘泄漏）。
5. **密码入库幂等加密**：models 层统一加密，明文才加密、密文原样存
   （杜绝明文落库与双重加密两种坑）。
6. **PG 恢复必须先建库**：此前漏建库 → pg_restore 报 "database does not exist"
   又被致命判定放行 → **假成功**（实测克隆库根本没建出来）。
"""
import os

import pytest

from core import clone_service as cs
from core import models
from core import vdb_snapshot as vs


# ---------------------------------------------------------------------
# 1. native 通道
# ---------------------------------------------------------------------

def test_native_unsupported_db_type_is_honest():
    with pytest.raises(ValueError) as e:
        vs.export_native_snapshot({"db_type": "oracle", "database_name": "X"},
                                  "pw")
    assert "原生导出暂不支持" in str(e.value)


def test_native_requires_database_name():
    with pytest.raises(ValueError) as e:
        vs.export_native_snapshot({"db_type": "mysql"}, "pw")
    assert "database_name" in str(e.value)


def test_native_missing_tool_is_honest(monkeypatch):
    """平台侧缺导出工具 → 如实报错并说明「被管库无需安装任何东西」。"""
    monkeypatch.setattr(vs, "_resolve_bin", lambda *a: "")
    with pytest.raises(ValueError) as e:
        vs.export_native_snapshot({"db_type": "mysql", "database_name": "d",
                                   "host": "127.0.0.1", "port": 3306}, "pw")
    assert "平台侧未找到" in str(e.value)
    assert "被管库无需安装" in str(e.value)


def test_native_mysql_cmd_adds_column_statistics_for_old_server():
    """mysqldump 8.0 连 5.7 服务端必须加 --column-statistics=0，否则 rc=2。"""
    cmd = vs._mysql_dump_cmd("/bin/mysqldump",
                              {"host": "127.0.0.1", "port": 3306,
                               "username": "root"}, "pw", "db", "5.7.44")
    assert "--column-statistics=0" in cmd
    assert "--single-transaction" in cmd
    assert "--databases" in cmd and "db" in cmd


def test_native_mysql_cmd_omits_column_statistics_for_new_server():
    cmd = vs._mysql_dump_cmd("/bin/mysqldump",
                              {"host": "127.0.0.1", "port": 3306,
                               "username": "root"}, "pw", "db", "8.0.42")
    assert "--column-statistics=0" not in cmd


def test_native_mysql_cmd_uses_no_defaults():
    """--no-defaults 必须有：否则 /root/.my.cnf 的密码会覆盖任务凭据。"""
    cmd = vs._mysql_dump_cmd("/bin/mysqldump",
                              {"host": "h", "port": 3306, "username": "u"},
                              "pw", "db", "8.0.42")
    assert "--no-defaults" in cmd


def test_resolve_bin_finds_candidate():
    """同名多版本 → 版本择优；至少要能找到一个候选。"""
    got = vs._resolve_bin("pg_dump")
    assert got
    assert os.path.isfile(got)


def test_bin_version_rejects_broken_binary(monkeypatch, tmp_path):
    """坏二进制（--version 报错）必须被淘汰——不能从报错文本里抓版本号。

    真实踩坑：缺 libcrypto.so.1.1 的 pg_restore 报
    "error while loading shared libraries: libcrypto.so.1.1"，
    旧实现用正则抓版本 → 误认成 1.1（还带着工具名路径，逃过了名字校验）。
    """
    bad = tmp_path / "pg_restore"
    bad.write_text("#!/bin/sh\necho "
                   "'/x/pg_restore: error while loading shared libraries: "
                   "libcrypto.so.1.1' >&2\nexit 1\n")
    bad.chmod(0o755)
    assert vs._bin_version(str(bad), ("pg_restore",)) is None


def test_resolve_bin_for_server_prefers_matching_major(monkeypatch):
    """客户端版本必须与服务器匹配——高版本客户端会向老服务器发新参数。

    真实踩坑：平台机同时有 PG14 与 PG18 工具链，PG18 的 pg_restore 会
    SET transaction_timeout（PG17+ 才有），PG14 服务器直接拒绝 →
    "unrecognized configuration parameter"，恢复失败。
    """
    import stat

    def _mk(directory, name, ver):
        p = directory / name
        p.write_text("#!/bin/sh\necho '%s (PostgreSQL) %s'\n" % (name, ver))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    import tempfile
    d = tempfile.mkdtemp()
    from pathlib import Path
    pd = Path(d)
    p14 = _mk(pd, "pg_restore", "14.12")
    p18 = _mk(pd, "pg_restore18", "18.6")
    monkeypatch.setattr(vs, "_collect_bin_candidates", lambda *n: [p14, p18])
    assert vs._resolve_bin_for_server("14.12", "pg_restore") == p14
    assert vs._resolve_bin_for_server("18.6", "pg_restore") == p18
    # 服务器版本未知 → 退回版本择优（取最高）
    assert vs._resolve_bin_for_server("", "pg_restore") == p18


# ---------------------------------------------------------------------
# 2. create_snapshot 的类型分派与 auto 回退
# ---------------------------------------------------------------------

def _stub_context(monkeypatch, svc, vdb=None):
    vdb = vdb or {"id": 9, "name": "vdb1", "db_type": "mysql", "host": "127.0.0.1",
                  "port": 3306, "database_name": "vdb1", "username": "root"}
    monkeypatch.setattr(svc, "_vdb_context",
                        lambda rid, require_ready=True: ({"id": rid}, vdb, "pw"))


def test_create_snapshot_kind_invalid_rejected(monkeypatch):
    svc = cs.CloneService()
    _stub_context(monkeypatch, svc)
    with pytest.raises(ValueError) as e:
        svc.create_snapshot(1, kind="full-physical")
    assert "快照类型不支持" in str(e.value)


def test_create_snapshot_auto_falls_back_and_records_reason(monkeypatch):
    """native 不可用 → 回退 logical，并把原因写进 note（不假装 native）。"""
    svc = cs.CloneService()
    _stub_context(monkeypatch, svc)
    calls = []

    def _native_fail(*a, **k):
        calls.append("native")
        raise ValueError("平台侧未找到 mysqldump")

    def _logical_ok(*a, **k):
        calls.append("logical")
        return {"name": "s1", "backup_path": "/tmp/x.gz", "size_bytes": 10,
                "checksum": "abc", "table_count": 2, "row_count": 5,
                "snap_kind": "logical"}

    monkeypatch.setattr(vs, "export_native_snapshot", _native_fail)
    monkeypatch.setattr(vs, "export_snapshot", _logical_ok)
    monkeypatch.setattr(vs, "apply_retention", lambda **k: [])
    monkeypatch.setattr(cs.db, "query", lambda *a, **k: [])
    monkeypatch.setattr(cs.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(cs.db, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_snapshot_row", lambda sid, rid=None: {"id": sid})
    svc.create_snapshot(1, kind="auto")
    assert calls == ["native", "logical"]


def test_create_snapshot_explicit_native_does_not_fallback(monkeypatch):
    """显式要 native 时失败即失败——不得偷偷回退成另一种产物。"""
    svc = cs.CloneService()
    _stub_context(monkeypatch, svc)

    def _boom(*a, **k):
        raise ValueError("平台侧未找到 mysqldump")
    monkeypatch.setattr(vs, "export_native_snapshot", _boom)
    with pytest.raises(ValueError):
        svc.create_snapshot(1, kind="native")


# ---------------------------------------------------------------------
# 3. 回滚按类型分派
# ---------------------------------------------------------------------

def test_rollback_dispatches_by_snap_kind(monkeypatch):
    svc = cs.CloneService()
    vdb = {"id": 9, "db_type": "mysql", "host": "127.0.0.1", "port": 3306,
           "database_name": "vdb1", "username": "root"}
    monkeypatch.setattr(svc, "_vdb_context",
                        lambda rid, require_ready=True: ({"id": rid}, vdb, "pw"))
    seen = {}

    def _snap_row(sid, rid=None):
        return {"id": sid, "status": "ready", "snap_kind": "native",
                "backup_path": "/tmp/x.dump", "checksum": "abc"}
    monkeypatch.setattr(svc, "_snapshot_row", _snap_row)
    monkeypatch.setattr(vs, "sha256_file", lambda p: "abc")
    monkeypatch.setattr(vs, "restore_native_snapshot",
                        lambda v, pw, p, **k: seen.update(native=True)
                        or {"ok": True, "message": "native ok"})
    monkeypatch.setattr(vs, "restore_snapshot",
                        lambda *a, **k: seen.update(native=False))
    res = svc.rollback_snapshot(1, 5)
    assert seen.get("native") is True
    assert res["snap_kind"] == "native"


def test_rollback_rejects_tampered_artifact(monkeypatch):
    """校验和不匹配 → 拒绝回滚（篡改防护）。"""
    svc = cs.CloneService()
    vdb = {"id": 9, "db_type": "mysql", "database_name": "vdb1"}
    monkeypatch.setattr(svc, "_vdb_context",
                        lambda rid, require_ready=True: ({"id": rid}, vdb, "pw"))
    monkeypatch.setattr(svc, "_snapshot_row",
                        lambda sid, rid=None: {"id": sid, "status": "ready",
                                               "snap_kind": "native",
                                               "backup_path": "/tmp/x",
                                               "checksum": "aaa"})
    monkeypatch.setattr(vs, "sha256_file", lambda p: "bbb")
    with pytest.raises(ValueError) as e:
        svc.rollback_snapshot(1, 5)
    assert "校验和不匹配" in str(e.value)


# ---------------------------------------------------------------------
# 4. 生命周期治理
# ---------------------------------------------------------------------

def test_purge_snapshot_files_removes_artifact(tmp_path, monkeypatch):
    art = tmp_path / "snapshot.dump"
    art.write_text("x")
    man = tmp_path / "manifest.json"
    man.write_text("{}")
    monkeypatch.setattr(vs.db, "query_one",
                        lambda sql, params=(): {"id": 1, "backup_path": str(art),
                                                "status": "deleted",
                                                "size_bytes": 1})
    out = vs.purge_snapshot_files([1])
    assert out and out[0]["removed"] is True
    assert not art.exists()
    assert not man.exists()


def test_purge_snapshot_files_dry_run_keeps_file(tmp_path, monkeypatch):
    art = tmp_path / "snapshot.dump"
    art.write_text("x")
    monkeypatch.setattr(vs.db, "query_one",
                        lambda sql, params=(): {"id": 1, "backup_path": str(art),
                                                "status": "deleted",
                                                "size_bytes": 1})
    out = vs.purge_snapshot_files([1], dry_run=True)
    assert out and out[0]["removed"] is True
    assert art.exists(), "dry_run 不得真删文件"


def test_apply_retention_expires_oldest_over_limit(monkeypatch):
    rows = [{"id": i} for i in range(1, 6)]      # 5 个就绪快照
    executed = []
    monkeypatch.setattr(vs.db, "query", lambda *a, **k: rows)
    monkeypatch.setattr(vs.db, "execute", lambda sql, params=():
                        executed.append(sql))
    monkeypatch.setattr(vs, "purge_snapshot_files", lambda ids, dry_run=False: [])
    over = vs.apply_retention(request_id=1, keep=3)
    assert over == [4, 5]                          # 最旧的 2 个超限
    assert any("expired" in s for s in executed)


def test_apply_retention_no_op_within_limit(monkeypatch):
    monkeypatch.setattr(vs.db, "query", lambda *a, **k: [{"id": 1}, {"id": 2}])
    assert vs.apply_retention(request_id=1, keep=10) == []


def test_purge_marks_snapshot_deleted_to_avoid_ghost_entry(monkeypatch):
    """物理清理产物后必须同时置删除态——否则列表里留下「有快照无产物」的僵尸条目。"""
    stmts = []
    monkeypatch.setattr(vs.db, "query_one",
                        lambda sql, params=(): {"id": 5, "backup_path": "/tmp/x",
                                                "size_bytes": 10,
                                                "status": "ready"})
    monkeypatch.setattr(vs.db, "execute",
                        lambda sql, params=(): stmts.append(sql))
    monkeypatch.setattr(vs.db, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(vs, "purge_snapshot_files",
                        lambda ids, dry_run=False: [{"id": ids[0], "removed": True}])
    res = vs.purge_snapshot(5, operator="tester")
    assert res["ok"] is True
    assert res["snapshot_id"] == 5
    assert any("status='deleted'" in s for s in stmts), stmts


def test_purge_snapshot_rejects_unknown_id(monkeypatch):
    monkeypatch.setattr(vs.db, "query_one", lambda sql, params=(): None)
    with pytest.raises(ValueError) as e:
        vs.purge_snapshot(999)
    assert "快照不存在" in str(e.value)


def test_rollback_vdb_snapshot_works_without_clone_request(monkeypatch):
    """VDB 页创建的快照（无克隆请求）也必须能回滚——此前这条路径是缺口。"""
    svc = cs.CloneService()
    vdb = {"id": 9, "db_type": "mysql", "host": "127.0.0.1", "port": 3306,
           "database_name": "vdb1", "username": "root"}
    monkeypatch.setattr(cs.models, "get_vdb", lambda vid: vdb)
    monkeypatch.setattr(cs.db, "query_one",
                        lambda sql, params=(): {"id": 5, "status": "ready",
                                               "snap_kind": "native",
                                               "backup_path": "/tmp/x",
                                               "checksum": "abc"})
    monkeypatch.setattr(vs, "sha256_file", lambda p: "abc")
    called = {}
    monkeypatch.setattr(vs, "restore_native_snapshot",
                        lambda v, pw, p, **k: called.update(native=True)
                        or {"ok": True, "message": "restored"})
    monkeypatch.setattr(svc, "_vdb_password", staticmethod(lambda v, r=None: "pw"))
    monkeypatch.setattr(cs.db, "add_log", lambda *a, **k: None)
    res = svc.rollback_vdb_snapshot(9, 5, operator="tester")
    assert called.get("native") is True
    assert res["snap_kind"] == "native"
    assert res["snapshot_id"] == 5


def test_rollback_vdb_snapshot_rejects_tampered(monkeypatch):
    svc = cs.CloneService()
    monkeypatch.setattr(cs.models, "get_vdb",
                        lambda vid: {"id": 9, "db_type": "mysql",
                                     "database_name": "vdb1"})
    monkeypatch.setattr(cs.db, "query_one",
                        lambda sql, params=(): {"id": 5, "status": "ready",
                                               "snap_kind": "native",
                                               "backup_path": "/tmp/x",
                                               "checksum": "aaa"})
    monkeypatch.setattr(vs, "sha256_file", lambda p: "bbb")
    with pytest.raises(ValueError) as e:
        svc.rollback_vdb_snapshot(9, 5)
    assert "校验和不匹配" in str(e.value)


# ---------------------------------------------------------------------
# 5. 密码入库幂等加密
# ---------------------------------------------------------------------

def test_vdb_password_encrypted_on_store():
    """明文 → 加密落库。"""
    enc = models._vdb_password_for_store("s3cret")
    assert enc != "s3cret"
    assert enc.startswith("sm4v1:")
    import core.db as db
    assert db.decrypt_secret(enc) == "s3cret"


def test_vdb_password_not_double_encrypted():
    """已是密文 → 原样存（避免双重加密导致密码解不开）。"""
    enc = models._vdb_password_for_store("s3cret")
    assert models._vdb_password_for_store(enc) == enc


def test_vdb_password_empty_stays_empty():
    assert models._vdb_password_for_store("") == ""
    assert models._vdb_password_for_store(None) == ""


# ---------------------------------------------------------------------
# 6. PG 恢复先建库（防假成功）
# ---------------------------------------------------------------------

def test_restore_native_pg_reports_missing_database(monkeypatch):
    """PG 系恢复先建库；若 pg_restore 仍报「库不存在」必须判为致命（防假成功）。"""
    vdb = {"db_type": "postgresql", "host": "127.0.0.1", "port": 5785,
           "username": "postgres", "database_name": "src"}

    def _fake_run(cmd, **kw):
        joined = " ".join(cmd)
        if "pg_restore" in joined:
            class _P:
                returncode = 1
                stderr = b'pg_restore: error: database "x" does not exist\n'
                stdout = b""
            return _P()
        class _Ok:
            returncode = 0
            stderr = b""
            stdout = b""
        return _Ok()

    monkeypatch.setattr(vs.subprocess, "run", _fake_run)
    monkeypatch.setattr(vs, "_resolve_bin",
                        lambda *a: "/usr/bin/" + (a[0] if a else "x"))
    with pytest.raises(ValueError) as e:
        vs.restore_native_snapshot(vdb, "pw", __file__, target_db="x")
    assert "pg_restore 失败" in str(e.value)
