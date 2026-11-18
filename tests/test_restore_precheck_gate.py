# -*- coding: utf-8 -*-
"""恢复前自动校验闸（P0-3）回归。

纯单测：record 走 monkeypatch、引擎 mock，零元数据库写入（避免与其他
测试模块的共享临时库撞号污染——test_op_guard 系列已知坑）。

覆盖：
1. precheck_restore 真实逻辑：篡改产物 sha256 fail / 产物缺失 fail
2. run_restore_now 预检 fail → RestorePrecheckBlocked 且报告透出
3. 开关 restore_auto_precheck=0 → fail 也不拦
4. 预检自身异常 → 放行
5. op-guard 配置端点透出/保存 restore_auto_precheck（走 API，隔离库）
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="restore_gate_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest


def _bypass_ransomware(monkeypatch):
    """反勒索安全门分层更高（安全拒绝优先于预检）——本文件聚焦预检闸。"""
    import core.ransomware_guard as _rg
    monkeypatch.setattr(_rg, "assert_restorable",
                        lambda *a, **k: {"allowed": True, "level": "pass"})


def _patch_pf(monkeypatch, pf):
    from core import backup_precheck
    monkeypatch.setattr(backup_precheck, "precheck_restore",
                        lambda *a, **k: pf)


def _patch_record(monkeypatch, record):
    import core.models as _models
    monkeypatch.setattr(_models, "get_record", lambda rid: record)
    monkeypatch.setattr(_models, "get_task",
                        lambda tid, include_secret=False: {
                            "id": tid, "name": "t", "db_type": "mysql"})


def _fake_engine(monkeypatch, ok=True):
    import core.engines as _engines
    from core.engines.base import BackupResult, BackupStatus

    class _Fake:
        def run_restore(self, path, **kw):
            return BackupResult(success=ok,
                                status=BackupStatus.SUCCESS if ok
                                else BackupStatus.FAILED,
                                message="fake restore", simulated=False)

    monkeypatch.setattr(_engines, "get_engine", lambda *a, **k: _Fake())


# ---- 1) precheck_restore 真实逻辑 ----

def test_precheck_tampered_artifact_sha256_fail():
    """篡改产物 → sha256 不一致 fail（真实 precheck_restore，不落库）。"""
    from core import backup_precheck, db as _db
    path = os.path.join(_TMP, "art.sql")
    with open(path, "wb") as f:
        f.write(b"-- mysqldump dummy\n" * 20)
    rec = {"id": 901, "db_type": "mysql", "backup_path": path,
           "checksum": _db.sha256_file(path), "task_id": 0}
    with open(path, "ab") as f:
        f.write(b"TAMPERED")
    pf = backup_precheck.precheck_restore(rec, None, "gate_new")
    assert pf["ok"] is False
    assert any(it["item"] == "integrity" and it["level"] == "fail"
               for it in pf["items"])


def test_precheck_missing_artifact_fail():
    from core import backup_precheck
    rec = {"id": 902, "db_type": "mysql",
           "backup_path": os.path.join(_TMP, "gone.sql"),
           "checksum": "", "task_id": 0}
    pf = backup_precheck.precheck_restore(rec, None, None)
    assert pf["ok"] is False
    assert any(it["item"] == "artifact" and it["level"] == "fail"
               for it in pf["items"])


def test_precheck_untampered_passes():
    from core import backup_precheck, db as _db
    path = os.path.join(_TMP, "good.sql")
    with open(path, "wb") as f:
        f.write(b"-- mysqldump dummy\n" * 20)
    rec = {"id": 903, "db_type": "mysql", "backup_path": path,
           "checksum": _db.sha256_file(path), "task_id": 0}
    pf = backup_precheck.precheck_restore(rec, None, None)
    assert pf["ok"] is True


# ---- 2) run_restore_now 预检 fail → 拦截并透出报告 ----

def test_run_restore_blocked_with_report(monkeypatch):
    from core import scheduler
    _bypass_ransomware(monkeypatch)
    _patch_record(monkeypatch, {"id": 910, "task_id": 1, "db_type": "mysql",
                                "backup_path": os.path.join(_TMP, "x.sql")})
    _patch_pf(monkeypatch, {"ok": False, "level": "fail", "items": [
        {"item": "integrity", "level": "fail", "message": "sha256 不一致"}]})
    with pytest.raises(scheduler.RestorePrecheckBlocked) as ei:
        scheduler.run_restore_now(910, target_db="gate_new", operator="t")
    assert "恢复预检未通过" in str(ei.value)
    assert ei.value.report["items"][0]["item"] == "integrity"


# ---- 3) 开关关闭 → fail 也不拦 ----

def test_switch_off_bypasses(monkeypatch):
    from core import db as _db
    from core import scheduler
    _bypass_ransomware(monkeypatch)
    _patch_record(monkeypatch, {"id": 911, "task_id": 1, "db_type": "mysql",
                                "backup_path": os.path.join(_TMP, "x.sql")})
    _fake_engine(monkeypatch)
    _patch_pf(monkeypatch, {"ok": False, "level": "fail", "items": [
        {"item": "integrity", "level": "fail", "message": "x"}]})
    _db.set_system_config("restore_auto_precheck", "0")
    try:
        scheduler.run_restore_now(911, target_db="gate_new", operator="t")
    except scheduler.RestorePrecheckBlocked:
        pytest.fail("开关关闭后仍被预检拦截")
    finally:
        _db.set_system_config("restore_auto_precheck", "1")


# ---- 4) 预检自身异常 → 放行 ----

def test_precheck_crash_passes(monkeypatch):
    from core import scheduler
    _bypass_ransomware(monkeypatch)
    _patch_record(monkeypatch, {"id": 912, "task_id": 1, "db_type": "mysql",
                                "backup_path": os.path.join(_TMP, "x.sql")})
    _fake_engine(monkeypatch)
    from core import backup_precheck
    monkeypatch.setattr(backup_precheck, "precheck_restore",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pf boom")))
    scheduler.run_restore_now(912, target_db="gate_new", operator="t")


# ---- 5) op-guard 配置端点 ----

@pytest.fixture(scope="module")
def client():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    with application.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123"})
        with c.session_transaction() as _sess:
            if not _sess.get("user"):
                _sess["user"] = {"username": "admin", "role": "admin"}
        assert r.status_code in (200, 302)
        yield c


def test_op_guard_config_roundtrip(client):
    r = client.get("/api/op-guard/config")
    assert r.status_code == 200
    assert r.get_json()["restore_auto_precheck"] is True
    r2 = client.post("/api/op-guard/config",
                     json={"restore_auto_precheck": False})
    assert r2.status_code == 200
    assert r2.get_json()["restore_auto_precheck"] is False
    r3 = client.post("/api/op-guard/config",
                     json={"restore_auto_precheck": True})
    assert r3.get_json()["restore_auto_precheck"] is True
