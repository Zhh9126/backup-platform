# -*- coding: utf-8 -*-
"""MySQL 5.7 -> PostgreSQL 14 异构实时同步（Binlog CDC）端到端测试。

流程：
1. 创建 realtime 同步任务（源 testdb57.users -> 目标 sync_test.users）
2. 启动后台实时同步线程（全量快照 + binlog 增量）
3. 等待全量快照完成，校验行数一致
4. 对源表做 INSERT / UPDATE / DELETE
5. 等待增量同步，逐行比对源/目标数据
6. 停止 runner，输出测试报告
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEMO_MODE"] = "off"
os.chdir(ROOT)

import psycopg2  # noqa: E402
import pymysql  # noqa: E402

from core.sync.engine import run_sync_task  # noqa: E402
from core.sync.realtime_runners import start_runner, stop_runner  # noqa: E402
from core import models  # noqa: E402

SRC = dict(host="127.0.0.1", port=3309, user="flink", password="flinkpw",
           db="testdb57")
TGT = dict(host="127.0.0.1", port=5785, user="postgres", password="postgres",
           db="sync_test")
TASK_NAME = "MySQL5.7-to-PG14 实时同步(异构CDC)"
TABLE = "users"


def mysql_conn():
    return pymysql.connect(host=SRC["host"], port=SRC["port"], user=SRC["user"],
                           password=SRC["password"], db=SRC["db"],
                           charset="utf8mb4")


def pg_conn():
    return psycopg2.connect(host=TGT["host"], port=TGT["port"], user=TGT["user"],
                            password=TGT["password"], dbname=TGT["db"])


def read_mysql():
    c = mysql_conn()
    with c.cursor() as cur:
        cur.execute(f"SELECT id,name,email,created_at FROM testdb57.{TABLE} ORDER BY id")
        rows = [list(r) for r in cur.fetchall()]
    c.close()
    return rows


def read_pg():
    conn = pg_conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT id,name,email,created_at FROM "{TABLE}" ORDER BY id')
        rows = [list(r) for r in cur.fetchall()]
    conn.close()
    return rows


def norm(rows):
    """把两库行值归一化后比较。"""
    out = []
    for r in rows:
        rr = []
        for v in r:
            if hasattr(v, "isoformat"):
                v = v.isoformat(sep=" ")
            elif isinstance(v, (bytes, bytearray)):
                v = v.decode("utf-8", "replace")
            rr.append(v)
        out.append(rr)
    return out


def create_task():
    for t in models.list_sync_tasks(include_secret=True):
        if t.get("name") == TASK_NAME:
            return t["id"]
    data = {
        "name": TASK_NAME,
        "source_type": "manual",
        "src_db_type": "mysql",
        "src_host": SRC["host"], "src_port": SRC["port"],
        "src_username": SRC["user"], "src_password": SRC["password"],
        "src_db_name": SRC["db"], "src_schema": SRC["db"],
        "source_tables_list": json.dumps([TABLE]),
        "tgt_db_type": "postgresql",
        "tgt_host": TGT["host"], "tgt_port": TGT["port"],
        "tgt_username": TGT["user"], "tgt_password": TGT["password"],
        "tgt_db_name": TGT["db"], "tgt_schema": "public",
        "sync_mode": "realtime", "save_mode": "upsert",
        "enabled": 1,
    }
    return models.create_sync_task(data)


def wait_rows(conn_factory, expected, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            rows = conn_factory()
            if len(rows) == expected:
                return rows
        except Exception:
            pass
        time.sleep(1)
    return None


def main():
    results = []
    task_id = create_task()
    print(f"[1] 任务已创建 id={task_id}", flush=True)

    ok = start_runner(task_id, run_sync_task, task_id)
    print(f"[2] 实时同步线程启动: {ok}", flush=True)

    # 全量快照：PG 应达到源端 35 行
    src_rows = read_mysql()
    print(f"[3] 等待全量快照 (期望 {len(src_rows)} 行)...", flush=True)
    pg_rows = wait_rows(read_pg, len(src_rows))
    if pg_rows is None:
        print("    [失败] 全量快照超时未完成", flush=True)
    elif norm(pg_rows) == norm(src_rows):
        results.append("全量快照一致")
        print(f"    [通过] 全量快照 {len(pg_rows)} 行与源端一致", flush=True)
    else:
        results.append("全量快照不一致")
        print("    [失败] 全量快照与源端不一致!", flush=True)
        print("    源:", norm(src_rows)[:5], flush=True)
        print("    目:", norm(pg_rows)[:5], flush=True)

    # 增量 DML
    c = mysql_conn()
    with c.cursor() as cur:
        cur.execute(f"""
            INSERT INTO testdb57.{TABLE} (name, email) VALUES
            ('pg增量-张三', 'zs_pg@test.com'),
            ('pg增量-李四', NULL),
            ('pg增量-王五', 'ww_pg@test.com')
        """)
        c.commit()
        cur.execute(f"UPDATE testdb57.{TABLE} SET email='updated_pg@test.com' WHERE name LIKE 'pg增量-%' LIMIT 2")
        c.commit()
        cur.execute(f"DELETE FROM testdb57.{TABLE} WHERE name='pg增量-李四'")
        c.commit()
        cur.execute(f"SELECT COUNT(*) FROM testdb57.{TABLE}")
        expected = cur.fetchone()[0]
    c.close()
    print(f"[4] 增量 DML 已提交，源端最新行数={expected}", flush=True)

    time.sleep(4)
    pg_after = read_pg()
    src_after = read_mysql()
    if len(pg_after) != expected:
        results.append(f"增量行数不符 源{expected}/目{len(pg_after)}")
        print(f"    [失败] 增量后行数不符 源={expected} 目={len(pg_after)}", flush=True)
    elif norm(pg_after) == norm(src_after):
        results.append("增量 DML 一致")
        print(f"    [通过] 增量 INSERT/UPDATE/DELETE 后 {expected} 行与源端完全一致", flush=True)
    else:
        results.append("增量数据不一致")
        print("    [失败] 增量后数据不一致!", flush=True)
        print("    源:", norm(src_after), flush=True)
        print("    目:", norm(pg_after), flush=True)

    res = stop_runner(task_id)
    print(f"[5] runner 已停止: {res.get('success')}", flush=True)

    print("\n========== 测试结论 ==========", flush=True)
    for r in results:
        print(" -", r, flush=True)
    ok_all = all("一致" in r or "通过" in r for r in results)
    print("整体:", "PASS" if ok_all else "FAIL", flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
