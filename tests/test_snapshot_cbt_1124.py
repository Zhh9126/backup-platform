# -*- coding: utf-8 -*-
"""一致性快照 + CBT 能力契约（block-cdp 插件）。"""
import os
import sys
import inspect
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "agent"))
import plugins  # noqa: E402


def test_block_cdp_v12_actions():
    p = plugins.PLUGIN_MAP["block-cdp"]
    assert p["version"] == "1.2.0"
    for action in ("snapshot_create", "snapshot_release", "cbt_start", "cbt_changed"):
        assert action in p["actions"], "缺少动作 %s" % action


def test_snapshot_create_dm_then_fallback():
    """dm 路径优先，失败时必须降级用户态副本并如实标注 mode/dm_reason。"""
    src = inspect.getsource(plugins._cdp_snapshot_create)
    assert "dmsetup create" in src and "snapshot %s %s" in src
    assert 'mode="user_copy"' in src and "dm_reason" in src
    assert "cp --sparse=always" in src or "dd if=" in src


def test_cbt_dm_exception_table_and_polling():
    src = inspect.getsource(plugins._cdp_cbt_changed)
    assert "dm_cow" in src and "COW exception" in src
    assert "polling" in src and "baseline" in src
    assert "非内核写入期 CBT" in inspect.getsource(plugins._cdp_cbt_start) or \
           "轮询位图 diff" in src


def test_cbt_changed_polling_real_diff():
    """轮询模式必须精确返回变更块索引。"""
    base = [[0, "a" * 16], [1, "b" * 16], [2, "c" * 16]]
    with mock.patch.object(plugins, "_cdp_bitmap", return_value={
            "ok": True, "bitmap": [[0, "a" * 16], [2, "c" * 16], [7, "z" * 16]]}):
        r = plugins._cdp_cbt_changed({"path": "/dev/loop0", "baseline": base,
                                      "mode": "polling", "block_size": 4096})
    assert r["ok"] and r["count"] == 2  # 新增块7 + 消失块1
    assert len(r["added"]) == 1 and len(r["removed"]) == 1
    assert sorted(int(x[0]) for x in r["changed_blocks"]) == [1, 7]


def test_snapshot_release_cleans_dm_and_cow():
    with mock.patch.object(plugins, "_run", return_value={"rc": 0, "stdout": "", "stderr": ""}) as m:
        r = plugins._cdp_snapshot_release({"name": "snap1", "mode": "dm_snapshot",
                                           "cow_dev": "/dev/loop9", "cow_image": ""})
    assert r["ok"] and len(r["released"]) == 2
    cmds = " ".join(str(c) for c in m.call_args_list)
    assert "dmsetup remove snap1" in cmds and "losetup -d /dev/loop9" in cmds
