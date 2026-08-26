# -*- coding: utf-8 -*-
"""轮询任务 49 的最新备份记录直到完成（success/failed），打印最终结果。"""
import sys, time, requests
BASE="http://127.0.0.1:8080"
s=requests.Session()
s.post(f"{BASE}/login", data={"username":"admin","password":"admin123"}, timeout=10)
TID=49
last_status=""
t0=time.time()
while time.time()-t0 < 900:  # 最多等 15 分钟
    try:
        recs = s.get(f"{BASE}/api/records", params={"task_id":TID}, timeout=10).json()
        recs.sort(key=lambda x: x.get("started_at",""), reverse=True)
        r = recs[0] if recs else {}
        st = r.get("status","?")
        if st != last_status:
            print(f"t+{int(time.time()-t0)}s  id={r.get('id')} status={st} size={r.get('size_bytes')} checksum={(r.get('checksum') or '')[:24]} path={(r.get('backup_path') or '')[:90]}")
            print(f"   msg: {(r.get('message') or '')[:200]}")
            last_status = st
        if st in ("success","failed","simulated","error"):
            print("\n=== 最终记录详情 ===")
            for k in ("id","status","size_bytes","checksum","backup_path","duration_sec","started_at","message"):
                v = r.get(k)
                if k=="checksum" and v: v = v[:48]+"..."
                print(f"  {k}: {v}")
            # 物理/逻辑区分
            print(f"\n判定: {'✅ 真实备份' if (st=='success' and (r.get('size_bytes') or 0)>1024) else ('❌ '+st)}")
            sys.exit(0)
    except Exception as e:
        print(f"poll err: {e}")
    time.sleep(10)
print("TIMEOUT")
