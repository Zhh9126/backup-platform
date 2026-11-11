# -*- coding: utf-8 -*-
"""异地容灾「云侧推对象存储 + 平台出站拉取」回归测试。

对象存储用内存 FakeS3 仿真**业务与登记链路**（推包→列举→下载→sha256→
摆渡入库→幂等）；S3 协议本身的真实验证由对象存储断点续传 E2E 覆盖。
"""
import json
import os
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="remote_pull_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest  # noqa: E402
import config  # noqa: E402
from core import db  # noqa: E402

class FakeS3:
    """内存对象存储：只实现本模块用到的 S3 子集。"""

    def __init__(self, data=None):
        self.objects = dict(data or {})

    def test_connection(self):
        return True, "fake ok"

    def bucket_exists(self, bucket):
        return True

    def iter_objects(self, bucket, prefix="", max_keys=1000):
        for k in sorted(self.objects):
            if k.startswith(prefix or ""):
                yield {"key": k, "size": len(self.objects[k]),
                       "etag": "fake", "last_modified": ""}

    def upload_file(self, bucket, key, src, **kw):
        with open(src, "rb") as f:
            self.objects[key] = f.read()
        return {"etag": "fake"}

    def download_to_file(self, bucket, key, dest, version_id="", resume=False,
                         expected_size=None, max_retries=3, on_chunk=None):
        if key not in self.objects:
            raise IOError("NoSuchKey: " + key)
        data = self.objects[key]
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return len(data)

    def delete_object(self, bucket, key, version_id=""):
        self.objects.pop(key, None)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    # config.META_DB_PATH 是 **运行时** 读取的模块属性（db.open_sqlite_conn）。
    # 顶层直接赋值会污染同进程后续测试模块，这里在 module fixture 内改并恢复。
    old_db, old_root = config.META_DB_PATH, config.BACKUP_ROOT
    config.META_DB_PATH = os.path.join(_TMP, "meta.db")
    config.BACKUP_ROOT = os.path.join(_TMP, "backups")
    db.init_schema()
    db.init_schema()   # 幂等
    yield
    config.META_DB_PATH, config.BACKUP_ROOT = old_db, old_root


@pytest.fixture()
def env(monkeypatch):
    """隔离收件箱 + 屏蔽摆渡后处理的异步副作用。"""
    from core import ferry_inbox
    inbox = os.path.join(_TMP, "inbox")
    for sub in ("", "processed", "failed"):
        os.makedirs(os.path.join(inbox, sub) if sub else inbox, exist_ok=True)
    monkeypatch.setattr(ferry_inbox, "inbox_dir", lambda: inbox)
    for mod, names in (("object_catalog", ("scan_async",)),
                       ("tier_replication", ("replicate_async",)),
                       ("webhooks", ("emit_async",))):
        try:
            m = __import__("core." + mod, fromlist=["x"])
        except Exception:
            continue
        for n in names:
            if hasattr(m, n):
                monkeypatch.setattr(m, n, lambda *a, **k: None)
    return {"inbox": inbox}


def _source(**kw):
    return {
        "name": "异地机房 MinIO", "provider": "minio",
        "endpoint": "10.10.0.5", "port": 9000, "secure": False,
        "verify_ssl": False, "region": "us-east-1",
        "access_key": "ak", "secret_key": "sk",
        "bucket": "aidbm-offsite", "prefix": "dr/siteB",
        "enabled": True, "interval_minutes": 30,
        "delete_after_pull": False, "remark": "专线",
        **kw,
    }


def _task(monkeypatch):
    from core import models, license as license_mod
    monkeypatch.setattr(license_mod, "check_quota", lambda *a, **k: None)
    return models.create_task({
        "name": "offsite-src", "biz_system": "E2E", "db_type": "mysql",
        "host": "10.0.0.9", "db_name": "appdb", "backup_type": "full",
        "enabled": 0,
    })


def _record(task_id):
    from core import models
    p = os.path.join(_TMP, "backups", "seed.sql")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write("中文内容-异地副本".encode("utf-8"))
    return models.create_record({
        "task_id": task_id, "backup_type": "full", "backup_mode": "logical",
        "status": "success", "backup_path": p, "size_bytes": os.path.getsize(p),
        "started_at": db.now_iso(), "finished_at": db.now_iso(),
        "note": "E2E 源记录",
    })


# ---------------------------------------------------------------- 表 / CRUD
def test_tables_exist_and_idempotent():
    for table, cols in (("remote_pull_sources",
                         {"id", "bucket", "secret_key", "last_status", "interval_minutes"}),
                        ("remote_pull_logs",
                         {"id", "source_id", "object_key", "status", "record_id"})):
        got = {r["name"] for r in db.query("PRAGMA table_info(%s)" % table)}
        assert cols.issubset(got), table


