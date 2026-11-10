# -*- coding: utf-8 -*-
"""并发能力回归：多任务同时发起 / 并发上限 / 队列去重 / 配置热更新。

覆盖 v2.3.7 的：
- core/scheduler.py submit_task_now + _BACKUP_EXECUTOR + _RUNNING_TASKS
- _ConcurrencyController 的 permit 释放修复与配置热变更
- api/tasks.py quick-backup 并行投递、/tasks/<id>/run 的 async 模式
- api/system.py /system/concurrency 读写端点
"""
import threading
import time

import pytest

import config
import core.scheduler as scheduler


# --------------------------------------------------------------------------
# 并发控制器
# --------------------------------------------------------------------------

def test_limit_sources_and_clamp(monkeypatch):
    c = scheduler._ConcurrencyController()
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 0)
    monkeypatch.setattr(scheduler.db, "get_system_config", lambda k, d=None: "7")
    assert c.limit() == 7
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 3)
    assert c.limit() == 3                       # env 覆盖 system_config
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 999)
    assert c.limit() == 64                      # 上限钳制
    monkeypatch.setattr(scheduler.db, "get_system_config",
                        lambda k, d=None: (_ for _ in ()).throw(ValueError("db down")))
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 0)
    assert c.limit() == 2                       # 读配置异常回落缺省 2


def test_release_uses_acquired_semaphore(monkeypatch):
    """配置热变更重建信号量后，permit 必须还给 acquire 时的那个信号量。"""
    c = scheduler._ConcurrencyController()
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 0)
    monkeypatch.setattr(scheduler.db, "get_system_config", lambda k, d=None: "2")
    sem1 = c.acquire()
    assert sem1._value == 1
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 5)
    c.invalidate()
    sem2 = c.acquire()
    assert sem1 is not sem2 and sem2._value == 4
    c.release(sem1)
    assert sem1._value == 2                     # 旧信号量恢复其 permit
    assert sem2._value == 4                     # 新信号量没有被误加


# --------------------------------------------------------------------------
# 并行执行器：真实并行 + 去重 + 异常回滚
# --------------------------------------------------------------------------

def _fake_task(tid):
    return {"id": tid, "name": f"task-{tid}", "db_type": "mysql",
            "extra_options": {}, "backup_type": "full"}


