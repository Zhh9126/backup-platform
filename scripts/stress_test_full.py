# -*- coding: utf-8 -*-
"""AIDBM 全面压力测试（批量任务 + 全接口 + 数据返回校验）。

分五个阶段：
  A 批量建任务：并发创建 N 个备份任务（默认 1000+），验证写入吞吐与无 5xx
  B 批量触发备份：并发对 N 个任务发起"立即备份"，验证并发执行与记录完整性
  C 全接口压测：自动发现 app.url_map 中所有 GET 接口（含动态参数用真实 id 填充），
                并发请求，统计 QPS / P50 / P95 / P99 / 状态码分布 / 5xx 明细
  D 数据返回校验：CRUD 往返一致、字段完整性、关联一致性、分页筛选、模板接口
  E 混合读写：读接口持续压测的同时插入写操作，验证读写并发下无锁错误

用法：
    python scripts/stress_test_full.py                          # 全量（1000 任务）
    python scripts/stress_test_full.py --tasks 100 --api-req 5  # 快速冒烟
    python scripts/stress_test_full.py --skip-backup            # 只压接口
"""
import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings()
# 压测客户端连接池必须大于并发数，否则 "Connection pool is full" 会把客户端自身
# 的连接瓶颈误记成服务端错误
POOL_SIZE = 512

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8080"
USER = "admin"
PWD = "admin123"


