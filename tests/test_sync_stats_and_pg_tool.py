# -*- coding: utf-8 -*-
"""逐库验证中发现的三个真缺陷回归（2026-11-13）。

1. ``create_sync_record`` 白名单漏掉 read_rows / error_rows / watermark
   → 同步记录永远「读取 0 行」，与消息「读取 N 行」矛盾；
2. ``/api/sync/records`` 忽略 task_id 过滤 → 前端按任务查历史会串任务；
3. 克隆/PITR 等恢复通道用裸 ``psql``（系统 9.2）→ PG10+ 服务端报
   "SCRAM authentication requires libpq version 10 or above"。
"""
import re
from pathlib import Path

import core.db as db
import core.models as models
from core import restore_extras as rx

SRC_DIR = Path(__file__).resolve().parent.parent


def test_sync_record_persists_all_stats():
    tid_row = db.query_one("SELECT id FROM sync_tasks ORDER BY id LIMIT 1")
    tid = tid_row["id"] if tid_row else None
    if not tid:
        import pytest
        pytest.skip("无同步任务可用于关联")
    rec_id = models.create_sync_record({
        "sync_task_id": tid, "started_at": "2026-11-13T00:00:00",
        "finished_at": "2026-11-13T00:00:01", "status": "success",
        "rows_synced": 7, "read_rows": 9, "error_rows": 2,
        "watermark": "100", "message": "自检",
    })
    try:
        row = models.get_sync_record(rec_id)
        assert row["rows_synced"] == 7
        assert row["read_rows"] == 9, "read_rows 被白名单静默丢弃"
        assert row["error_rows"] == 2
        assert row["watermark"] == "100"
    finally:
        db.execute("DELETE FROM sync_records WHERE id=?", (rec_id,))


def test_pg_tool_prefers_modern_client():
    """解析出的 psql/pg_restore 必须是 >=10（否则 SCRAM 认证必失败）。"""
    for name in ("psql", "pg_restore"):
        path = rx._pg_tool(name)
        major = rx._pg_version(path)
        assert major >= 10, f"{name} 解析到 {path}（版本 {major}），无法连接 PG10+"


def test_sync_records_api_accepts_task_filter():
    """接口源码必须读取 task_id/sync_task_id 过滤参数（防回归）。"""
    src = (SRC_DIR / "api" / "sync.py").read_text(encoding="utf-8")
    assert "def compat_list_all_records" in src
    body = src.split("def compat_list_all_records", 1)[1][:800]
    assert re.search(r'task_id', body), "同步记录接口未处理 task_id 过滤"
    assert "list_sync_records(tid)" in body or "list_sync_records(int" in body
