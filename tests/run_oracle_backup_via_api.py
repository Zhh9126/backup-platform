# -*- coding: utf-8 -*-
"""
通过 API（8080 服务进程）对真实数据库任务触发备份 — 这就是 UI 点"运行"走的同一条链路。
如果返回真实备份记录（非仿真），证明：新服务 + oracle.py 真实 SSH 备份 + API 路由全通。
"""
import sys, time, json
import requests

BASE = "http://127.0.0.1:8080"

def login():
    s = requests.Session()
    s.post(f"{BASE}/login", data={"username": "admin", "password": "admin123"}, timeout=10).raise_for_status()
    return s

def main(task_id):
    s = login()
    # 查任务确认存在且是 Oracle
    t = s.get(f"{BASE}/api/tasks/{task_id}", timeout=10).json()
    print(f"[任务 #{task_id}] {t.get('name')}  db_type={t.get('db_type')}  mode={t.get('backup_mode')}  host={t.get('host')}")
    # 触发备份
    print(f"[触发备份] POST /api/tasks/{task_id}/run ...")
    r = s.post(f"{BASE}/api/tasks/{task_id}/run", json={}, timeout=10)
    print(f"  HTTP {r.status_code}: {r.text[:300]}")
    # 轮询最新记录
    print("[轮询记录] /api/records?task_id=...")
    for i in range(120):
        time.sleep(2)
        recs = s.get(f"{BASE}/api/records", params={"task_id": task_id}, timeout=10).json()
        if not isinstance(recs, list) or not recs:
            continue
        recs.sort(key=lambda x: x.get("started_at",""), reverse=True)
        rec = recs[0]
        st = rec.get("status"); sz = rec.get("size_bytes",0)
        msg = (rec.get("message") or "")[:200]
        print(f"  t+{i*2}s  status={st}  size={sz}B  msg={msg[:120]}")
        if st in ("success","failed","simulated","error"):
            print("\n=== 最新记录详情 ===")
            for k in ("id","status","size_bytes","checksum","backup_path","message","duration_sec"):
                v = rec.get(k)
                if k=="checksum" and v: v = v[:24]+"..."
                print(f"  {k}: {v}")
            return rec
    print("TIMEOUT")

if __name__=="__main__":
    tid = int(sys.argv[1]) if len(sys.argv)>1 else 49
    main(tid)
