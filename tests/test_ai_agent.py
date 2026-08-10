# -*- coding: utf-8 -*-
"""
AI Agent 模块单元/集成测试。

运行方式（必须用系统 Python，DEMO_MODE=on）：
    SET DEMO_MODE=on
    python -m pytest tests/test_ai_agent.py -v

覆盖验收标准：
  1. Tool 注册/查找/导出
  2. 7 个工具参数 schema 验证
  3. Session CRUD
  4. Message CRUD
  5. LLM 解析（多种格式）
  6. 危险确认逻辑
  7. 端到端：模拟用户消息 → Agent 解析 → 返回结果
  8. Agent 核心流程（monkeypatch 避免真实 HTTP/LLM 调用）
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch, MagicMock

# ---------------- 0. 运行环境（必须在导入 config 之前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="ai_agent_")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "instance", "meta.db")
os.environ["SCHEDULER_ENABLED"] = "false"
os.makedirs(os.environ["INSTANCE_DIR"], exist_ok=True)
os.makedirs(os.environ["LOG_DIR"], exist_ok=True)
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402

db.init_schema()

import core.models as models                    # noqa: E402
from core.ai_agent.tools import Tool, ToolRegistry, TOOL_DEFINITIONS, create_default_registry   # noqa: E402
from core.ai_agent.executor import ToolExecutor    # noqa: E402
from core.ai_agent.session import SessionManager    # noqa: E402
from core.ai_agent.agent import (                              # noqa: E402
    AIAgent, _pending_confirms, AGENT_MAX_TOKENS, TRUNCATION_NOTICE)
from core.ai_alert import AIPredictor, DEFAULT_MODEL_MAX_TOKENS    # noqa: E402


# ============================ 1. Tool 注册/查找 ============================
class TestToolRegistry(unittest.TestCase):
    """验收 1：Tool 注册/查找/导出。"""

    def test_register_and_get(self):
        """注册工具后可以按名称查找。"""
        registry = ToolRegistry()
        tool = Tool(name="test_tool", description="测试", parameters={"type": "object"})
        registry.register(tool)
        found = registry.get("test_tool")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "test_tool")

    def test_get_nonexistent_returns_none(self):
        """查找不存在的工具返回 None。"""
        registry = ToolRegistry()
        self.assertIsNone(registry.get("nonexistent"))

    def test_register_overwrites(self):
        """重复注册会覆盖。"""
        registry = ToolRegistry()
        registry.register(Tool(name="t1", description="v1", parameters={"type": "object"}))
        registry.register(Tool(name="t1", description="v2", parameters={"type": "object"}))
        tool = registry.get("t1")
        self.assertEqual(tool.description, "v2")

    def test_list_all(self):
        """list_all 返回所有已注册工具。"""
        registry = ToolRegistry()
        registry.register(Tool(name="a", description="a", parameters={"type": "object"}))
        registry.register(Tool(name="b", description="b", parameters={"type": "object"}))
        all_tools = registry.list_all()
        self.assertEqual(len(all_tools), 2)
        names = {t.name for t in all_tools}
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_to_openai_tools_format(self):
        """导出为 OpenAI function calling 格式。"""
        registry = ToolRegistry()
        registry.register(Tool(
            name="list_tasks",
            description="列出所有备份任务",
            parameters={"type": "object", "properties": {"type": {"type": "string"}}},
        ))
        result = registry.to_openai_tools()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["function"]["name"], "list_tasks")
        self.assertIn("properties", result[0]["function"]["parameters"])

    def test_tools_description_for_prompt(self):
        """工具描述段生成包含名称、描述和参数。"""
        registry = create_default_registry()
        desc = registry.tools_description_for_prompt()
        self.assertIn("list_tasks", desc)
        self.assertIn("run_backup_task", desc)
        self.assertIn("⚠需确认", desc)

    def test_empty_registry(self):
        """空注册表 list_all 返回空列表。"""
        registry = ToolRegistry()
        self.assertEqual(registry.list_all(), [])
        self.assertEqual(registry.to_openai_tools(), [])


# ============================ 2. 7 个工具参数 schema ============================
class TestToolSchemas(unittest.TestCase):
    """验收 2：7 个 MVP 工具的参数 schema 验证。"""

    def _get_default_registry(self):
        return create_default_registry()

    def test_all_7_tools_registered(self):
        """默认注册表应包含 7 个工具。"""
        registry = self._get_default_registry()
        self.assertEqual(len(registry.list_all()), 7)

    def test_tool_names_match_prd(self):
        """7 个工具名与 PRD 定义一致。"""
        registry = self._get_default_registry()
        expected = [
            "run_backup_task", "run_inspection", "list_recent_records",
            "list_alert_predictions", "get_storage_usage", "list_tasks",
            "get_inspection_report",
        ]
        actual = {t.name for t in registry.list_all()}
        for name in expected:
            self.assertIn(name, actual, f"工具 '{name}' 应在注册表中")

    def test_run_backup_task_schema(self):
        """run_backup_task 有 required task_id。"""
        tool = self._get_default_registry().get("run_backup_task")
        self.assertTrue(tool.requires_confirm)
        self.assertIn("task_id", tool.parameters.get("required", []))
        self.assertEqual(tool.api_method, "POST")
        self.assertIn("{task_id}", tool.api_path)

    def test_run_inspection_schema(self):
        """run_inspection 有 scope enum 和 requires_confirm。"""
        tool = self._get_default_registry().get("run_inspection")
        self.assertTrue(tool.requires_confirm)
        scope_prop = tool.parameters.get("properties", {}).get("scope", {})
        self.assertIn("enum", scope_prop)
        self.assertEqual(scope_prop["enum"], ["quick", "full"])

    def test_list_recent_records_schema(self):
        """list_recent_records 有 limit default=20，不需确认。"""
        tool = self._get_default_registry().get("list_recent_records")
        self.assertFalse(tool.requires_confirm)
        limit_prop = tool.parameters.get("properties", {}).get("limit", {})
        self.assertEqual(limit_prop.get("default"), 20)

    def test_list_alert_predictions_schema(self):
        """list_alert_predictions 有 days default=7。"""
        tool = self._get_default_registry().get("list_alert_predictions")
        self.assertFalse(tool.requires_confirm)
        days_prop = tool.parameters.get("properties", {}).get("days", {})
        self.assertEqual(days_prop.get("default"), 7)

    def test_get_storage_usage_schema(self):
        """get_storage_usage 不需确认。"""
        tool = self._get_default_registry().get("get_storage_usage")
        self.assertFalse(tool.requires_confirm)
        self.assertEqual(tool.api_method, "GET")

    def test_list_tasks_schema(self):
        """list_tasks GET 方法，不需确认。"""
        tool = self._get_default_registry().get("list_tasks")
        self.assertFalse(tool.requires_confirm)
        self.assertEqual(tool.api_method, "GET")
        self.assertEqual(tool.api_path, "/api/tasks")

    def test_get_inspection_report_schema(self):
        """get_inspection_report GET 方法，不需确认。"""
        tool = self._get_default_registry().get("get_inspection_report")
        self.assertFalse(tool.requires_confirm)
        self.assertEqual(tool.api_method, "GET")

    def test_all_tools_have_executor(self):
        """所有工具都绑定了 executor 函数。"""
        registry = self._get_default_registry()
        for tool in registry.list_all():
            self.assertIsNotNone(tool.executor, f"工具 '{tool.name}' 应有 executor")


# ============================ 3. Session CRUD ============================
class TestSessionCRUD(unittest.TestCase):
    """验收 3：会话 CRUD。"""

    def setUp(self):
        self.sm = SessionManager()

    def test_create_session(self):
        """创建会话返回 session_id。"""
        sid = self.sm.create("测试会话")
        self.assertTrue(sid)
        session = self.sm.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["title"], "测试会话")

    def test_create_session_default_title(self):
        """默认标题为"新对话"。"""
        sid = self.sm.create()
        session = self.sm.get_session(sid)
        self.assertEqual(session["title"], "新对话")

    def test_list_sessions(self):
        """列出会话。"""
        sid1 = self.sm.create("会话1")
        sid2 = self.sm.create("会话2")
        sessions = self.sm.list_sessions()
        self.assertGreaterEqual(len(sessions), 2)
        ids = {s["id"] for s in sessions}
        self.assertIn(sid1, ids)
        self.assertIn(sid2, ids)

    def test_get_session_nonexistent(self):
        """获取不存在的会话返回 None。"""
        result = self.sm.get_session("nonexistent-id")
        self.assertIsNone(result)

    def test_delete_session(self):
        """删除会话后 get_session 返回 None。"""
        sid = self.sm.create("要删除的会话")
        self.sm.delete_session(sid)
        session = self.sm.get_session(sid)
        self.assertIsNone(session)

    def test_update_session_title(self):
        """更新会话标题。"""
        sid = self.sm.create("原标题")
        self.sm.update_session(sid, title="新标题")
        session = self.sm.get_session(sid)
        self.assertEqual(session["title"], "新标题")

    def test_update_session_updated_at(self):
        """更新 updated_at。"""
        sid = self.sm.create()
        now = db.now_iso()
        self.sm.update_session(sid, updated_at=now)
        session = self.sm.get_session(sid)
        self.assertEqual(session["updated_at"], now)


# ============================ 4. Message CRUD ============================
class TestMessageCRUD(unittest.TestCase):
    """验收 4：消息 CRUD。"""

    def setUp(self):
        self.sm = SessionManager()
        self.sid = self.sm.create("消息测试会话")

    def test_add_user_message(self):
        """添加用户消息。"""
        msg = self.sm.add_user_message(self.sid, "你好")
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"], "你好")

    def test_add_assistant_message(self):
        """添加助手消息。"""
        msg = self.sm.add_assistant_message(self.sid, "你好，有什么可以帮你？")
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "你好，有什么可以帮你？")

    def test_add_assistant_message_with_tool_calls(self):
        """添加含工具调用的助手消息。"""
        tool_calls = [{"name": "list_tasks", "args": {}}]
        msg = self.sm.add_assistant_message(self.sid, "正在查询...", tool_calls=tool_calls)
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["tool_calls"], tool_calls)

    def test_add_tool_message(self):
        """添加工具结果消息。"""
        result = {"tasks": [{"id": 1, "name": "任务1"}]}
        msg = self.sm.add_tool_message(self.sid, "list_tasks", result)
        self.assertEqual(msg["role"], "tool")
        self.assertEqual(msg["tool_name"], "list_tasks")
        self.assertEqual(msg["tool_result"], result)

    def test_get_history(self):
        """加载会话历史。"""
        self.sm.add_user_message(self.sid, "列出所有任务")
        self.sm.add_assistant_message(self.sid, "正在查询...", tool_calls=[{"name": "list_tasks", "args": {}}])
        self.sm.add_tool_message(self.sid, "list_tasks", {"tasks": []})
        self.sm.add_assistant_message(self.sid, "当前没有备份任务")

        history = self.sm.get_history(self.sid)
        self.assertGreaterEqual(len(history), 4)
        roles = [h["role"] for h in history]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)

    def test_get_history_limit(self):
        """历史滑窗截断。"""
        for i in range(30):
            self.sm.add_user_message(self.sid, f"消息{i}")

        history = self.sm.get_history(self.sid, max_messages=10)
        self.assertLessEqual(len(history), 10)

    def test_delete_session_removes_messages(self):
        """删除会话后消息也消失。"""
        self.sm.add_user_message(self.sid, "测试消息")
        self.sm.delete_session(self.sid)
        history = self.sm.get_history(self.sid)
        self.assertEqual(len(history), 0)

    def test_auto_title_from_first_message(self):
        """第一条消息自动更新会话标题（前 20 字）。"""
        sid = self.sm.create()
        long_msg = "这是一条很长的用户消息用来测试标题自动截断功能"
        self.sm.add_user_message(sid, long_msg)
        session = self.sm.get_session(sid)
        self.assertNotEqual(session["title"], "新对话")
        self.assertTrue(session["title"].startswith("这是一条很长的用户消息"))

    def test_build_messages_for_llm(self):
        """构建 LLM 输入消息列表包含 system prompt。"""
        self.sm.add_user_message(self.sid, "你好")
        system_prompt = "你是AI助手"
        messages = self.sm.build_messages_for_llm(self.sid, system_prompt, "")
        self.assertGreaterEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], system_prompt)


# ============================ 5. LLM 解析（多种格式） ============================
class TestLLMResponseParsing(unittest.TestCase):
    """验收 5：LLM 输出解析支持多种格式。"""

    def setUp(self):
        registry = create_default_registry()
        executor = ToolExecutor(registry)
        predictor = MagicMock()
        sm = SessionManager()
        self.agent = AIAgent(predictor, executor, sm)

    def test_parse_json_code_block_answer(self):
        """解析 ```json {"type": "answer"} ``` 格式。"""
        text = '```json\n{"type": "answer", "content": "RPO是恢复点目标"}\n```'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "RPO是恢复点目标")

    def test_parse_json_code_block_tool_call(self):
        """解析 ```json {"type": "tool_call"} ``` 格式。"""
        text = '```json\n{"type": "tool_call", "tool": "list_tasks", "args": {}}\n```'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_tasks")

    def test_parse_json_code_block_confirm_required(self):
        """解析 ```json {"type": "confirm_required"} ``` 格式。"""
        text = '```json\n{"type": "confirm_required", "tool": "run_inspection", "args": {"scope": "full"}, "reason": "全量巡检影响性能"}\n```'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "confirm_required")
        self.assertEqual(parsed["tool"], "run_inspection")
        self.assertEqual(parsed["reason"], "全量巡检影响性能")

    def test_parse_pure_json_answer(self):
        """解析纯 JSON（无 code block）格式。"""
        text = '{"type": "answer", "content": "备份是将数据复制到另一个位置"}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "备份是将数据复制到另一个位置")

    def test_parse_pure_json_tool_call(self):
        """解析纯 JSON tool_call。"""
        text = '{"type": "tool_call", "tool": "list_recent_records", "args": {"limit": 10}}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_recent_records")
        self.assertEqual(parsed["args"]["limit"], 10)

    def test_parse_react_action_format(self):
        """解析 ReAct Action: X / Action Input: {...} 格式。"""
        text = 'Action: list_tasks\nAction Input: {"type": "mysql"}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_tasks")
        self.assertEqual(parsed["args"]["type"], "mysql")

    def test_parse_react_action_nonjson_input(self):
        """解析 ReAct 格式但 Action Input 不是 JSON。"""
        text = 'Action: list_tasks\nAction Input: some plain text'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_tasks")
        self.assertIn("raw_input", parsed["args"])

    def test_parse_embedded_json_in_text(self):
        """解析文本中嵌入的 JSON 片段。"""
        text = '根据您的需求，我来查询备份记录：{"type": "tool_call", "tool": "list_recent_records", "args": {"limit": 5}}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_recent_records")

    def test_parse_plain_text_as_answer(self):
        """无法解析时视为纯文本回答。"""
        text = "RPO是恢复点目标，指灾难发生后允许丢失的数据量时间窗口。"
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], text)

    def test_parse_empty_text(self):
        """空文本返回空回答。"""
        parsed = self.agent._parse_response("")
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "")

    def test_parse_json_without_type_but_with_tool(self):
        """JSON 无 type 字段但有 tool/args → 视为 tool_call。"""
        text = '{"tool": "list_tasks", "args": {"enabled": "1"}}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_tasks")

    def test_parse_json_without_type_but_with_content(self):
        """JSON 无 type 字段但有 content → 视为 answer。"""
        text = '{"content": "这是一个关于备份的问题"}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "这是一个关于备份的问题")


# ================= 5b. LLM 输出截断容错（围栏泄漏缺陷回归） =================
class TestTruncatedResponseParsing(unittest.TestCase):
    """回归：模型输出被 max_tokens 截断时，绝不能把裸 JSON / ``` 围栏甩给用户。

    缺陷现场：`_call_model` 硬编码 max_tokens=1024，对话场景把 14 个备份任务
    塞进 JSON content 时被截断 → 围栏不闭合、JSON 不完整 → `_parse_response`
    所有分支失配 → 原样返回 '```json\\n{"type": "answer", "content": "…'。
    """

    def setUp(self):
        registry = create_default_registry()
        executor = ToolExecutor(registry)
        predictor = MagicMock()
        sm = SessionManager()
        self.agent = AIAgent(predictor, executor, sm)

    # ---- 用例 1：未闭合围栏 + 完整 JSON ----
    def test_unclosed_fence_with_complete_json(self):
        """未闭合围栏但内部 JSON 完整 → 正常解析出 content，无围栏残留。"""
        text = '```json\n{"type": "answer", "content": "当前系统共有 14 个备份任务"}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "当前系统共有 14 个备份任务")
        self.assertNotIn("```", parsed["content"])

    def test_unclosed_fence_with_complete_json_tool_call(self):
        """未闭合围栏 + 完整 tool_call JSON → 仍能识别为工具调用。"""
        text = '```json\n{"type": "tool_call", "tool": "list_tasks", "args": {}}'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "list_tasks")

    # ---- 用例 2：未闭合围栏 + 截断 JSON（提取部分 content） ----
    def test_unclosed_fence_with_truncated_json(self):
        """未闭合围栏 + 截断 JSON → 提取已生成的 content 部分并提示截断。"""
        text = (
            '```json\n{"type": "answer", "content": "当前系统共有 14 个备份任务：\\n'
            '1. ID: 10 | 名称: 日常全量 | 类型: 全量\\n'
            '12. ID: 21 | 名称: 123 | 类型: 全量 | 状态: 启用 | 最后状态: success |'
        )
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        content = parsed["content"]
        # 不得泄漏围栏标记与 JSON 结构噪声
        self.assertNotIn("```", content)
        self.assertNotIn('"type"', content)
        self.assertNotIn('"content"', content)
        # 已生成的正文要保留下来
        self.assertIn("当前系统共有 14 个备份任务", content)
        self.assertIn("日常全量", content)
        # JSON 转义 \n 必须还原成真实换行
        self.assertIn("\n", content)
        self.assertNotIn("\\n", content)
        # 截断需要如实告知用户
        self.assertIn("截断", content)

    def test_truncated_json_unescapes_quotes_and_backslash(self):
        r"""截断内容里的 \" 与 \\ 转义要正确还原。"""
        text = '```json\n{"type": "answer", "content": "任务 \\"主库\\" 路径 C:\\\\data\\n未完'
        parsed = self.agent._parse_response(text)
        content = parsed["content"]
        self.assertIn('任务 "主库"', content)
        self.assertIn("C:\\data", content)
        self.assertNotIn('\\"', content)

    def test_truncated_json_without_fence(self):
        """无围栏的裸截断 JSON → 同样要剥掉结构噪声。"""
        text = '{"type": "answer", "content": "备份任务列表：\\n1. 日常全量'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertIn("备份任务列表", parsed["content"])
        self.assertNotIn('"type"', parsed["content"])

    def test_truncated_before_content_value_falls_back(self):
        """截断发生在 content 值之前 → 返回兜底提示，绝不返回 JSON 骨架。"""
        text = '```json\n{"type": "answer", "conte'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertNotIn("```", parsed["content"])
        self.assertNotIn("{", parsed["content"])
        self.assertTrue(parsed["content"].strip())

    # ---- 用例 3：闭合围栏 + 正常 JSON（回归） ----
    def test_closed_fence_normal_json_regression(self):
        """回归：闭合围栏 + 完整 JSON 行为不变，且不追加截断提示。"""
        text = '```json\n{"type": "answer", "content": "RPO是恢复点目标"}\n```'
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], "RPO是恢复点目标")
        self.assertNotIn("截断", parsed["content"])

    def test_closed_fence_confirm_required_regression(self):
        """回归：闭合围栏 + confirm_required 解析不受影响。"""
        text = ('```json\n{"type": "confirm_required", "tool": "run_inspection", '
                '"args": {"scope": "full"}, "reason": "全量巡检影响性能"}\n```')
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "confirm_required")
        self.assertEqual(parsed["tool"], "run_inspection")

    # ---- 用例 4：纯文本无围栏（回归） ----
    def test_plain_text_no_fence_regression(self):
        """回归：纯文本回答原样返回，不被截断逻辑改写。"""
        text = "RPO是恢复点目标，指灾难发生后允许丢失的数据量时间窗口。"
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], text)

    def test_non_json_code_fence_preserved(self):
        """回归：非 JSON 代码围栏（如 ```sql）保持原文，供前端 Markdown 渲染。"""
        text = "可以这样查询：\n```sql\nSELECT * FROM tasks;\n```"
        parsed = self.agent._parse_response(text)
        self.assertEqual(parsed["type"], "answer")
        self.assertEqual(parsed["content"], text)

    def test_live_defect_sample_no_fence_leak(self):
        """线上 run2 缺陷样本：修复后 content 不得以 ``` 开头。"""
        text = (
            '```json\n{"type": "answer", "content": "当前系统共有 14 个备份任务，'
            '详细信息如下：\\n\\n1. ID: 8 | 名称: mysql-full | 类型: 全量 | 状态: 启用 '
            '| 最后状态: simulated | 最后执行: 2026-07-30 10:27:43 | 主机: 127.0.0.1\\n'
            '12. ID: 21 | 名称: 123 | 类型: 全量 | 状态: 启用 | 最后状态: success |'
        )
        parsed = self.agent._parse_response(text)
        self.assertFalse(parsed["content"].lstrip().startswith("```"),
                         "修复后 content 不应以围栏标记开头")
        self.assertIn("mysql-full", parsed["content"])


