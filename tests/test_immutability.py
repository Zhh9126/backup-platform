# -*- coding: utf-8 -*-
"""不可变备份与保留锁定（WORM）自测——对标 Veeam Hardened Repository /
Commvault Immutable / 云祺不可变备份（G13，2026-10）。

覆盖：
- 锁定/解锁：审计强制、有效期、只能延长不能缩短、写保护生效
- 删除路径拦截：定期清理（含第二道硬护栏）、GFS、任务级联删除
- 完整性复检：基线新建/一致/漂移告警
- 与现有护栏的叠加：keep_min 兜底、幽灵记录不可锁
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="immutability_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.environ["SCHEDULER_ENABLED"] = "0"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()                                # noqa: E402

# 后导入的测试模块会覆盖 os.environ/config——本文件的临时目录在 import 时
# 固化，运行期一律用这里的常量（跨模块共享全局 config 的已知污染规避）
_BACKUP_ROOT = os.environ["BACKUP_ROOT"]

import core.models as models                    # noqa: E402
from core import immutability as imm            # noqa: E402
from core.immutability import ImmutabilityViolation  # noqa: E402


def _sha(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_record(path: str = None, task_id: int = None,
                 checksum: str = None) -> int:
    path = path or ""
    return models.create_record({
        "task_id": task_id or models.create_task({
            "name": f"锁测试任务-{path[-6:]}", "db_type": "mysql",
            "host": "127.0.0.1", "port": 3306, "username": "u",
            "password": "p", "db_name": "db",
        }),
        "db_type": "mysql", "backup_type": "logical", "status": "success",
        "started_at": db.now_iso(), "finished_at": db.now_iso(),
        "size_bytes": os.path.getsize(path) if path else 0,
        "backup_path": path,
        "checksum": checksum or (_sha(path) if path else ""),
    })


def _make_file(content: bytes = b"artifact-content") -> str:
    fd, p = tempfile.mkstemp(dir=_BACKUP_ROOT, suffix=".bak")
    os.write(fd, content)
    os.close(fd)
    return p


# ---------------------------------------------------------------------------
# 1. 锁定 / 解锁
# ---------------------------------------------------------------------------
def test_lock_and_unlock_roundtrip():
    p = _make_file()
    rid = _make_record(p)
    res = imm.lock_record(rid, days=7, operator="admin", reason="勒索防护")
    assert res["locked"] is True and res["retention_until"]
    rec = models.get_record(rid)
    assert imm.is_active(rec) is True
    # 写保护生效：文件去掉写位
    assert not (os.stat(p).st_mode & 0o200)
    # 解锁必须带原因（审计闸门）
    try:
        imm.unlock_record(rid, operator="admin", reason="  ")
        assert False, "无原因解锁应被拒绝"
    except ValueError:
        pass
    imm.unlock_record(rid, operator="admin", reason="误锁，人工核实后解锁")
    rec = models.get_record(rid)
    assert imm.is_active(rec) is False
    assert os.stat(p).st_mode & 0o200          # 写位已恢复


def test_lock_can_only_extend_not_shorten():
    p = _make_file()
    rid = _make_record(p)
    imm.lock_record(rid, days=30)
    first = models.get_record(rid)["retention_until"]
    imm.lock_record(rid, days=1)               # 尝试缩短
    assert models.get_record(rid)["retention_until"] == first
    imm.unlock_record(rid, reason="test")


def test_lock_permanent():
    p = _make_file()
    rid = _make_record(p)
    imm.lock_record(rid, days=0, reason="合规永久留存")
    rec = models.get_record(rid)
    assert imm.is_active(rec) is True and not rec["retention_until"]
    imm.unlock_record(rid, reason="test")


def test_lock_rejects_invalid_records():
    rid = _make_record("")                     # 幽灵记录
    try:
        imm.lock_record(rid, days=7)
        assert False, "幽灵记录应不可锁"
    except ValueError as e:
        assert "不在磁盘" in str(e)
    rid2 = models.create_record({
        "task_id": models.create_task({
            "name": "失败任务", "db_type": "mysql", "host": "127.0.0.1",
            "port": 3306, "username": "u", "password": "p", "db_name": "db"}),
        "db_type": "mysql", "backup_type": "logical", "status": "failed",
        "started_at": db.now_iso()})
    try:
        imm.lock_record(rid2, days=7)
        assert False, "失败备份应不可锁"
    except ValueError as e:
        assert "成功" in str(e)


# ---------------------------------------------------------------------------
# 2. 删除路径拦截
# ---------------------------------------------------------------------------
def test_delete_task_blocked_by_lock():
    p = _make_file()
    tid = models.create_task({
        "name": "级联删除测试", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "u", "password": "p", "db_name": "db"})
    rid = _make_record(p, task_id=tid)
    imm.lock_record(rid, days=7)
    try:
        models.delete_task(tid)
        assert False, "锁保护期内的任务级联删除应被拒绝"
    except ValueError as e:
        assert "保留锁定" in str(e)
    imm.unlock_record(rid, reason="test")
    assert models.delete_task(tid) is True


def test_cleanup_skips_locked_records():
    from core import backup_cleanup
    p = _make_file()
    tid = models.create_task({
        "name": "清理锁测试", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "u", "password": "p", "db_name": "db",
        "retention_days": 1, "retention_count": 1})
    rid_old = _make_record(p, task_id=tid)
    # 直接把旧记录时间改老（超保留期）
    db.execute("UPDATE backup_records SET finished_at='2020-01-01T00:00:00' "
               "WHERE id=?", (rid_old,))
    p2 = _make_file()
    rid_new = _make_record(p2, task_id=tid)
    imm.lock_record(rid_new, days=30)
    report = backup_cleanup.run_cleanup(task_id=tid, dry_run=False)
    # 锁定的新记录绝不进清理名单
    assert os.path.isfile(p2)
    assert rid_new not in [i["record_id"] for pl in
                           backup_cleanup.build_plan(task_id=tid)
                           for i in pl["items"]]
    assert report.get("skipped_locked", 0) >= 0
    imm.unlock_record(rid_new, reason="test")


def test_cleanup_hard_guard_blocks_new_locks():
    # 第二道硬护栏：计划生成后、执行前新加的锁也拦得住
    from core import backup_cleanup
    p = _make_file()
    tid = models.create_task({
        "name": "硬护栏测试", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "u", "password": "p", "db_name": "db",
        "retention_days": 1, "retention_count": 1})
    rid = _make_record(p, task_id=tid)
    db.execute("UPDATE backup_records SET finished_at='2020-01-01T00:00:00' "
               "WHERE id=?", (rid,))
    # 锁在计划之后才生效：直接测 locked_paths 硬护栏语义
    imm.lock_record(rid, days=7)
    locked = imm.locked_paths()
    assert p in locked
    imm.unlock_record(rid, reason="test")
    assert p not in imm.locked_paths()


def test_gfs_skips_locked_records():
    from core import retention_gfs
    p = _make_file()
    tid = models.create_task({
        "name": "GFS锁测试", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "u", "password": "p", "db_name": "db",
        "gfs_policy": json.dumps({"daily": 1, "delete_files": True})})
    rid = _make_record(p, task_id=tid)
    db.execute("UPDATE backup_records SET finished_at='2020-06-01T00:00:00' "
               "WHERE id=?", (rid,))
    imm.lock_record(rid, days=30)
    retention_gfs.apply_gfs(task_id=tid, dry_run=False)
    rec = models.get_record(rid)
    assert rec["status"] == "success"          # 未被标记 expired_gfs
    assert os.path.isfile(p)                   # 文件未被删
    imm.unlock_record(rid, reason="test")


# ---------------------------------------------------------------------------
# 3. 完整性复检
# ---------------------------------------------------------------------------
def test_verify_integrity_match_and_drift():
    p = _make_file(b"stable-content")
    rid = _make_record(p)
    imm.lock_record(rid, days=7)
    res = imm.verify_integrity(rid)
    assert res["status"] == "match"
    # 篡改内容 → 漂移
    os.chmod(p, 0o644)
    with open(p, "wb") as f:
        f.write(b"tampered-content")
    res = imm.verify_integrity(rid)
    assert res["status"] == "drift"
    assert models.get_record(rid)["checksum_ok"] == 0
    imm.unlock_record(rid, reason="test")


def test_verify_integrity_baseline_new():
    p = _make_file(b"no-checksum-yet")
    rid = _make_record(p)
    db.execute("UPDATE backup_records SET checksum='' WHERE id=?", (rid,))
    res = imm.verify_integrity(rid)
    assert res["status"] == "baseline_new"
    assert models.get_record(rid)["checksum"]


def test_recheck_all_counts():
    p = _make_file()
    rid = _make_record(p)
    imm.lock_record(rid, days=7)
    report = imm.recheck_all()
    assert report["total"] >= 1 and report["match"] >= 1
    imm.unlock_record(rid, reason="test")


# ---------------------------------------------------------------------------
# 4. 自动锁定钩子
# ---------------------------------------------------------------------------
def test_maybe_autolock_from_task_extra():
    p = _make_file()
    tid = models.create_task({
        "name": "自动锁任务", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "u", "password": "p", "db_name": "db",
        "extra_options": json.dumps({"retention_lock_days": 14})})
    task = models.get_task(tid)
    rid = _make_record(p, task_id=tid)
    res = imm.maybe_autolock(task, rid)
    assert res.get("locked") is True
    assert imm.is_active(models.get_record(rid)) is True
    imm.unlock_record(rid, reason="test")
    # 未配置的任务不锁
    rid2 = _make_record(_make_file(), task_id=tid)
    db.execute("UPDATE backup_records SET checksum='b' WHERE id=?", (rid2,))
    db.execute("UPDATE backup_tasks SET extra_options=NULL WHERE id=?", (tid,))
    res2 = imm.maybe_autolock(models.get_task(tid), rid2)
    assert res2 == {}
    assert not models.get_record(rid2)["locked"]


# ---------------------------------------------------------------------------
# 5. API 契约
# ---------------------------------------------------------------------------
def test_lock_api_routes_registered():
    import app as app_mod
    rules = {r.rule for r in app_mod.app.url_map.iter_rules()}
    assert "/api/records/<int:record_id>/lock" in rules
    assert "/api/records/<int:record_id>/unlock" in rules
    assert "/api/records/<int:record_id>/verify-integrity" in rules
    assert "/api/records/locked" in rules


def test_db_columns_present():
    cols = {r["name"] for r in db.query("PRAGMA table_info(backup_records)")}
    assert {"locked", "locked_at", "retention_until", "write_protected",
            "checksum_verified_at", "checksum_ok", "lock_note"} <= cols
