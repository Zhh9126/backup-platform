# -*- coding: utf-8 -*-
"""QA 独立验证：max_tokens 零回归。

不依赖工程师编写的任何测试，直接在 HTTP 出口 (urllib.request.urlopen)
拦截真实请求体，断言：
  1. 预测告警路径（不传参）实际发出的 max_tokens == 1024
  2. 对话 Agent 路径 实际发出的 max_tokens == 4096
  3. 配置项异常值 0 / -1 / "abc" / None / 空串 / 浮点 安全回落
  4. 配置项合法值能正确覆盖默认
"""
import io
import json
import os
import sys
import urllib.request

os.environ.setdefault("DEMO_MODE", "on")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.db as db  # noqa: E402
from core.ai_alert import AIPredictor, DEFAULT_MODEL_MAX_TOKENS  # noqa: E402
from core.ai_agent.agent import AIAgent, AGENT_MAX_TOKENS  # noqa: E402
from core.ai_agent.tools import create_default_registry  # noqa: E402
from core.ai_agent.executor import ToolExecutor  # noqa: E402
from core.ai_agent.session import SessionManager  # noqa: E402

CAPTURED = []
RESULTS = []


class _FakeResp:
    """模拟 urlopen 返回的响应对象。"""
    status = 200

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": json.dumps(
                {"risk_score": 10, "risk_level": "low",
                 "predicted_content": "ok", "basis": []})}}]
        }).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    """拦截真实 HTTP 出口，记录请求体。"""
    body = req.data.decode("utf-8")
    CAPTURED.append(json.loads(body))
    return _FakeResp()


def check(name, actual, expected):
    ok = actual == expected
    RESULTS.append((name, actual, expected, ok))
    flag = "PASS" if ok else "**FAIL**"
    print(f"[{flag}] {name}: actual={actual!r} expected={expected!r}")
    return ok


def setup_cfg(extra_ai_model=None):
    """写入启用远程模型的配置（DEMO DB）。"""
    p = AIPredictor()
    ai_model = {
        "enabled": True,
        "provider": "openai",
        "endpoint": "http://qa-fake-endpoint.local/v1",
        "api_key": "qa-test-key-123456",
        "model_name": "qa-model",
        "request_timeout_sec": 30,
    }
    if extra_ai_model:
        ai_model.update(extra_ai_model)
    p.save_config({"enabled": True, "ai_model": ai_model})
    return p


def main():
    original = db.get_system_config("ai_alert_config")
    urllib.request.urlopen = _fake_urlopen
    try:
        # ---------- 1. 预测告警路径：不传 max_tokens ----------
        print("\n=== 1. 预测告警路径（_call_model 不传参）===")
        p = setup_cfg()
        cfg = p.get_config()
        CAPTURED.clear()
        p._call_model("qa prompt", cfg)
        check("alert/_call_model 无 override 的 max_tokens",
              CAPTURED[-1]["max_tokens"], DEFAULT_MODEL_MAX_TOKENS)
        check("alert/_call_model 常量值就是 1024", DEFAULT_MODEL_MAX_TOKENS, 1024)

        # ---------- 1b. 端到端 predict_with_ai ----------
        print("\n=== 1b. 端到端 predict_with_ai（真实告警入口）===")
        for metric in ("backup_fail", "storage_full",
                       "link_degraded", "drill_overdue"):
            CAPTURED.clear()
            p.predict_with_ai(metric)
            if CAPTURED:
                check(f"predict_with_ai({metric}) 出口 max_tokens",
                      CAPTURED[-1]["max_tokens"], 1024)
            else:
                print(f"[SKIP] predict_with_ai({metric}) 未发出 HTTP（规则引擎短路）")

        # ---------- 2. 对话 Agent 路径 ----------
        print("\n=== 2. 对话 Agent 路径（_call_model_with_messages）===")
        agent = AIAgent(AIPredictor(), ToolExecutor(create_default_registry()),
                        SessionManager())
        CAPTURED.clear()
        agent._call_model_with_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "请列出所有备份任务"},
        ])
        check("agent 出口 max_tokens", CAPTURED[-1]["max_tokens"], AGENT_MAX_TOKENS)
        check("AGENT_MAX_TOKENS 常量值", AGENT_MAX_TOKENS, 4096)

        # ---------- 3. 异常配置值回落 ----------
        print("\n=== 3. 配置项异常值安全回落（_resolve_max_tokens）===")
        for bad in (0, -1, "abc", None, "", [], {}, "0", -999, 3.7, True, "  "):
            got = AIPredictor._resolve_max_tokens(
                {"ai_model": {"max_tokens": bad}}, None)
            expect = 1024
            if bad == 3.7:
                expect = 3  # int(3.7)=3 >0 视为合法
            elif bad is True:
                expect = 1  # bool 是 int 子类，int(True)=1
            check(f"cfg.max_tokens={bad!r} 回落", got, expect)

        # 缺失 ai_model / cfg 为 None
        check("cfg=None", AIPredictor._resolve_max_tokens(None, None), 1024)
        check("cfg 无 ai_model", AIPredictor._resolve_max_tokens({}, None), 1024)
        check("ai_model 非 dict",
              AIPredictor._resolve_max_tokens({"ai_model": "x"}, None), 1024)

        # ---------- 3b. 异常值经过真实 HTTP 出口 ----------
        print("\n=== 3b. 异常配置值经真实出口验证 ===")
        for bad, expect in ((0, 1024), (-1, 1024), ("abc", 1024), (None, 1024)):
            p2 = setup_cfg({"max_tokens": bad})
            CAPTURED.clear()
            p2._call_model("qa", p2.get_config())
            check(f"出口 max_tokens (cfg={bad!r})",
                  CAPTURED[-1]["max_tokens"], expect)

        # ---------- 4. 合法配置值覆盖 + 调用方优先级 ----------
        print("\n=== 4. 优先级：调用方传参 > cfg > 默认 ===")
        p3 = setup_cfg({"max_tokens": 2048})
        CAPTURED.clear()
        p3._call_model("qa", p3.get_config())
        check("cfg=2048 生效", CAPTURED[-1]["max_tokens"], 2048)
        CAPTURED.clear()
        p3._call_model("qa", p3.get_config(), max_tokens=4096)
        check("override=4096 压过 cfg=2048", CAPTURED[-1]["max_tokens"], 4096)

        # 关键：cfg 设了异常值时，对话路径仍是 4096
        p4 = setup_cfg({"max_tokens": "abc"})
        agent2 = AIAgent(AIPredictor(), ToolExecutor(create_default_registry()),
                         SessionManager())
        CAPTURED.clear()
        agent2._call_model_with_messages([{"role": "user", "content": "hi"}])
        check("cfg 异常时对话路径仍 4096", CAPTURED[-1]["max_tokens"], 4096)

        # ---------- 5. 其他字段未被污染 ----------
        print("\n=== 5. 请求体其他字段零回归 ===")
        last = CAPTURED[-1]
        check("stream 仍为 False", last.get("stream"), False)
        check("temperature 仍为 0.2", last.get("temperature"), 0.2)
        check("请求体字段集合", sorted(last.keys()),
              sorted(["model", "messages", "temperature", "max_tokens", "stream"]))

    finally:
        if original is not None:
            db.set_system_config("ai_alert_config", original)

    print("\n" + "=" * 62)
    failed = [r for r in RESULTS if not r[3]]
    print(f"总计 {len(RESULTS)} 项断言, 通过 {len(RESULTS) - len(failed)}, 失败 {len(failed)}")
    for name, actual, expected, _ in failed:
        print(f"  FAIL {name}: actual={actual!r} expected={expected!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
