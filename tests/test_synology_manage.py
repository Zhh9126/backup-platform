# -*- coding: utf-8 -*-
"""群晖全面纳管回归测试（设备表 / CRUD / 探测 / 资产 / 一键任务 / 引擎全量增量链）。

如实边界：DSM 侧用**本地真实 HTTP 服务**按 SYNO 协议仿真（复用
``test_synology_client.py`` 的 DSMStub），走完整 HTTP 会话、能力发现、
分页清单与 Range 下载；不是群晖真机。真机验证需要用户提供 DSM 设备，
未跑之前不宣称已适配真机。
"""
import importlib.util
import json
import os
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="synology_manage_")
_DB = os.path.join(_TMP, "meta.db")
_BACKUPS = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = _DB
os.environ["BACKUP_ROOT"] = _BACKUPS
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest  # noqa: E402
import config  # noqa: E402
from core import db  # noqa: E402

# 复用协议级仿真服务
_spec = importlib.util.spec_from_file_location(
    "_syn_stub", os.path.join(ROOT, "tests", "test_synology_client.py"))
stub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stub)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    old_db, old_root = config.META_DB_PATH, config.BACKUP_ROOT
    config.META_DB_PATH = _DB
    config.BACKUP_ROOT = _BACKUPS
    db.init_schema()
    db.init_schema()          # 幂等：连续两次不报错不丢数据
    yield
    config.META_DB_PATH, config.BACKUP_ROOT = old_db, old_root
    # 临时目录交给系统清理，不主动 delete（safe-delete 钩子环境）


@pytest.fixture()
def dsm(monkeypatch):
    """起一台本地 DSM stub，并把清单与数据置为 size 自洽的一组。"""
    data = {
        "/backup/a.txt": b"hello world",
        "/backup/sub/中文.txt": "中文内容".encode("utf-8"),
    }
    def entry(path, name, isdir=False):
        return {
            "path": path, "name": name, "isdir": isdir,
            "additional": {
                "size": 0 if isdir else len(data[path]),
                "time": {"mtime": 1700000000 + len(path)},
            },
        }
    files = {
        "/backup": [entry("/backup/a.txt", "a.txt"),
                    entry("/backup/sub", "sub", True)],
        "/backup/sub": [entry("/backup/sub/中文.txt", "中文.txt")],
    }
    monkeypatch.setattr(stub, "DATA", data)
    monkeypatch.setattr(stub, "FILES", files)
    stub.STATE.update({"sessions": set(), "seq": 0, "expire_once": False,
                       "hide_dsm_info": False, "last_range": None})
    srv = stub.ThreadingHTTPServer(("127.0.0.1", 0), stub.DSMStub)
    t = __import__("threading").Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield {"port": srv.server_address[1], "data": data, "files": files}
    finally:
        srv.shutdown()
        srv.server_close()


def _device(port, name="DSM-E2E"):
    return {"name": name, "hostname": "127.0.0.1", "port": port,
            "username": "admin", "password": "pw", "timeout": 5,
            "secure": False, "verify_ssl": False, "enabled": True}


def _engine(port, task_id=901, extra=None):
    from core.engines.synology import SynologyEngine
    task = {
        "id": task_id, "name": "syn-e2e", "db_type": "synology",
        "host": "127.0.0.1", "port": port, "username": "admin",
        "password": "pw", "db_name": "/backup",
        "extra_options": {"synology_paths": ["/backup"],
                          "synology_timeout": 5,
                          **(extra or {})},
    }
    return SynologyEngine(task, _BACKUPS, logger=None)


# ---------------------------------------------------------------- 表 / CRUD
def test_table_exists_and_idempotent():
    cols = {r["name"] for r in db.query("PRAGMA table_info(synology_devices)")}
    assert {"id", "name", "hostname", "port", "secure", "password",
            "capabilities", "last_status", "last_check_at",
            "dsm_version"}.issubset(cols)
    # OTP 绝不落库
    assert "otp_code" not in cols