# ----------------------------- 工具 -----------------------------
class Stat:
    """压测结果累加器（线程安全）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lat = []            # 延迟样本
        self.codes = Counter()   # 状态码分布
        self.errors = []         # (url, 错误信息)
        self.n = 0

    def add(self, code, dt, url="", err=""):
        with self.lock:
            self.n += 1
            self.lat.append(dt)
            self.codes[code] += 1
            if err:
                self.errors.append((url, err[:300]))
            elif code >= 500:
                self.errors.append((url, f"HTTP {code}"))

    def merge(self, other: "Stat"):
        with self.lock:
            self.lat += other.lat
            self.codes.update(other.codes)
            self.errors += other.errors
            self.n += other.n

    def summary(self, elapsed):
        lat = sorted(self.lat) or [0]
        ok = sum(v for k, v in self.codes.items() if 200 <= k < 400)
        return {
            "requests": self.n,
            "ok": ok,
            "failed": self.n - ok,
            "qps": round(self.n / elapsed, 1) if elapsed > 0 else 0,
            "avg_ms": round(statistics.mean(self.lat) * 1000, 1),
            "p50_ms": round(lat[int(len(lat) * 0.50)] * 1000, 1),
            "p95_ms": round(lat[int(len(lat) * 0.95)] * 1000, 1),
            "p99_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.99))] * 1000, 1),
            "max_ms": round(lat[-1] * 1000, 1),
            "codes": dict(self.codes),
            "errors": self.errors[:20],
        }


def new_session(base=None):
    base = base or BASE
    s = requests.Session()
    s.trust_env = False
    adapter = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE,
                          max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    r = s.post(f"{base}/login", json={"username": USER, "password": PWD}, timeout=15)
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"登录失败: {r.status_code} {r.text[:200]}")
    return s


def discover_get_apis(session):
    """从 Flask url_map 自动发现所有 GET 接口，并用真实 id 填充动态参数。"""
    from app import create_app
    app = create_app()
    rules = []
    for r in app.url_map.iter_rules():
        if not r.rule.startswith("/api") or "GET" not in r.methods:
            continue
        rules.append(r.rule)
    # 采样真实 id 用于填充 <int:xxx> 参数
    samples = {}
    try:
        tasks = session.get(f"{BASE}/api/tasks", timeout=15).json()
        if isinstance(tasks, list) and tasks:
            samples["task_id"] = tasks[0]["id"]
            samples["id"] = tasks[0]["id"]
    except Exception:
        pass
    try:
        recs = session.get(f"{BASE}/api/records?limit=1", timeout=15).json()
        if isinstance(recs, list) and recs:
            samples["record_id"] = recs[0]["id"]
    except Exception:
        pass
    for key, api in (("host_id", "/api/hosts"), ("policy_id", "/api/policy"),
                     ("plan_id", "/api/db-migrate"), ("link_id", "/api/disaster-links")):
        try:
            data = session.get(f"{BASE}{api}", timeout=15).json()
            rows = data if isinstance(data, list) else (data.get("data") or [])
            if rows and isinstance(rows[0], dict) and rows[0].get("id"):
                samples[key] = rows[0]["id"]
        except Exception:
            pass

    out, skipped = [], []
    for rule in sorted(set(rules)):
        path = rule
        missing = False
        while "<" in path:
            i, j = path.index("<"), path.index(">")
            token = path[i + 1:j]
            name = token.split(":")[-1]
            if name in samples:
                path = path[:i] + str(samples[name]) + path[j + 1:]
            else:
                missing = True
                break
        if missing:
            skipped.append(rule)
        else:
            out.append(path)
    return out, skipped


def hit(session, method, path, stat: Stat, timeout=60, **kw):
    url = f"{BASE}{path}"
    t0 = time.perf_counter()
    try:
        r = session.request(method, url, timeout=timeout, **kw)
        dt = time.perf_counter() - t0
        stat.add(r.status_code, dt, path)
        return r
    except Exception as e:
        dt = time.perf_counter() - t0
        stat.add(0, dt, path, f"{type(e).__name__}: {e}")
        return None


class ResourceSampler:
    """压测期间采样平台进程资源占用（RSS / CPU / 线程数）。"""

    def __init__(self, interval=3.0):
        self.interval = interval
        self.stop_flag = False
        self.samples = []
        self._t = None
        self._pid = self._find_platform_pid()

    @staticmethod
    def _find_platform_pid():
        explicit = os.environ.get("AIDBM_STRESS_PID")
        if explicit:
            return int(explicit)
        import subprocess
        try:
            out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines():
                if "run.py" in line and "grep" not in line:
                    return int(line.split()[0])
        except Exception:
            pass
        return None

    def _sample(self):
        while not self.stop_flag:
            if self._pid:
                try:
                    with open(f"/proc/{self._pid}/status") as f:
                        txt = f.read()
                    rss = int([l for l in txt.splitlines()
                               if l.startswith("VmRSS")][0].split()[1])
                    thr = int([l for l in txt.splitlines()
                               if l.startswith("Threads")][0].split()[1])
                    with open(f"/proc/{self._pid}/stat") as f:
                        parts = f.read().split()
                    cpu = (int(parts[13]) + int(parts[14])) / 100.0
                    self.samples.append((rss / 1024.0, cpu, thr))
                except Exception:
                    pass
            time.sleep(self.interval)

    def start(self):
        if not self._pid:
            return self
        self._t = threading.Thread(target=self._sample, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self.stop_flag = True
        if self._t:
            self._t.join(timeout=self.interval + 1)

    def summary(self):
        if not self.samples:
            return {}
        return {
            "rss_mb_max": round(max(s[0] for s in self.samples), 1),
            "rss_mb_last": round(self.samples[-1][0], 1),
            "cpu_sec_max": round(max(s[1] for s in self.samples), 1),
            "threads_max": max(s[2] for s in self.samples),
            "samples": len(self.samples),
        }


def run_pool(fn, items, concurrency):
    """并发执行 fn(item)，返回 (结果列表, 耗时)。"""
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(fn, it) for it in items]
        for f in as_completed(futs):
            f.result()
    return time.perf_counter() - t0


def pct(n, total):
    return f"{(n / total * 100):.1f}%" if total else "0%"


def print_errors(stat: Stat, limit: int = 6):
    """打印失败样本明细（5xx / 网络异常），便于定位服务端问题。"""
    if not stat.errors:
        return
    for url, err in stat.errors[:limit]:
        print(f"      ! {url} -> {err}")
    if len(stat.errors) > limit:
        print(f"      ! ...另有 {len(stat.errors) - limit} 条")


# ----------------------------- 阶段 A：批量建任务 -----------------------------
def phase_create_tasks(session, n, concurrency):
    print(f"\n[A] 批量创建 {n} 个备份任务（并发 {concurrency}）…")
    stat = Stat()
    created = []
    lock = threading.Lock()

    def one(i):
        payload = {
            "name": f"ST-{int(time.time())}-{i}",
            "biz_system": "压测",
            "db_type": "mysql",
            "host": "127.0.0.1",
            "port": 3306,
            "username": "root",
            "password": "Ceshi@133",
            "db_name": "custom_src_stress",
            "backup_type": "full",
            # 自定义脚本：极轻量（仅 echo 一个小文件），可真实跑通备份全链路
            "backup_mode": "custom",
            "schedule_type": "none",
            "enabled": 0,
            "retention_days": 1,
            "extra_options": json.dumps({
                "custom_script": (
                    "#!/bin/bash\nset -e\n"
                    "F=\"$PLATFORM_BACKUP_DIR/st_${PLATFORM_TASK_ID}_$(date +%s%N).sql\"\n"
                    "echo \"-- stress test backup\\nSELECT 1;\" > \"$F\"\n"
                    "echo \"ok $F\"\n"),
                "custom_scope": "full_database",
                "custom_timeout": 60,
            }, ensure_ascii=False),
        }
        r = hit(session, "POST", "/api/tasks", stat, json=payload)
        if r is not None and r.status_code in (200, 201):
            try:
                tid = r.json().get("id")
                if tid:
                    with lock:
                        created.append(tid)
            except Exception:
                pass

    elapsed = run_pool(one, range(n), concurrency)
    s = stat.summary(elapsed)
    print(f"    请求 {s['requests']}  成功 {s['ok']} ({pct(s['ok'], s['requests'])})  "
          f"QPS {s['qps']}  P95 {s['p95_ms']}ms  P99 {s['p99_ms']}ms  耗时 {elapsed:.1f}s")
    print(f"    状态码: {s['codes']}")
    print_errors(stat)
    return created, s, elapsed


# ----------------------------- 阶段 B：批量触发备份 -----------------------------
def phase_run_backups(session, task_ids, concurrency):
    print(f"\n[B] 批量触发 {len(task_ids)} 个备份任务（并发 {concurrency}）…")
    stat = Stat()
    counter = {"success": 0, "failed": 0, "other": 0, "none": 0}
    lock = threading.Lock()

    def one(tid):
        r = hit(session, "POST", f"/api/tasks/{tid}/run", stat, timeout=300,
                json={"backup_type": "full"})
        if r is None:
            with lock:
                counter["none"] += 1
            return
        try:
            d = r.json()
        except Exception:
            with lock:
                counter["other"] += 1
            return
        if d.get("accepted"):
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = session.get(f"{BASE}/api/records", params={"task_id": tid, "limit": 1},
                                       timeout=15)
                response.raise_for_status()
                rows = response.json()
                if isinstance(rows, list) and rows and rows[0].get("status") != "running":
                    d = rows[0]
                    break
                time.sleep(0.2)
        st = (d.get("status") or "").lower()
        with lock:
            if st == "success":
                counter["success"] += 1
            elif st in ("failed", "error"):
                counter["failed"] += 1
            else:
                counter["other"] += 1

    t0 = time.perf_counter()
    run_pool(one, task_ids, concurrency)
    elapsed = time.perf_counter() - t0
    s = stat.summary(elapsed)
    print(f"    请求 {s['requests']}  HTTP成功 {s['ok']} ({pct(s['ok'], s['requests'])})  "
          f"QPS {s['qps']}  P95 {s['p95_ms']}ms  P99 {s['p99_ms']}ms  耗时 {elapsed:.1f}s")
    print(f"    备份结果: success={counter['success']} failed={counter['failed']} "
          f"其他={counter['other']} 无响应={counter['none']}")
    print(f"    状态码: {s['codes']}")
    print_errors(stat)
    return s, elapsed, counter


# ----------------------------- 阶段 B2：落库对账 -----------------------------
def phase_verify_records(task_ids, expected_success):
    """备份记录落库对账：每条触发都必须留下记录，且成功记录的产物真实存在。"""
    print(f"\n[B2] 备份记录落库对账（{len(task_ids)} 个任务）…")
    import core.db as db
    db.init_schema()
    ids = [int(t) for t in task_ids]
    rows = {"total": 0, "by_status": {}, "empty_size": 0, "missing_file": 0,
            "no_checksum": 0}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in db.query(
                f"SELECT status, COUNT(*) c FROM backup_records "
                f"WHERE task_id IN ({ph}) GROUP BY status", tuple(chunk)):
            rows["by_status"][r["status"]] = rows["by_status"].get(r["status"], 0) + r["c"]
        for r in db.query(
                f"SELECT backup_path, size_bytes, checksum FROM backup_records "
                f"WHERE task_id IN ({ph}) AND status='success'", tuple(chunk)):
            if not (r["size_bytes"] or 0):
                rows["empty_size"] += 1
            if not (r["checksum"] or ""):
                rows["no_checksum"] += 1
            if r["backup_path"] and not os.path.exists(r["backup_path"]):
                rows["missing_file"] += 1
    rows["total"] = sum(rows["by_status"].values())
    success = rows["by_status"].get("success", 0)
    print(f"    记录总数 {rows['total']}（触发 {len(ids)} 次）  状态分布 {rows['by_status']}")
    print(f"    success 记录中：size=0 的 {rows['empty_size']} 条，"
          f"缺 checksum 的 {rows['no_checksum']} 条，产物文件缺失 {rows['missing_file']} 条")
    ok = (rows["total"] >= len(ids) and success >= expected_success
          and rows["empty_size"] == 0 and rows["missing_file"] == 0
          and rows["no_checksum"] == 0)
    print(f"    结论: {'PASS' if ok else 'FAIL'}"
          f"（期望 success ≥ {expected_success}）")
    return ok, rows


# ----------------------------- 阶段 C：全接口压测 -----------------------------
def phase_api(session, apis, concurrency, per_api):
    print(f"\n[C] 全接口压测：{len(apis)} 个 GET 接口 × {per_api} 次 "
          f"（并发 {concurrency}）…")
    stat = Stat()
    per_stat = defaultdict(Stat)
    lock = threading.Lock()

    jobs = []
    for path in apis:
        jobs.extend([path] * per_api)
    random.shuffle(jobs)

    def one(path):
        local = Stat()
        hit(session, "GET", path, local, timeout=60)
        with lock:
            stat.merge(local)
            per_stat[path].merge(local)

    t0 = time.perf_counter()
    run_pool(one, jobs, concurrency)
    elapsed = time.perf_counter() - t0
    s = stat.summary(elapsed)
    print(f"    请求 {s['requests']}  成功 {s['ok']} ({pct(s['ok'], s['requests'])})  "
          f"QPS {s['qps']}  P50 {s['p50_ms']}ms  P95 {s['p95_ms']}ms  "
          f"P99 {s['p99_ms']}ms  最大 {s['max_ms']}ms")
    print(f"    状态码: {s['codes']}")
    print_errors(stat)
    slow = sorted(per_stat.items(), key=lambda kv: -kv[1].lat[0] if kv[1].lat else 0)[:8]
    if slow:
        print("    最慢接口 TOP8:")
        for p, st in slow:
            if st.lat:
                print(f"      {st.lat[0]*1000:8.1f}ms  {p}")
    return s, elapsed, {p: st.summary(1) for p, st in per_stat.items()}


# ----------------------------- 阶段 D：数据返回校验 -----------------------------
def phase_data(session):
    print("\n[D] 数据返回与一致性校验…")
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print(f"    [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    # D1 任务列表结构
    r = session.get(f"{BASE}/api/tasks", timeout=30)
    tasks = r.json() if r.status_code == 200 else []
    need = {"id", "name", "db_type", "backup_mode", "host", "port", "enabled"}
    check("GET /api/tasks 返回数组且字段完整",
          r.status_code == 200 and isinstance(tasks, list) and
          (not tasks or need.issubset(set(tasks[0].keys()))),
          f"共 {len(tasks)} 条")

    # D2 创建 → 读取 → 更新 → 删除 往返一致
    payload = {
        "name": f"ST-ROUNDTRIP-{int(time.time())}", "biz_system": "压测",
        "db_type": "mysql", "host": "127.0.0.1", "port": 3306,
        "username": "root", "password": "Ceshi@133", "db_name": "rt_db",
        "backup_type": "full", "backup_mode": "logical",
        "schedule_type": "none", "enabled": 0, "retention_days": 7,
    }
    r = session.post(f"{BASE}/api/tasks", json=payload, timeout=30)
    created_ok = r.status_code in (200, 201)
    tid = r.json().get("id") if created_ok else None
    check("POST /api/tasks 创建成功", created_ok and tid, f"id={tid}")
    if tid:
        r2 = session.get(f"{BASE}/api/tasks/{tid}", timeout=30)
        d = r2.json() if r2.status_code == 200 else {}
        same = (d.get("name") == payload["name"] and d.get("db_type") == "mysql"
                and str(d.get("port")) == "3306" and d.get("db_name") == "rt_db")
        check("GET /api/tasks/{id} 字段与提交一致", same,
              f"name={d.get('name')} db={d.get('db_name')}")
        r3 = session.put(f"{BASE}/api/tasks/{tid}", json={"retention_days": 15}, timeout=30)
        d3 = session.get(f"{BASE}/api/tasks/{tid}", timeout=30).json()
        check("PUT /api/tasks/{id} 更新生效",
              r3.status_code in (200, 201) and str(d3.get("retention_days")) == "15",
              f"retention_days={d3.get('retention_days')}")
        r4 = session.delete(f"{BASE}/api/tasks/{tid}", timeout=30)
        r5 = session.get(f"{BASE}/api/tasks/{tid}", timeout=30)
        check("DELETE 后查询返回 404",
              r4.status_code in (200, 204) and r5.status_code == 404,
              f"delete={r4.status_code} get={r5.status_code}")

    # D3 备份记录与任务关联 + 字段完整
    r = session.get(f"{BASE}/api/records?limit=1", timeout=30)
    recs = r.json() if r.status_code == 200 else []
    if recs:
        rec = recs[0]
        check("GET /api/records 含关键字段",
              {"id", "task_id", "status", "size_bytes", "started_at"}.issubset(set(rec.keys())),
              f"样例 id={rec.get('id')} task={rec.get('task_id')}")
        rid = rec["id"]
        r2 = session.get(f"{BASE}/api/records/{rid}", timeout=30)
        ok2 = r2.status_code == 200 and r2.json().get("id") == rid
        check("GET /api/records/{id} 单条可读", ok2)
        r3 = session.get(f"{BASE}/api/records?task_id={rec['task_id']}", timeout=30)
        rows = r3.json() if r3.status_code == 200 else []
        check("GET /api/records?task_id 过滤生效",
              bool(rows) and all(x.get("task_id") == rec["task_id"] for x in rows),
              f"{len(rows)} 条")
        r4 = session.get(f"{BASE}/api/records?limit=5", timeout=30)
        rows4 = r4.json() if r4.status_code == 200 else []
        check("GET /api/records?limit 分页/截断生效", len(rows4) <= 5, f"{len(rows4)} 条")
    else:
        check("GET /api/records 有数据", False, "无记录")

    # D4 仪表盘汇总字段
    r = session.get(f"{BASE}/api/dashboard", timeout=30)
    d = r.json() if r.status_code == 200 else {}
    check("GET /api/dashboard 返回汇总字段",
          r.status_code == 200 and isinstance(d, dict) and len(d) > 0,
          f"keys={list(d.keys())[:8]}")

    # D5 自定义脚本模板接口（三种范围）
    for scope in ("full_database", "single_table", "full_instance"):
        r = session.get(f"{BASE}/api/custom-script/template",
                        params={"db_type": "mysql", "scope": scope}, timeout=30)
        t = r.json() if r.status_code == 200 else {}
        check(f"模板接口 scope={scope} 返回脚本",
              r.status_code == 200 and len(t.get("backup", "")) > 50
              and len(t.get("restore", "")) > 50,
              f"backup={len(t.get('backup',''))}B")
    r = session.get(f"{BASE}/api/custom-script/template",
                    params={"db_type": "postgresql", "scope": "single_table"}, timeout=30)
    check("模板接口 PG 单表含 PLATFORM_TABLES",
          "PLATFORM_TABLES" in (r.json().get("backup", "") if r.status_code == 200 else ""))

    # D6 错误输入应返回 4xx 而非 5xx
    r = session.post(f"{BASE}/api/tasks", json={"name": ""}, timeout=30)
    check("非法任务入参返回 4xx（非 5xx）", 400 <= r.status_code < 500, f"HTTP {r.status_code}")
    r = session.get(f"{BASE}/api/tasks/99999999", timeout=30)
    check("不存在的任务返回 404", r.status_code == 404, f"HTTP {r.status_code}")
    r = session.get(f"{BASE}/api/records/99999999", timeout=30)
    check("不存在的记录返回 404", r.status_code == 404, f"HTTP {r.status_code}")

    # D7 并发一致性：同一任务并发读 50 次结果一致
    if tasks:
        tid0 = tasks[0]["id"]
        outs = []
        lock = threading.Lock()

        def read_one(_):
            rr = session.get(f"{BASE}/api/tasks/{tid0}", timeout=30)
            with lock:
                outs.append((rr.status_code, json.dumps(rr.json(), sort_keys=True,
                                                        ensure_ascii=False)))

        run_pool(read_one, range(50), 25)
        uniq = set(outs)
        check("并发读同一任务结果一致（无脏读/串数据）", len(uniq) == 1,
              f"{len(uniq)} 种不同响应")

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"    合计 {passed}/{len(checks)} 项通过")
    return checks


# ----------------------------- 阶段 E：混合读写 -----------------------------
def phase_mixed(session, apis, concurrency, seconds):
    print(f"\n[E] 混合读写压测：持续 {seconds}s（并发 {concurrency}）…")
    stat = Stat()
    stop = time.time() + seconds
    lock = threading.Lock()
    writes = Counter()

    def one(i):
        while time.time() < stop:
            path = random.choice(apis)
            hit(session, "GET", path, stat, timeout=60)
            if i % 5 == 0:  # 20% 的线程混入写操作
                payload = {
                    "name": f"ST-MIX-{int(time.time()*1000)}-{i}",
                    "biz_system": "压测", "db_type": "mysql", "host": "127.0.0.1",
                    "port": 3306, "username": "root", "password": "Ceshi@133",
                    "db_name": "mix_db", "backup_type": "full", "backup_mode": "logical",
                    "schedule_type": "none", "enabled": 0, "retention_days": 1,
                }
                r = hit(session, "POST", "/api/tasks", stat, json=payload)
                if r is not None and r.status_code in (200, 201):
                    with lock:
                        writes["created"] += 1
                        created_id = r.json().get("id")
                    if created_id:
                        hit(session, "DELETE", f"/api/tasks/{created_id}", stat)
                        with lock:
                            writes["deleted"] += 1

    t0 = time.perf_counter()
    run_pool(one, range(concurrency), concurrency)
    elapsed = time.perf_counter() - t0
    s = stat.summary(elapsed)
    print(f"    请求 {s['requests']}  成功 {s['ok']} ({pct(s['ok'], s['requests'])})  "
          f"QPS {s['qps']}  P95 {s['p95_ms']}ms  P99 {s['p99_ms']}ms")
    print(f"    写操作: 创建 {writes['created']} / 删除 {writes['deleted']}")
    print(f"    状态码: {s['codes']}")
    print_errors(stat)
    return s, elapsed


# ----------------------------- 阶段 F：写接口并发 -----------------------------
def phase_write_apis(session, concurrency, rounds):
    """写接口并发压测：创建-删除配对，只要不出现 5xx / 连接异常即通过。

    合法负载与"最小负载"混合：最小负载返回 4xx 属正常（字段校验），
    但绝不允许 500（服务端异常）。
    """
    print(f"\n[F] 写接口并发压测（{rounds} 轮 × 并发 {concurrency}）…")
    stat = Stat()
    sample_task = None
    try:
        rows = session.get(f"{BASE}/api/tasks", timeout=30).json()
        if rows:
            sample_task = rows[0]["id"]
    except Exception:
        pass
    created_ids = {"task": [], "rv": [], "policy": []}
    lock = threading.Lock()

    def one(i):
        # 1) 任务：创建 → 删除（合法负载）
        r = hit(session, "POST", "/api/tasks", stat, json={
            "name": f"ST-W-{int(time.time()*1000)}-{i}", "biz_system": "压测",
            "db_type": "mysql", "host": "127.0.0.1", "port": 3306,
            "username": "root", "password": "Ceshi@133", "db_name": "w_db",
            "backup_type": "full", "backup_mode": "logical",
            "schedule_type": "none", "enabled": 0, "retention_days": 1})
        if r is not None and r.status_code in (200, 201):
            tid = r.json().get("id")
            if tid:
                hit(session, "DELETE", f"/api/tasks/{tid}", stat)
                with lock:
                    created_ids["task"].append(tid)
        # 2) 恢复校验策略：创建 → 删除
        if sample_task:
            r2 = hit(session, "POST", "/api/restore-verify-policies", stat,
                     json={"task_id": sample_task})
            if r2 is not None and r2.status_code in (200, 201):
                pid = (r2.json().get("data") or {}).get("id") or r2.json().get("id")
                if pid:
                    hit(session, "DELETE", f"/api/restore-verify-policies/{pid}", stat)
                    with lock:
                        created_ids["rv"].append(pid)
        # 3) 非法/最小负载：必须 4xx，不能 5xx
        hit(session, "POST", "/api/tasks", stat, json={"name": ""})
        hit(session, "POST", "/api/records/99999999/xxx", stat, json={})

    t0 = time.perf_counter()
    run_pool(one, range(rounds), concurrency)
    elapsed = time.perf_counter() - t0
    s = stat.summary(elapsed)
    bad = sum(v for c, v in s["codes"].items() if c == 0 or c >= 500)
    print(f"    请求 {s['requests']}  QPS {s['qps']}  P95 {s['p95_ms']}ms  "
          f"5xx/异常 {bad}")
    print(f"    状态码: {s['codes']}")
    print_errors(stat)
    # 兜底清理（接口已删，这里确保无残留）
    for tid in created_ids["task"]:
        try:
            session.delete(f"{BASE}/api/tasks/{tid}", timeout=15)
        except Exception:
            pass
    return s, elapsed, bad


# ----------------------------- 平台侧错误日志检查 -----------------------------
def platform_error_delta(session, since_ts):
    """统计压测期间平台 ERROR 级别日志增量。"""
    try:
        rows = session.get(f"{BASE}/api/logs?limit=200", timeout=30).json()
        rows = rows if isinstance(rows, list) else rows.get("data", [])
        n = 0
        for r in rows:
            if (r.get("level") or "").upper() == "ERROR" and (r.get("ts") or "") >= since_ts:
                n += 1
        return n, [r.get("message", "")[:120] for r in rows
                   if (r.get("level") or "").upper() == "ERROR"][:5]
    except Exception:
        return -1, []


# ----------------------------- 主流程 -----------------------------
def main():
    global BASE, USER, PWD
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--user", default=USER)
    p.add_argument("--password", default=PWD)
    p.add_argument("--tasks", type=int, default=1000, help="批量创建/触发的任务数")
    p.add_argument("--concurrency", type=int, default=100, help="批量阶段并发客户端数")
    p.add_argument("--api-req", type=int, default=3, help="每个接口压测请求数")
    p.add_argument("--api-concurrency", type=int, default=50, help="接口压测并发")
    p.add_argument("--mixed-seconds", type=int, default=20, help="混合读写持续秒数")
    p.add_argument("--skip-backup", action="store_true", help="跳过批量建任务/触发备份")
    p.add_argument("--report", default="", help="报告落盘路径(json)")
    args = p.parse_args()

    BASE = args.base.rstrip("/")
    USER, PWD = args.user, args.password

    print("=" * 70)
    print(f"  AIDBM 全面压力测试  目标: {BASE}")
    print("=" * 70)
    session = new_session()
    print("[+] 登录成功")

    report = {"base": BASE, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    t_all = time.perf_counter()

    apis, skipped = discover_get_apis(session)
    print(f"[*] 自动发现 GET 接口 {len(apis)} 个"
          f"（{len(skipped)} 个因缺少真实 id 参数跳过）")
    report["api_count"] = len(apis)
    report["api_skipped"] = skipped

    since_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    created = []
    sampler = ResourceSampler().start()
    if not args.skip_backup:
        created, s, el = phase_create_tasks(session, args.tasks, args.concurrency)
        report["phase_a"] = s
        report["phase_a"]["created"] = len(created)
        report["phase_a"]["elapsed"] = round(el, 1)
        if created:
            s, el, counter = phase_run_backups(session, created, args.concurrency)
            report["phase_b"] = s
            report["phase_b"]["elapsed"] = round(el, 1)
            report["phase_b"]["result"] = counter
            ok, rows = phase_verify_records(created, counter.get("success", 0))
            report["phase_b2"] = {"ok": ok, **rows}

    s, el, per_api = phase_api(session, apis, args.api_concurrency, args.api_req)
    report["phase_c"] = s
    report["phase_c"]["elapsed"] = round(el, 1)

    checks = phase_data(session)
    report["phase_d"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]

    s, el = phase_mixed(session, apis, args.api_concurrency, args.mixed_seconds)
    report["phase_e"] = s
    report["phase_e"]["elapsed"] = round(el, 1)

    s, el, bad = phase_write_apis(session, args.api_concurrency,
                                  max(20, args.tasks // 10))
    report["phase_f"] = s
    report["phase_f"]["elapsed"] = round(el, 1)
    report["phase_f"]["bad"] = bad

    sampler.stop()
    report["resource"] = sampler.summary()
    err_n, err_samples = platform_error_delta(session, since_ts)
    report["platform_errors_during_test"] = err_n
    report["platform_error_samples"] = err_samples

    # 清理压测任务
    if created:
        print(f"\n[*] 清理压测任务 {len(created)} 个…")
        def cleanup(tid):
            try:
                session.delete(f"{BASE}/api/tasks/{tid}", timeout=30)
            except Exception:
                pass
        el = run_pool(cleanup, created, 50)
        print(f"    清理完成，耗时 {el:.1f}s")

    total = time.perf_counter() - t_all
    report["total_elapsed"] = round(total, 1)

    # 总评
    print("\n" + "=" * 70)
    print("  压测总览")
    print("=" * 70)
    ok_all = True
    for k in ("phase_a", "phase_b", "phase_c", "phase_e", "phase_f"):
        if k in report:
            s = report[k]
            bad = sum(v for code, v in s["codes"].items() if code == 0 or code >= 500)
            status = "OK" if bad == 0 else f"NOT OK({bad})"
            ok_all = ok_all and bad == 0
            print(f"  {k}: 请求 {s['requests']:>6}  QPS {s['qps']:>7}  "
                  f"P95 {s['p95_ms']:>8}ms  5xx/异常 {bad}  -> {status}")
    if "phase_b2" in report:
        b2 = report["phase_b2"]
        print(f"  phase_b2(落库对账): 记录 {b2['total']} 条  "
              f"状态 {b2['by_status']}  -> {'OK' if b2['ok'] else 'NOT OK'}")
        ok_all = ok_all and b2["ok"]
    d_ok = sum(1 for c in report["phase_d"] if c["ok"])
    d_all = len(report["phase_d"])
    print(f"  phase_d(数据校验): {d_ok}/{d_all} 通过" + ("" if d_ok == d_all else "  -> NOT OK"))
    ok_all = ok_all and d_ok == d_all
    if report.get("resource"):
        r = report["resource"]
        print(f"  平台进程资源: RSS 峰值 {r['rss_mb_max']}MB（结束 {r['rss_mb_last']}MB） "
              f"CPU 峰值 {r['cpu_sec_max']}s  线程峰值 {r['threads_max']}")
    print(f"  压测期间平台 ERROR 日志: {err_n}")
    print(f"  总耗时: {total:.1f}s")
    print("=" * 70)
    print("  结论: " + ("全部通过，系统在压测下无 5xx / 无异常" if ok_all
                         else "存在失败项，详见上方明细"))

    report["passed"] = ok_all
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n[+] 报告已写入 {args.report}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
