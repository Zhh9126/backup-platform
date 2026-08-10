# -*- coding: utf-8 -*-
"""QA 独立验证：_parse_response 截断容错边界测试。

验收铁律：任何情况下返回给用户的 content 都不得包含 ```json 标记
或 {"type": "answer" 这类结构噪声。
"""
import os
import sys

os.environ.setdefault("DEMO_MODE", "on")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.ai_agent.agent import AIAgent  # noqa: E402
from core.ai_agent.tools import create_default_registry  # noqa: E402
from core.ai_agent.executor import ToolExecutor  # noqa: E402
from core.ai_agent.session import SessionManager  # noqa: E402
from core.ai_alert import AIPredictor  # noqa: E402

agent = AIAgent(AIPredictor(), ToolExecutor(create_default_registry()),
                SessionManager())

# 结构噪声特征（出现在 content 中即判定泄漏）
NOISE_MARKERS = [
    "```json", '{"type"', '{ "type"', '"type": "answer"', '"type":"answer"',
    '"content":', '"content" :', "```JSON",
]

ROWS = []


def probe(name, text, expect_type=None, expect_content=None,
          allow_noise=False, note=""):
    """跑一个场景并检查噪声泄漏。"""
    try:
        out = agent._parse_response(text)
    except Exception as e:  # noqa: BLE001
        ROWS.append((name, "EXCEPTION", f"{type(e).__name__}: {e}",
                     "**崩溃**", note))
        return None

    content = out.get("content", "") if isinstance(out, dict) else str(out)
    leaked = []
    if not allow_noise and isinstance(content, str):
        for m in NOISE_MARKERS:
            if m in content:
                leaked.append(m)

    verdict = "泄漏:" + ",".join(leaked) if leaked else "干净"
    if expect_type and out.get("type") != expect_type:
        verdict += f" | 类型不符(得到 {out.get('type')})"
    if expect_content is not None and content != expect_content:
        verdict += f" | 内容不符"

    disp = content if isinstance(content, str) else repr(content)
    if len(disp) > 90:
        disp = disp[:90] + "…"
    ROWS.append((name, out.get("type"), disp, verdict, note))
    return out


# ---------------- 场景集 ----------------
print("运行截断边界场景...\n")

# 1. 未闭合围栏 + 完整 JSON
probe("1 未闭合围栏+完整JSON",
      '```json\n{"type": "answer", "content": "共有 14 个备份任务。"}')

# 2. 未闭合围栏 + content 中间截断
probe("2 未闭合围栏+content中间截断",
      '```json\n{"type": "answer", "content": "共有 14 个备份任务：\\n1. 每日全量\\n2. 增量备')

# 3. 未闭合围栏 + "content": 之前就截断
probe("3 截断在 content 字段名之前",
      '```json\n{"type": "ans')

# 3b. 截断在 content 键名生成一半
probe("3b 截断在 content 键名中间",
      '```json\n{"type": "answer", "cont')

# 3c. 只有 {"type": "answer",
probe("3c 只有 type 字段",
      '```json\n{"type": "answer",')

# 4. 闭合围栏 + 正常 JSON（回归）
probe("4 闭合围栏+正常JSON（回归）",
      '```json\n{"type": "answer", "content": "一切正常。"}\n```',
      expect_type="answer", expect_content="一切正常。")

# 5. 纯文本无围栏（回归）
probe("5 纯文本无围栏（回归）", "你好，我是备份助手。",
      expect_type="answer", expect_content="你好，我是备份助手。")

# 6. 转义字符还原
probe("6 含 \\n \\\" \\\\ 转义",
      '```json\n{"type": "answer", "content": "第一行\\n第二行 \\"引号\\" 反斜杠\\\\ 结束"}\n```',
      expect_type="answer",
      expect_content='第一行\n第二行 "引号" 反斜杠\\ 结束')

# 6b. 转义 + 截断
probe("6b 转义+未闭合截断",
      '```json\n{"type": "answer", "content": "第一行\\n第二行 \\"引号\\" 未完')

# 7. 中文 + \\uXXXX 尾部残缺
probe("7 \\uXXXX 尾部残缺",
      '```json\n{"type": "answer", "content": "备份任务\\u4e2d\\u65')

# 7b. 转义符本身被截断（尾部单个反斜杠）
probe("7b 尾部残缺反斜杠",
      '```json\n{"type": "answer", "content": "备份任务列表\\\\')

# 8. 空字符串 / 只有 ```json / 只有 {
probe("8a 空字符串", "", expect_type="answer", expect_content="")
probe("8b 只有 ```json", "```json")
probe("8c 只有 {", "{")
probe("8d 只有 ```json\\n{", "```json\n{")
probe("8e 纯空白", "   \n  ")