def test_device_crud_password_and_duplicate(dsm):
    from core.synology import devices as dv
    did = dv.create_device(_device(dsm["port"]))
    row = db.query_one("SELECT password FROM synology_devices WHERE id=?", (did,))
    assert row["password"].startswith("sm4v1:")
    listed = dv.list_devices(include_secret=False)
    one = next(x for x in listed if x["id"] == did)
    assert one["password"] == "" and one["has_password"] is True
    assert dv.get_device(did, include_secret=True)["password"] == "pw"
    # 空密码不覆盖
    dv.update_device(did, {"remark": "改备注", "password": ""})
    assert dv.get_device(did, include_secret=True)["password"] == "pw"
    assert dv.get_device(did)["remark"] == "改备注"
    with pytest.raises(dv.DeviceError):
        dv.create_device(_device(dsm["port"], name="重复"))
    assert dv.delete_device(did) is True
    assert dv.delete_device(did) is False


def test_device_normalize_rejects_scheme():
    from core.synology import devices as dv
    with pytest.raises(dv.DeviceError):
        dv.create_device({**_device(5000), "hostname": "http://1.2.3.4"})


# ---------------------------------------------------------------- 探测 / 资产
def test_probe_persists_real_capabilities(dsm):
    from core.synology import devices as dv
    did = dv.create_device(_device(dsm["port"], "probe-dev"))
    out = dv.probe_device(did)
    assert out["ok"] is True
    assert out["dsm_version"] == "DSM 7.2.1-69057"
    assert out["model"] == "DS3622xs+"
    assert out["filestation"] is True
    assert "SYNO.FileStation.List" in out["public_apis"]
    row = dv.get_device(did)
    assert row["last_status"] == "online"
    assert row["dsm_version"] == "DSM 7.2.1-69057"
    assert row["capabilities"]["filestation"] is True
    # 非公开 API 只报告命中，不承诺可用
    assert "storage" in out["unofficial"]
    dv.delete_device(did)


def test_probe_bad_password_is_honest(dsm):
    from core.synology import devices as dv
    bad = {**_device(dsm["port"]), "password": "wrong"}
    out = dv.probe_device(dict(bad), persist=False)
    assert out["ok"] is False
    assert "400" in out["error"] or out["error_code"] == 400


def test_shares_browse_and_assets(dsm):
    from core.synology import devices as dv
    did = dv.create_device(_device(dsm["port"], "asset-dev"))
    shares = dv.list_shares(did)
    assert {s["path"] for s in shares} == {"/backup", "/homes"}
    items = dv.browse_folder(did, "/backup")
    assert any(i["path"] == "/backup/a.txt" for i in items)
    assets = dv.walk_assets(did, "/backup")
    assert assets["files"] == 2 and assets["dirs"] == 1
    assert assets["bytes"] == len(dsm["data"]["/backup/a.txt"]) + \
        len(dsm["data"]["/backup/sub/中文.txt"])
    with pytest.raises(dv.DeviceError):
        dv.browse_folder(did, "/../etc")
    dv.delete_device(did)


def test_scenarios_declare_iscsi_boundaries():
    from core.synology import devices as dv
    s = dv.scenarios()
    ids = {x["id"] for x in s["channels"]}
    assert {"S1", "S2", "S3", "S4", "S5", "S6", "S10"}.issubset(ids)
    s3 = next(x for x in s["channels"] if x["id"] == "S3")
    assert "裸 LUN" in s3["summary"] and s3["status"] == "boundary"
    s5 = next(x for x in s["channels"] if x["id"] == "S5")
    assert "集群" in s5["summary"]
    assert any("IQN" in x for x in s["delivery_template"])
    s10 = next(x for x in s["channels"] if x["id"] == "S10")
    assert "路线 A" in s10["summary"] and "路线 B" in s10["summary"]
    assert "S2" in s["implemented"]


# ---------------------------------------------------------------- 引擎注册
def test_engine_registered_and_meta_honest():
    from core import engines
    assert "synology" in engines.supported_types()
    meta = engines.engine_meta_map()["synology"]
    assert meta["supports_incremental"] is True
    assert meta["supports_full_instance"] is False
    assert meta["supports_sync"] is False
    assert meta["backup_modes"] == ["logical"]
    assert "裸 LUN" in meta["description"]
    assert config.DEFAULT_PORTS["synology"] == 5000
    assert config.DB_DISPLAY_NAMES["synology"].startswith("群晖")


