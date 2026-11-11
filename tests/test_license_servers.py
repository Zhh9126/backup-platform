# -*- coding: utf-8 -*-
"""按授权服务器数计量（distinct host 视图）回归测试。"""
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="license_servers_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest  # noqa: E402
from core import db  # noqa: E402
from core import license as lic  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    import config
    old_db, old_root = config.META_DB_PATH, config.BACKUP_ROOT
    config.META_DB_PATH = os.path.join(_TMP, "meta.db")
    config.BACKUP_ROOT = os.path.join(_TMP, "backups")
    db.init_schema()
    db.init_schema()
    yield
    config.META_DB_PATH, config.BACKUP_ROOT = old_db, old_root


@pytest.fixture(autouse=True)
def clean_tables():
    for t in ("backup_tasks", "ssh_hosts", "synology_devices",
              "vm_hypervisors", "netdev_devices"):
        try:
            db.execute("DELETE FROM %s" % t)
        except Exception:
            pass
    db.execute("DELETE FROM system_config WHERE key=?",
               (lic.SERVER_METERING_KEY,))
    lic._LICENSE_CACHE = None
    yield


def _task(host, db_type="mysql", name="t"):
    db.execute(
        "INSERT INTO backup_tasks (name, biz_system, db_type, host, backup_type, "
        "created_at, last_status) VALUES (?,?,?,?,?,?,?)",
        (name, "E2E", db_type, host, "full", db.now_iso(), "never"))


def _host(hostname):
    db.execute("INSERT INTO ssh_hosts (name, host_key, hostname, port, username) "
               "VALUES (?,?,?,?,?)", (hostname, "root@%s:22" % hostname,
                                      hostname, 22, "root"))


# ---------------------------------------------------------------- 归一化
def test_normalize_host_variants():
    cases = {
        "root@192.168.220.150:22": "192.168.220.150",
        "10.0.0.9": "10.0.0.9",
        "https://vcenter.example.com:443/sdk": "vcenter.example.com",
        "https://NAS.EXAMPLE.COM:5001/": "nas.example.com",
        "admin@[::1]:22": "::1",
        "  ": "",
        None: "",
    }
    for raw, want in cases.items():
        assert lic.normalize_host(raw) == want, raw
    assert lic.is_loopback_host("127.0.0.1")
    assert lic.is_loopback_host("localhost")
    assert not lic.is_loopback_host("10.0.0.9")


# ---------------------------------------------------------------- 视图
def test_server_view_deduplicates_tasks_on_same_host():
    _task("root@10.0.0.9:3306", "mysql", "db1")
    _task("10.0.0.9", "postgresql", "db2")
    _task("10.0.0.9", "file", "file1")
    _task("127.0.0.1", "mysql", "local")
    v = lic.server_view()
    assert v["distinct_count"] == 2          # 10.0.0.9 + 平台自身
    assert v["billable_count"] == 1
    assert v["platform_self_count"] == 1
    s = next(x for x in v["servers"] if x["host"] == "10.0.0.9")
    assert s["task_count"] == 3
    assert set(s["db_types"]) == {"mysql", "postgresql", "file"}
    assert s["is_platform_self"] is False
    assert v["limit"] == lic.FREE_MAX_QUOTA
    assert v["remaining"] == lic.FREE_MAX_QUOTA - 1
    assert v["metering_enabled"] is False


def test_server_view_merges_managed_entities():
    _task("10.0.0.9", "mysql")
    _host("10.0.0.9")
    _host("10.0.0.10")
    db.execute("INSERT INTO synology_devices (name, hostname, port, username) "
               "VALUES (?,?,?,?)", ("nas", "10.0.0.11", 5000, "admin"))
    db.execute("INSERT INTO vm_hypervisors (name, provider, endpoint) "
               "VALUES (?,?,?)", ("vc", "vmware", "https://10.0.0.12:443"))
    db.execute("INSERT INTO netdev_devices "
               "(name, device_type, vendor, host, port, username) "
               "VALUES (?,?,?,?,?,?)",
               ("sw", "switch", "generic", "10.0.0.13", 22, "admin"))
    v = lic.server_view()
    assert {"10.0.0.9", "10.0.0.10", "10.0.0.11", "10.0.0.12",
            "10.0.0.13"}.issubset({s["host"] for s in v["servers"]})
    merged = next(x for x in v["servers"] if x["host"] == "10.0.0.9")
    assert set(merged["sources"]) == {"backup_task", "ssh_host"}
    assert next(x for x in v["servers"] if x["host"] == "10.0.0.11")[
        "sources"] == ["synology_device"]


def test_license_info_exposes_max_servers():
    info = lic.get_license_info()
    assert info["max_servers"] == lic.FREE_MAX_QUOTA
    assert info["is_licensed"] is False


# ---------------------------------------------------------------- 可选强校验
def test_metering_default_off_is_zero_breakage():
    assert lic.metering_enabled() is False
    for h in ("10.0.0.%d" % i for i in range(1, 20)):
        _task(h)
    assert lic.server_view()["billable_count"] == 19
    lic.check_server_quota("10.0.0.100")     # 不抛错