def test_source_crud_secret_and_normalize():
    from core import remote_pull as rp
    sid = rp.create_source(_source())
    raw = db.query_one("SELECT secret_key FROM remote_pull_sources WHERE id=?", (sid,))
    assert raw["secret_key"].startswith("sm4v1:")
    listed = rp.list_sources()
    one = next(x for x in listed if x["id"] == sid)
    assert one["secret_key"] == "" and one["has_secret_key"] is True
    assert rp.get_source(sid, include_secret=True)["secret_key"] == "sk"
    assert rp.get_source(sid, include_secret=True)["secure"] == 0
    assert rp.update_source(sid, {"remark": "改了", "secret_key": "",
                                  "interval_minutes": 0}) is True
    assert rp.get_source(sid, include_secret=True)["secret_key"] == "sk"
    assert rp.get_source(sid)["remark"] == "改了"
    with pytest.raises(rp.RemotePullError):
        rp.create_source(_source(name="", bucket="b"))
    assert rp.delete_source(sid) is True
    assert rp.delete_source(sid) is False


def test_scenario_note_is_honest():
    from core import remote_pull as rp
    s = rp.scenario_note()
    assert s["is_p2p_tunnel"] is False
    assert any("P2P" in x for x in s["not_promised"])
    assert any("对象存储" in x for x in s["requires"])
    assert "离线" in s["offline"]


# ---------------------------------------------------------------- 推 / 拉 闭环
def test_push_then_pull_registers_record_and_is_idempotent(monkeypatch, env):
    from core import remote_pull as rp
    tid = _task(monkeypatch)
    rid = _record(tid)
    sid = rp.create_source(_source())
    cli = FakeS3()
    monkeypatch.setattr(rp, "_client_for", lambda src: cli)

    pushed = rp.push_record(sid, rid)
    assert pushed["ok"] and pushed["package_key"].endswith(".inc.tar.gz")
    assert set(cli.objects) == {pushed["package_key"], pushed["manifest_key"]}
    man = json.loads(cli.objects[pushed["manifest_key"]].decode("utf-8"))
    assert man["task_id"] == tid and man["sha256"] == pushed["sha256"]
    assert man["file_name"] == "seed.sql"
    import io
    with tarfile.open(fileobj=io.BytesIO(cli.objects[pushed["package_key"]]),
                      mode="r:gz") as tf:
        assert "seed.sql" in tf.getnames()

    pre = rp.preview(sid)
    assert pre["count"] == 1 and pre["pending"][0]["manifest_key"] == pushed["manifest_key"]

    first = rp.pull_source(sid)
    assert first["pulled"] == 1 and first["failed"] == 0, first["details"]
    assert len(first["record_ids"]) == 1
    imported = db.query_one("SELECT * FROM backup_records WHERE id=?",
                            (first["record_ids"][0],))
    assert imported["status"] == "success"
    assert "摆渡导入" in (imported.get("message") or "")
    assert os.path.isfile(imported["backup_path"])

    # 幂等：同一对象不会重复登记
    second = rp.pull_source(sid)
    assert second["pulled"] == 0 and second["skipped"] == 1
    assert len(rp.list_logs(sid)) == 1
    assert rp.get_source(sid)["last_status"] == "online"
    rp.delete_source(sid)
    db.execute("DELETE FROM backup_tasks WHERE id=?", (tid,))


def test_corrupted_package_fails_honestly(monkeypatch, env):
    from core import remote_pull as rp
    tid = _task(monkeypatch)
    rid = _record(tid)
    sid = rp.create_source(_source())
    cli = FakeS3()
    monkeypatch.setattr(rp, "_client_for", lambda src: cli)
    pushed = rp.push_record(sid, rid)
    cli.objects[pushed["package_key"]] = b"tampered"
    out = rp.pull_source(sid)
    assert out["pulled"] == 0 and out["failed"] == 1
    assert "sha256" in out["details"][0]["message"]
    logs = rp.list_logs(sid)
    assert logs[0]["status"] == "failed" and logs[0]["record_id"] is None
    assert rp.get_source(sid)["last_status"] == "error"
    rp.delete_source(sid)
    db.execute("DELETE FROM backup_tasks WHERE id=?", (tid,))


def test_test_source_and_dry_run(monkeypatch, env):
    from core import remote_pull as rp
    cli = FakeS3()
    monkeypatch.setattr(rp, "_client_for", lambda src: cli)
    out = rp.test_source(dict(_source()))
    assert out["ok"] is True and out["bucket_exists"] is True
    sid = rp.create_source(_source())
    out2 = rp.test_source(sid)
    assert out2["ok"] is True and rp.get_source(sid)["last_status"] == "online"
    dry = rp.pull_source(sid, dry_run=True)
    assert dry["dry_run"] is True and dry["pending"] == []
    rp.delete_source(sid)


