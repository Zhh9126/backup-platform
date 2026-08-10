# -*- coding: utf-8 -*-
"""QA 关键实验：max_tokens 1024 vs 4096 对上游延迟的影响。

判定「遗留问题1（30s 超时）」是否为本次修复引入的回归。
用同一条真实对话 prompt，仅改 max_tokens，各跑 N 次比较延迟/超时率。
"""
import os
import sys
import time

os.environ.setdefault("DEMO_MODE", "on")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.ai_alert import AIPredictor  # noqa: E402
from core.ai_agent.agent import AIAgent  # noqa: E402
from core.ai_agent.tools import create_default_registry  # noqa: E402
from core.ai_agent.executor import ToolExecutor  # noqa: E402
from core.ai_agent.session import SessionManager  # noqa: E402

N = 3
agent = AIAgent(AIPredictor(), ToolExecutor(create_default_registry()),
                SessionManager())
predictor = agent.predictor
cfg = predictor.get_config()

if not cfg.get("ai_model", {}).get("enabled"):
    print("模型未启用，无法做真实延迟实验")
    sys.exit(2)

print(f"endpoint={cfg['ai_model'].get('endpoint')}")
print(f"model={cfg['ai_model'].get('model_name')}")
print(f"request_timeout_sec={cfg['ai_model'].get('request_timeout_sec')}\n")

# 构造与线上第二轮完全同构的 prompt：系统提示 + 用户问题 + 工具结果
TASKS = [
    {"id": i, "name": f"备份任务-{i}", "type": ["full", "incr", "diff"][i % 3],
     "schedule": "0 2 * * *", "enabled": True,
     "target": f"minio-bucket-{i}", "retention_days": 30,
     "last_status": "success", "description": f"这是第 {i} 个备份任务的说明文字"}
    for i in range(1, 15)
]
import json  # noqa: E402
tool_result = json.dumps({"ok": True, "data": TASKS, "is_collection": True},
                         ensure_ascii=False)

messages = [
    {"role": "system", "content": agent._build_system_prompt()},
    {"role": "user", "content": "请列出所有备份任务"},
    {"role": "assistant", "content": '```json\n{"type": "tool_call", '
                                     '"tool": "list_tasks", "args": {}}\n```'},
    {"role": "tool", "name": "list_tasks", "content": tool_result},
]

prompt_parts = []
for m in messages:
    r = m["role"]
    if r == "system":
        prompt_parts.append(f"[系统指令]\n{m['content']}")
    elif r == "user":
        prompt_parts.append(f"[用户]\n{m['content']}")
    elif r == "assistant":
        prompt_parts.append(f"[助手]\n{m['content']}")
    elif r == "tool":
        prompt_parts.append(f"[工具结果-{m['name']}]\n{m['content']}")
prompt = "\n\n".join(prompt_parts)
print(f"prompt 长度 = {len(prompt)} 字符\n")

results = {}
for mt in (1024, 4096):
    print(f"=== max_tokens = {mt} ===")
    lat, to, okc = [], 0, 0
    for i in range(1, N + 1):
        t0 = time.time()
        r = predictor._call_model(prompt, cfg, max_tokens=mt)
        dt = round(time.time() - t0, 1)
        if r.get("ok"):
            okc += 1
            body = r.get("response_body", "")
            try:
                content = (json.loads(body)["choices"][0]["message"]["content"])
            except Exception:  # noqa: BLE001
                content = ""
            fin = ""
            try:
                fin = json.loads(body)["choices"][0].get("finish_reason", "")
            except Exception:  # noqa: BLE001
                pass
            lat.append(dt)
            closed = content.count("```") % 2 == 0
            print(f"  [{i}] {dt}s OK  输出={len(content)}字符 "
                  f"finish={fin} 围栏闭合={closed}")
        else:
            if "超时" in str(r.get("error", "")) or "timed out" in str(r.get("error", "")):
                to += 1
                print(f"  [{i}] {dt}s **超时**")
            else:
                print(f"  [{i}] {dt}s 失败: {r.get('error')}")
        time.sleep(1)
    results[mt] = (lat, to, okc)
    if lat:
        print(f"  → 成功 {okc}/{N}, 超时 {to}, "
              f"延迟 min={min(lat)} max={max(lat)} "
              f"avg={round(sum(lat)/len(lat),1)}")
    else:
        print(f"  → 成功 {okc}/{N}, 超时 {to}")
    print()

print("=" * 60)
for mt, (lat, to, okc) in results.items():
    avg = round(sum(lat) / len(lat), 1) if lat else "N/A"
    print(f"max_tokens={mt:<5} 成功={okc}/{N} 超时={to} 平均延迟={avg}s")
