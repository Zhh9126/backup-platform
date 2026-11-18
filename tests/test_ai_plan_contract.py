# -*- coding: utf-8 -*-
"""AI 保护计划契约回归：异步运行、编辑定位、资产任务入口、层定义一致。

这些点都是 2026-11-14 用户反馈后修复的（"怎么先选择已有的备份任务，逻辑有问题"）：
- 运行改异步 202 + 并发护栏，前端轮询报告，不再挂断 HTTP；
- 编辑按 id 定位，不再按计划名反查（改名会改错计划）；
- 弹窗内可直接创建资产备份任务，不必先去别的页面建任务。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def test_run_endpoint_is_async():
    src = _read("api/ai_backup.py")
    assert 202 in [202] or "202" in src, "运行端点应返回 202（异步受理）"
    assert "_RUNNING" in src and "_RUN_LOCK" in src, "缺少并发护栏"
    assert "threading.Thread" in src, "运行应放到后台线程"
    # 不应在请求线程里同步调用 run_plan
    m = re.search(r"def run_ai_plan.*?(?=\n@api_bp|\Z)", src, re.S)
    assert m, "未找到 run_ai_plan"
    body = m.group(0)
    assert "ai_protection.run_plan(plan_id" in body
    assert "threading.Thread" in body


def test_frontend_edits_by_id_not_name():
    src = _read("static/js/app.js")
    assert "AI_EDITING_ID" in src, "前端应以 id 记录正在编辑的计划"
    assert "p.name === payload.name" not in src, "不得再按计划名反查（改名会改错计划）"


def test_frontend_has_task_cache_and_quick_create():
    src = _read("static/js/app.js")
    assert "loadAiTasks" in src, "任务列表应缓存一次加载"
    assert "aiQuickCreateAssetTask" in src, "缺少弹窗内新建资产任务"
    assert "AI_LAYER_HINT" in src, "缺少按库型推荐状态层"
    html = _read("templates/ai_backup.html")
    for token in ("aiNoTaskTip", "aiQuickCreate", "ai_q_paths", "aiQuickCreateAssetTask"):
        assert token in html, "页面缺少 %s" % token


def test_layers_match_between_backend_and_frontend():
    core = _read("core/ai_protection.py")
    block = re.search(r"LAYERS = \[(.*?)\]", core, re.S).group(1)
    backend_keys = re.findall(r'\("([a-z_]+)",\s*"', block)
    js = _read("static/js/app.js")
    jsblock = re.search(r"const AI_LAYERS = \[(.*?)\];", js, re.S).group(1)
    front_keys = re.findall(r'\["([a-z_]+)",\s*"', jsblock)
    assert backend_keys == front_keys, (backend_keys, front_keys)
    assert len(backend_keys) == 7


def test_run_result_written_back():
    core = _read("core/ai_protection.py")
    assert "last_result=?" in core, "运行结果需写回计划（前端轮询读取）"
    assert "consistency" in core
