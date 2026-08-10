# -*- coding: utf-8 -*-
"""线上验收：AI 问答「围栏泄漏 / 输出截断」缺陷验证。

复现场景：连续多次问「请列出所有备份任务」，统计返回 content 中有几次
以 ``` 开头（即把原始 ```json 文本甩给了用户）。

验收标准：5 次全部为 0 次围栏泄漏。

用法（服务需已在 8080 运行）：
    DEMO_MODE=on <python> live_agent_fence_check.py
"""
import json
import time
import http.cookiejar
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:8080"
ROUNDS = 5
QUESTION = "请列出所有备份任务"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post_json(path: str, payload: dict, timeout: int = 180):
    """发送 JSON POST 请求，返回 (status, body_text)。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
    return r.status, body


def post_form(path: str, payload: dict, timeout: int = 30):
    """发送表单 POST 请求（登录用），返回 (status, body_text)。"""
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    with opener.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
    return r.status, body


def new_session(title: str) -> str:
    """创建会话并返回 session_id（注意 id 嵌在 session.id 里）。"""
    _st, body = post_json("/api/agent/sessions", {"title": title})
    obj = json.loads(body)
    return (obj.get("session") or {}).get("id") or obj.get("session_id") or obj.get("id")


def main() -> int:
    print("=" * 72)
    st, _ = post_form("/login", {"username": "admin", "password": "admin123"})
    print(f"[login] -> {st}")

    fence_leaks = 0
    crashes = 0
    model_errors = 0
    rows = []

    for i in range(ROUNDS):
        sid = new_session(f"围栏泄漏验收-{i}")
        started = time.time()
        st, body = post_json("/api/agent/chat",
                             {"session_id": sid, "message": QUESTION})
        elapsed = round(time.time() - started, 1)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            obj = {}
        content = obj.get("content", "") or ""

        starts_fence = content.lstrip().startswith("```")
        has_json_noise = ('"type"' in content and '"content"' in content)
        crashed = ("'list' object has no attribute 'get'" in body
                   or "处理消息时出错" in body)
        # 上游模型超时/不可用（非围栏泄漏，但需单独统计）
        model_error = "AI 模型未返回有效内容" in content

        if starts_fence:
            fence_leaks += 1
        if crashed:
            crashes += 1
        if model_error:
            model_errors += 1

        rows.append({
            "run": i,
            "http": st,
            "sec": elapsed,
            "len": len(content),
            "fence": "是" if starts_fence else "否",
            "noise": "是" if has_json_noise else "否",
            "truncated_notice": "是" if "内容较长已截断" in content else "否",
            "model_error": "是" if model_error else "否",
        })
        print(f"[run{i}] http={st} {elapsed}s len={len(content)} "
              f"开头是```: {'是' if starts_fence else '否'} "
              f"JSON噪声: {'是' if has_json_noise else '否'} "
              f"截断提示: {'是' if '内容较长已截断' in content else '否'} "
              f"上游错误: {'是' if model_error else '否'}", flush=True)
        print(f"        head: {content[:80]!r}", flush=True)
        print(f"        tail: {content[-80:]!r}", flush=True)

    print("-" * 72)
    print(f"{'轮次':<6}{'耗时s':<8}{'长度':<8}{'开头```':<10}{'JSON噪声':<10}"
          f"{'截断提示':<10}{'上游错误':<10}")
    for r in rows:
        print(f"{r['run']:<8}{r['sec']:<10}{r['len']:<10}{r['fence']:<12}"
              f"{r['noise']:<12}{r['truncated_notice']:<12}{r['model_error']:<10}")
    print("-" * 72)
    print(f"总轮次: {ROUNDS}  围栏泄漏次数: {fence_leaks}  "
          f"崩溃次数: {crashes}  上游模型错误次数: {model_errors}")

    ok = (fence_leaks == 0 and crashes == 0)
    print(f"\n>>> LIVE RESULT: {'PASS' if ok else 'FAIL'} "
          f"(验收标准: 5 次全部 0 次围栏泄漏)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
