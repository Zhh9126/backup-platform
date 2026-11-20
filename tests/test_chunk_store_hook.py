# -*- coding: utf-8 -*-
"""备份成功后「块化入库」钩子单测。

要求：
  - 默认关闭 → 完全不动（不写块、不写库）；
  - 开启 → 转存并回写 chunk_manifest_id；
  - 产物超过阈值 → 跳过（纯 Python 吞吐有限，不能拖垮大库备份）；
  - 任何异常都不得影响备份结果（返回 None，不抛）。
"""
import pytest

import config
from core import container_store, scheduler


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ROOT", str(tmp_path), raising=False)
    calls = []

    def _exec(sql, params=()):
        calls.append((sql, params))
        return 1

    monkeypatch.setattr(scheduler.db, "execute", _exec)
    art = tmp_path / "bak.sql.zst"
    art.write_bytes(b"x" * (512 * 1024))
    return {"path": str(art), "size": 512 * 1024,
            "calls": calls, "store": str(tmp_path)}


def test_disabled_by_default(env):
    th = scheduler._maybe_chunk_store({"id": 1, "extra_options": {}},
                                      rec_id=1, path=env["path"], size=env["size"])
    assert th is None
    assert container_store.stats(env["store"])["blocks_indexed"] == 0
    assert env["calls"] == []


def test_enabled_container_mode(env):
    task = {"id": 2, "extra_options": {"chunk_store_enabled": True}}
    scheduler._maybe_chunk_store(task, rec_id=99, path=env["path"],
                                 size=env["size"], sync=True)
    st = container_store.stats(env["store"])
    assert st["blocks_indexed"] >= 1
    assert env["calls"], "应回写 chunk_manifest_id"
    sql, params = env["calls"][0]
    assert "chunk_manifest_id" in sql and params[1] == 99
    assert "containers" in st["store_root"]


def test_extra_options_as_json_string(env):
    task = {"id": 3, "extra_options": '{"chunk_store_enabled": 1}'}
    scheduler._maybe_chunk_store(task, rec_id=100, path=env["path"],
                                 size=env["size"], sync=True)
    assert container_store.stats(env["store"])["blocks_indexed"] >= 1


def test_skip_when_too_large(env):
    # 阈值 1MB、产物 8MB → 跳过（不能拖垮大库备份）
    task = {"id": 4, "extra_options": {"chunk_store_enabled": True,
                                       "chunk_store_max_mb": 1}}
    scheduler._maybe_chunk_store(task, rec_id=101, path=env["path"],
                                 size=8 * 1024 * 1024, sync=True)
    assert container_store.stats(env["store"])["blocks_indexed"] == 0
    assert env["calls"] == []


def test_zero_threshold_means_unlimited(env):
    """显式配 0 = 不限制（不能因为 `or 512` 被当成默认值吞掉）。"""
    task = {"id": 6, "extra_options": {"chunk_store_enabled": True,
                                       "chunk_store_max_mb": 0}}
    scheduler._maybe_chunk_store(task, rec_id=103, path=env["path"],
                                 size=900 * 1024 * 1024, sync=True)
    assert container_store.stats(env["store"])["blocks_indexed"] >= 1


def test_failure_does_not_raise(env, monkeypatch):
    """存储抛异常时必须静默降级（只写日志），绝不冒泡到备份主流程。"""
    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(container_store, "store_file", _boom)
    task = {"id": 5, "extra_options": {"chunk_store_enabled": True}}
    # sync 路径同样不能抛
    assert scheduler._maybe_chunk_store(task, rec_id=102, path=env["path"],
                                        size=env["size"], sync=True) is None
