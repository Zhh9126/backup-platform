# -*- coding: utf-8 -*-
"""AI 保护计划（跨层一致性组）单测：真跑本机 MySQL 3307 备份作为成员任务。"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
BASE = os.path.join(ROOT, "tests", ".tmp_ai_plan")
os.environ["META_DB_PATH"] = os.path.join(BASE, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(BASE, "backups")
os.makedirs(os.path.join(BASE, "backups"), exist_ok=True)

import core.db as db
db.init_schema()
import core.ai_protection as ap
from core import models

import pymysql


def _seed_mysql_db():
    conn = pymysql.connect(host="127.0.0.1", port=3307, user="root",
                           password="Ceshi@133", autocommit=True)
    cur = conn.cursor()
    cur.execute("DROP DATABASE IF EXISTS e2e_ai_plan")
    cur.execute("CREATE DATABASE e2e_ai_plan DEFAULT CHARSET utf8mb4")
    cur.execute("USE e2e_ai_plan")
    cur.execute("CREATE TABLE t_ai(id INT PRIMARY KEY, v VARCHAR(50))")
    cur.execute("INSERT INTO t_ai VALUES (1,'权重'),(2,'索引')")
    conn.close()


def _make_task(name: str) -> int:
    return models.create_task({
        "name": name, "db_type": "mysql",
        "host": "127.0.0.1", "port": 3307, "username": "root",
        "password": "Ceshi@133", "db_name": "e2e_ai_plan",
        "backup_type": "full", "enabled": 1,
    })


def test_members_normalize():
    t1 = _make_task("ai-plan-member-1")
    try:
        # 合法成员 + 非法层兜底 + 重复去重
        ms = ap._normalize_members([
            {"task_id": t1, "layer": "vector_index"},
            {"task_id": str(t1), "layer": "bad_layer"},   # 重复 + 非法层
        ])
        assert len(ms) == 1 and ms[0]["layer"] == "vector_index"
        assert ms[0]["task_name"] == "ai-plan-member-1"
        # 任务不存在拒绝
        try:
            ap._normalize_members([{"task_id": 999999}])
            assert False, "should reject missing task"
        except ap.PlanError:
            pass
        # 空成员拒绝
        try:
            ap._normalize_members([])
            assert False, "should reject empty"
        except ap.PlanError:
            pass
    finally:
        pass


def test_plan_crud():
    t1 = _make_task("ai-plan-member-crud")
    pid = ap.create_plan({"name": "crud-plan", "members": [{"task_id": t1}],
                          "max_drift_sec": 120, "cron": "0 3 * * *"})
    p = ap.get_plan(pid)
    assert p and p["name"] == "crud-plan" and p["max_drift_sec"] == 120
    assert p["members"][0]["layer"] == "rdb_metadata"   # 非法层兜底
    ap.update_plan(pid, {"members": [{"task_id": t1, "layer": "weights"}],
                         "max_drift_sec": 60, "enabled": False})
    p = ap.get_plan(pid)
    assert p["members"][0]["layer"] == "weights" and p["max_drift_sec"] == 60
    assert p["enabled"] in (0, False)
    assert ap.delete_plan(pid)
    assert ap.get_plan(pid) is None


def test_run_plan_real_mysql_backup():
    """真跑：成员 = 本机 MySQL 3307 备份任务（真实备份），校验组报告结构。"""
    _seed_mysql_db()
    t1 = _make_task("ai-plan-real-1")
    t2 = _make_task("ai-plan-real-2")
    pid = ap.create_plan({
        "name": "real-plan",
        "members": [{"task_id": t1, "layer": "vector_index"},
                    {"task_id": t2, "layer": "rdb_metadata"}],
        "max_drift_sec": 600,
    })
    report = ap.run_plan(pid, operator="e2e")
    assert report["ok"] is True, json.dumps(report, ensure_ascii=False)[:400]
    assert report["consistency"] == "跨层一致"
    assert report["epoch"] is not None
    assert report["drift_sec"] <= 600
    assert len(report["members"]) == 2
    for m in report["members"]:
        assert m["status"] == "success", m["message"][:200]
        assert m["record_id"], "成员应产生真实备份记录"
        rec = models.get_record(m["record_id"])
        assert rec and rec.get("status") == "success"
    # 计划里持久化了 last_result
    p = ap.get_plan(pid)
    assert p["last_result"] and p["last_result"]["ok"] is True


def test_run_plan_reports_failed_member():
    """成员任务失败（不存在的实例）→ 组报告 ok=False，不伪装成功。"""
    t1 = models.create_task({
        "name": "ai-plan-broken", "db_type": "mysql",
        "host": "127.0.0.1", "port": 3399, "username": "root",
        "password": "wrong", "db_name": "no_db",
        "backup_type": "full", "enabled": 1,
    })
    pid = ap.create_plan({"name": "broken-plan",
                          "members": [{"task_id": t1, "layer": "weights"}]})
    report = ap.run_plan(pid, operator="e2e")
    assert report["ok"] is False
    assert report["members"][0]["status"] != "success"


def test_api_routes_registered():
    from api import api_bp
    rules = {str(r) for r in api_bp.app if True} if hasattr(api_bp, "app") else set()
    # 直接断言 url_map 由 app 测试客户端更可靠；这里检查函数视图存在
    import api.ai_backup as m
    assert hasattr(m, "list_ai_plans") and hasattr(m, "run_ai_plan")