def test_preflight_requires_credentials():
    eng = _engine(5000, 990, {"synology_device_id": 999999})
    ok, detail = eng.preflight()
    assert ok is False and "不存在" in detail


# ---------------------------------------------------------------- 全量 / 增量 / 恢复
def test_full_incremental_chain_restore_and_delete_replay(dsm):
    from core.engines.base import BackupType
    eng = _engine(dsm["port"], 901)
    ok, _detail = eng.preflight()
    assert ok is True

    full = eng.backup(BackupType.FULL)
    assert full.success, full.message
    assert os.path.isfile(full.backup_path)
    assert "全量" in full.message
    mf1 = _manifest(full.backup_path)
    assert mf1["file_count"] == 2 and mf1["backup_type"] == "full"
    assert mf1["parent"] is None
    assert any("裸 LUN" in v for v in mf1["limits"].values())

    # ① 改一个文件、② 删一个文件 → 第二次增量的差分与删除回放
    dsm["data"]["/backup/a.txt"] = b"hello world v2"
    f = dsm["files"]["/backup"][0]
    f["additional"]["size"] = len(dsm["data"]["/backup/a.txt"])
    f["additional"]["time"]["mtime"] = 1700009999
    inc = eng.backup(BackupType.INCREMENTAL)
    assert inc.success, inc.message
    mf2 = _manifest(inc.backup_path)
    assert mf2["backup_type"] == "incremental"
    assert mf2["parent"] == os.path.basename(full.backup_path)
    assert [e["path"] for e in mf2["files"]] == ["/backup/a.txt"]
    assert mf2["reused_files"] == 0

    dsm["files"]["/backup/sub"] = []
    dsm["data"].pop("/backup/sub/中文.txt", None)
    inc2 = eng.backup(BackupType.INCREMENTAL)
    assert inc2.success, inc2.message
    mf3 = _manifest(inc2.backup_path)
    assert mf3["file_count"] == 0
    assert mf3["deleted"] == ["/backup/sub/中文.txt"]

    # 恢复到最新：回放 full + inc + inc2，逐文件 sha256 硬核对
    r = eng.restore(inc2.backup_path, target_path=os.path.join(_TMP, "restore_final"))
    assert r.success, r.message
    assert r.verified is True
    got = _read(r.backup_path, "files/backup/a.txt")
    assert got == b"hello world v2"
    assert not os.path.exists(os.path.join(
        r.backup_path, "files/backup/sub/中文.txt"))
    assert "SHA256 硬核对一致" in r.message

    # 恢复到全量时点：不应带上后续增量
    r_full = eng.restore(full.backup_path,
                         target_path=os.path.join(_TMP, "restore_full"))
    assert r_full.success, r_full.message
    assert _read(r_full.backup_path, "files/backup/a.txt") == b"hello world"
    assert os.path.exists(os.path.join(
        r_full.backup_path, "files/backup/sub/中文.txt"))

    # 校验通道
    v = eng.verify_record({"backup_path": full.backup_path,
                           "checksum": full.checksum})
    assert v.success and v.verified, v.message
    bad = eng.verify_record({"backup_path": full.backup_path,
                             "checksum": "sha256:" + "0" * 64})
    assert bad.success is False and "SHA256" in bad.message


def test_incremental_without_full_falls_back_honestly(dsm):
    from core.engines.base import BackupType
    eng = _engine(dsm["port"], 902)
    r = eng.backup(BackupType.INCREMENTAL)
    assert r.success, r.message
    assert "诚实回退为全量" in r.message
    assert _manifest(r.backup_path)["backup_type"] == "full"


def test_engine_uses_managed_device_credentials(dsm):
    from core.synology import devices as dv
    from core.engines.base import BackupType
    did = dv.create_device(_device(dsm["port"], "engine-dev"))
    eng = _engine(5000, 903, {"synology_device_id": did})
    ok, _ = eng.preflight()
    assert ok is True
    r = eng.backup(BackupType.FULL)
    assert r.success, r.message
    assert _manifest(r.backup_path)["device_id"] == did
    dv.delete_device(did)


