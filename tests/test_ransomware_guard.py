# -*- coding: utf-8 -*-
"""备份数据防勒索闭环回归：检测落库 / 自动隔离 / 恢复安全门 / 可信恢复点 / 审计。

使用真实临时文件（正常 gzip 产物 vs 满熵疑似加密产物）与隔离 SQLite 元库，
不依赖任何外部服务；测试期间禁止 webhook 外发与内核 immutable。
"""
import gzip
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="rw_guard_")
_MODULE_DB = os.path.join(_TMP, "meta.db")
_BACKUP_ROOT = os.path.join(_TMP, "backups")
os.makedirs(_BACKUP_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = _BACKUP_ROOT
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import core.db as db  # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = _BACKUP_ROOT
db.init_schema()

import pytest  # noqa: E402
from core import backup_cleanup, immutability, ransomware_guard as guard  # noqa: E402
from core.ransomware_guard import RansomwareBlocked  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离运行期全局配置（同进程其它测试模块会改写 config.META_DB_PATH）。"""
    config.META_DB_PATH = _MODULE_DB
    config.BACKUP_ROOT = _BACKUP_ROOT
    for table in ("ransomware_events", "backup_records", "backup_tasks"):
        try:
            db.execute("DELETE FROM " + table)
        except Exception:
            pass
    for key in guard.DEFAULTS:
        try:
            db.execute("DELETE FROM system_config WHERE key=?", (key,))
        except Exception:
            pass
    # 不发真实网络请求；内核 immutable 默认关，测试中强制关
    monkeypatch.setattr("core.webhooks.emit_async", lambda *a, **k: None)
    monkeypatch.setattr(guard, "apply_fs_immutable", lambda paths: ([], []))
    monkeypatch.setattr(guard, "release_fs_immutable", lambda paths: ([], []))
    yield


def _task(name="rw-task", retention_days=0) -> int:
    tid = db.execute(
        "INSERT INTO backup_tasks (name, db_type, host, port, username, "
        "password, db_name, backup_mode, created_at, updated_at) "
        "VALUES (?, 'mysql', 'h', 3306, 'u', ?, 'd', 'logical', ?, ?)",
        (name, db.encrypt_secret("p"), db.now_iso(), db.now_iso()))
    if retention_days:
        db.execute("UPDATE backup_tasks SET retention_days=? WHERE id=?",
                   (retention_days, tid))
    return tid


def _record(task_id: int, path: str, size: int = 100_000) -> int:
    return db.execute(
        "INSERT INTO backup_records (task_id, backup_type, status, "
        "backup_path, size_bytes, started_at, finished_at, is_simulated) "
        "VALUES (?, 'full', 'success', ?, ?, ?, ?, 0)",
        (task_id, path, size, db.now_iso(), db.now_iso()))


def _clean_artifact(name: str) -> str:
    p = os.path.join(_BACKUP_ROOT, name)
    with gzip.open(p, "wb") as g:
        g.write(b"-- INSERT INTO t VALUES (1)\n" * 4096)
    return p


def _encrypted_like_artifact(name: str) -> str:
    """满熵、非压缩后缀：模拟勒索软件把产物整体加密覆写。"""
    p = os.path.join(_BACKUP_ROOT, name)
    with open(p, "wb") as f:
        f.write(os.urandom(300_000))
    return p


# --------------------------------------------------------------------------

def test_auto_scan_persists_clean_result():
    tid = _task()
    rid = _record(tid, _clean_artifact("clean.sql.gz"))
    res = guard.after_backup(rid, {"id": tid}, operator="unit-test")
    assert res["level"] == "clean", json.dumps(res, ensure_ascii=False)
    row = db.query_one("SELECT security_level, security_checked_at, "
                       "security_detail, quarantined FROM backup_records WHERE id=?",
                       (rid,))
    assert row["security_level"] == "clean"
    assert row["security_checked_at"]
    assert isinstance(json.loads(row["security_detail"]), list)
    assert not row["quarantined"]


def test_suspect_is_quarantined_locked_and_blocks_restore():
    tid = _task()
    rid = _record(tid, _encrypted_like_artifact("encrypted.sql"))
    res = guard.scan_record(rid, operator="unit-test", auto=True)
    assert res["level"] == "suspect", json.dumps(res, ensure_ascii=False)
    assert "quarantined" in res["actions"]
    row = db.query_one("SELECT quarantined, locked, security_level "
                       "FROM backup_records WHERE id=?", (rid,))
    assert row["quarantined"] == 1 and row["locked"] == 1
    assert row["security_level"] == "suspect"
    kinds = [e["event"] for e in guard.events(20)]
    assert "ransomware_suspect" in kinds and "quarantine" in kinds

    with pytest.raises(RansomwareBlocked) as ei:
        guard.assert_restorable(dict(row) | {"id": rid}, operator="unit-test")
    assert ei.value.record_id == rid
    assert any("隔离" in r or "可疑" in r for r in ei.value.reasons)

    forced = guard.assert_restorable(dict(row) | {"id": rid}, force=True,
                                     operator="unit-test")
    assert forced["allowed"] and forced["forced"]
    assert "unsafe_restore_override" in [e["event"] for e in guard.events(20)]


def test_checksum_drift_upgrades_to_suspect_and_isolation():
    tid = _task()
    rid = _record(tid, _clean_artifact("drift.sql.gz"))
    guard.on_integrity_drift(rid, digest="deadbeef")
    row = db.query_one("SELECT security_level, quarantined FROM backup_records "
                       "WHERE id=?", (rid,))
    assert row["security_level"] == "suspect" and row["quarantined"] == 1
    with pytest.raises(RansomwareBlocked):
        guard.assert_restorable(dict(row) | {"id": rid, "checksum_ok": 0})
    assert "integrity_drift" in [e["event"] for e in guard.events(20)]


def test_unknown_level_does_not_block_restore():
    """远端产物本地不可见是正常形态，不能因无法判定就误伤恢复。"""
    tid = _task()
    rid = _record(tid, os.path.join(_BACKUP_ROOT, "missing.sql"))
    res = guard.scan_record(rid)
    assert res["level"] == "unknown"
    verdict = guard.assert_restorable(db.query_one(
        "SELECT * FROM backup_records WHERE id=?", (rid,)))
    assert verdict["allowed"] is True


def test_release_quarantine_requires_reason_and_keeps_lock():
    tid = _task()
    rid = _record(tid, _encrypted_like_artifact("q2.sql"))
    guard.scan_record(rid, auto=True)
    with pytest.raises(ValueError):
        guard.release_quarantine(rid, reason="  ", operator="unit-test")
    out = guard.release_quarantine(rid, reason="人工复核为误报",
                                   operator="unit-test")
    assert out["quarantined"] is False
    row = db.query_one("SELECT locked, security_level FROM backup_records "
                       "WHERE id=?", (rid,))
    assert row["locked"] == 1, "解除隔离不得顺带解除取证锁（防绕过）"
    assert row["security_level"] is None, "结论清空，须重新检测"


def test_last_trusted_point_skips_newest_suspect():
    tid = _task(retention_days=1)
    p1 = _clean_artifact("p1.sql.gz")
    p2 = _clean_artifact("p2.sql.gz")
    p3 = _clean_artifact("p3.sql.gz")
    r1, r2, r3 = (_record(tid, p) for p in (p1, p2, p3))
    # 让记录超过保留期，否则 build_plan 根本不会把它们列入待清理
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    db.execute("UPDATE backup_records SET finished_at=?, started_at=? "
               "WHERE task_id=?", (old, old, tid))
    # 最新一条被判定可疑，可信恢复点必须回退到上一条
    db.execute("UPDATE backup_records SET security_level='suspect', "
               "quarantined=1 WHERE id=?", (r3,))
    trusted = guard.trusted_point_ids()
    assert r3 not in trusted and r2 in trusted
    assert guard.is_last_trusted_point(r2)
    # 清理计划与执行报告都要跳过该可信恢复点
    plans = backup_cleanup.build_plan(task_id=tid, keep_min=0)
    assert plans and plans[0]["skipped_trusted"] >= 1
    assert all(i["record_id"] != r2 for i in plans[0]["items"])
    report = backup_cleanup.run_cleanup(task_id=tid, dry_run=True)
    assert "skipped_trusted" in report


def test_keep_last_clean_can_be_disabled():
    tid = _task()
    rid = _record(tid, _clean_artifact("single.sql.gz"))
    assert rid in guard.trusted_point_ids()
    guard.set_config({"ransomware_keep_last_clean": 0})
    assert guard.trusted_point_ids() == set()
    guard.set_config({"ransomware_keep_last_clean": 1})
    assert rid in guard.trusted_point_ids()


def test_summary_and_config_contract():
    tid = _task()
    _record(tid, _clean_artifact("s.sql.gz"))
    s = guard.summary()
    for k in ("total", "locked", "lock_rate", "clean", "suspect",
              "quarantined", "unscanned", "drift", "trusted_points",
              "config", "fs_capability", "recent_events", "suspects"):
        assert k in s, k
    assert set(s["config"]) == set(guard.DEFAULTS)
    assert set(s["fs_capability"]) >= {"available", "reason"}
    cfg = guard.set_config({"ransomware_auto_scan": 0, "no_such_key": 1})
    assert cfg["ransomware_auto_scan"] == 0
    assert "no_such_key" not in cfg


# --------------------------------------------------------------------------
# API 契约（Flask test_client，隔离库）
# --------------------------------------------------------------------------

@pytest.fixture()
def client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": 1, "username": "admin",
                     "display_name": "admin", "role": "admin"}
    return c


def test_download_blocked_for_quarantined_record(client):
    tid = _task()
    rid = _record(tid, _clean_artifact("dl.sql.gz"))
    db.execute("UPDATE backup_records SET quarantined=1 WHERE id=?", (rid,))
    r = client.get(f"/api/records/{rid}/download")
    assert r.status_code == 403
    assert "隔离" in r.get_json()["error"]


def test_restore_api_returns_structured_security_block(client, monkeypatch):
    import core.scheduler as scheduler
    tid = _task()
    rid = _record(tid, _clean_artifact("rs.sql.gz"))

    def blocked(*a, **k):
        raise RansomwareBlocked("恢复安全门拒绝：可疑",
                                reasons=["检测到可疑特征"], record_id=rid)
    monkeypatch.setattr(scheduler, "run_restore_now", blocked)
    r = client.post("/api/restores", json={"record_id": rid})
    assert r.status_code == 403
    body = r.get_json()
    assert body["security_blocked"] is True and body["record_id"] == rid
    assert body["reasons"] == ["检测到可疑特征"]

    # 显式二次确认后放行（stub 换成成功响应）
    monkeypatch.setattr(scheduler, "run_restore_now",
                        lambda *a, **k: {"id": 1, "status": "success"})
    r2 = client.post("/api/restores", json={
        "record_id": rid, "force_unsafe_restore": True,
        "confirm_unsafe": True})
    assert r2.status_code == 201, r2.get_data(as_text=True)


def test_pipeline_hooks_are_wired():
    """防回退：检测/隔离/恢复门必须挂在主流程上，不能退化成孤立 API。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def src(rel):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()
    assert "ransomware_guard.after_backup" in src("core/scheduler.py")
    assert "ransomware_guard.assert_restorable" in src("core/scheduler.py")
    assert "on_integrity_drift" in src("core/immutability.py")
    assert "trusted_point_ids" in src("core/backup_cleanup.py")
    assert "RansomwareBlocked" in src("api/restore.py")
    assert "quarantined" in src("api/records.py")
    assert 'href="/ransomware"' in src("templates/base.html")
    assert "security_blocked" in src("static/js/app.js")


def test_unlock_requires_security_permission(client, monkeypatch):
    tid = _task()
    rid = _record(tid, _clean_artifact("ul.sql.gz"))
    monkeypatch.setattr(immutability, "unlock_record",
                        lambda *a, **k: {"record_id": rid, "locked": False})
    viewer = client.application.test_client()
    with viewer.session_transaction() as s:
        s["user"] = {"id": 2, "username": "v", "role": "viewer"}
    r = viewer.post(f"/api/records/{rid}/unlock", json={"reason": "test"})
    assert r.status_code == 403
    ok = client.post(f"/api/records/{rid}/unlock", json={"reason": "复核通过"})
    assert ok.status_code == 200
    assert "unlock" in [e["event"] for e in guard.events(20)]