# ================= 5c. max_tokens 可配置（生成层修复） =================
class TestMaxTokensConfiguration(unittest.TestCase):
    """验收：对话 Agent 用更大的 max_tokens，AI 预测告警保持 1024 不变。"""

    def setUp(self):
        registry = create_default_registry()
        executor = ToolExecutor(registry)
        self.predictor = MagicMock()
        sm = SessionManager()
        self.agent = AIAgent(self.predictor, executor, sm)

    def test_default_max_tokens_is_1024(self):
        """无参数、无配置项时默认 1024（AI 预测告警既有行为）。"""
        self.assertEqual(DEFAULT_MODEL_MAX_TOKENS, 1024)
        self.assertEqual(AIPredictor._resolve_max_tokens({"ai_model": {}}), 1024)
        self.assertEqual(AIPredictor._resolve_max_tokens({}), 1024)
        self.assertEqual(AIPredictor._resolve_max_tokens(None), 1024)

    def test_caller_override_wins(self):
        """调用方显式传参优先级最高。"""
        self.assertEqual(
            AIPredictor._resolve_max_tokens({"ai_model": {"max_tokens": 2048}}, 4096), 4096)

    def test_config_value_used_when_no_override(self):
        """未传参时可从配置项读取。"""
        self.assertEqual(
            AIPredictor._resolve_max_tokens({"ai_model": {"max_tokens": 2048}}), 2048)

    def test_invalid_values_fall_back_to_default(self):
        """非法值（0/负数/非数字）回落默认 1024。"""
        for bad in (0, -5, "abc", "", [], {}):
            self.assertEqual(
                AIPredictor._resolve_max_tokens({"ai_model": {"max_tokens": bad}}), 1024,
                f"非法值 {bad!r} 应回落 1024")

    def test_agent_requests_larger_budget(self):
        """对话 Agent 调用 _call_model 时显式传入 AGENT_MAX_TOKENS。"""
        self.predictor.get_config.return_value = {"ai_model": {"enabled": True}}
        self.predictor._call_model.return_value = {"ok": True, "response_body": "{}"}

        self.agent._call_model_with_messages([{"role": "user", "content": "hi"}])

        self.assertTrue(self.predictor._call_model.called)
        _, kwargs = self.predictor._call_model.call_args
        self.assertEqual(kwargs.get("max_tokens"), AGENT_MAX_TOKENS)
        self.assertGreaterEqual(AGENT_MAX_TOKENS, 4096)

    def test_alert_path_request_body_still_1024(self):
        """无回归证据：预测告警路径实际发出的请求体 max_tokens 仍为 1024。"""
        captured = {}

        class _FakeResp:
            status = 200

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp()

        predictor = AIPredictor()
        cfg = {"ai_model": {"enabled": True, "provider": "openai",
                            "endpoint": "http://127.0.0.1:9/v1",
                            "model_name": "test", "api_key": "k"}}

        with patch.object(AIPredictor, "_get_model_uri",
                          return_value="http://127.0.0.1:9/v1/chat/completions"), \
                patch("urllib.request.urlopen", _fake_urlopen):
            # 预测告警调用方式：不传 max_tokens
            predictor._call_model("prompt", cfg)
        self.assertEqual(captured["body"]["max_tokens"], 1024)

        with patch.object(AIPredictor, "_get_model_uri",
                          return_value="http://127.0.0.1:9/v1/chat/completions"), \
                patch("urllib.request.urlopen", _fake_urlopen):
            # 对话 Agent 调用方式：显式传参
            predictor._call_model("prompt", cfg, max_tokens=AGENT_MAX_TOKENS)
        self.assertEqual(captured["body"]["max_tokens"], AGENT_MAX_TOKENS)


