# -*- coding: utf-8 -*-
"""接口对接中心（2026-11-25）单测。

覆盖：JSONPath 子集、字段映射、关键字过滤、去重、pull/push 真实 HTTP 往返、
对端日志源接口（log_source）、未保存配置的测试通道、API 契约。
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_TMP = tempfile.mkdtemp(prefix="integration_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core import integration as I  # noqa: E402


# --------------------------------------------------------------------------- #
# 假的第三方日志接口
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/fail"):
            self.send_response(500)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path.startswith("/text"):
            body = "2026-11-25 ERROR 磁盘满\n2026-11-25 INFO 正常\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send({"success": True, "data": {"logs": [
            {"ts": "2026-11-25T10:00:00", "level": "ERROR",
             "source": "backup", "message": "备份失败 timeout"},
            {"ts": "2026-11-25T10:00:01", "level": "INFO",
             "source": "backup", "message": "备份成功"},
        ]}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        _Handler.received.append({"path": self.path, "body": raw,
                                  "auth": self.headers.get("Authorization") or ""})
        self._send({"ok": True})


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


_SRV, _PORT = _server()


def _base(**kw):
    cfg = {"name": "t", "direction": "pull",
           "endpoint": "http://127.0.0.1:%d/logs" % _PORT, "method": "GET",
           "parse_type": "json", "json_path": "$.data.logs[*]",
           "schedule_type": "manual", "alarm_keywords": "timeout"}
    cfg.update(kw)
    return cfg


# --------------------------------------------------------------------------- #
# JSONPath / 字段映射
# --------------------------------------------------------------------------- #
def test_jsonpath_subset():
    data = {"data": {"logs": [{"m": 1}, {"m": 2}, {"m": 3}]}}
    assert I.jsonpath(data, "$.data.logs[*]") == [{"m": 1}, {"m": 2}, {"m": 3}]
    assert I.jsonpath(data, "$.data.logs[1]") == [{"m": 2}]
    assert I.jsonpath(data, "$.data.logs[0:2]") == [{"m": 1}, {"m": 2}]
    assert I.jsonpath({"a": {"b": {"c": 9}}}, "$.a.b.c") == [9]
    assert I.jsonpath([1, 2], "$") == [[1, 2]]
    assert I.jsonpath(data, "$.missing[*]") == []
    # 递归下降明确不支持（给出可读错误，而不是静默返回空）
    try:
        I.jsonpath(data, "$..message")
        raise AssertionError("应当拒绝递归下降")
    except ValueError as exc:
        assert "递归下降" in str(exc)


def test_field_map_and_text_parse():
    recs = I._extract([{"msg": "hello", "lvl": "warning", "t": "2026-01-01"}],
                      {"message": "msg", "level": "lvl", "ts": "t"})
    assert recs[0]["message"] == "hello"
    assert recs[0]["level"] == "WARN"
    assert recs[0]["ts"] == "2026-01-01"
    # text 逐行：每行一条，级别缺省 INFO
    cfg = _base(endpoint="http://127.0.0.1:%d/text" % _PORT, parse_type="text",
                json_path="$")
    res = I.test_task(data=cfg)
    assert res["ok"] and res["fetched"] == 2


# --------------------------------------------------------------------------- #
# 采集 / 去重 / 告警
# --------------------------------------------------------------------------- #
def test_pull_persist_dedupe_and_alarm():
    tid = I.create_task(_base(name="pull-1"))
    first = I.run_task(tid)
    assert first["ok"] and first["inserted"] == 2 and first["fetched"] == 2
    assert first["new_alarm_hits"] == 1
    rows, total = I.list_logs(task_id=tid)
    assert total == 2
    assert any(r["alerted"] for r in rows)
    # 第二次拉取同样内容：指纹去重，不重复入库、不重复告警
    second = I.run_task(tid)
    assert second["inserted"] == 0 and second["new_alarm_hits"] == 0
    assert I.list_logs(task_id=tid)[1] == 2
    task = I.get_task(tid)
    assert task["last_status"] == "success" and task["success_count"] >= 2
    assert task["has_secret"] is False and "auth_secret_enc" not in task


def test_keyword_filters():
    tid = I.create_task(_base(name="filter-1", include_keywords="error",
                              alarm_keywords=""))
    res = I.run_task(tid)
    assert res["matched"] == 1 and res["filtered_out"] == 1


def test_push_sends_local_logs_and_advances_watermark():
    _Handler.received.clear()
    tid = I.create_task({"name": "push-1", "direction": "push",
                         "endpoint": "http://127.0.0.1:%d/ingest" % _PORT,
                         "method": "POST", "schedule_type": "manual",
                         "auth_type": "bearer", "auth_secret": "tk-123",
                         "max_items": 50})
    res = I.run_task(tid)
    assert res["ok"] and res["pushed"] >= 1
    assert _Handler.received, "对端没有收到推送"
    sent = json.loads(_Handler.received[-1]["body"])
    assert sent["source"] == "aidbm" and sent["logs"]
    assert _Handler.received[-1]["auth"] == "Bearer tk-123"
    task = I.get_task(tid)
    assert task["watermark"].isdigit() and int(task["watermark"]) > 0
    # 水位推进后不重复推送同一批
    res2 = I.run_task(tid)
    assert res2["ok"] and res2["pushed"] == 0


def test_http_error_is_reported_not_faked():
    tid = I.create_task(_base(name="fail-1",
                              endpoint="http://127.0.0.1:%d/fail" % _PORT))
    res = I.run_task(tid)
    assert res["ok"] is False
    assert "HTTP 500" in res["error"]
    task = I.get_task(tid)
    assert task["last_status"] == "failed" and task["fail_count"] == 1
    runs = I.list_runs(tid)
    assert runs and runs[0]["status"] == "failed"


def test_secret_encrypted_and_not_returned():
    tid = I.create_task(_base(name="auth-1", auth_type="bearer",
                              auth_secret="super-secret"))
    raw = _db.query_one("SELECT auth_secret_enc FROM integration_tasks WHERE id=?", (tid,))
    assert raw["auth_secret_enc"] and "super-secret" not in raw["auth_secret_enc"]
    assert I.get_task(tid, include_secret=True)["auth_secret"] == "super-secret"
    # 更新时空密钥保留旧值
    I.update_task(tid, {"name": "auth-1-renamed"})
    assert I.get_task(tid, include_secret=True)["auth_secret"] == "super-secret"


# --------------------------------------------------------------------------- #
# 测试通道（未保存配置）
# --------------------------------------------------------------------------- #
def test_test_task_accepts_unsaved_config():
    res = I.test_task(data=_base(endpoint="http://127.0.0.1:%d/logs" % _PORT))
    assert res["ok"] and res["fetched"] == 2 and res["matched"] == 2
    assert res["samples"] and "raw_preview" in res
    assert "解析正常" in res["hint"]


def test_test_task_reports_bad_jsonpath_and_unreachable():
    # 非法 JSONPath 属于配置错误：直接抛出可读原因（API 转 400 给前端提示）
    try:
        I.test_task(data=_base(json_path="$.data.logs[oops]"))
        raise AssertionError("非法 JSONPath 应当报错")
    except ValueError as exc:
        assert "JSONPath" in str(exc)
    unreachable = I.test_task(data=_base(endpoint="http://127.0.0.1:1/logs"))
    assert unreachable["ok"] is False and unreachable["hint"]


def test_log_source_incremental_by_id_and_ts():
    _db.add_log("ERROR", "unit-test", "log-source-error-marker")
    first = I.log_source(limit=5)
    assert first["count"] >= 1 and first["next_since"].isdigit()
    assert first["logs"][0]["id"] <= first["logs"][-1]["id"], "日志应按由旧到新返回"
    # 按水位继续拉，直到拉空（分页语义：id > since 升序取 limit 条）
    since = first["next_since"]
    for _ in range(20):
        page = I.log_source(since=since, limit=5)
        if not page["count"]:
            break
        assert int(page["logs"][0]["id"]) > int(since)
        since = page["next_since"]
    else:
        raise AssertionError("水位递增未能拉空")
    assert I.log_source(since=since, limit=5)["count"] == 0
    # 时间戳水位同样可用
    assert I.log_source(since="2000-01-01T00:00:00", limit=5)["count"] >= 1


# --------------------------------------------------------------------------- #
# API 契约
# --------------------------------------------------------------------------- #
def test_api_endpoints_contract():
    import app as app_module
    client = app_module.create_app().test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"id": 1, "username": "admin", "role": "admin"}
        sess["username"] = "admin"
    tid = I.create_task(_base(name="api-1"))
    r = client.get("/api/v1/integration/summary")
    assert r.status_code == 200 and r.get_json()["stats"]["tasks"] >= 1
    r = client.get("/api/v1/integration-tasks/%d" % tid)
    assert r.status_code == 200 and r.get_json()["task"]["name"] == "api-1"
    r = client.post("/api/v1/integration-tasks/test",
                    json=_base(endpoint="http://127.0.0.1:%d/logs" % _PORT))
    assert r.status_code == 200 and r.get_json()["fetched"] == 2
    r = client.post("/api/v1/integration-tasks/%d/run" % tid)
    assert r.status_code in (200, 502)
    r = client.get("/api/v1/integration/logs")
    assert r.status_code == 200 and "logs" in r.get_json()
    r = client.get("/api/v1/integration/log-source?limit=2")
    assert r.status_code == 200 and r.get_json()["success"] is True
    r = client.get("/integration")
    assert r.status_code == 200 and "接口对接中心" in r.get_data(as_text=True)
    r = client.delete("/api/v1/integration-tasks/%d" % tid)
    assert r.status_code == 200