def _manifest(tar_path):
    with tarfile.open(tar_path, "r:gz") as tf:
        return json.loads(tf.extractfile("manifest.json").read().decode("utf-8"))


def _read(target, rel):
    with open(os.path.join(target, rel), "rb") as f:
        return f.read()


# ---------------------------------------------------------------- API / 一键任务
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


def test_api_routes_registered_both_prefixes(client):
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()}
    for suffix in ("/synology/devices", "/synology/scenarios",
                   "/synology/devices/<int:dev_id>/quick-task"):
        assert "/api" + suffix in rules
        assert "/api/v1" + suffix in rules


def test_api_device_crud_and_scenarios(client, dsm):
    r = client.post("/api/synology/devices", json=_device(dsm["port"], "api-dev"))
    assert r.status_code == 201, r.get_json()
    did = r.get_json()["id"]
    lst = client.get("/api/synology/devices").get_json()
    assert any(x["id"] == did and x["password"] == "" for x in lst)
    pr = client.post("/api/synology/devices/%d/test" % did, json={})
    assert pr.status_code == 200 and pr.get_json()["ok"] is True
    sh = client.get("/api/synology/devices/%d/shares" % did)
    assert sh.status_code == 200 and sh.get_json()["count"] >= 2
    sc = client.get("/api/v1/synology/scenarios")
    assert sc.status_code == 200 and "S3" in sc.get_json()["boundary"]
    assert client.delete("/api/synology/devices/%d" % did).status_code == 200


def test_api_test_unsaved_config(client, dsm):
    good = _device(dsm["port"])
    assert client.post("/api/synology/test", json=good).status_code == 200
    bad = {**good, "password": "wrong"}
    r = client.post("/api/synology/test", json=bad)
    assert r.status_code == 400 and r.get_json()["ok"] is False


def test_quick_task_mixed_and_linked_delete_guard(client, dsm, monkeypatch):
    from core import license as license_mod
    from core import scheduler
    monkeypatch.setattr(license_mod, "check_quota", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "reload_scheduler", lambda *a, **k: None)
    did = client.post("/api/synology/devices",
                      json=_device(dsm["port"], "qt-dev")).get_json()["id"]
    r = client.post("/api/synology/devices/%d/quick-task" % did, json={
        "paths": ["/backup"], "strategy": "mixed",
        "incremental_minutes": 0, "biz_system": "E2E业务",
        "full_cron": "0 3 * * 0",
    })
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    assert out["strategy"] == "mixed" and out["incremental_minutes"] == 1
    task = db.query_one("SELECT * FROM backup_tasks WHERE id=?", (out["id"],))
    assert task["db_type"] == "synology" and task["mixed_backup"] == 1
    assert task["full_schedule_expr"] == "0 3 * * 0"
    assert task["incremental_schedule_type"] == "interval"
    assert task["incremental_schedule_expr"] == "1"   # 60 秒下限
    assert task["password"] in ("", None)              # 凭据不复制到任务
    extra = json.loads(task["extra_options"])
    assert extra["synology_device_id"] == did
    assert extra["synology_paths"] == ["/backup"]
    # 组合策略调度契约：两个子调度都能被 scheduler._make_trigger 解析，
    # 且增量 interval 恰好为 60 秒下限（不是被 `or 默认值` 吞成 60 分钟）
    from core import scheduler as sm
    full_trig = sm._make_trigger(
        dict(task), st_key="full_schedule_type", expr_key="full_schedule_expr",
        minutes_key="full_schedule_expr", days_key="full_schedule_days")
    inc_trig = sm._make_trigger(
        dict(task), st_key="incremental_schedule_type",
        expr_key="incremental_schedule_expr",
        minutes_key="incremental_schedule_expr",
        days_key="incremental_schedule_days")
    assert full_trig is not None and inc_trig is not None
    assert inc_trig.interval.total_seconds() == 60
    assert client.get("/api/synology/tasks?device_id=%d" % did).get_json()
    # 有任务引用时不允许删除设备
    guard = client.delete("/api/synology/devices/%d" % did)
    assert guard.status_code == 409 and guard.get_json()["task_ids"]
    db.execute("DELETE FROM backup_tasks WHERE id=?", (out["id"],))
    assert client.delete("/api/synology/devices/%d" % did).status_code == 200