# ============================ 6. 危险确认逻辑 ============================
class TestDangerousConfirm(unittest.TestCase):
    """验收 6：危险操作确认拦截。"""

    def setUp(self):
        self.registry = create_default_registry()
        self.executor = ToolExecutor(self.registry)
        self.sm = SessionManager()

    def test_run_backup_task_requires_confirm(self):
        """run_backup_task 执行返回 needs_confirm。"""
        result = self.executor.execute("run_backup_task", {"task_id": "1"}, {})
        self.assertTrue(result.get("needs_confirm"))
        self.assertEqual(result["tool_name"], "run_backup_task")

    def test_run_inspection_requires_confirm(self):
        """run_inspection 执行返回 needs_confirm。"""
        result = self.executor.execute("run_inspection", {"scope": "full"}, {})
        self.assertTrue(result.get("needs_confirm"))
        self.assertEqual(result["tool_name"], "run_inspection")

    def test_list_tasks_no_confirm(self):
        """list_tasks 不需要确认，直接尝试执行（会因网络失败返回错误）。"""
        result = self.executor.execute("list_tasks", {}, {})
        # 由于内部 API 不可达，会返回网络错误，但不应有 needs_confirm
        self.assertFalse(result.get("needs_confirm", False))

    def test_confirm_reason_for_backup_task(self):
        """run_backup_task 的确认提示包含任务 ID。"""
        result = self.executor.execute("run_backup_task", {"task_id": "T-123"}, {})
        self.assertIn("T-123", result.get("message", ""))

    def test_confirm_reason_for_full_inspection(self):
        """全量巡检的确认提示包含'性能'。"""
        result = self.executor.execute("run_inspection", {"scope": "full"}, {})
        self.assertIn("性能", result.get("message", ""))

    def test_nonexistent_tool_returns_error(self):
        """执行不存在的工具返回错误。"""
        result = self.executor.execute("nonexistent_tool", {}, {})
        self.assertFalse(result.get("ok", True))
        self.assertIn("未注册", result.get("error", ""))

    def test_missing_required_param_returns_error(self):
        """缺少必填参数返回错误。"""
        result = self.executor.execute("run_backup_task", {}, {})
        # 由于 requires_confirm 先拦截，返回 needs_confirm
        # 但从 _validate_args 角度看
        validated = self.executor._validate_args(
            self.registry.get("run_backup_task"), {})
        self.assertIn("error", validated)
        self.assertIn("task_id", validated["error"])


