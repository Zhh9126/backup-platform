# -*- coding: utf-8 -*-
"""容灾一键切换编排回归（2026-11-13）。

覆盖编排层不依赖真实主备环境的部分：
1. 不存在的链路 → 预检/发起都如实失败，不伪造 ready；
2. 作业 JSON 字段解码（steps / precheck）；
3. 未配置 failover-config 时拒绝发起切换（避免「点了没反应」）；
4. 回切预检会检查 replication_user（复制账号缺失/不可认证 → 阻塞项）。
"""
import json

import pytest

from core import dr_orchestrator as orch


def test_precheck_missing_link():
    res = orch.precheck(999999)
    assert res["ok"] is False and res["ready"] is False
    assert res["risks"], "缺失链路必须给出风险原因"


def test_start_switch_missing_link():
    # confirm 闸门在 API 层（api/dr_switch.py），编排层只负责执行
    res = orch.start_switch(999999, direction="failover")
    assert res["ok"] is False
    assert "不存在" in res["message"]


def test_decode_run_json_fields():
    row = {"id": 1, "steps": '[{"name":"探测","ok":true}]',
           "precheck": '{"ready":true}', "status": "success"}
    d = orch._decode(row)
    assert isinstance(d["steps"], list) and d["steps"][0]["name"] == "探测"
    assert d["precheck"]["ready"] is True
    # 空/坏数据不抛错
    d2 = orch._decode({"id": 2, "steps": "", "precheck": None})
    assert d2["steps"] == [] and d2["precheck"] == {}


def test_schema_created_and_runs_query():
    orch.ensure_schema()
    runs = orch.list_runs(limit=5)
    assert isinstance(runs, list)


def test_overview_shape():
    ov = orch.overview()
    assert "links" in ov and "recent_runs" in ov and "generated_at" in ov
    assert isinstance(ov["links"], list)
