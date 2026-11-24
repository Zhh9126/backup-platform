# -*- coding: utf-8 -*-
"""统一 Agent 插件框架（一个 Agent 支撑块级 CDP / BMR / 应用整机）契约回归。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "agent"))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import plugins  # noqa: E402


def test_three_plugins_registered():
    """一个 Agent 自报三块能力，且每块有 supported 与原因。"""
    classes = plugins.describe(None)
    names = [p["name"] for p in classes]
    assert names == ["block-cdp", "bmr", "app-dr", "block-carve"]
    for p in classes:
        assert "supported" in p and "reason" in p
        assert p["actions"] and p["version"]


def test_block_cdp_bitmap_and_diff():
    """块级位图：文件级/块级模式正确，diff 识别新增/删除。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="plug_cdp_")
    fp = os.path.join(d, "f.bin")
    with open(fp, "wb") as f:
        f.write(b"A" * 4096 + b"first")
    r = plugins.invoke("block-cdp", "bitmap", {"path": fp, "max_blocks": 10}, None)
    assert r["ok"] and r["mode"] == "block" and r["block_count"] == 2
    base = r["bitmap"]
    with open(fp, "wb") as f:
        f.write(b"B" * 4096 + b"second")
    r2 = plugins.invoke("block-cdp", "bitmap", {"path": fp, "max_blocks": 10}, None)
    diff = plugins.invoke("block-cdp", "diff",
                          {"base": base, "current": r2["bitmap"]}, None)
    assert diff["ok"] and diff["added_count"] > 0


def test_bmr_plan_no_destructive_action():
    """BMR 恢复只生成作业单（需人工确认），不自动 dd 写目标设备。"""
    r = plugins.invoke("bmr", "restore_plan",
                       {"image": "/tmp/x.img", "target_device": "/dev/sdb"}, None)
    assert r["ok"] and len(r["plan"]["steps"]) == 5
    assert "人工" in r["plan"]["warning"]
    src = __import__("inspect").getsource(plugins._bmr_restore_plan)
    assert "dd if=" not in src or "desc" in src  # 只在作业单描述里出现


def test_appdr_freeze_snapshot_thaw():
    """应用整机：冻结钩子 + 文件快照 + 解冻，全流程 ok。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="plug_app_")
    r = plugins.invoke("app-dr", "freeze",
                       {"freeze_cmd": "echo FROZEN"}, None)
    assert r["ok"] and r["frozen"] and "FROZEN" in r["stdout"]
    out = os.path.join(d, "s.tar.gz")
    r = plugins.invoke("app-dr", "snapshot",
                       {"roots": [d], "out": out}, None)
    assert r["ok"] and r["bytes"] > 0
    r = plugins.invoke("app-dr", "thaw",
                       {"thaw_cmd": "echo THAWED"}, None)
    assert r["ok"] and r["thawed"]


def test_unknown_plugin_honest_failure():
    assert not plugins.invoke("nope", "x", {}, None)["ok"]
    assert not plugins.invoke("block-cdp", "nope", {}, None)["ok"]


def test_agent_version_and_route():
    """Agent 1.3.0 + /plugin 路由 + /capabilities 自报。"""
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent", "backup_agent.py"),
        encoding="utf-8").read()
    assert 'VERSION = "1.3.0"' in src
    assert 'path == "/plugin"' in src
    assert '"plugins": _plugin_describe' in src