# ============================ 7. 端到端模拟 ============================
class TestEndToEnd(unittest.TestCase):
    """验收 7：端到端模拟（monkeypatch 避免 HTTP/LLM 调用）。"""

    def setUp(self):
        self.registry = create_default_registry()
        self.executor = ToolExecutor(self.registry)
        self.sm = SessionManager()
        # 清除 pending confirms
        _pending_confirms.clear()

    def _make_agent_with_mock_llm(self, llm_responses):
        """创建 Agent 实例，mock LLM 返回指定响应列表。

        Args:
            llm_responses: list of str（LLM 返回的文本）
        """
        predictor = MagicMock()
        # 模拟 _call_model 返回成功响应
        call_results = []
        for resp_text in llm_responses:
            call_results.append({
                "ok": True,
                "status_code": 200,
                "latency_ms": 100,
                "response_body": json.dumps({
                    "choices": [{
                        "message": {"content": resp_text},
                        "finish_reason": "stop",
                    }],
                }),
            })

        predictor._call_model = MagicMock(side_effect=call_results)
        predictor.get_config = MagicMock(return_value={
            "ai_model": {
                "enabled": True,
                "provider": "mock",
                "endpoint": "http://mock",
            },
        })

        agent = AIAgent(predictor, self.executor, self.sm)
        return agent

    def test_pure_qa_flow(self):
        """纯问答：用户问"什么是RPO" → LLM 返回 answer → 直接返回。"""
        agent = self._make_agent_with_mock_llm([
            '{"type": "answer", "content": "RPO是恢复点目标"}',
        ])
        sid = self.sm.create()
        result = agent.chat(sid, "什么是RPO？")
        self.assertEqual(result["type"], "answer")
        self.assertEqual(result["content"], "RPO是恢复点目标")
        self.assertTrue(result.get("ok"))

    def test_tool_call_flow(self):
        """工具调用：用户说"列出所有任务" → LLM 返回 tool_call → 执行工具 → 再次调 LLM 综合回答。

        由于内部 API 不可达，monkeypatch executor 返回模拟数据。
        """
        # Mock executor.execute 返回模拟数据
        mock_result = {"ok": True, "tasks": [{"id": 1, "name": "任务1"}]}
        with patch.object(self.executor, 'execute', return_value=mock_result):
            agent = self._make_agent_with_mock_llm([
                '{"type": "tool_call", "tool": "list_tasks", "args": {}}',
                '{"type": "answer", "content": "当前有1个备份任务：任务1"}',
            ])
            sid = self.sm.create()
            result = agent.chat(sid, "列出所有任务")
            self.assertEqual(result["type"], "answer")
            self.assertIn("任务1", result.get("content", ""))
            self.assertTrue(result.get("ok"))

    def test_confirm_required_flow(self):
        """危险确认：用户说"跑一次巡检" → LLM 返回 confirm_required → 返回确认请求。"""
        agent = self._make_agent_with_mock_llm([
            '{"type": "confirm_required", "tool": "run_inspection", "args": {"scope": "full"}, "reason": "全量巡检影响性能"}',
        ])
        sid = self.sm.create()
        result = agent.chat(sid, "帮我跑一次全量巡检")
        self.assertEqual(result["type"], "confirm_required")
        self.assertIn("pending_confirm", result)
        self.assertEqual(result["pending_confirm"]["tool_name"], "run_inspection")

    def test_confirm_and_execute(self):
        """确认后执行：先 confirm_required → 用户确认 → 执行工具 → 综合回答。"""
        # Step 1: chat 返回 confirm_required
        agent = self._make_agent_with_mock_llm([
            '{"type": "confirm_required", "tool": "run_inspection", "args": {"scope": "full"}, "reason": "全量巡检影响性能"}',
        ])
        sid = self.sm.create()
        result = agent.chat(sid, "帮我跑一次全量巡检")
        self.assertEqual(result["type"], "confirm_required")
        tool_call_id = result["pending_confirm"]["tool_call_id"]

        # Step 2: 确认执行
        # 重新 mock LLM 和 executor
        mock_tool_result = {"ok": True, "report_id": "RPT-001", "summary": "巡检完成"}
        with patch.object(self.executor, '_call_get', return_value=mock_tool_result):
            # 重新 mock agent 的 LLM 返回（综合回答）
            agent.predictor._call_model = MagicMock(side_effect=[{
                "ok": True,
                "status_code": 200,
                "response_body": json.dumps({
                    "choices": [{"message": {"content": "全量巡检已完成，发现2个问题"}, "finish_reason": "stop"}],
                }),
            }])
            agent.predictor.get_config = MagicMock(return_value={
                "ai_model": {"enabled": True, "provider": "mock"},
            })
            confirm_result = agent.confirm_execute(sid, tool_call_id, approved=True)
            self.assertTrue(confirm_result.get("ok"))
            self.assertEqual(confirm_result["type"], "answer")
            self.assertIn("巡检", confirm_result.get("content", ""))

    def test_confirm_rejected(self):
        """用户拒绝确认 → 返回拒绝消息。"""
        agent = self._make_agent_with_mock_llm([
            '{"type": "confirm_required", "tool": "run_backup_task", "args": {"task_id": "1"}, "reason": "需要确认"}',
        ])
        sid = self.sm.create()
        result = agent.chat(sid, "跑一次备份")
        tool_call_id = result["pending_confirm"]["tool_call_id"]

        confirm_result = agent.confirm_execute(sid, tool_call_id, approved=False)
        self.assertEqual(confirm_result["type"], "rejected")
        self.assertIn("拒绝", confirm_result.get("content", ""))

    def test_confirm_invalid_id(self):
        """无效确认 ID → 返回错误。"""
        agent = self._make_agent_with_mock_llm([])
        result = agent.confirm_execute("nonexistent-session", "invalid-id", True)
        self.assertFalse(result.get("ok"))
        self.assertIn("不存在", result.get("error", ""))

    def test_llm_failure_graceful_degradation(self):
        """LLM 调用失败 → 返回错误消息（不抛异常）。"""
        predictor = MagicMock()
        predictor._call_model = MagicMock(return_value={"ok": False, "error": "网络超时"})
        predictor.get_config = MagicMock(return_value={"ai_model": {"enabled": True}})

        agent = AIAgent(predictor, self.executor, self.sm)
        sid = self.sm.create()
        result = agent.chat(sid, "你好")
        # LLM 失败 → _extract_content_from_llm_result 返回空 → 返回错误
        # 期望优雅降级：ok=False 且 type="error"，并携带提示内容（不抛异常）
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("type"), "error")
        self.assertTrue(result.get("content"))

    def test_ai_model_not_enabled(self):
        """AI 模型未启用 → _call_model_with_messages 返回错误。"""
        predictor = MagicMock()
        predictor.get_config = MagicMock(return_value={"ai_model": {"enabled": False}})

        agent = AIAgent(predictor, self.executor, self.sm)
        result = agent._call_model_with_messages([{"role": "user", "content": "test"}])
        self.assertFalse(result.get("ok"))
        self.assertIn("未启用", result.get("error", ""))

    def test_build_system_prompt_contains_tools(self):
        """system prompt 包含工具描述。"""
        agent = self._make_agent_with_mock_llm([])
        prompt = agent._build_system_prompt()
        self.assertIn("list_tasks", prompt)
        self.assertIn("可用工具", prompt)
        self.assertIn("输出格式", prompt)

    def test_session_nonexistent_returns_error(self):
        """chat 传入不存在的 session_id → LLM 调用前 session.get_session 返回 None，
        但 add_user_message 仍可创建消息（session_id 是独立 UUID）。"""
        # SessionManager.create 之外创建的 session_id 在 add_user_message 时会写入消息，
        # 但 get_session 会返回 None — 这里通过 API 层检查，
        # Agent 层 add_user_message 仍可写入（不依赖 FK 强约束）
        pass  # API 层会检查 session 是否存在

    def test_react_max_rounds_force_output(self):
        """ReAct 超过最大轮数强制输出。"""
        # 连续 3 次返回 tool_call
        agent = self._make_agent_with_mock_llm([
            '{"type": "tool_call", "tool": "list_tasks", "args": {}}',
            '{"type": "tool_call", "tool": "list_recent_records", "args": {}}',
            '{"type": "tool_call", "tool": "list_alert_predictions", "args": {}}',
        ])
        # Mock executor 返回数据（避免 HTTP 调用）
        mock_result = {"ok": True, "data": []}
        with patch.object(self.executor, 'execute', return_value=mock_result):
            sid = self.sm.create()
            result = agent.chat(sid, "查查所有信息")
            # 第 3 轮后强制输出
            self.assertEqual(result["type"], "answer")

    def test_list_tasks_returns_array_body_no_crash(self):
        """Bug 修复回归：工具端点返回 JSON 数组（裸 list）时不应崩溃。

        真实场景：/api/tasks 返回 jsonify(tasks) 即列表，
        _call_get 原样返回裸 list → exec_result.get(...) 抛
        'list' object has no attribute 'get'。修复后 executor 已包装为 dict，
        此处让真实 executor 走 _call_get（monkeypatch urlopen 返回数组），
        验证整条 ReAct 链路不再抛 list 错误且 ok=True。
        """
        # 直接让 executor 的 _call_get 返回裸 list，模拟修复前的端点行为，
        # 验证 _react_loop 的防御性归一化 + 包装逻辑协作后链路不崩溃。
        raw_list = [
            {"id": 1, "name": "备份任务A", "status": "done"},
            {"id": 2, "name": "备份任务B", "status": "running"},
        ]

        def fake_execute(tool_name, args, context):
            # 模拟端点返回裸 list（修复前 _call_get 的行为）
            return raw_list

        with patch.object(self.executor, 'execute', side_effect=fake_execute):
            agent = self._make_agent_with_mock_llm([
                '{"type": "tool_call", "tool": "list_tasks", "args": {}}',
                '{"type": "answer", "content": "共2个备份任务：A、B"}',
            ])
            sid = self.sm.create()
            result = agent.chat(sid, "请列出所有备份任务")
        # 不得抛异常、不得是错误、不得出现 list 报错信息
        self.assertTrue(result.get("ok"), msg=f"result={result}")
        self.assertEqual(result.get("type"), "answer")
        self.assertNotIn("list", result.get("content", ""))
        self.assertNotIn("attribute", result.get("content", ""))
        # 工具轨迹应记录 list_tasks 执行结果（已包装为 dict，含 is_collection）
        traced = [t for t in result.get("tool_trace", []) if t.get("name") == "list_tasks"]
        self.assertTrue(traced, msg="应记录 list_tasks 工具调用")
        traced_result = traced[0].get("result", {})
        self.assertIsInstance(traced_result, dict)


