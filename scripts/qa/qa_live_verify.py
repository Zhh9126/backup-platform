# -*- coding: utf-8 -*-
"""QA 独立线上验证：/api/agent/chat 真实链路噪声泄漏检查。

四项验收指标（全部必须为 0）：
  1. content 以 ``` 开头的次数
  2. content 含 '"type": "answer"' 的次数
  3. 出现「处理消息时出错」的次数
  4. 出现 'list' object has no attribute 'get' 的次数
"""
import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8080"
ROUNDS = 8
MAIN_Q = "请列出所有备份任务"
EXTRA_Q = ["最近的备份记录有哪些", "系统存储情况怎么样"]

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def login():
    data = urllib.parse.urlencode(
        {"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=data, method="POST")
    with opener.open(req, timeout=30) as r:
        r.read()
    return any(c.name == "session" for c in cj)


def new_session(title):
    body = json.dumps({"title": title}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/agent/sessions", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=30) as r:
        obj = json.loads(r.read().decode("utf-8"))
    # session_id 在 session.id 嵌套里
    return (obj.get("session") or {}).get("id")


def chat(session_id, message, timeout=120):
    body = json.dumps({"session_id": session_id,
                       "message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/agent/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with opener.open(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    return obj, round(time.time() - t0, 1)


def inspect(obj):
    """返回四项检查结果 + content。"""
    content = obj.get("content", "") or ""
    blob = json.dumps(obj, ensure_ascii=False)
    return {
        "fence_start": content.lstrip().startswith("```"),
        "type_answer": ('"type": "answer"' in content
                        or '"type":"answer"' in content),
        "proc_error": "处理消息时出错" in blob,
        "list_attr": "'list' object has no attribute 'get'" in blob,
        "content": content,
        "type": obj.get("type"),
        "ok": obj.get("ok"),
    }


def run_batch(label, question, rounds):
    rows = []
    for i in range(1, rounds + 1):
        sid = new_session(f"qa-{label}-{i}")
        if not sid:
            print(f"  [{i}] 建会话失败")
            continue
        try:
            obj, dur = chat(sid, question)
        except Exception as e:  # noqa: BLE001
            rows.append({"i": i, "dur": None, "err": f"{type(e).__name__}: {e}",
                         "len": 0, "fence_start": False, "type_answer": False,
                         "proc_error": False, "list_attr": False,
                         "content": "", "type": "EXCEPTION"})
            print(f"  [{i}] 异常 {type(e).__name__}: {e}")
            continue
        r = inspect(obj)
        r.update({"i": i, "dur": dur, "len": len(r["content"]), "err": None})
        rows.append(r)
        flags = []
        if r["fence_start"]:
            flags.append("围栏开头")
        if r["type_answer"]:
            flags.append("type:answer")
        if r["proc_error"]:
            flags.append("处理消息时出错")
        if r["list_attr"]:
            flags.append("list.get崩溃")
        status = ("**" + ",".join(flags) + "**") if flags else "干净"
        print(f"  [{i}] {dur}s len={r['len']:<5} type={str(r['type']):<8} {status}")
        print(f"       预览: {r['content'][:75]!r}")
    return rows


def main():
    if not login():
        print("登录失败")
        return 2
    print("登录成功\n")

    all_rows = []
    print(f"=== 主问题 x{ROUNDS}: {MAIN_Q} ===")
    main_rows = run_batch("main", MAIN_Q, ROUNDS)
    all_rows += main_rows

    extra_rows = {}
    for q in EXTRA_Q:
        print(f"\n=== 附加问题: {q} ===")
        rows = run_batch("extra", q, 2)
        extra_rows[q] = rows
        all_rows += rows

    print("\n" + "=" * 70)
    print("汇总统计")
    print("=" * 70)
    n = len(all_rows)
    fence = sum(1 for r in all_rows if r["fence_start"])
    ta = sum(1 for r in all_rows if r["type_answer"])
    pe = sum(1 for r in all_rows if r["proc_error"])
    la = sum(1 for r in all_rows if r["list_attr"])
    errs = sum(1 for r in all_rows if r.get("err"))
    durs = [r["dur"] for r in all_rows if r["dur"]]
    print(f"总请求数: {n}")
    print(f"1) content 以 ``` 开头        : {fence}")
    print(f"2) content 含 \"type\": \"answer\": {ta}")
    print(f"3) 出现「处理消息时出错」      : {pe}")
    print(f"4) 'list' object has no attr  : {la}")
    print(f"传输层异常(超时等)            : {errs}")
    if durs:
        print(f"耗时: min={min(durs)}s max={max(durs)}s "
              f"avg={round(sum(durs)/len(durs),1)}s")
    lens = [r["len"] for r in all_rows if r["len"]]
    if lens:
        print(f"长度: min={min(lens)} max={max(lens)} "
              f"avg={round(sum(lens)/len(lens))}")

    bad = fence + ta + pe + la
    print("\n验收: " + ("通过（四项全 0）" if bad == 0 else f"**不通过，命中 {bad} 次**"))
    if errs:
        print(f"注意: {errs} 次传输层异常需单独评估")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
