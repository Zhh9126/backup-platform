#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多库型主机 E2E 测试矩阵（能力沉淀·独立可复用）。

对一个/多个真实主机，按库型自动完成：纳管 SSH 主机 → 创建备份任务 →
立即备份 → 产物校验 → 输出矩阵报告（JSON + 文本表）。是平台各库型
"备份-校验"最小闭环的标准验收入口，新库型接入后在此登记即可回归。

用法（在平台项目根目录）：
  .venv/bin/python scripts/e2e_matrix.py --hosts tests/e2e/matrix_hosts.json \
      [--only 139-pg14] [--keep]

hosts 文件格式（tests/e2e/matrix_hosts.json）：
{
  "targets": [
    {
      "id": "139-pg14",
      "ssh": {"host": "192.168.220.139", "port": 22,
              "username": "root", "password": "xxx", "auth_type": "password"},
      "task": {"name": "matrix-pg14", "db_type": "postgresql",
               "host": "192.168.220.139", "port": 5432,
               "username": "postgres", "password": "xxx",
               "db_name": "backup_test", "backup_mode": "logical",
               "extra_options": {}},
      "expect": {"message_contains": "pg_dump"}
    }
  ]
}
"""
import argparse
import json
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")
os.environ.setdefault("META_DB_PATH", os.path.join(PROJ, "data", "meta.db"))
os.environ.setdefault("BACKUP_ROOT", os.path.join(PROJ, "data", "backups"))
os.makedirs(os.environ["META_DB_PATH"].rsplit("/", 1)[0], exist_ok=True)
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.chdir(PROJ)

RESULTS = []


def report(case, ok, detail=""):
    RESULTS.append((case, bool(ok)))
    print(("PASS " if ok else "FAIL ") + case +
          (("  | " + str(detail)[:180]) if detail else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    import core.db as db
    db.init_schema()
    from core import models, scheduler

    cfg = json.load(open(args.hosts, encoding="utf-8"))
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    report_rows = []

    for tgt in cfg.get("targets", []):
        tid = tgt.get("id")
        if only and tid not in only:
            continue
        ssh = tgt.get("ssh") or {}
        tk = tgt.get("task") or {}
        expect = tgt.get("expect") or {}

        # 1) 纳管 SSH 主机（幂等）
        host_key = f"{ssh.get('username')}@{ssh.get('host')}:{ssh.get('port') or 22}"
        db.execute("DELETE FROM ssh_hosts WHERE host_key=?", (host_key,))
        hid = db.execute(
            "INSERT INTO ssh_hosts (name,host_key,hostname,port,username,password,"
            "auth_type,os_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"matrix-{tid}", host_key, ssh.get("host"), ssh.get("port") or 22,
             ssh.get("username"), db.encrypt_secret(ssh.get("password") or ""),
             ssh.get("auth_type") or "password", ssh.get("os_type") or "linux",
             db.now_iso(), db.now_iso()))
        report(f"{tid}·纳管SSH", bool(hid))

        # 2) 创建备份任务（幂等：按名删除重建）
        old = [t for t in (models.list_tasks() or [])
               if t.get("name") == tk.get("name")]
        for t in old:
            try:
                models.delete_task(t["id"])
            except Exception:
                pass
        tk_full = {"ssh_host_id": hid, "backup_type": "full",
                   "schedule_type": "none", "enabled": 1, **tk}
        if isinstance(tk_full.get("extra_options"), dict):
            tk_full["extra_options"] = json.dumps(tk_full["extra_options"],
                                                  ensure_ascii=False)
        task_id = models.create_task(tk_full)
        report(f"{tid}·建任务", task_id > 0, f"task_id={task_id}")

        # 3) 立即备份
        t0 = time.time()
        try:
            rec = scheduler.run_task_now(task_id, backup_type="full",
                                         operator="e2e-matrix")
        except Exception as e:
            rec = {"status": "error", "message": str(e)[:200]}
        dur = round(time.time() - t0, 1)
        ok = bool(rec) and rec.get("status") == "success"
        msg = (rec or {}).get("message", "")
        if expect.get("message_contains"):
            ok = ok and expect["message_contains"] in msg
        report(f"{tid}·备份成功", ok, f"{dur}s {msg}")
        record_id = (rec or {}).get("id")

        # 4) 产物校验（verify_record：真实解包/解析，非模拟）
        if ok and record_id:
            try:
                from core.engines import get_engine
                task = models.get_task(task_id, include_secret=True)
                eng = get_engine(task["db_type"], task, os.environ["BACKUP_ROOT"])
                vr = eng.verify_record(rec)
                vok = bool(getattr(vr, "success", False))
                report(f"{tid}·产物校验", vok,
                       getattr(vr, "message", "")[:150])
            except Exception as e:
                report(f"{tid}·产物校验", False, f"{type(e).__name__}: {e}")
        else:
            report(f"{tid}·产物校验", False, "备份失败跳过")

        report_rows.append({
            "id": tid, "db_type": tk.get("db_type"), "ok": ok,
            "duration_sec": dur, "message": msg[:300],
            "record_id": record_id,
        })

    passed = sum(1 for _, ok in RESULTS if ok)
    print("\n===== 矩阵结果: %d/%d PASS =====" % (passed, len(RESULTS)))
    out = {"summary": f"{passed}/{len(RESULTS)}", "rows": report_rows}
    with open("matrix_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("报告: matrix_report.json")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