# 9. ```sql 非 JSON 围栏（不应被误伤）
probe("9a ```sql 围栏（闭合）",
      "```sql\nSELECT * FROM backup_tasks;\n```",
      allow_noise=True, note="应原文保留")
probe("9b ```sql 围栏（未闭合）",
      "```sql\nSELECT * FROM backup_tasks;",
      allow_noise=True, note="应原文保留")
probe("9c ```python 围栏",
      "```python\nprint('hi')\n```", allow_noise=True, note="应原文保留")

# 10. content 内嵌 Markdown 围栏（嵌套围栏）
probe("10a content 含嵌套 ``` 围栏",
      '```json\n{"type": "answer", "content": "示例：\\n```sql\\nSELECT 1\\n```\\n以上。"}\n```',
      note="内层围栏不应骗过解析")
probe("10b content 含嵌套围栏+未闭合外层",
      '```json\n{"type": "answer", "content": "示例：\\n```sql\\nSELECT 1\\n```\\n以上。"}',
      note="内层围栏不应骗过解析")

# 11. 其他真实形态
probe("11a 无围栏裸 JSON 截断",
      '{"type": "answer", "content": "备份任务共 14 个，其中')
probe("11b 前置说明文字+围栏",
      '好的，我来回答：\n```json\n{"type": "answer", "content": "共 14 个任务。"}\n```')
probe("11c tool_call 正常（回归）",
      '```json\n{"type": "tool_call", "tool": "list_tasks", "args": {}}\n```',
      expect_type="tool_call")
probe("11d confirm_required 正常（回归）",
      '```json\n{"type": "confirm_required", "tool": "del", "args": {}, "reason": "危险"}\n```',
      expect_type="confirm_required")
probe("11e tool_call 被截断",
      '```json\n{"type": "tool_call", "tool": "list_ta')

# 12. content 里本身含 ```json 文本（模型解释格式时）
probe("12 content 内含 ```json 字面量",
      '```json\n{"type": "answer", "content": "你应该这样输出：```json 加大括号"}\n```',
      allow_noise=True, note="content 原样含 ```json 属正常语义")

# 13. 前置诱饵 JSON + 真实 answer（格式3 只取首个匹配）
probe("13 诱饵JSON+真answer",
      '第一段 {"foo": 1} 然后真正的答案 '
      '{"type": "answer", "content": "真答案"}',
      note="格式3 首个匹配失配后落纯文本兜底")
probe("13b 诱饵JSON+真answer(围栏)",
      '说明 {"a": 1} \n```json\n{"type": "answer", "content": "真答案"}\n```',
      note="对照：有围栏时应正常")

# 14. _extract_answer_from_llm（confirm_execute 后路径）
import json as _json  # noqa: E402


def _mk(c):
    return {"ok": True, "response_body": _json.dumps(
        {"choices": [{"message": {"content": c}}]})}


for _name, _text in [
    ("14a extract_answer/tool_call闭合",
     '```json\n{"type": "tool_call", "tool": "list_tasks", "args": {}}\n```'),
    ("14b extract_answer/tool_call未闭合",
     '```json\n{"type": "tool_call", "tool": "list_tasks", "args": {}}'),
    ("14c extract_answer/confirm_required",
     '```json\n{"type": "confirm_required", "tool": "d", "args": {}, '
     '"reason": "x"}\n```'),
    ("14d extract_answer/answer正常",
     '```json\n{"type": "answer", "content": "完成了"}\n```'),
]:
    _out = agent._extract_answer_from_llm(_mk(_text))
    _leaked = [m for m in NOISE_MARKERS if m in _out]
    _disp = _out if len(_out) <= 90 else _out[:90] + "…"
    ROWS.append((_name, "str",
                 _disp.replace("\n", "\\n"),
                 ("泄漏:" + ",".join(_leaked)) if _leaked else "干净",
                 "confirm_execute 后路径"))

# ---------------- 输出表格 ----------------
print(f"{'场景':<34}{'type':<18}{'content 预览':<48}{'判定'}")
print("-" * 130)
leaks = 0
crashes = 0
for name, typ, disp, verdict, note in ROWS:
    if "泄漏" in verdict:
        leaks += 1
    if "崩溃" in verdict:
        crashes += 1
    print(f"{name:<34}{str(typ):<18}{disp:<48}{verdict}  {note}")

print("-" * 130)
print(f"总场景 {len(ROWS)}，噪声泄漏 {leaks}，崩溃 {crashes}")
sys.exit(1 if (leaks or crashes) else 0)
