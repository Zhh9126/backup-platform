# -*- coding: utf-8 -*-
"""备份管理平台并发压力测试。

用法:
    python scripts/stress_test.py                 # 默认本机 8080
    python scripts/stress_test.py --base http://127.0.0.1:8080 --users 20 --requests 200
"""
import argparse
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

APIS = [
    ("GET", "/api/meta"),
    ("GET", "/api/tasks"),
    ("GET", "/api/records"),
    ("GET", "/api/deploy"),
    ("GET", "/api/plugins"),
    ("GET", "/api/logs"),
    ("GET", "/api/settings/backup-quality-thresholds"),
    ("GET", "/api/hosts"),
]


def login(base, user, password):
    s = requests.Session()
    r = s.post(f"{base}/login", json={"username": user, "password": password}, timeout=10)
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"登录失败: {r.status_code} {r.text[:200]}")
    return s


def worker(session, base, idx, results, errors):
    api = APIS[idx % len(APIS)]
    method, path = api
    t0 = time.perf_counter()
    try:
        r = session.request(method, f"{base}{path}", timeout=30)
        dt = time.perf_counter() - t0
        results.append((method, path, r.status_code, dt))
    except Exception as e:
        dt = time.perf_counter() - t0
        errors.append((method, path, str(e)))
        results.append((method, path, 0, dt))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="admin123")
    p.add_argument("--users", type=int, default=20, help="并发线程数")
    p.add_argument("--requests", type=int, default=200, help="总请求数")
    p.add_argument("--ramp", type=float, default=0.0, help="每请求启动间隔(秒)")
    args = p.parse_args()

    print(f"[*] 登录 {args.base} ...")
    session = login(args.base, args.user, args.password)
    print("[+] 登录成功")

    # 先探测当前进程是否已加载最新 deploy 代码 (接受 direct_host)
    probe = session.post(
        f"{args.base}/api/deploy",
        json={"name": "__probe_direct_stress", "db_type": "mysql",
              "direct_host": "10.0.0.1", "direct_user": "root"},
        timeout=10,
    )
    loaded = probe.status_code in (201, 400)
    print(f"[*] 最新deploy代码探测: HTTP {probe.status_code} -> "
          f"{'已加载' if loaded else '未加载(旧进程)'}")
    # 清理 probe 创建的数据
    try:
        if probe.status_code == 201:
            dep_id = probe.json().get("id")
            if dep_id:
                session.delete(f"{args.base}/api/deploy/{dep_id}", timeout=10)
    except Exception:
        pass

    print(f"[*] 开始压测: {args.users} 并发, 共 {args.requests} 请求")
    results = []
    errors = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.users) as ex:
        futures = []
        for i in range(args.requests):
            if args.ramp:
                time.sleep(args.ramp)
            f = ex.submit(worker, session, args.base, i, results, errors)
            futures.append(f)
        for f in futures:
            f.result()

    t_end = time.perf_counter()
    elapsed = t_end - t_start

    # 统计
    total = len(results)
    ok = sum(1 for m, pth, code, dt in results if 200 <= code < 400)
    fail = total - ok
    lat = [dt for _, _, code, dt in results]
    lat.sort()
    p50 = lat[int(len(lat) * 0.50)] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    p99 = lat[int(len(lat) * 0.99)] if lat else 0
    mx = max(lat) if lat else 0
    avg = sum(lat) / len(lat) if lat else 0

    by_code = {}
    for m, pth, code, dt in results:
        by_code[code] = by_code.get(code, 0) + 1

    print("=" * 56)
    print(f"  总请求数 : {total}")
    print(f"  成功     : {ok}")
    print(f"  失败     : {fail}")
    print(f"  耗时     : {elapsed:.2f}s")
    print(f"  吞吐(QPS): {total / elapsed:.1f}")
    print(f"  平均延迟 : {avg*1000:.1f} ms")
    print(f"  P50      : {p50*1000:.1f} ms")
    print(f"  P95      : {p95*1000:.1f} ms")
    print(f"  P99      : {p99*1000:.1f} ms")
    print(f"  最大延迟 : {mx*1000:.1f} ms")
    print(f"  状态码分布: {by_code}")
    if errors:
        from collections import Counter
        c = Counter(e[2] for e in errors)
        print(f"  错误类型: {dict(c)}")
    print("=" * 56)


if __name__ == "__main__":
    main()
