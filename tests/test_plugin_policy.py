# -*- coding: utf-8 -*-
"""备份插件：全类型覆盖 + 任务级开关 + 一键包回归（2026-11-13）。

用户诉求：所有备份类型（数据库 / 文件 / 对象存储 / 虚拟机）都要有插件能力，
且**由用户按需选择是否使用**，对外交付形态是「装一个插件包」。

锁定契约：
1. 插件目录覆盖 DB / 文件 / 对象存储 / 虚拟机 / 通用五类，且每条都有真实可检测的
   ``required_clients``（不造假：检测不到就是未安装）；
2. 任务级三态模式 auto / off / require：
   - off → 强制零安装通道；
   - auto → 有则用、无则回落；
   - require 且无插件 → ``ok=False`` 并给出可执行指引（由任务运行入口拦截）；
3. ``bundle_for_tasks`` 能按一组任务推导出缺失插件（对外"一个包"）。
"""
from core import plugin_catalog, plugin_policy


def _mk_task(db_type="mysql", use_plugin=None):
    extra = "" if use_plugin is None else '{"use_plugin": "%s"}' % use_plugin
    return {"id": 1, "db_type": db_type, "extra_options": extra}


def test_catalog_covers_all_backup_categories():
    rows = plugin_catalog.list_plugins()
    cats = {r.get("category") for r in rows}
    assert {"file", "object", "vm", "common"} <= cats, cats
    # 每个插件都必须声明可检测的客户端工具（诚实检测的前提）
    for r in rows:
        raw = plugin_catalog.load_all().get(r["id"]) or {}
        assert raw.get("required_clients"), f"{r['id']} 缺少 required_clients"


def test_mode_parsing_and_aliases():
    assert plugin_policy.task_mode(_mk_task()) == "auto"
    assert plugin_policy.task_mode(_mk_task(use_plugin="off")) == "off"
    assert plugin_policy.task_mode(_mk_task(use_plugin="true")) == "require"
    assert plugin_policy.task_mode(_mk_task(use_plugin="bogus")) == "auto"


def test_off_forces_zero_install_channel():
    res = plugin_policy.resolve(_mk_task(use_plugin="off"))
    assert res["ok"] is True
    assert res["use_plugin"] is False
    assert "零安装" in res["reason"]


def test_require_blocks_when_no_plugin_installed():
    # redis 插件在本机不存在（redis-cli 未装）→ require 必须阻塞
    res = plugin_policy.resolve(_mk_task("redis", use_plugin="require"))
    if res["installed"]:
        import pytest
        pytest.skip("本机恰好装了 redis-cli，无法构造缺失场景")
    assert res["ok"] is False
    assert "插件缺失" in res["message"]


def test_auto_falls_back_when_missing():
    res = plugin_policy.resolve(_mk_task("redis", use_plugin="auto"))
    assert res["ok"] is True
    if not res["installed"]:
        assert res["use_plugin"] is False
        assert "回落" in res["reason"]


def test_coverage_matrix_shape():
    cov = plugin_policy.coverage()
    assert cov["total_plugins"] > 0
    types = {r["type"] for r in cov["rows"]}
    # 矩阵按"备份类型"分组；通用插件（zstd/pigz）按其 supports 展开到各类型下
    assert {"file", "object_storage", "vm"} <= types, types
    for r in cov["rows"]:
        assert r["total"] == len(r["plugins"])
        assert 0 <= r["installed"] <= r["total"]


def test_bundle_dedupes_plugins_for_tasks():
    plan = plugin_policy.bundle_for_tasks([152, 215])
    ids = [p["id"] for p in plan["plugins"]]
    assert len(ids) == len(set(ids)), "同一插件不应重复出现"
    assert plan["count"] == len(ids)