# ============================ 8. Executor 参数校验与路径解析 ============================
class TestExecutorDetails(unittest.TestCase):
    """验收 8：Executor 参数校验与路径解析。"""

    def setUp(self):
        self.registry = create_default_registry()
        self.executor = ToolExecutor(self.registry)

    def test_resolve_path_with_placeholder(self):
        """路径占位符替换。"""
        path = self.executor._resolve_path("/api/tasks/{task_id}/run", {"task_id": "42"})
        self.assertEqual(path, "/api/tasks/42/run")

    def test_resolve_path_no_placeholder(self):
        """无占位符路径不变。"""
        path = self.executor._resolve_path("/api/tasks", {"task_id": "42"})
        self.assertEqual(path, "/api/tasks")

    def test_validate_args_required_missing(self):
        """缺少必填参数返回错误。"""
        tool = self.registry.get("run_backup_task")
        result = self.executor._validate_args(tool, {})
        self.assertIn("error", result)

    def test_validate_args_required_present(self):
        """必填参数存在返回 cleaned args。"""
        tool = self.registry.get("run_backup_task")
        result = self.executor._validate_args(tool, {"task_id": "1"})
        self.assertIn("args", result)
        self.assertEqual(result["args"]["task_id"], "1")

    def test_validate_args_fills_defaults(self):
        """填充 default 值。"""
        tool = self.registry.get("list_recent_records")
        result = self.executor._validate_args(tool, {})
        self.assertIn("args", result)
        self.assertEqual(result["args"]["limit"], 20)

    def test_build_confirm_reason_backup(self):
        """确认提示包含任务 ID。"""
        tool = self.registry.get("run_backup_task")
        reason = self.executor._build_confirm_reason(tool, {"task_id": "T-99"})
        self.assertIn("T-99", reason)

    def test_build_confirm_reason_full_inspection(self):
        """全量巡检确认提示。"""
        tool = self.registry.get("run_inspection")
        reason = self.executor._build_confirm_reason(tool, {"scope": "full"})
        self.assertIn("性能", reason)

    def test_build_confirm_reason_quick_inspection(self):
        """快速巡检确认提示。"""
        tool = self.registry.get("run_inspection")
        reason = self.executor._build_confirm_reason(tool, {"scope": "quick"})
        self.assertIn("性能", reason)


