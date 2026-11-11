# -*- coding: utf-8 -*-
"""应用级容灾回归：探活 / 业务验证 / 切换并发护栏 / API 契约。"""
import http.server
import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="dr_app_ha_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db  # noqa: E402
db.init_schema()

import pytest  # noqa: E402
from core import dr_app_ha, dr_failover, net_guard  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"app":"AIDBM-E2E"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def http_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_probe_app_default_is_noop():
    assert dr_app_ha.probe_app({})["ok"] is True
    assert dr_app_ha.probe_app({"enabled": False, "url": "http://x"})["ok"] is True


def test_probe_app_blocks_loopback_ssrf():
    r = dr_app_ha.probe_app({"url": "http://127.0.0.1:8080/healthz"})
    assert r["ok"] is False and "安全策略" in str(r.get("error"))


def test_probe_app_real_http(monkeypatch, http_server):
    monkeypatch.setattr(net_guard, "validate_url", lambda u: (True, ""))
    url = "http://127.0.0.1:%s/healthz" % http_server
    ok = dr_app_ha.probe_app({"url": url, "expect_body": "AIDBM-E2E"})
    assert ok["ok"] is True and ok["status"] == 200
    bad = dr_app_ha.probe_app({"url": url, "expect_status": 201})
    assert bad["ok"] is False and bad["status"] == 200
    missing = dr_app_ha.probe_app(
        {"url": "http://127.0.0.1:%s/nope" % http_server})
    assert missing["ok"] is False and missing["status"] == 404


def test_sql_check_rejects_non_select():
    ok, detail = dr_app_ha._sql_check(
        {"query": "UPDATE t SET x=1"}, {}, "failover", None)
    assert ok is False and "只允许 SELECT" in detail


def test_verification_default_disabled():
    r = dr_app_ha.run_post_switch_verification({}, "failover")
    assert r["ok"] is True and r["checks"] == []


def test_verification_sql_and_failure(monkeypatch):
    class _Conn:
        def cursor(self):
            return self

        def execute(self, q):
            self.q = q

        def fetchone(self):
            return (5,)

        def close(self):
            pass

    monkeypatch.setattr(dr_app_ha.native_conn, "connect",
                        lambda *a, **kw: _Conn())
    cfg = {"post_switch_verification": {"enabled": True, "checks": [
        {"name": "行数", "type": "sql", "query": "SELECT COUNT(*) FROM t",
         "expect": "5"},
        {"name": "写操作拦截", "type": "sql", "query": "DELETE FROM t",
         "expect": "0"},
    ]}, "standby_conn": {"host": "10.0.0.1", "user": "root"}}
    r = dr_app_ha.run_post_switch_verification(
        cfg, "failover", decrypt_conn=lambda c: c)
    assert r["configured"] is True and r["ok"] is False
    assert r["checks"][0]["ok"] is True
    assert r["checks"][1]["ok"] is False


def test_sanitize_checks_strips_secrets():
    out = dr_app_ha.sanitize_checks([
        {"name": "a", "type": "sql", "query": "SELECT 1", "password": "pw",
         "__proto__": 1}])
    assert out == [{"name": "a", "type": "sql", "query": "SELECT 1"}]


def test_concurrent_switch_rejected(monkeypatch):
    monkeypatch.setattr(
        dr_failover.db, "query_one",
        lambda *a, **kw: {"id": 77, "name": "t", "failover_config": "{}"})

    def slow(self, force=False):
        time.sleep(0.35)
        return {"direction": "failover", "steps": []}

    monkeypatch.setattr(dr_failover.FailoverEngine, "failover", slow)
    errs, oks = [], []

    def call():
        try:
            dr_failover.run_failover(77)
            oks.append(1)
        except dr_failover.FailoverError as e:
            errs.append(str(e))

    t1 = threading.Thread(target=call)
    t1.start()
    time.sleep(0.06)
    t2 = threading.Thread(target=call)
    t2.start()
    t1.join()
    t2.join()
    assert len(oks) == 1 and len(errs) == 1
    assert "已有切换" in errs[0]


def test_health_snapshot_exposes_app_probe():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "core", "dr_failover.py"),
        encoding="utf-8").read()
    assert '"app": None' in src
    assert "dr_app_ha.probe_app" in src
