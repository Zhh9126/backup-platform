# -*- coding: utf-8 -*-
"""线上验证：10 次主问题 + 3 次备份记录问题，统计成功率/四项噪声。"""
import json, http.cookiejar, urllib.request, urllib.parse, time, sys

BASE = "http://127.0.0.1:8080"
PYTHON = sys.executable

def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def login(op):
    data = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
    op.open(urllib.request.Request(BASE + "/login", data=data, method="POST"), timeout=30)

def create_session(op, title):
    data = json.dumps({"title": title}).encode()
    r = op.open(urllib.request.Request(BASE + "/api/agent/sessions",
        data=data, headers={"Content-Type": "application/json"}, method="POST"), timeout=30)
    return json.loads(r.read())["session"]["id"]

def chat(op, sid, message):
    data = json.dumps({"session_id": sid, "message": message}).encode()
    r = op.open(urllib.request.Request(BASE + "/api/agent/chat",
        data=data, headers={"Content-Type": "application/json"}, method="POST"), timeout=120)
    return json.loads(r.read().decode("utf-8"))

def check_noise(content):
    """检查四项噪声指标。返回 (leaked_fence, leaked_type, has_error_msg, has_list_crash)。"""
    c = content or ""
    leaked_fence = c.lstrip().startswith("```")
    leaked_type = '"type": "answer"' in c or '"type":"answer"' in c
    has_error_msg = "处理消息时出错" in c
    has_list_crash = "'list' object has no attribute 'get'" in c
    return leaked_fence, leaked_type, has_error_msg, has_list_crash

def run_batch(question, n):
    results = []
    for i in range(n):
        op = make_opener()
        login(op)
        sid = create_session(op, f"验证-{question[:6]}-{i}")
        t0 = time.time()
        try:
            d = chat(op, sid, question)
            elapsed = time.time() - t0
            content = d.get("content", "")
            msg_type = d.get("type", "")
            lf, lt, he, hc = check_noise(content)
            ok = not (lf or lt or he or hc) and msg_type != "error"
            results.append({
                "i": i, "ok": ok, "type": msg_type,
                "len": len(content), "elapsed": round(elapsed, 1),
                "fence": lf, "type_leak": lt, "error_msg": he, "list_crash": hc,
                "preview": content[:80].replace("\n", " "),
            })
            status = "OK" if ok else "FAIL"
            print(f"  [{question[:12]}] #{i} {status} {elapsed:.1f}s len={len(content)} "
                  f"fence={lf} type_leak={lt} err={he} crash={hc}")
            if not ok:
                print(f"    preview: {content[:120]}")
        except Exception as e:
            elapsed = time.time() - t0
            results.append({
                "i": i, "ok": False, "type": "exception",
                "len": 0, "elapsed": round(elapsed, 1),
                "fence": False, "type_leak": False,
                "error_msg": True, "list_crash": False,
                "preview": str(e)[:120],
            })
            print(f"  [{question[:12]}] #{i} EXC {elapsed:.1f}s {e}")
    return results

print("=" * 70)
print("线上验证 — 10 次「请列出所有备份任务」")
print("=" * 70)
main_results = run_batch("请列出所有备份任务", 10)

print()
print("=" * 70)
print("线上验证 — 3 次「最近的备份记录有哪些」")
print("=" * 70)
records_results = run_batch("最近的备份记录有哪些", 3)

# 汇总
print()
print("=" * 70)
print("汇总")
print("=" * 70)
for label, results in [("主问题", main_results), ("备份记录", records_results)]:
    total = len(results)
    ok = sum(1 for r in results if r["ok"])
    fence = sum(1 for r in results if r["fence"])
    type_leak = sum(1 for r in results if r["type_leak"])
    err = sum(1 for r in results if r["error_msg"])
    crash = sum(1 for r in results if r["list_crash"])
    avg_t = sum(r["elapsed"] for r in results) / total if total else 0
    print(f"{label}: {ok}/{total} 成功, 围栏泄漏={fence}, type泄漏={type_leak}, "
          f"错误消息={err}, list崩溃={crash}, 平均耗时={avg_t:.1f}s")

all_results = main_results + records_results
total_ok = sum(1 for r in all_results if r["ok"])
total_n = len(all_results)
all_clean = total_ok == total_n
print(f"\n总计: {total_ok}/{total_n} 成功, {'ALL CLEAN ✅' if all_clean else 'HAS FAILURES ❌'}")
