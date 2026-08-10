# -*- coding: utf-8 -*-
"""线上真实链路验证：登录 -> 建会话 -> 智能问答（请列出所有备份任务）
用于确认 executor 裸 list 包装修复后不再出现 'list' object has no attribute 'get'
"""
import json
import http.cookiejar
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:8080"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=120) as r:
        body = r.read().decode("utf-8", "ignore")
    return r.status, body


def post_form(path, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    with opener.open(req, timeout=30) as r:
        body = r.read().decode("utf-8", "ignore")
    return r.status, body


def main():
    print("=" * 60)
    st, _ = post_form("/login", {"username": "admin", "password": "admin123"})
    print("[1] login ->", st)

    st, body = post_json("/api/agent/sessions", {"title": "线上验证"})
    print("[2] create session ->", st, body[:200])
    _j = json.loads(body)
    sid = (_j.get("session") or {}).get("id") or _j.get("session_id") or _j.get("id")
    print("    session_id =", sid)

    st, body = post_json("/api/agent/chat",
                         {"session_id": sid, "message": "请列出所有备份任务"})
    print("[3] chat ->", st)
    print("    raw:", body[:800])

    ok = True
    if "'list' object has no attribute 'get'" in body:
        print("\n>>> LIVE RESULT: FAIL  (裸 list 崩溃仍存在)")
        ok = False
    elif "处理消息时出错" in body:
        print("\n>>> LIVE RESULT: FAIL  (仍有处理异常)")
        ok = False
    else:
        print("\n>>> LIVE RESULT: PASS  (无 list.get 崩溃)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