def test_run_due_sources_throttles(monkeypatch, env):
    from core import remote_pull as rp
    cli = FakeS3()
    monkeypatch.setattr(rp, "_client_for", lambda src: cli)
    sid = rp.create_source(_source(interval_minutes=60))
    calls = []
    monkeypatch.setattr(rp, "pull_source",
                        lambda s, dry_run=False, limit=50, client=None: (
                            calls.append(s), {"pulled": 0, "failed": 0})[1])
    rp.run_due_sources()          # last_run_at 空 → 跑
    rp.run_due_sources()          # 刚跑过 → 节流跳过
    assert calls == [sid]
    rp.delete_source(sid)


# ---------------------------------------------------------------- API / 前端
@pytest.fixture(scope="module")
def client():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    with application.test_client() as c:
        # 批量收集测试模块时 META_DB_PATH 可能被其它模块先固化，
        # 默认口令/现场口令都要兼容；登录失败会返回 200（重渲染登录页），
        # 不能只看状态码，否则后续 API 全 401。
        ok = False
        for pwd in ("admin123", "CMict@2026"):
            c.post("/login", data={"username": "admin", "password": pwd})
            with c.session_transaction() as sess:
                if sess.get("user"):
                    ok = True
                    break
        if not ok:
            with c.session_transaction() as sess:
                sess["user"] = {"username": "admin", "role": "admin"}
        yield c


def test_api_routes_both_prefixes(client):
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()}
    for suffix in ("/remote-pull/sources", "/remote-pull/scenarios",
                   "/remote-pull/logs",
                   "/remote-pull/sources/<int:sid>/pull"):
        assert "/api" + suffix in rules
        assert "/api/v1" + suffix in rules


def test_api_source_crud_and_scenarios(client, monkeypatch):
    from core import remote_pull as _rp
    monkeypatch.setattr(_rp, "_client_for", lambda src: FakeS3())
    r = client.post("/api/remote-pull/sources", json=_source(name="api-src"))
    assert r.status_code == 201, r.get_json()
    sid = r.get_json()["id"]
    assert all(x["secret_key"] == "" for x in
               client.get("/api/remote-pull/sources").get_json())
    s = client.get("/api/v1/remote-pull/scenarios").get_json()
    assert s["is_p2p_tunnel"] is False
    assert client.get("/api/remote-pull/sources/%d/preview" % sid).status_code in (200, 400)
    assert client.get("/api/remote-pull/logs").status_code == 200
    assert client.delete("/api/remote-pull/sources/%d" % sid).status_code == 200


def test_page_and_scheduler_registration():
    import io
    app_src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    sched = io.open(os.path.join(ROOT, "core", "scheduler.py"),
                    encoding="utf-8").read()
    base = io.open(os.path.join(ROOT, "templates", "base.html"),
                   encoding="utf-8").read()
    dr = io.open(os.path.join(ROOT, "templates", "drlink.html"),
                 encoding="utf-8").read()
    shell = io.open(os.path.join(ROOT, "templates", "remote_pull.html"),
                    encoding="utf-8").read()
    panel = io.open(os.path.join(ROOT, "templates", "_remote_pull_panel.html"),
                    encoding="utf-8").read()
    js = io.open(os.path.join(ROOT, "static", "js", "remote_pull.js"),
                 encoding="utf-8").read()
    # 旧入口保留但重定向到「容灾链路」页的拉取标签页，深链不丢
    assert '@app.route("/remote-pull")' in app_src
    assert 'args["tab"] = "remote-pull"' in app_src
    assert 'url_for("dr_link_page"' in app_src
    # 侧栏不再保留重复的独立菜单，容灾链路菜单是唯一入口
    assert 'href="/remote-pull"' not in base
    assert 'href="/dr-link"' in base
    # 两个页面只 include 同一份 DOM 片段，不维护第二套页面
    assert '{% include "_remote_pull_panel.html" %}' in dr
    assert '{% include "_remote_pull_panel.html" %}' in shell
    assert 'id="tabRemotePullBtn"' in dr and "tab=remote-pull" in dr
    # start 与 reload 两条路径都要注册
    assert sched.count("_register_remote_pull(_scheduler)") == 2
    assert 'id="remote_pull_tick"' in sched
    import re

    def _rp_stamp(txt):
        m = re.search(r"remote_pull\.js\?v=(\d{8,10})", txt)
        return int(m.group(1)) if m else 0

    # 两个入口必须引用同一版本，且不低于清理前的基线
    assert _rp_stamp(dr) >= 2026111001
    assert _rp_stamp(shell) == _rp_stamp(dr)
    for key in ("rpSourceTable", "rpOpSource", "rpPullBtn", "rpPushBtn",
                "rpScenarios", "rpRecordId"):
        assert 'id="%s"' % key in panel, key
    assert "P2P" in panel and "P2P" in js
    assert "/api/remote-pull" in js