# ============================ 8b. _call_get/_call_post 响应包装 ============================
class TestExecutorHttpResponseWrapping(unittest.TestCase):
    """Bug 修复回归：工具端点返回 JSON 数组/标量时，executor 必须包装为 dict。

    根因：/api/tasks、/api/backup-records 等端点直接 jsonify(list)，
    _call_get/_call_post 原样返回裸 list，调用方 exec_result.get(...) 抛
    'list' object has no attribute 'get'。修复后统一包装为
    {"ok": True, "data": <parsed>, "is_collection": <bool>}。
    """

    def setUp(self):
        self.registry = create_default_registry()
        self.executor = ToolExecutor(self.registry)

    def _fake_urlopen(self, payload):
        """构造一个 context manager 模拟 urllib 响应。"""
        body = json.dumps(payload).encode("utf-8")

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return body

        return lambda req, timeout=None: _Resp()

    def test_call_get_array_body_wrapped_as_collection(self):
        """GET 返回 JSON 数组 → 包装为 dict，is_collection=True，data 为列表。"""
        payload = [
            {"id": 1, "name": "任务A"},
            {"id": 2, "name": "任务B"},
        ]
        with patch("urllib.request.urlopen", self._fake_urlopen(payload)):
            result = self.executor._call_get("http://127.0.0.1:8080/api/tasks", {}, {})
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("is_collection"))
        self.assertEqual(result.get("data"), payload)
        self.assertTrue(result.get("ok"))

    def test_call_get_dict_body_passes_through(self):
        """GET 返回 JSON 对象 → 原样返回（不包装）。"""
        payload = {"ok": True, "tasks": [{"id": 1}]}
        with patch("urllib.request.urlopen", self._fake_urlopen(payload)):
            result = self.executor._call_get("http://127.0.0.1:8080/api/tasks", {}, {})
        self.assertIsInstance(result, dict)
        self.assertEqual(result, payload)
        self.assertNotIn("is_collection", result)

    def test_call_get_scalar_body_wrapped_no_collection(self):
        """GET 返回标量（如数字）→ 包装为 dict，is_collection=False。"""
        with patch("urllib.request.urlopen", self._fake_urlopen(42)):
            result = self.executor._call_get("http://127.0.0.1:8080/api/ping", {}, {})
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("is_collection"))
        self.assertEqual(result.get("data"), 42)

    def test_call_post_array_body_wrapped_as_collection(self):
        """POST 返回 JSON 数组 → 同样包装为 dict，is_collection=True。"""
        payload = [{"id": 10, "status": "ok"}]
        with patch("urllib.request.urlopen", self._fake_urlopen(payload)):
            result = self.executor._call_post("http://127.0.0.1:8080/api/records", {}, {})
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("is_collection"))
        self.assertEqual(result.get("data"), payload)

    def test_call_get_invalid_json_returns_raw(self):
        """非法 JSON → 走 json.JSONDecodeError 分支，返回 {ok, raw}。"""
        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"not json"

        with patch("urllib.request.urlopen", lambda req, timeout=None: _Resp()):
            result = self.executor._call_get("http://127.0.0.1:8080/api/x", {}, {})
        self.assertIsInstance(result, dict)
        self.assertIn("raw", result)
        self.assertTrue(result.get("ok"))


