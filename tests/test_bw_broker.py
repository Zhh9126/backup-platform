# -*- coding: utf-8 -*-
"""全局带宽 Max-Min 仲裁单测（对标 PB 级方案 §5.1）。

语义：
  - 未配置全局配额 → 完全沿用任务自身限速（零行为变化）；
  - 配置后按权重公平分配，低需求任务先满足、剩余额度留在池中；
  - 单任务有保底额度，避免被饿死；
  - 长期不活跃的任务自动剔除。
"""
import pytest

from core import bw_broker


@pytest.fixture(autouse=True)
def _clean():
    bw_broker.reset()
    yield
    bw_broker.reset()


@pytest.fixture
def quota(monkeypatch):
    box = {"v": 0}

    def _get(key, default=None):
        if key == "global_bandwidth_kbps":
            return box["v"]
        return default

    monkeypatch.setattr(bw_broker, "_global_quota", lambda: int(box["v"] or 0))
    return box


# ---------------- 水填充算法 ----------------

def test_equal_split_when_all_unlimited():
    r = bw_broker.water_fill(1200, {"a": None, "b": None})
    assert r == {"a": 600, "b": 600}


def test_low_demand_capped_and_rest_stays():
    # a 只要 100，剩余 900 给 b，不强行摊给 a
    r = bw_broker.water_fill(1000, {"a": 100, "b": None})
    assert r == {"a": 100, "b": 900}


def test_three_tasks_two_capped():
    r = bw_broker.water_fill(1000, {"a": 100, "b": 200, "c": None})
    assert r["a"] == 100 and r["b"] == 200 and r["c"] == 700


def test_weights_share():
    r = bw_broker.water_fill(900, {"a": None, "b": None}, {"a": 2, "b": 1})
    assert r == {"a": 600, "b": 300}


def test_all_capped_leaves_pool_unused():
    r = bw_broker.water_fill(1000, {"a": 100, "b": 100})
    assert r == {"a": 100, "b": 100}


def test_zero_total_returns_zeros():
    assert bw_broker.water_fill(0, {"a": None}) == {"a": 0}


# ---------------- 与引擎集成语义 ----------------

def test_disabled_returns_task_limit_unchanged(quota):
    quota["v"] = 0
    assert bw_broker.effective_limit(1, 500) == 500
    assert bw_broker.effective_limit(2, 0) == 0


def test_enabled_allocates_within_quota(quota):
    quota["v"] = 1000
    # 两个任务都进入活跃集合后，再各取一次（引擎每个限速周期都会重新取）
    bw_broker.effective_limit(11, 900)
    bw_broker.effective_limit(12, 900)
    a = bw_broker.effective_limit(11, 900)
    b = bw_broker.effective_limit(12, 900)
    assert a > 0 and b > 0
    assert a + b <= 1000 + 2      # 允许取整误差
    assert abs(a - b) <= 2        # 同权重应接近平均


def test_enabled_respects_small_demand(quota):
    quota["v"] = 1000
    small = bw_broker.effective_limit(21, 100)
    big = bw_broker.effective_limit(22, 5000)
    assert small <= 100
    assert big >= 500


def test_min_alloc_floor(quota):
    quota["v"] = 10                 # 极端小配额
    v = bw_broker.effective_limit(31, 5000)
    assert v >= bw_broker.MIN_ALLOC_KBPS or v == 0


def test_snapshot_shape(quota):
    quota["v"] = 600
    bw_broker.effective_limit(41, 300)
    snap = bw_broker.snapshot()
    assert snap["enabled"] is True
    assert snap["global_quota_kbps"] == 600
    assert any(r["task_id"] == 41 for r in snap["active"])
    assert isinstance(snap["note"], str) and snap["note"]


def test_stale_task_removed(quota, monkeypatch):
    quota["v"] = 600
    bw_broker.effective_limit(51, 300)
    # 模拟任务已结束：把活跃时间推早到过期
    with bw_broker._LOCK:
        bw_broker._ACTIVE[51]["seen"] -= bw_broker.STALE_SECONDS + 10
        bw_broker._LAST_CALC = 0
    snap = bw_broker.snapshot()
    assert all(r["task_id"] != 51 for r in snap["active"])


# ---------------- transfer_params 接入（remote_dump 通道） ----------------

def test_transfer_params_uses_arbitration(quota, monkeypatch):
    """remote_dump.transfer_params 必须经 broker，而不是直读任务字段。"""
    from core import remote_dump
    quota["v"] = 1000
    bw_broker.reset()
    t1 = {"id": 9001, "bandwidth_limit": 800, "extra_options": {}}
    t2 = {"id": 9002, "bandwidth_limit": 800, "extra_options": {}}
    remote_dump.transfer_params(t1)          # 两个任务都进入活跃集合
    remote_dump.transfer_params(t2)
    r1 = remote_dump.transfer_params(t1)["rate_kbps"]
    r2 = remote_dump.transfer_params(t2)["rate_kbps"]
    # 第二个任务加入后会重算 → 各 500；此处至少验证被配额约束
    assert r1 + r2 <= 1000 + 2
    assert r1 > 0 and r2 > 0


def test_transfer_params_no_quota_keeps_task_limit(quota):
    from core import remote_dump
    quota["v"] = 0
    bw_broker.reset()
    assert remote_dump.transfer_params(
        {"id": 9003, "bandwidth_limit": 700})["rate_kbps"] == 700
    assert remote_dump.transfer_params(
        {"id": 9004, "bandwidth_limit": 0})["rate_kbps"] == 0


def test_arbitrate_rate_failure_falls_back(quota, monkeypatch):
    """仲裁异常时回退任务原值（限速问题不能影响备份主链路）。"""
    from core import remote_dump
    quota["v"] = 1000

    def _boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(bw_broker, "effective_limit", _boom)
    assert remote_dump._arbitrate_rate({"id": 9005, "bandwidth_limit": 640}, {}) == 640


def test_arbitrate_rate_weight(quota):
    from core import remote_dump
    quota["v"] = 900
    bw_broker.reset()
    remote_dump._arbitrate_rate({"id": 9010, "bandwidth_limit": 5000}, {"bw_weight": 2})
    remote_dump._arbitrate_rate({"id": 9011, "bandwidth_limit": 5000}, {"bw_weight": 1})
    a = remote_dump._arbitrate_rate({"id": 9010, "bandwidth_limit": 5000}, {"bw_weight": 2})
    b = remote_dump._arbitrate_rate({"id": 9011, "bandwidth_limit": 5000}, {"bw_weight": 1})
    assert a > b, "权重高的任务应分到更多额度"
    assert a + b <= 902