def test_metering_on_rejects_new_host_over_limit():
    db.set_system_config(lic.SERVER_METERING_KEY, "1")
    assert lic.metering_enabled() is True
    for i in range(1, lic.FREE_MAX_QUOTA + 1):
        _task("10.0.1.%d" % i)
    assert lic.server_view()["billable_count"] == lic.FREE_MAX_QUOTA
    lic.check_server_quota("10.0.1.3")       # 已有主机：不新增名额
    lic.check_server_quota("127.0.0.1")      # 平台自身：不计
    with pytest.raises(lic.QuotaExceededError) as e:
        lic.check_server_quota("root@10.0.1.250:3306")
    assert "按授权服务器数计量" in str(e.value)
    assert "10.0.1.250" in str(e.value)


def test_create_task_honors_server_metering(monkeypatch):
    from core import models
    monkeypatch.setattr(lic, "check_quota", lambda *a, **k: None)
    db.set_system_config(lic.SERVER_METERING_KEY, "1")
    tid = models.create_task({"name": "ok", "biz_system": "E2E",
                              "db_type": "mysql", "host": "10.0.2.1",
                              "backup_type": "full", "enabled": 0})
    assert tid
    # 同一主机再建任务：不占新名额
    assert models.create_task({"name": "ok2", "biz_system": "E2E",
                               "db_type": "mysql", "host": "10.0.2.1",
                               "backup_type": "full", "enabled": 0})
    for i in range(2, lic.FREE_MAX_QUOTA + 1):
        models.create_task({"name": "h%d" % i, "biz_system": "E2E",
                            "db_type": "mysql", "host": "10.0.2.%d" % i,
                            "backup_type": "full", "enabled": 0})
    assert lic.server_view()["billable_count"] == lic.FREE_MAX_QUOTA
    with pytest.raises(lic.QuotaExceededError):
        models.create_task({"name": "over", "biz_system": "E2E",
                            "db_type": "mysql", "host": "10.0.2.250",
                            "backup_type": "full", "enabled": 0})


# ---------------------------------------------------------------- API / 前端
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


def test_api_server_view_and_license_envelope(client):
    r = client.get("/api/system/license/servers")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert {"servers", "billable_count", "limit", "note"}.issubset(data)
    base = client.get("/api/system/license").get_json()["data"]
    assert "server_view" in base and base["server_view"]["distinct_count"] >= 0
    assert client.get("/api/v1/system/license/servers").status_code == 200


def test_frontend_contract():
    tpl = io.open(os.path.join(ROOT, "templates", "settings.html"),
                  encoding="utf-8").read()
    for key in ("licServerUsed", "licServerLimit", "licServerNote",
                "licServerTable"):
        assert 'id="%s"' % key in tpl, key
    assert "renderServerView" in tpl
    assert "按授权服务器数计量" in tpl
    assert "server_view" in tpl
    models_src = io.open(os.path.join(ROOT, "core", "models.py"),
                         encoding="utf-8").read()
    assert "check_server_quota" in models_src


# ---------------------------------------------------------------- 免费版配额扩容
def test_free_quota_expanded_to_100():
    """正式库社区免费版每类纳管配额 5 -> 100（用户 2026-11-11 要求）。"""
    assert lic.FREE_MAX_QUOTA == 100
    info = lic.get_license_info()
    assert info["quota_limit"] == 100
    assert info["max_devices"] == 100
    assert info["max_servers"] == 100
    q = lic.get_quotas()["database"]
    assert q["limit"] == 100 and q["remaining"] == 100
    assert q["exceeded"] is False


def test_database_quota_rejects_only_above_100():
    """第 100 个数据库任务仍可建，第 101 个才拦截且错误文案带真实上限。"""
    for i in range(lic.FREE_MAX_QUOTA - 1):
        _task("10.0.3.%d" % i, "mysql", "q%d" % i)
    lic.check_quota("database")          # used=99，新增后正好 100：放行
    _task("10.0.3.99", "mysql", "q99")
    assert lic.get_counts()["database"] == lic.FREE_MAX_QUOTA
    with pytest.raises(lic.QuotaExceededError) as e:
        lic.check_quota("database")
    assert "100" in str(e.value)
    q = lic.get_quotas()["database"]
    assert q["used"] == 100 and q["remaining"] == 0 and q["exceeded"] is True


def test_frontend_quota_is_not_hardcoded():
    """授权页配额展示必须跟随服务端配额，不能残留写死 5。"""
    tpl = io.open(os.path.join(ROOT, "templates", "settings.html"),
                  encoding="utf-8").read()
    assert 'id="licFreeQuotaHint"' in tpl
    assert "freeQuota" in tpl
    assert "每类限额 5 台" not in tpl
    assert "上限为 5" not in tpl
    assert "(q.limit || 5)" not in tpl
    base = io.open(os.path.join(ROOT, "templates", "base.html"),
                   encoding="utf-8").read()
    assert "quota_limit" in base