def _wait_idle(tids, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if not any(scheduler.is_task_submitted(t) for t in tids):
            return True
        time.sleep(0.02)
    return False


def test_parallel_submit_respects_limit(monkeypatch):
    """4 个任务一次性投递，真实并行度必须等于 max_concurrent_backups。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 2)
    monkeypatch.setattr(config, "BACKUP_WORKERS", 8)
    monkeypatch.setattr(scheduler.models, "get_task",
                        lambda tid, include_secret=False: _fake_task(tid))
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "done": []}

    def fake_core(task, bt, operator=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.12)                        # 制造重叠窗口
        with lock:
            state["active"] -= 1
            state["done"].append(task["id"])
        return {"status": "success"}

    monkeypatch.setattr(scheduler, "_execute_backup_core", fake_core)
    ids = [9101, 9102, 9103, 9104]
    accepted = [scheduler.submit_task_now(t) for t in ids]
    assert all(a.get("accepted") for a in accepted), accepted
    assert _wait_idle(ids), "并行任务未在超时内结束"
    assert sorted(state["done"]) == sorted(ids)
    assert state["peak"] == 2, f"真实并行度应等于上限 2，实际 {state['peak']}"


def test_duplicate_submit_rejected(monkeypatch):
    monkeypatch.setattr(scheduler.models, "get_task",
                        lambda tid, include_secret=False: _fake_task(tid))
    gate = threading.Event()

    def fake_core(task, bt, operator=None):
        gate.wait(timeout=5)
        return {"status": "success"}

    monkeypatch.setattr(scheduler, "_execute_backup_core", fake_core)
    first = scheduler.submit_task_now(9201)
    assert first.get("accepted")
    second = scheduler.submit_task_now(9201)
    assert second.get("accepted") is False and second.get("duplicate") is True
    gate.set()
    assert _wait_idle([9201])


def test_submit_failure_rolls_back_dedup(monkeypatch):
    monkeypatch.setattr(scheduler.models, "get_task",
                        lambda tid, include_secret=False: _fake_task(tid))

    class BrokenExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("queue full")

    monkeypatch.setattr(scheduler, "_backup_executor", lambda: BrokenExecutor())
    res = scheduler.submit_task_now(9301)
    assert res.get("accepted") is False
    assert scheduler.is_task_submitted(9301) is False, "提交失败必须释放去重标记"
    # 修复后可再次提交（不残留僵尸 running）
    monkeypatch.setattr(scheduler.models, "get_task",
                        lambda tid, include_secret=False: None)
    assert scheduler.submit_task_now(9301).get("accepted") is False


# --------------------------------------------------------------------------
# API 契约
# --------------------------------------------------------------------------

@pytest.fixture()
def client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = {"id": 1, "username": "admin",
                         "display_name": "admin", "role": "admin"}
        yield c


def test_concurrency_config_api(client, monkeypatch):
    # 隔离 system_config 读写，避免测试把并发上限写进真实运行元库
    store = {"max_concurrent_backups": "2"}
    monkeypatch.setattr(config, "MAX_CONCURRENT_BACKUPS", 0)
    monkeypatch.setattr(scheduler.db, "get_system_config",
                        lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(scheduler.db, "set_system_config",
                        lambda k, v: store.__setitem__(k, str(v)))
    r = client.get("/api/system/concurrency")
    assert r.status_code == 200
    body = r.get_json()
    assert 1 <= body["max_concurrent_backups"] <= 64
    assert "stats" in body and "submitted" in body["stats"]
    r2 = client.put("/api/system/concurrency", json={"max_concurrent_backups": 4})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json()["effective"] == 4
    assert client.get("/api/system/concurrency").get_json()["max_concurrent_backups"] == 4
    assert client.put("/api/system/concurrency",
                      json={"max_concurrent_backups": 0}).status_code == 400
    assert store["max_concurrent_backups"] == "4"


def test_quick_backup_uses_parallel_submit(client, monkeypatch):
    calls = []

    def fake_submit(tid, backup_type=None, operator=None):
        calls.append((tid, backup_type, operator))
        return {"accepted": True, "task_id": tid, "status": "running",
                "message": "已提交并行执行队列"}

    monkeypatch.setattr(scheduler, "submit_task_now", fake_submit)
    monkeypatch.setattr(scheduler, "concurrency_stats", lambda: {"submitted": 2})
    monkeypatch.setattr("api.tasks.models.list_tasks",
                        lambda include_secret=False: [
                            {"id": 1, "name": "a", "enabled": 1},
                            {"id": 2, "name": "b", "enabled": 1},
                            {"id": 3, "name": "c", "enabled": 0},
                        ])
    r = client.post("/api/tasks/quick-backup", json={"task_ids": [1, 2]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["triggered"] == 2 and body["total"] == 2 and body["parallel"] is True
    assert [c[0] for c in calls] == [1, 2]
    assert all(c[1] == "full" and c[2] == "quick-backup" for c in calls)


def test_quick_backup_rejects_bad_task_ids(client):
    """用户可控的 task_ids 不能把接口打成 500。"""
    for payload in ({"task_ids": "1,2"}, {"task_ids": ["abc"]},
                    {"task_ids": [None]}, {"task_ids": {"a": 1}}):
        r = client.post("/api/tasks/quick-backup", json=payload)
        assert r.status_code == 400, (payload, r.status_code)


def test_run_task_async_mode(client, monkeypatch):
    monkeypatch.setattr(scheduler, "submit_task_now",
                        lambda tid, backup_type=None, operator=None: {
                            "accepted": True, "task_id": tid, "status": "running"})
    monkeypatch.setattr(scheduler, "is_task_submitted", lambda tid: False)
    r = client.post("/api/tasks/44/run", json={"async": True})
    assert r.status_code == 202
    assert r.get_json()["accepted"] is True
    monkeypatch.setattr(scheduler, "submit_task_now",
                        lambda tid, backup_type=None, operator=None: {
                            "accepted": False, "duplicate": True})
    assert client.post("/api/tasks/44/run",
                       json={"async": True}).status_code == 409
