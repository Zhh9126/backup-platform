# -*- coding: utf-8 -*-
"""DSM 客户端契约测试（本地真实 HTTP 服务，走完整 HTTP/SYN O 协议栈）。

如实边界：这里是**协议级仿真服务**，验证客户端的会话、能力发现、分页、
错误码、Range 续传与「不虚报」行为；不是群晖真机。真机验证需要用户提供
DSM 设备（IP / 版本 / 账号 / 已启用服务），未跑之前不宣称已适配真机。
"""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from core.synology import DSMClient, DSMError

API_INFO = {
    "SYNO.API.Info": {"path": "query.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
    "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    "SYNO.FileStation.Download": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    "SYNO.DSM.Info": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.Storage.CGI.Storage": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
}

SHARES = [{"path": "/backup", "name": "backup",
           "additional": {"real_path": "/volume1/backup"}},
          {"path": "/homes", "name": "homes",
           "additional": {"real_path": "/volume1/homes"}}]

FILES = {
    "/backup": [
        {"path": "/backup/a.txt", "name": "a.txt", "isdir": False,
         "additional": {"size": 11, "time": {"mtime": 1700000000}}},
        {"path": "/backup/sub", "name": "sub", "isdir": True,
         "additional": {"size": 0, "time": {"mtime": 1700000001}}},
    ],
    "/backup/sub": [
        {"path": "/backup/sub/中文.txt", "name": "中文.txt", "isdir": False,
         "additional": {"size": 9, "time": {"mtime": 1700000002}}},
    ],
}

DATA = {"/backup/a.txt": b"hello world", "/backup/sub/中文.txt": "中文内容".encode("utf-8")}

STATE = {"sessions": set(), "seq": 0, "expire_once": False,
         "hide_dsm_info": False, "last_range": None}


class DSMStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body, status=200, headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _fail(self, code):
        self._json({"success": False, "error": {"code": code}})

    def _ok(self, data=None):
        self._json({"success": True, "data": data if data is not None else {}})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        api, method = q.get("api", ""), q.get("method", "")

        if api == "SYNO.API.Info" and method == "query":
            info = dict(API_INFO)
            if STATE["hide_dsm_info"]:
                info.pop("SYNO.DSM.Info")
            return self._ok(info)

        if api == "SYNO.API.Auth" and method == "login":
            if q.get("account") != "admin" or q.get("passwd") != "pw":
                return self._fail(400)
            STATE["seq"] += 1
            sid = "sid-%d" % STATE["seq"]
            STATE["sessions"].add(sid)
            return self._ok({"sid": sid, "synotoken": "tok"})

        if q.get("_sid") not in STATE["sessions"]:
            return self._fail(119)

        if api == "SYNO.API.Auth" and method == "logout":
            STATE["sessions"].discard(q.get("_sid"))
            return self._ok()

        if api == "SYNO.FileStation.List" and method == "list_share":
            if STATE["expire_once"]:
                STATE["expire_once"] = False
                return self._fail(106)
            offset, limit = int(q.get("offset") or 0), int(q.get("limit") or 1000)
            return self._ok({"total": len(SHARES), "offset": offset,
                             "shares": SHARES[offset:offset + limit]})

        if api == "SYNO.FileStation.List" and method == "list":
            folder = q.get("folder_path", "")
            return self._ok({"total": len(FILES.get(folder, [])), "offset": 0,
                             "files": FILES.get(folder, [])})

        if api == "SYNO.FileStation.Download" and method == "download":
            path = json.loads(q.get("path") or "[]")[0]
            full = DATA[path]
            rng = self.headers.get("Range")
            STATE["last_range"] = rng
            if rng and rng.startswith("bytes="):
                n = int(rng.split("=", 1)[1].rstrip("-"))
                body = full[n:]
                return self._bytes(body, 206, {
                    "Content-Range": "bytes %d-%d/%d" % (n, len(full) - 1, len(full))})
            return self._bytes(full)

        if api == "SYNO.DSM.Info" and method == "getinfo":
            return self._ok({"version": "DSM 7.2.1-69057", "model": "DS3622xs+",
                             "serial": "TEST123", "temperature": 41})

        if api == "SYNO.Core.Share" and method == "list":
            return self._fail(105)

        return self._fail(102)


@pytest.fixture()
def dsm():
    STATE.update({"sessions": set(), "seq": 0, "expire_once": False,
                  "hide_dsm_info": False, "last_range": None})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DSMStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    client = DSMClient("127.0.0.1", port=srv.server_address[1],
                       username="admin", password="pw", timeout=5)
    try:
        yield client
    finally:
        srv.shutdown()
        srv.server_close()


def test_discover_and_login(dsm):
    info = dsm.discover()
    assert info["SYNO.API.Auth"]["maxVersion"] == 7
    assert info["SYNO.FileStation.List"]["path"] == "entry.cgi"
    sid = dsm.login()
    assert sid.startswith("sid-") and sid in STATE["sessions"]
    assert dsm.dsm_major == 7
    dsm.logout()
    assert sid not in STATE["sessions"]


def test_bad_password_reports_syno_code():
    from http.server import ThreadingHTTPServer
    STATE.update({"sessions": set(), "seq": 0})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DSMStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = DSMClient("127.0.0.1", port=srv.server_address[1],
                      username="admin", password="wrong")
        with pytest.raises(DSMError) as e:
            c.login()
        assert e.value.code == 400 and "登录失败" in str(e.value)
        assert "wrong" not in str(e.value)  # 凭据不进异常文本
    finally:
        srv.shutdown()
        srv.server_close()


def test_capabilities_split_public_and_unofficial(dsm):
    caps = dsm.capabilities()
    assert "SYNO.FileStation.List" in caps["public_apis"]
    assert caps["filestation"] is True and caps["download"] is True
    assert caps["system_info"] is True and caps["storage"] is True
    assert caps["san_manager"] is False and caps["snapshot"] is False
    assert "system_info" in caps["unofficial"]
    assert "不承诺跨 DSM 版本稳定" in caps["note"]


def test_probe_reports_real_inventory(dsm):
    r = dsm.probe()
    assert r["ok"] is True and r["dsm_version"] == "DSM 7.2.1-69057"
    assert r["share_count"] == 2 and r["shares"] == ["backup", "homes"]


def test_list_shares_paginates(dsm):
    dsm.login()
    shares = dsm.list_shares()
    assert [s["name"] for s in shares] == ["backup", "homes"]
    assert shares[0]["additional"]["real_path"] == "/volume1/backup"


def test_walk_files_gives_incremental_fingerprint(dsm):
    dsm.login()
    items = dsm.walk_files("/backup")
    files = {i["path"]: i for i in items if not i["isdir"]}
    assert files["/backup/a.txt"]["size"] == 11
    assert files["/backup/a.txt"]["mtime"] == 1700000000
    assert files["/backup/sub/中文.txt"]["size"] == 9


def test_download_bytes_and_sha(dsm, tmp_path):
    dsm.login()
    dest = str(tmp_path / "a.txt")
    assert dsm.download("/backup/a.txt", dest) == len(DATA["/backup/a.txt"])
    assert open(dest, "rb").read() == DATA["/backup/a.txt"]
    assert DSMClient.sha256_file(dest) == hashlib.sha256(DATA["/backup/a.txt"]).hexdigest()


def test_download_resume_uses_http_range(dsm, tmp_path):
    dsm.login()
    dest = str(tmp_path / "a.txt")
    full = DATA["/backup/a.txt"]
    part = dest + ".aidbm_part"
    with open(part, "wb") as f:
        f.write(full[:4])
    got = dsm.download("/backup/a.txt", dest, resume=True,
                       expected_size=len(full))
    assert got == len(full)
    assert STATE["last_range"] == "bytes=4-"
    assert open(dest, "rb").read() == full
    assert not os.path.exists(part)


def test_session_expiry_triggers_one_relogin(dsm):
    dsm.login()
    old = dsm._sid
    STATE["expire_once"] = True
    shares = dsm.list_shares()
    assert len(shares) == 2
    assert dsm._sid and dsm._sid != old


def test_permission_error_is_explicit(dsm):
    dsm.login()
    with pytest.raises(DSMError) as e:
        dsm.call("SYNO.Core.Share", "list", version=1)
    assert e.value.code == 105 and "失败" in str(e.value)


def test_missing_unofficial_api_is_not_faked(dsm):
    STATE["hide_dsm_info"] = True
    dsm._api_info = {}
    dsm.login()
    assert dsm.system_info() == {}
    assert dsm.capabilities()["system_info"] is False
