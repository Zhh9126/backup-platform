# -*- coding: utf-8 -*-
"""AI 助手 list-body Bug 修复 — 端到端验证脚本。

通过 Flask test_client 走完整 HTTP 栈：
  /login → /api/agent/sessions → /api/agent/chat
并 monkeypatch 模型客户端，使其返回确定性的 list_tasks 工具调用，
从而触发真实 executor → GET /api/tasks → 返回 JSON 数组（裸 list）→
修复后包装为 dict → 不再抛 'list' object has no attribute 'get'。

验证点：
  - chat 返回 ok=True / type=answer，不含 "处理消息时出错"
  - tool_trace 中包含 list_tasks，且其 result 为 dict（含 is_collection）
  - 整个链路不崩溃
"""

import os
import sys
import json

os.environ["DEMO_MODE"] = "on"
os.environ.setdefault("INSTANCE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance_e2e"))
os.environ.setdefault("LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs_e2e"))
os.environ.setdefault("BACKUP_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups_e2e"))
os.environ.setdefault("META_DB_PATH", os.path.join(os.environ["INSTANCE_DIR"], "meta.db"))
os.environ.setdefault("SCHEDULER_ENABLED", "false")
for d in (os.environ["INSTANCE_DIR"], os.environ["LOG_DIR"], os.environ["BACKUP_ROOT"]):
    os.makedirs(d, exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                                  # noqa: E402
from app import create_app                     # noqa: E402
from core.ai_agent.agent import get_agent      # noqa: E402
from core.ai_alert import AIPredictor           # noqa: E402
from core.ai_agent.executor import ToolExecutor  # noqa: E402


def _stub_predictor():
    """在类级别替换 AIPredictor 的方法（get_agent() 每次返回新实例，
    因此必须 patch 类方法才能覆盖路由实际使用的实例）。

    让模型返回确定性的 list_tasks 工具调用 → 触发真实 executor。
    同时把 ToolExecutor.execute 替换为返回「裸 list」（精确复现修复前
    /api/tasks 返回 JSON 数组、_call_get 原样返回裸 list 的现场），
    验证 _react_loop 的防御性归一化 + 包装逻辑协作后链路不崩溃。
    """
    calls = {
        "first": json.dumps({
            "type": "tool_call", "tool": "list_tasks", "args": {}
        }),
        "second": json.dumps({
            "type": "answer", "content": "已为你列出所有备份任务。"
        }),
    }
    seq = {"n": 0}

    def fake_call_model(self, prompt, cfg, max_tokens=None):
        idx = "first" if seq["n"] == 0 else "second"
        seq["n"] += 1
        return {
            "ok": True,
            "status_code": 200,
            "latency_ms": 1,
            "response_body": json.dumps({
                "choices": [{"message": {"content": calls[idx]}, "finish_reason": "stop"}]
            }),
        }

    AIPredictor._call_model = fake_call_model
    AIPredictor.get_config = lambda self: {
        "ai_model": {"enabled": True, "provider": "mock"}
    }

    # 模拟 /api/tasks 端点返回裸 list（修复前 _call_get 直接 json.loads 返回 list）。
    # 保留真实 execute()，让其内部的 _call_get 走「裸 list → 包装为 dict」的修复逻辑，
    # 从而精确验证修复后 is_collection=True、且 _react_loop 不再对 list 调 .get。
    raw_list = [
        {"id": 1, "name": "每日全量备份", "status": "done"},
        {"id": 2, "name": "每周增量备份", "status": "running"},
    ]

    # 仅 monkeypatch urllib.request.urlopen：让「真实的」_call_get 拿到 list 响应体，
    # 从而真正执行修复后的 json.loads → 裸 list → 包装为 {ok,data,is_collection} 逻辑。
    import urllib.request as _urllib_req

    list_body = json.dumps(raw_list).encode("utf-8")

    class _FakeResp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return list_body

    _orig_urlopen = _urllib_req.urlopen

    def fake_urlopen(req, timeout=None):
        # 模型调用已被 AIPredictor._call_model 桩替换，不会走到这里；
        # 此处仅 executor._call_get 使用，返回 list 响应体。
        return _FakeResp()

    _urllib_req.urlopen = fake_urlopen


def main():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # 1) 登录 admin/admin123
    r = client.post("/login", json={"username": config.WEB_USERNAME, "password": config.WEB_PASSWORD},
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.get_data(as_text=True)}"
    assert r.get_json().get("ok") is True, "login not ok"
    print("[OK] 登录成功 admin/admin123")

    # 2) 创建会话
    r = client.post("/api/agent/sessions", json={"title": "e2e-bugfix"})
    assert r.status_code == 200, f"create session failed: {r.status_code}"
    body = r.get_json()
    assert body.get("ok") and body.get("session"), "create session body invalid"
    sid = body["session"]["id"]
    print(f"[OK] 创建会话 sid={sid}")

    # 3) monkeypatch 模型（确定性 tool_call）
    _stub_predictor()

    # 4) 聊天：触发 list_tasks → 真实 GET /api/tasks 返回数组
    r = client.post("/api/agent/chat",
                    json={"session_id": sid, "message": "请列出所有备份任务"},
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200, f"chat failed: {r.status_code}"
    result = r.get_json()

    # 5) 断言
    assert "处理消息时出错" not in json.dumps(result, ensure_ascii=False), \
        "出现 '处理消息时出错'，list 崩溃未修复！"
    assert result.get("ok") is True, f"chat ok!=True: {result}"
    assert result.get("type") == "answer", f"type!=answer: {result}"

    traced = [t for t in result.get("tool_trace", []) if t.get("name") == "list_tasks"]
    assert traced, f"tool_trace 未记录 list_tasks: {result.get('tool_trace')}"
    traced_result = traced[0].get("result", {})
    assert isinstance(traced_result, dict), f"list_tasks result 非 dict（裸 list 崩溃复现）: {traced_result!r}"
    assert traced_result.get("is_collection") is True, \
        f"list_tasks 结果未标记为集合: {traced_result!r}"

    print(f"[OK] chat 返回 ok=True, type=answer，无 '处理消息时出错'")
    print(f"[OK] tool_trace 记录 list_tasks，result 为 dict（is_collection={traced_result.get('is_collection')}）")
    print(f"     list_tasks 返回数据条数: {len(traced_result.get('data', []))}")
    print("\nE2E RESULT: PASS")


if __name__ == "__main__":
    main()