# ============================ 9. 提取 LLM 内容 ============================
class TestExtractContent(unittest.TestCase):
    """验收 9：从 LLM 返回结果中提取内容。"""

    def setUp(self):
        registry = create_default_registry()
        executor = ToolExecutor(registry)
        predictor = MagicMock()
        sm = SessionManager()
        self.agent = AIAgent(predictor, executor, sm)

    def test_extract_standard_message_content(self):
        """标准 choices[0].message.content 格式。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps({
                "choices": [{"message": {"content": "这是回答"}, "finish_reason": "stop"}],
            }),
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "这是回答")

    def test_extract_delta_content(self):
        """流式 delta.content 拼接格式。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps({
                "choices": [
                    {"delta": {"content": "这"}, "finish_reason": None},
                    {"delta": {"content": "是"}, "finish_reason": None},
                    {"delta": {"content": "回答"}, "finish_reason": "stop"},
                ],
            }),
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "这是回答")

    def test_extract_empty_result(self):
        """LLM 返回空内容。"""
        llm_result = {"ok": False, "error": "网络超时"}
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "")

    def test_extract_empty_response_body(self):
        """response_body 为空。"""
        llm_result = {"ok": True, "response_body": ""}
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "")

    def test_extract_answer_from_llm_answer_type(self):
        """从 LLM 结果中提取 answer 类型回答。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps({
                "choices": [{"message": {"content": '{"type": "answer", "content": "巡检完成"}'}, "finish_reason": "stop"}],
            }),
        }
        answer = self.agent._extract_answer_from_llm(llm_result)
        self.assertEqual(answer, "巡检完成")

    def test_extract_answer_from_llm_plain_text(self):
        """LLM 返回非结构化文本 → 直接返回原文。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps({
                "choices": [{"message": {"content": "巡检已完成"}, "finish_reason": "stop"}],
            }),
        }
        answer = self.agent._extract_answer_from_llm(llm_result)
        self.assertEqual(answer, "巡检已完成")

    def test_extract_list_response_body_message_content(self):
        """Bug 修复回归：响应体为 JSON 数组（元素为 {'message': {...}}）。

        根因：部分模型/网关（z.ai/glm-5.2 经 MoMA）把整个 HTTP 响应体以
        JSON 数组返回，原代码对 list 调用 .get('choices') 抛
        'list' object has no attribute 'get'。修复后应把 list 当作 choices 用。
        """
        llm_result = {
            "ok": True,
            "response_body": json.dumps([{"message": {"content": "你好"}}]),
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "你好")

    def test_extract_list_response_body_dict_elements(self):
        """Bug 修复回归：响应体为字典数组（非 message 嵌套），只断言不抛异常。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps([{"content": "直接数组"}]),
        }
        # 不应对 list 调用 .get，断言不抛任何异常
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertIsInstance(text, str)

    def test_extract_list_response_body_string_elements(self):
        """Bug 修复回归：响应体为字符串数组，应拼接为 content 返回。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps(["段一", "段二", "段三"]),
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "段一段二段三")

    def test_extract_list_response_body_non_json(self):
        """防御性回归：响应体为非法 JSON 时应安全返回截断原文。"""
        llm_result = {
            "ok": True,
            "response_body": "这不是 json 也不是 list",
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "这不是 json 也不是 list"[:2000])

    def test_extract_standard_object_remains_working(self):
        """回归：标准对象型 choices 响应仍正确返回（未破坏原有行为）。"""
        llm_result = {
            "ok": True,
            "response_body": json.dumps({
                "choices": [{"message": {"content": "标准"}, "finish_reason": "stop"}],
            }),
        }
        text = self.agent._extract_content_from_llm_result(llm_result)
        self.assertEqual(text, "标准")


def _main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n通过率: {total - failed}/{total}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    code = 1
    try:
        code = _main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