def test_quick_task_rejects_empty_paths(client, dsm):
    did = client.post("/api/synology/devices",
                      json=_device(dsm["port"], "qt-empty")).get_json()["id"]
    r = client.post("/api/synology/devices/%d/quick-task" % did,
                    json={"paths": []})
    assert r.status_code == 400
    client.delete("/api/synology/devices/%d" % did)


# ---------------------------------------------------------------- 前端契约
def test_frontend_controls_and_cache_stamp():
    tpl = open(os.path.join(ROOT, "templates", "synology.html"),
               encoding="utf-8").read()
    js = open(os.path.join(ROOT, "static", "js", "synology.js"),
              encoding="utf-8").read()
    base = open(os.path.join(ROOT, "templates", "base.html"),
                encoding="utf-8").read()
    for key in ("synDeviceTable", "synAssetDevice", "synQuickTaskBtn",
                "syn_qt_strategy", "syn_qt_inc_min", "syn_qt_excludes", "synScenarios",
                "syn_d_otp", "synProbeModal"):
        assert 'id="%s"' % key in tpl, key
    import re
    m = re.search(r"synology\.js\?v=(\d{8,10})", tpl)
    assert m and int(m.group(1)) >= 2026111002, "缓存戳不得低于清理前基线"
    assert 'href="/synology"' in base and "群晖 NAS 纳管" in base
    assert "quick-task" in js and "verify_code" in js
    assert "SAN Manager" in tpl


def test_production_entry_full_and_restore(dsm, monkeypatch):
    """走 scheduler 生产入口，而不是只直调引擎：证明全量+恢复真能落地记录。"""
    from core import license as license_mod
    from core import models, scheduler
    monkeypatch.setattr(license_mod, "check_quota", lambda *a, **k: None)
    tid = models.create_task({
        "name": "syn-prod-e2e", "biz_system": "E2E业务",
        "db_type": "synology", "host": "127.0.0.1", "port": dsm["port"],
        "username": "admin", "password": "pw", "db_name": "/backup",
        "backup_type": "full", "backup_mode": "logical",
        "schedule_type": "interval", "interval_minutes": 60,
        "enabled": 0, "compress": 1,
        "extra_options": {"synology_paths": ["/backup"],
                          "synology_timeout": 5},
    })
    rec = scheduler.run_task_now(tid, backup_type="full", operator="pytest")
    assert rec, "run_task_now 未返回备份记录"
    assert rec.get("status") == "success", rec.get("message")
    assert os.path.isfile(rec["backup_path"])
    assert os.path.getsize(rec["backup_path"]) > 0

    target = os.path.join(_TMP, "restore_prod")
    rr = scheduler.run_restore_now(rec["id"], target_db=target,
                                   operator="pytest")
    assert rr, "run_restore_now 未返回恢复记录"
    assert rr.get("status") == "success", rr.get("message")
    assert _read(target, "files/backup/a.txt") == b"hello world"
    assert os.path.exists(os.path.join(target, "files/backup/sub/中文.txt"))
    # 审计轨迹落在正式记录表
    assert db.query_one("SELECT id FROM restore_records WHERE id=?", (rr["id"],))
    db.execute("DELETE FROM backup_tasks WHERE id=?", (tid,))


def test_exclude_rules_are_actually_applied(dsm):
    from core.engines.base import BackupType
    from core.engines.synology import _is_excluded
    assert _is_excluded("/backup/x.tmp", ["*.tmp"]) is True
    assert _is_excluded("/backup/a.txt", ["*.tmp"]) is False
    assert _is_excluded("/backup/sub/中文.txt", ["backup/sub"]) is True
    assert _is_excluded("/backup/a.txt", []) is False

    eng = _engine(dsm["port"], 905,
                  {"synology_excludes": ["*.tmp", "backup/sub"]})
    r = eng.backup(BackupType.FULL)
    assert r.success, r.message
    mf = _manifest(r.backup_path)
    assert mf["file_count"] == 1
    assert mf["excluded_count"] == 1
    assert mf["excludes"] == ["*.tmp", "backup/sub"]
    assert [e["path"] for e in mf["files"]] == ["/backup/a.txt"]
