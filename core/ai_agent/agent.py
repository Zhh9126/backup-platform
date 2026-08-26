# -*- coding: utf-8 -*-
"""
AI Agent 核心：ReAct 循环 + 消息编排。

AIAgent 处理用户消息的核心流程：
1. 加载会话历史
2. 构造 ReAct system prompt（包含工具描述 + Few-shot 示例）
3. 调用 LLM
4. 解析 LLM 输出（支持多种格式：JSON block、Action 格式等）
5. 若是工具调用：执行/确认拦截 → 再次调 LLM 综合回答
6. 保存消息到 session

ReAct 轮数上限：3 轮工具调用后强制输出纯文本回答。
"""

import json
import re
import time
import uuid
import urllib.error
from typing import Dict, List, Any, Optional

import core.db as db
from core.ai_alert import AIPredictor

from .tools import ToolRegistry, create_default_registry
from .executor import ToolExecutor
from .session import SessionManager

_logger = db.get_logger("ai_agent.agent")

# ReAct 轮数上限
MAX_REACT_ROUNDS = 3

# 对话场景的模型输出上限（tokens）。
# AI 预测告警只需输出一个短 JSON 评估结果，1024 足够；但对话 Agent 需要把
# 工具返回的长列表（如 14 个备份任务）塞进 JSON 的 content 字段，还要做
# JSON 转义（\n / \" 都占 token），1024 会导致输出被截断、围栏不闭合。
# 因此对话链路显式传入更大的值，且不改动预测告警的默认值（零回归）。
AGENT_MAX_TOKENS = 4096

# 对话场景的请求超时（秒）。
# 预测告警是后台调度、单次短输出，30s 足够；但一轮对话要串行 2~3 次 LLM 调用
# （tool_call 判定 → 工具结果综合回答），上游 MoMA 抖动时单次 30s 超时会直接
# 让整轮对话失败（实测失败率 44%，超时占 24%）。这里显式放宽到 60s，
# 通过 _call_model(..., timeout=) 传入，不改动预测告警的默认值（零回归）。
AGENT_TIMEOUT_SEC = 60

# LLM 调用失败后的重试次数（1 = 最多再试一次，总计 2 次尝试）
LLM_MAX_RETRIES = 1

# 重试前的退避基数（秒）：第 n 次重试等待 n * 该值，给上游一点喘息时间
LLM_RETRY_BACKOFF_SEC = 1.5

# 只有网络层/超时错误才值得重试。
# 「模型正常返回但内容不合法」「鉴权失败」「端点不存在」重试也是同样结果，
# 白白让用户多等一轮，因此明确排除在外。
RETRYABLE_LLM_ERROR_CATEGORIES = frozenset({"timeout", "network"})

# 输出被截断时追加的温和提示，让用户知情而不是以为内容完整
TRUNCATION_NOTICE = "\n\n（内容较长已截断）"

# 完全无法恢复出有效文本时的兜底回答（绝不把裸 JSON/围栏甩给用户）
INCOMPLETE_ANSWER_FALLBACK = "AI 返回的内容不完整（可能已被截断），请重试或缩小提问范围。"

# 确认执行后综合回答缺失时的兜底文案
POST_ACTION_FALLBACK = "操作已完成，具体结果请查看上述信息。"

# pending 确认请求缓存（session_id → {tool_call_id, tool_name, args, reason}）
_pending_confirms: Dict[str, Dict] = {}


# ---- ReAct System Prompt ----

SYSTEM_PROMPT_TEMPLATE = """你是数据备份管理平台的 AI 智能助手。你可以回答运维知识问题，也可以通过工具查询备份/巡检/存储/告警信息，或执行备份/巡检操作。

## 可用工具

{tools_description}

## 输出格式

你必须严格按以下 JSON 格式输出，不要输出任何 JSON 以外的解释文字：

### 纯问答（不调用工具）：
```json
{{"type": "answer", "content": "你的回答文本"}}
```

### 调用查询类工具（无需确认）：
```json
{{"type": "tool_call", "tool": "工具名", "args": {{参数对象}}}}
```

### 需要确认的危险操作（备份、巡检）：
```json
{{"type": "confirm_required", "tool": "工具名", "args": {{参数对象}}, "reason": "需要确认的原因"}}
```

## Few-shot 示例

用户: "列出所有备份任务"
助手: ```json
{{"type": "tool_call", "tool": "list_tasks", "args": {{}}}}
```

用户: "最近备份有没有失败？"
助手: ```json
{{"type": "tool_call", "tool": "list_recent_records", "args": {{"limit": 10}}}}
```

用户: "查询存储用量"
助手: ```json
{{"type": "tool_call", "tool": "get_storage_usage", "args": {{}}}}
```

用户: "查询最近的 AI 预测告警"
助手: ```json
{{"type": "tool_call", "tool": "list_alert_predictions", "args": {{"days": 7}}}}
```

用户: "帮我跑一次生产库巡检"
助手: ```json
{{"type": "confirm_required", "tool": "run_inspection", "args": {{"scope": "quick"}}, "reason": "巡检操作会短暂影响数据库性能，请确认是否继续？"}}
```

用户: "立即执行备份任务 5"
助手: ```json
{{"type": "confirm_required", "tool": "run_backup_task", "args": {{"task_id": "5"}}, "reason": "即将执行备份任务 5，该操作会对数据库产生实际影响，请确认是否继续？"}}
```

用户: "什么是RPO？"
助手: ```json
{{"type": "answer", "content": "RPO（Recovery Point Objective）是恢复点目标，指灾难发生后允许丢失的数据量时间窗口。"}}
```

用户: "查询存储用量"
助手: ```json
{{"type": "tool_call", "tool": "get_storage_usage", "args": {{}}}}
```

工具返回: {{"ok": true, "message": "", "data": {{"target_name": "本地存储", "path": "/data/backups", "total_gb": 500, "used_gb": 120, "free_gb": 380, "used_percent": 24}}}}
助手: ```json
{{"type": "answer", "content": "当前本地存储（/data/backups）总空间 500 GB，已用 120 GB（24%），剩余 380 GB，空间充足。"}}
```

## 约束
1. 一次只调用一个工具。
2. 涉及执行操作（run_backup_task / run_inspection）时必须先返回 confirm_required，不能直接执行。
3. 查询类工具（list_tasks / list_recent_records / get_storage_usage / list_alert_predictions / get_inspection_report）不需要确认，直接调用。
4. 不确定参数时，先使用 list_tasks 等查询工具获取信息，再决定下一步。
5. 绝不虚构 task_id、target_id 等 ID，不确定时先用 list_tasks 查询。
6. get_storage_usage 不指定 target_id 时默认查询默认本地存储。
7. 你给出的 content 要简洁、面向用户，不要包含 JSON 结构、代码围栏或 "type"/"tool"/"args" 等技术字段。
8. 当消息历史中已经包含某工具的返回结果（role=tool）时，必须直接输出 answer 总结该结果，禁止再次调用同一工具。"""


class AIAgent:
    """AI Agent 核心：ReAct 循环 + 消息编排。

    Args:
        predictor: AIPredictor 实例（复用其 _call_model 方法）
        executor: ToolExecutor 实例
        session_mgr: SessionManager 实例
    """

    def __init__(self, predictor: AIPredictor, executor: ToolExecutor,
                 session_mgr: SessionManager) -> None:
        self.predictor = predictor
        self.executor = executor
        self.session_mgr = session_mgr
        self.registry = executor.registry

    def chat(self, session_id: str, user_message: str,
             request_headers: Dict[str, str] = None) -> Dict[str, Any]:
        """处理用户消息，返回结果。

        Args:
            session_id: 会话 ID
            user_message: 用户输入文本
            request_headers: 前端鉴权 header（用于透传到内部 API）

        Returns:
            {"ok": True, "type": "answer"/"confirm_required"/"error",
             "content": str, "tool_calls": list, "pending_confirm": dict}
        """
        context = {"request_headers": request_headers or {}}

        try:
            # 1. 保存用户消息到 session
            self.session_mgr.add_user_message(session_id, user_message)

            # 2. 构建 system prompt
            system_prompt = self._build_system_prompt()

            # 3. ReAct 循环
            result = self._react_loop(session_id, system_prompt, context, user_message)

            return result

        except Exception as e:
            _logger.error(f"chat 异常: {e}")
            # Graceful degradation：返回明确错误消息
            return {
                "ok": False,
                "type": "error",
                "content": f"处理消息时出错: {str(e)}",
            }

    def confirm_execute(self, session_id: str, tool_call_id: str,
                        approved: bool) -> Dict[str, Any]:
        """确认执行危险操作。

        Args:
            session_id: 会话 ID
            tool_call_id: 确认请求 ID
            approved: 用户是否批准执行

        Returns:
            执行结果或拒绝结果
        """
        # 查找 pending 确认请求
        pending = _pending_confirms.get(session_id)
        if not pending or pending.get("tool_call_id") != tool_call_id:
            return {"ok": False, "error": "确认请求不存在或已过期"}

        if not approved:
            # 用户拒绝执行
            _pending_confirms.pop(session_id, None)
            # 保存助手消息（拒绝）
            self.session_mgr.add_assistant_message(
                session_id, content="用户拒绝了该操作，已取消执行。")
            return {"ok": True, "type": "rejected", "content": "用户拒绝了该操作，已取消执行。"}

        # 用户批准执行
        tool_name = pending["tool_name"]
        args = pending["args"]
        reason = pending["reason"]
        context = pending.get("context", {})

        _pending_confirms.pop(session_id, None)

        try:
            # 执行工具（确认后不再拦截）
            tool = self.registry.get(tool_name)
            if tool and tool.requires_confirm:
                result = self._execute_tool_directly(tool_name, args, context)
            else:
                result = self.executor.execute(tool_name, args, context)

            # 保存工具结果（作为上下文给 LLM 综合；LLM 失败时也会被展示）
            self.session_mgr.add_tool_message(session_id, tool_name, result)

            # 尝试让 LLM 生成综合回答
            system_prompt = self._build_system_prompt()
            messages = self.session_mgr.build_messages_for_llm(
                session_id, system_prompt, "")

            llm_result = self._call_model_with_messages(messages)
            assistant_content = None
            if llm_result.get("error") == "AI_MODEL_DISABLED":
                assistant_content = self._format_tool_result_to_answer(tool_name, result)
            else:
                text = self._extract_content_from_llm_result(llm_result)
                if text:
                    parsed = self._parse_response(text)
                    if parsed.get("type") == "answer" and parsed.get("content"):
                        assistant_content = parsed["content"]

            # LLM 综合失败或模型未启用时，直接用工具结果生成格式化回答
            if not assistant_content:
                assistant_content = self._format_tool_result_to_answer(tool_name, result)

            # 保存最终助手回答
            self.session_mgr.add_assistant_message(session_id, assistant_content)

            return {
                "ok": True,
                "type": "answer",
                "content": assistant_content,
                "tool_trace": [{"name": tool_name, "args": args, "result": result}],
            }

        except Exception as e:
            _logger.error(f"确认执行异常: {e}")
            return {"ok": False, "type": "error", "content": f"执行操作时出错: {str(e)}"}

    def _react_loop(self, session_id: str, system_prompt: str,
                    context: Dict, user_message: str = "") -> Dict[str, Any]:
        """ReAct 循环：最多 MAX_REACT_ROUNDS 轮工具调用后强制输出。

        Args:
            session_id: 会话 ID
            system_prompt: system prompt
            context: 执行上下文
            user_message: 用户原始输入（LLM 不可用时的兜底意图识别用）

        Returns:
            最终结果 dict
        """
        tool_trace: List[Dict] = []
        round_count = 0

        # 构建 LLM 输入消息列表
        # 注意：用户消息已经保存到 session，build_messages_for_llm 会包含它
        # new_user_msg 传空字符串，避免重复添加
        messages = self.session_mgr.build_messages_for_llm(
            session_id, system_prompt, "")

        while round_count < MAX_REACT_ROUNDS:
            round_count += 1

            # 调用 LLM
            llm_result = self._call_model_with_messages(messages)
            llm_text = self._extract_content_from_llm_result(llm_result)

            if not llm_text:
                # LLM 返回空内容：区分模型未启用与其他调用失败
                if llm_result.get("error") == "AI_MODEL_DISABLED":
                    error_msg = "AI 问答助手未配置模型：请在【系统设置 → AI 告警/助手】中启用并配置大模型服务（OpenAI/兼容 API、模型名称、API Key）后再试。"
                    self.session_mgr.add_assistant_message(session_id, error_msg)
                    return {"ok": False, "type": "error", "content": error_msg}

                # 若上一轮已执行工具但 LLM 综合失败，直接基于工具结果 fallback
                if tool_trace:
                    fallback = self._format_tool_trace_to_answer(tool_trace)
                    self.session_mgr.add_assistant_message(session_id, fallback_placeholder := fallback)
                    return {"ok": True, "type": "answer", "content": fallback, "tool_trace": tool_trace}

                # LLM 完全不可用（网络/超时/未配置）且尚未执行任何工具：
                # 启用本地意图识别兜底，确保用户每次提问都有实质信息返回，
                # 而不是空白或"模型未返回内容"的无意义提示。
                fb = self._fallback_intent_handle(session_id, user_message, context)
                if fb:
                    return fb

                error_msg = "AI 模型未返回有效内容，请稍后重试"
                self.session_mgr.add_assistant_message(session_id, error_msg)
                return {"ok": False, "type": "error", "content": error_msg}

            # 解析 LLM 输出
            parsed = self._parse_response(llm_text)

            if parsed.get("type") == "answer":
                # 纯问答：直接返回
                self.session_mgr.add_assistant_message(
                    session_id, parsed["content"])
                return {
                    "ok": True,
                    "type": "answer",
                    "content": parsed["content"],
                    "tool_trace": tool_trace,
                }

            elif parsed.get("type") == "tool_call":
                # 工具调用
                tool_name = parsed.get("tool", "")
                args = parsed.get("args", {})

                # 检查工具是否存在
                tool = self.registry.get(tool_name)
                if not tool:
                    # 工具不存在，返回错误提示并继续循环
                    error_content = f"工具 '{tool_name}' 不存在，请使用可用工具列表中的工具"
                    self.session_mgr.add_assistant_message(session_id, error_content)
                    return {
                        "ok": False,
                        "type": "error",
                        "content": error_content,
                    }

                # 防御性检查：查询类工具已经拿到结果就不要再重复调用，
                # 避免 LLM 陷入死循环导致最后只能返回兜底文案
                if not tool.requires_confirm and any(
                    step.get("name") == tool_name for step in tool_trace
                ):
                    fallback_content = self._format_tool_trace_to_answer(tool_trace)
                    self.session_mgr.add_assistant_message(session_id, fallback_content)
                    return {
                        "ok": True,
                        "type": "answer",
                        "content": fallback_content,
                        "tool_trace": tool_trace,
                    }

                # 检查是否需要确认
                if tool.requires_confirm:
                    reason = parsed.get("reason") or self.executor._build_confirm_reason(tool, args)
                    tool_call_id = str(uuid.uuid4())

                    # 缓存确认请求
                    _pending_confirms[session_id] = {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "args": args,
                        "reason": reason,
                        "context": context,
                    }

                    return {
                        "ok": True,
                        "type": "confirm_required",
                        "content": reason,
                        "pending_confirm": {
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "args": args,
                            "reason": reason,
                        },
                        "tool_trace": tool_trace,
                    }

                # 不需确认：执行工具
                exec_result = self.executor.execute(tool_name, args, context)

                # 防御性：executor 在某些代码路径下可能仍返回非 dict（例如端点
                # 直接返回列表），统一归一化以确保后续 .get(...) 调用安全
                if not isinstance(exec_result, dict):
                    exec_result = {
                        "ok": False,
                        "error": f"工具返回格式异常: {type(exec_result).__name__}",
                    }

                if exec_result.get("needs_confirm"):
                    # executor 返回确认标记（双重保险）
                    tool_call_id = str(uuid.uuid4())
                    _pending_confirms[session_id] = {
                        "tool_call_id": tool_call_id,
                        "tool_name": exec_result["tool_name"],
                        "args": exec_result["args"],
                        "reason": exec_result["message"],
                        "context": context,
                    }
                    return {
                        "ok": True,
                        "type": "confirm_required",
                        "content": exec_result["message"],
                        "pending_confirm": {
                            "tool_call_id": tool_call_id,
                            "tool_name": exec_result["tool_name"],
                            "args": exec_result["args"],
                            "reason": exec_result["message"],
                        },
                        "tool_trace": tool_trace,
                    }

                # 保存工具结果
                self.session_mgr.add_tool_message(
                    session_id, tool_name, exec_result, content=f"工具 {tool_name} 执行结果")

                tool_trace.append({"name": tool_name, "args": args, "result": exec_result})

                # 重建 LLM 消息列表（含工具结果），再次调 LLM
                messages = self.session_mgr.build_messages_for_llm(
                    session_id, system_prompt, "")

                # 继续循环（让 LLM 综合回答）
                continue

            elif parsed.get("type") == "confirm_required":
                # LLM 主动要求确认
                tool_name = parsed.get("tool", "")
                args = parsed.get("args", {})
                reason = parsed.get("reason", "该操作需要确认")

                tool = self.registry.get(tool_name)
                if not tool:
                    error_content = f"工具 '{tool_name}' 不存在"
                    self.session_mgr.add_assistant_message(session_id, error_content)
                    return {"ok": False, "type": "error", "content": error_content}

                tool_call_id = str(uuid.uuid4())
                _pending_confirms[session_id] = {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "args": args,
                    "reason": reason,
                    "context": context,
                }

                return {
                    "ok": True,
                    "type": "confirm_required",
                    "content": reason,
                    "pending_confirm": {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "args": args,
                        "reason": reason,
                    },
                    "tool_trace": tool_trace,
                }

            else:
                # 解析失败，将 LLM 原文作为回答返回（graceful degradation）
                _logger.warning(f"LLM 输出解析失败，原文返回: {llm_text[:200]}")
                self.session_mgr.add_assistant_message(session_id, llm_text)
                return {
                    "ok": True,
                    "type": "answer",
                    "content": llm_text,
                    "tool_trace": tool_trace,
                }

        # 超过最大轮数，强制输出：用工具结果格式化兜底，确保用户能看到实际数据
        _logger.warning(f"ReAct 循环超过 {MAX_REACT_ROUNDS} 轮，强制输出")
        fallback_content = self._format_tool_trace_to_answer(tool_trace)
        self.session_mgr.add_assistant_message(session_id, fallback_content)
        return {
            "ok": True,
            "type": "answer",
            "content": fallback_content,
            "tool_trace": tool_trace,
        }

    def _build_system_prompt(self) -> str:
        """构造 ReAct system prompt（包含工具描述）。"""
        tools_desc = self.registry.tools_description_for_prompt()
        return SYSTEM_PROMPT_TEMPLATE.format(tools_description=tools_desc)

    def _call_model_with_messages(self, messages: List[Dict],
                                  max_retries: int = LLM_MAX_RETRIES) -> Dict:
        """调用 LLM（复用 AIPredictor._call_model）。

        将 OpenAI 格式的 messages 列表转换为单条 prompt 文本，
        然后调用 _call_model；网络层/超时错误自动重试。

        Args:
            messages: [{"role": "system/user/assistant/tool", "content": str}]
            max_retries: 网络层/超时错误的重试次数（默认 1）

        Returns:
            _call_model 的返回结果
        """
        cfg = self.predictor.get_config()
        ai_model = cfg.get("ai_model", {})

        if not ai_model.get("enabled"):
            return {"ok": False, "error": "AI_MODEL_DISABLED"}

        # 将多轮对话格式拼接为单条 prompt（适配现有 _call_model 接口）
        prompt_parts = []
        for msg in messages:
            # 防御性：若 messages 中混入非 dict 元素（理论上不应发生），
            # 跳过以避免 msg.get(...) 抛 AttributeError
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                prompt_parts.append(f"[系统指令]\n{content}")
            elif role == "user":
                prompt_parts.append(f"[用户]\n{content}")
            elif role == "assistant":
                # 含工具调用的 assistant 消息
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        tool_name = fn.get("name", "")
                        tool_args = fn.get("arguments", "")
                        prompt_parts.append(f"[助手-工具调用] {tool_name}({tool_args})")
                if content:
                    prompt_parts.append(f"[助手]\n{content}")
            elif role == "tool":
                tool_name = msg.get("name", "")
                prompt_parts.append(f"[工具结果-{tool_name}]\n{content}")

        prompt = "\n\n".join(prompt_parts)

        # 对话场景显式加大输出上限与超时（预测告警仍走默认 1024 / 30s）
        return self._invoke_model_with_retry(prompt, cfg, max_retries)

    def _invoke_model_with_retry(self, prompt: str, cfg: Dict,
                                 max_retries: int = LLM_MAX_RETRIES) -> Dict:
        """调用模型，超时/网络失败时带退避重试。

        一轮对话需要串行 2~3 次 LLM 调用，上游任意一次抖动就会让整轮失败。
        这里对「网络层」错误做有限重试；模型正常返回但内容不合法不重试。

        Args:
            prompt: 拼接好的单条 prompt 文本
            cfg: 完整 AI 配置
            max_retries: 最大重试次数（0 表示不重试）

        Returns:
            最后一次 _call_model 的返回结果
        """
        attempt = 0
        while True:
            result = self.predictor._call_model(
                prompt, cfg,
                max_tokens=AGENT_MAX_TOKENS,
                timeout=AGENT_TIMEOUT_SEC,
            )

            # 防御性：predictor 被替换/桩化时可能返回非 dict
            if not isinstance(result, dict):
                return {
                    "ok": False,
                    "error": f"模型返回格式异常: {type(result).__name__}",
                    "error_category": "unknown",
                }

            if result.get("ok"):
                if attempt:
                    _logger.info(f"LLM 调用第 {attempt + 1} 次尝试成功")
                return result

            category = result.get("error_category", "")
            if attempt >= max_retries or category not in RETRYABLE_LLM_ERROR_CATEGORIES:
                return result

            attempt += 1
            backoff = LLM_RETRY_BACKOFF_SEC * attempt
            _logger.warning(
                f"LLM 调用失败(category={category}, error={result.get('error', '')})，"
                f"{backoff}s 后进行第 {attempt} 次重试")
            time.sleep(backoff)

    def _extract_content_from_llm_result(self, llm_result: Dict) -> str:
        """从 _call_model 返回结果中提取 LLM 输出文本。

        支持：
        - choices[0].message.content（标准 OpenAI 格式）
        - choices[0].delta.content 拼接（流式格式）
        - 直接 response_body 解析
        """
        if not llm_result.get("ok"):
            return ""

        resp_body = llm_result.get("response_body", "")
        if not resp_body:
            return ""

        try:
            resp_json = json.loads(resp_body)
        except (json.JSONDecodeError, TypeError):
            return resp_body[:2000] if resp_body else ""

        # 兼容响应体为 list 的情况：部分模型/网关（如 z.ai/glm-5.2 经 MoMA）
        # 会把整个 HTTP 响应体以 JSON 数组形式返回，例如 [{...}] 或直接是
        # content 数组。若此处直接调用 resp_json.get(...) 会抛
        # 'list' object has no attribute 'get'，导致解析崩溃、ReAct 循环
        # 无法走到工具调用分支。
        if isinstance(resp_json, list):
            # 若列表元素全部为字符串，直接拼接为 content 返回
            if resp_json and all(isinstance(item, str) for item in resp_json):
                return "".join(resp_json)
            # 否则将整个 list 当作 choices 列表使用
            choices = resp_json
        elif isinstance(resp_json, dict):
            choices = resp_json.get("choices", [])
        else:
            # 既非 dict 也非 list（理论上不会出现），兜底返回字符串，
            # 避免任何 .get 调用崩溃
            return str(resp_json)

        # 防御性：确保 choices 为 list，避免后续 choices[0] 越界/类型崩溃
        choices = choices if isinstance(choices, list) else []

        if choices:
            first = choices[0]
            # 标准 message.content
            msg = first.get("message", {})
            if msg.get("content"):
                return msg["content"]

            # 流式 delta.content 拼接
            deltas = [c for c in choices if c.get("delta") and c.get("delta", {}).get("content")]
            if deltas:
                return "".join(d["delta"]["content"] for d in deltas)

        return ""

    def _extract_answer_from_llm(self, llm_result: Dict) -> str:
        """从 LLM 结果中提取最终回答文本（用于确认执行后的综合回答）。

        如果 LLM 返回结构化 JSON answer，提取 content；
        否则返回原文。
        """
        text = self._extract_content_from_llm_result(llm_result)
        if not text:
            return "操作已完成，具体结果请查看上述信息。"

        # 尝试解析为 JSON answer
        parsed = self._parse_response(text)
        if parsed.get("type") == "answer" and parsed.get("content"):
            return parsed["content"]

        # 非结构化回答（tool_call / confirm_required 等），
        # 剥离围栏标记与 JSON 结构噪声后返回，绝不把裸 ```json 甩给用户
        recovered = self._recover_truncated_response(text)
        if recovered and recovered.get("type") == "answer" and recovered.get("content"):
            return recovered["content"]
        cleaned = self._strip_structural_noise(text)
        if cleaned and cleaned.strip():
            return cleaned
        return ""

    def _format_tool_trace_to_answer(self, tool_trace: List[Dict]) -> str:
        """将工具执行链路格式化为自然语言回答（LLM 综合失败时 fallback）。"""
        if not tool_trace:
            return POST_ACTION_FALLBACK
        parts = []
        for step in tool_trace:
            tool_name = step.get("name", "")
            result = step.get("result", {})
            formatted = self._format_tool_result_to_answer(tool_name, result)
            parts.append(formatted)
        return "\n\n".join(parts)

    def _format_tool_result_to_answer(self, tool_name: str, result: Dict) -> str:
        """将单个工具结果格式化为面向用户的文本。"""
        if not isinstance(result, dict):
            return f"工具 {tool_name} 返回格式异常，请稍后重试。"

        ok = result.get("ok")
        message = result.get("message") or ""
        data = result.get("data")
        error = result.get("error")

        if not ok:
            return f"操作未成功：{error or message or '未知错误'}"

        # 根据工具类型做格式化展示
        if tool_name == "list_tasks":
            rows = data or []
            if not rows:
                return "当前没有任何备份任务。"
            lines = ["当前共有 {} 个备份任务：".format(len(rows))]
            lines.append("| ID | 名称 | 数据库类型 | 备份模式 | 主机 | 状态 |")
            lines.append("|---|---|---|---|---|---|")
            for r in rows[:30]:
                lines.append(
                    "| {} | {} | {} | {} | {} | {} |".format(
                        r.get("id", "-"),
                        (r.get("name") or "-").replace("|", "\\|"),
                        r.get("db_type") or "-",
                        r.get("backup_mode") or "-",
                        "{}:{}".format(r.get("host") or "-", r.get("port") or "-") if r.get("host") else "-",
                        r.get("last_status") or "-",
                    )
                )
            return "\n".join(lines)

        if tool_name == "list_recent_records":
            rows = data or []
            if not rows:
                return "最近没有备份执行记录。"
            lines = ["最近 {} 条备份记录如下：".format(len(rows))]
            lines.append("| 记录ID | 任务 | 类型 | 状态 | 大小 | 开始时间 | 仿真 |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in rows[:30]:
                size = r.get("size_bytes")
                size_str = "-"
                if size:
                    size_str = "{:.2f} MB".format(size / (1024 * 1024)) if size > 1024 * 1024 else "{:.2f} KB".format(size / 1024)
                sim = "是" if r.get("is_simulated") else "否"
                lines.append(
                    "| {} | {} | {} | {} | {} | {} | {} |".format(
                        r.get("id", "-"),
                        (r.get("task_name") or "-").replace("|", "\\|"),
                        r.get("backup_type") or "-",
                        r.get("status") or "-",
                        size_str,
                        r.get("started_at") or "-",
                        sim,
                    )
                )
            return "\n".join(lines)

        if tool_name == "get_storage_usage":
            d = data or {}
            return (
                "存储目标：{}（{}）\n"
                "总空间：{} GB，已用：{} GB，可用：{} GB\n"
                "使用率：{}%".format(
                    d.get("target_name", "-"),
                    d.get("path", "-"),
                    d.get("total_gb", "-"),
                    d.get("used_gb", "-"),
                    d.get("free_gb", "-"),
                    d.get("used_percent", "-"),
                )
            )

        if tool_name == "list_alert_predictions":
            rows = data or []
            if not rows:
                return "最近没有 AI 预测告警。"
            lines = ["最近 AI 预测告警（共 {} 条）：".format(len(rows))]
            lines.append("| 指标 | 风险等级 | 分数 | 预测时间 | 内容 |")
            lines.append("|---|---|---|---|---|")
            for r in rows[:20]:
                lines.append(
                    "| {} | {} | {} | {} | {} |".format(
                        r.get("metric") or "-",
                        r.get("risk_level") or "-",
                        r.get("risk_score") or "-",
                        r.get("predicted_at") or "-",
                        (r.get("predicted_content") or "-").replace("|", "\\|"),
                    )
                )
            return "\n".join(lines)

        if tool_name == "get_inspection_report":
            d = data or {}
            detail = d.get("detail")
            if isinstance(detail, dict):
                items = detail.get("items") or []
                lines = ["巡检报告 #{}（{}）：".format(d.get("id", "-"), d.get("status", "-"))]
                lines.append("| 检查项 | 结果 | 说明 |")
                lines.append("|---|---|---|")
                for item in items[:30]:
                    lines.append(
                        "| {} | {} | {} |".format(
                            (item.get("name") or "-").replace("|", "\\|"),
                            item.get("result") or "-",
                            (item.get("message") or "-").replace("|", "\\|"),
                        )
                    )
                return "\n".join(lines)
            return message or "巡检报告已生成。"

        if tool_name == "run_backup_task":
            d = data or {}
            if d.get("accepted"):
                return "备份任务已提交后台执行，可在备份记录页面查看进度。"
            status = d.get("status", "未知")
            return "备份任务执行结果：{}。{}".format(status, message)

        if tool_name == "run_inspection":
            d = data or {}
            return (
                "巡检完成：共 {} 项，通过 {}，警告 {}，失败 {}。".format(
                    d.get("total", "-"),
                    d.get("pass", "-"),
                    d.get("warn", "-"),
                    d.get("fail", "-"),
                )
                + ("\n详细检查项：\n" + "\n".join(
                    "- {}: {}".format(
                        (i.get("name") or "-").replace("|", "\\|"),
                        (i.get("message") or "").replace("|", "\\|"),
                    )
                    for i in d.get("items", [])
                ) if d.get("items") else "")
            )

        # 默认：返回 message 或简短 JSON
        if message:
            return message
        try:
            import json as _json
            return _json.dumps(data, ensure_ascii=False, default=str)[:1000]
        except Exception:
            return "操作已完成。"

    # 内置运维知识库（LLM 不可用时的纯问答兜底）
    _KNOWLEDGE_BASE = {
        "rpo": "RPO（Recovery Point Objective，恢复点目标）：灾难发生后允许丢失的数据量对应的时间窗口。例如 RPO=5 分钟，表示最多容忍丢失最近 5 分钟的数据。",
        "rto": "RTO（Recovery Time Objective，恢复时间目标）：从灾难发生到业务系统恢复可用所允许的最大停机时间。例如 RTO=2 小时，表示必须在 2 小时内恢复服务。",
        "全量备份": "全量备份（Full Backup）：对指定数据源进行完整拷贝，恢复快但占用空间大、耗时长，通常作为增量/差异备份的基线。",
        "增量备份": "增量备份（Incremental Backup）：仅备份自上次备份（任意类型）以来变化的数据，节省空间但恢复需依赖完整链。",
        "差异备份": "差异备份（Differential Backup）：备份自上次全量备份以来所有变化的数据，恢复只需全量+最新差异，空间介于全量与增量之间。",
        "物理备份": "物理备份：直接拷贝数据库的数据文件/目录（如 pg_basebackup、xtrabackup），恢复快、一致性好，但要求与源环境兼容。",
        "逻辑备份": "逻辑备份：导出数据为 SQL 或逻辑格式（如 mysqldump、pg_dump、mongodump），跨版本/跨平台兼容性好，但恢复较慢。",
        "pg_basebackup": "pg_basebackup 是 PostgreSQL 官方物理备份工具，通过流复制协议拉取整个数据目录，常配合 -Ft（tar）与 -z（压缩）生成基础备份，用于 PITR 基线。",
        "gtid": "GTID（Global Transaction ID，全局事务标识）：MySQL 用于唯一标记每个已提交事务，便于主从复制与故障切换；恢复含 GTID 的 dump 到非空实例时需先 RESET MASTER。",
    }

    def _fallback_intent_handle(self, session_id: str, user_message: str,
                               context: Dict) -> Optional[Dict]:
        """LLM 不可用时的本地意图识别兜底。

        根据关键词将用户问题路由到：
        - 知识库纯问答（RPO/RT0/备份概念等）
        - 查询类工具（list_tasks / list_recent_records / get_storage_usage 等）
        - 危险操作类工具：返回确认请求（不直接执行）

        返回 dict 或 None（无法识别时交给上层错误提示）。
        """
        if not user_message:
            return None
        text = user_message.strip().lower()

        # 1) 知识库纯问答
        for kw, answer in self._KNOWLEDGE_BASE.items():
            if kw in text:
                self.session_mgr.add_assistant_message(session_id, answer)
                return {"ok": True, "type": "answer", "content": answer}

        # 2) 危险操作类（需确认）—— 注意：先排除"列出/查询"类意图
        is_query_intent = any(k in text for k in
                              ("列出", "所有任务", "任务列表", "有哪些任务", "有哪些备份",
                               "查询", "查看", "显示", "多少", "列表", "巡检报告", "巡检结果", "告警", "预测", "风险"))
        if not is_query_intent and any(k in text for k in
                                       ("执行备份", "跑备份", "备份一下", "立即备份", "开始备份", "做一次备份", "手动备份")):
            # 尝试从文本提取 task id
            import re
            m = re.search(r"任务\s*#?\s*(\d+)", user_message)
            tid = m.group(1) if m else None
            args = {"task_id": tid} if tid else {}
            reason = (f"即将执行备份任务 {tid}，该操作会对数据库产生实际影响，请确认是否继续？"
                      if tid else "即将执行备份任务，请确认任务 ID 后继续？")
            tool_call_id = str(uuid.uuid4())
            _pending_confirms[session_id] = {
                "tool_call_id": tool_call_id,
                "tool_name": "run_backup_task",
                "args": args,
                "reason": reason,
                "context": context,
            }
            return {
                "ok": True,
                "type": "confirm_required",
                "content": reason,
                "pending_confirm": {
                    "tool_call_id": tool_call_id,
                    "tool_name": "run_backup_task",
                    "args": args,
                    "reason": reason,
                },
            }
        if not is_query_intent and any(k in text for k in ("巡检", "检查一遍", "跑一次巡检", "执行巡检", "做一次巡检")):
            scope = "full" if "全量" in text else "quick"
            reason = ("全量巡检会对数据库性能产生较大影响，请确认是否继续？"
                      if scope == "full" else "巡检操作会短暂影响数据库性能，请确认是否继续？")
            tool_call_id = str(uuid.uuid4())
            _pending_confirms[session_id] = {
                "tool_call_id": tool_call_id,
                "tool_name": "run_inspection",
                "args": {"scope": scope},
                "reason": reason,
                "context": context,
            }
            return {
                "ok": True,
                "type": "confirm_required",
                "content": reason,
                "pending_confirm": {
                    "tool_call_id": tool_call_id,
                    "tool_name": "run_inspection",
                    "args": {"scope": scope},
                    "reason": reason,
                },
            }

        # 3) 查询类工具
        if any(k in text for k in ("所有任务", "备份任务", "任务列表", "有哪些任务", "列出任务")):
            return self._run_fallback_tool(session_id, "list_tasks", {}, context)
        if any(k in text for k in ("失败", "最近备份", "备份记录", "上次的备份", "备份情况", "有没有失败")):
            return self._run_fallback_tool(session_id, "list_recent_records", {"limit": 10}, context)
        if any(k in text for k in ("存储", "空间", "磁盘", "用量", "容量")):
            return self._run_fallback_tool(session_id, "get_storage_usage", {}, context)
        if any(k in text for k in ("告警", "预测", "风险", "预警")):
            return self._run_fallback_tool(session_id, "list_alert_predictions", {"days": 7}, context)
        if any(k in text for k in ("巡检报告", "巡检结果", "检查报告")):
            return self._run_fallback_tool(session_id, "get_inspection_report", {}, context)
        if any(k in text for k in ("巡检任务", "所有巡检")):
            return self._run_fallback_tool(session_id, "list_tasks", {}, context)

        # 无法识别：返回 None，由上层给出通用提示
        return None

    def _run_fallback_tool(self, session_id: str, tool_name: str, args: Dict,
                          context: Dict) -> Dict:
        """兜底执行单个查询工具并格式化结果。"""
        tool = self.registry.get(tool_name)
        if not tool:
            return None
        exec_result = self.executor.execute(tool_name, args, context)
        if not isinstance(exec_result, dict):
            exec_result = {"ok": False, "error": "工具返回格式异常"}
        self.session_mgr.add_tool_message(session_id, tool_name, exec_result)
        content = self._format_tool_result_to_answer(tool_name, exec_result)
        self.session_mgr.add_assistant_message(session_id, content)
        return {
            "ok": True,
            "type": "answer",
            "content": content,
            "tool_trace": [{"name": tool_name, "args": args, "result": exec_result}],
        }

    def _parse_response(self, llm_text: str) -> Dict:
        """解析 LLM 输出文本，支持多种格式。

        支持格式：
        1. JSON code block: ```json {...} ```（闭合围栏）
        2. 纯 JSON: {...}
        3. 嵌入式 JSON：文本中含 JSON 片段
        4. ReAct 格式: Action: X\nAction Input: {...}
        5. 截断容错：未闭合围栏 / 不完整 JSON（提取已生成的 content）
        6. 纯文本回答

        无论输入多残缺，返回的 content 都不会包含 ``` 围栏标记或裸 JSON 结构。

        Returns:
            {"type": "answer"/"tool_call"/"confirm_required",
             "content"/"tool"/"args"/"reason"}
        """
        if not llm_text:
            return {"type": "answer", "content": ""}

        text = llm_text.strip()

        # ---- 格式 1: JSON code block ----
        # 提取 ```json ... ``` 或 ``` ... ``` 内的内容
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1).strip()
            parsed = self._try_parse_json(json_str)
            if parsed:
                return parsed

        # ---- 格式 2: 纯 JSON（整体就是 JSON） ----
        # 尝试直接解析整段文本
        parsed = self._try_parse_json(text)
        if parsed:
            return parsed

        # ---- 格式 3: 嵌入式 JSON（文本中含 JSON 片段） ----
        # 遍历所有 { ... } JSON 候选，逐个尝试解析直到成功
        # （首个匹配可能是诱饵 JSON，不能只取第一个就放弃）
        for json_match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
            json_str = json_match.group(0)
            parsed = self._try_parse_json(json_str)
            if parsed:
                return parsed

        # ---- 格式 4: ReAct Action 格式 ----
        action_match = re.search(r"Action:\s*(\w+)\s*\n\s*Action Input:\s*(.*)", text, re.DOTALL)
        if action_match:
            tool_name = action_match.group(1).strip()
            args_str = action_match.group(2).strip()
            try:
                args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                args = {"raw_input": args_str}
            return {"type": "tool_call", "tool": tool_name, "args": args}

        # ---- 格式 5: 截断容错（未闭合围栏 / 不完整 JSON） ----
        # 模型输出被 max_tokens 截断时，围栏不闭合、JSON 不完整，格式 1-4 全部
        # 失配。这里按「完整 JSON → 部分 content → 剥离结构噪声」降级恢复，
        # 保证绝不把 ```json 标记和裸 JSON 原文直接甩给用户。
        recovered = self._recover_truncated_response(text)
        if recovered:
            return recovered

        # ---- 格式 6: 纯文本回答 ----
        # 所有格式都无法解析，视为纯文本回答
        return {"type": "answer", "content": text}

    def _recover_truncated_response(self, text: str) -> Optional[Dict]:
        """从被截断的 LLM 输出中尽力恢复出可读回答。

        降级顺序：
          a) 未闭合围栏内部其实是完整 JSON → 正常解析
          b) JSON 不完整 → 提取 "content" 字段已生成的部分（还原 JSON 转义）
          c) 仍失败 → 剥掉围栏标记与 JSON 头部噪声后返回纯文本
          d) 连文本都提取不到 → 返回兜底提示

        Args:
            text: LLM 原始输出文本

        Returns:
            标准化 dict，或 None 表示不属于「截断的结构化输出」（交给纯文本分支）
        """
        fence = self._extract_fence_body(text)
        if fence is not None:
            lang, payload, closed = fence
        else:
            lang, payload, closed = "", text, True

        payload = (payload or "").strip()
        if not payload:
            # 有围栏标记但体为空（如输入仅 ```json），不能返回 None
            # 否则纯文本分支会把围栏标记原样甩给用户
            if fence is not None:
                return {"type": "answer", "content": INCOMPLETE_ANSWER_FALLBACK}
            return None

        looks_like_json = payload.startswith("{") or lang.lower() == "json"
        # 无围栏且不像 JSON 的普通文本，不属于本分支处理范围
        if fence is None and not looks_like_json:
            return None

        # ---- a) 围栏未闭合但内部 JSON 完整 ----
        parsed = self._try_parse_json(payload)
        if parsed:
            return parsed

        # ---- b) JSON 不完整：提取 content 已生成部分 ----
        if looks_like_json and '"content"' in payload:
            extracted = self._extract_partial_content(payload)
            if extracted is not None:
                content, truncated = extracted
                content = content.strip()
                if content:
                    if truncated or not closed:
                        content += TRUNCATION_NOTICE
                    return {"type": "answer", "content": content}

        # ---- c) 兜底：剥离围栏标记与 JSON 结构噪声 ----
        if looks_like_json:
            cleaned = self._strip_structural_noise(payload)
            if cleaned:
                if not closed:
                    cleaned += TRUNCATION_NOTICE
                return {"type": "answer", "content": cleaned}
            # ---- d) 什么都提取不到：返回兜底提示，绝不返回裸 JSON ----
            return {"type": "answer", "content": INCOMPLETE_ANSWER_FALLBACK}

        # 围栏内是非 JSON 内容（如 ```sql 代码块），保持原文由前端按 Markdown 渲染
        return None

    def _extract_fence_body(self, text: str) -> Optional[tuple]:
        """提取 Markdown 代码围栏内部文本，兼容「未闭合围栏」。

        Args:
            text: 原始文本

        Returns:
            (lang, body, closed) 三元组；无围栏时返回 None。
            closed 表示围栏是否正常闭合（False 通常意味着输出被截断）。
        """
        if not text:
            return None

        open_match = re.search(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\r?\n?", text)
        if not open_match:
            return None

        lang = open_match.group(1) or ""
        body = text[open_match.end():]

        close_idx = body.find("```")
        if close_idx != -1:
            return lang, body[:close_idx], True
        return lang, body, False

    def _extract_partial_content(self, json_str: str) -> Optional[tuple]:
        """从（可能不完整的）JSON 文本中提取 "content" 字段已生成的部分。

        手工扫描而非 json.loads，因为截断的 JSON 无法被标准解析器接受。
        扫描时正确处理转义序列，遇到未转义的 `"` 视为字段正常结束。

        Args:
            json_str: JSON 文本（可能被截断）

        Returns:
            (content_text, truncated) 二元组；未找到 content 字段时返回 None。
            truncated=True 表示字符串未正常闭合（即被截断）。
        """
        key_match = re.search(r'"content"\s*:\s*"', json_str)
        if not key_match:
            return None

        idx = key_match.end()
        total = len(json_str)
        chunks: List[str] = []
        closed = False

        while idx < total:
            ch = json_str[idx]
            if ch == "\\":
                if idx + 1 >= total:
                    # 转义符本身被截断，丢弃这个残缺字符
                    break
                chunks.append(json_str[idx:idx + 2])
                idx += 2
                continue
            if ch == '"':
                closed = True
                break
            chunks.append(ch)
            idx += 1

        raw = "".join(chunks)
        return self._unescape_json_string(raw), (not closed)

    def _unescape_json_string(self, raw: str) -> str:
        """还原 JSON 字符串转义（\\n / \\" / \\\\ / \\uXXXX 等）。

        优先用标准 JSON 解码器（strict=False 以容忍裸控制字符）；
        若尾部存在残缺转义序列，逐字符回退重试；仍失败则手工还原。

        Args:
            raw: JSON 字符串字面量的内部原始片段（不含首尾引号）

        Returns:
            还原后的真实文本
        """
        if not raw:
            return ""

        decoder = json.JSONDecoder(strict=False)
        candidate = raw
        # 最多回退 8 个字符，覆盖 \uXXXX（6 字符）等最长残缺转义
        for _ in range(8):
            if not candidate:
                break
            try:
                return decoder.decode('"%s"' % candidate)
            except (json.JSONDecodeError, ValueError):
                candidate = candidate[:-1]

        return self._manual_unescape(raw)

    @staticmethod
    def _manual_unescape(raw: str) -> str:
        """手工还原常见 JSON 转义（标准解码器失败时的兜底）。"""
        mapping = {
            "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
            '"': '"', "\\": "\\", "/": "/",
        }
        out: List[str] = []
        idx = 0
        total = len(raw)

        while idx < total:
            ch = raw[idx]
            if ch == "\\" and idx + 1 < total:
                nxt = raw[idx + 1]
                if nxt in mapping:
                    out.append(mapping[nxt])
                    idx += 2
                    continue
                if nxt == "u" and idx + 6 <= total:
                    try:
                        out.append(chr(int(raw[idx + 2:idx + 6], 16)))
                        idx += 6
                        continue
                    except ValueError:
                        pass
                out.append(nxt)
                idx += 2
                continue
            out.append(ch)
            idx += 1

        return "".join(out)

    def _strip_structural_noise(self, payload: str) -> str:
        """剥掉围栏标记与 `{"type": "answer", "content": "` 这类 JSON 结构噪声。

        Args:
            payload: 可能含结构噪声的文本

        Returns:
            清洗后的可读文本；无可用文本时返回空串
        """
        text = (payload or "").strip()
        text = re.sub(r"^```[ \t]*[A-Za-z0-9_+\-]*[ \t]*\r?\n?", "", text)
        text = re.sub(r"\r?\n?```\s*$", "", text).strip()

        # 匹配 { "type": "answer", ... , "content": " 形式的 JSON 头
        head = re.match(
            r'^\{\s*(?:"[^"]*"\s*:\s*(?:"[^"]*"|[^,{}]+)\s*,\s*)*"content"\s*:\s*"',
            text,
        )
        if head:
            body = text[head.end():]
            # 去掉尾部残留的 JSON 收尾符号
            body = re.sub(r'"\s*\}?\s*$', "", body)
            return self._unescape_json_string(body).strip()

        if text.startswith("{"):
            # 只有 JSON 骨架、没有 content 文本可用
            return ""

        return text

    def _try_parse_json(self, json_str: str) -> Optional[Dict]:
        """尝试解析 JSON 字符串为 Agent 消息格式。

        Returns:
            标准化后的 dict（含 type/content/tool/args/reason）或 None
        """
        try:
            obj = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(obj, dict):
            return None

        msg_type = obj.get("type", "")

        if msg_type == "answer":
            return {
                "type": "answer",
                "content": obj.get("content", ""),
            }
        elif msg_type == "tool_call":
            return {
                "type": "tool_call",
                "tool": obj.get("tool", ""),
                "args": obj.get("args", {}) or {},
            }
        elif msg_type == "confirm_required":
            return {
                "type": "confirm_required",
                "tool": obj.get("tool", ""),
                "args": obj.get("args", {}) or {},
                "reason": obj.get("reason", ""),
            }
        elif "tool" in obj and "args" in obj:
            # 无 type 字段但有 tool/args → 视为 tool_call
            return {
                "type": "tool_call",
                "tool": obj.get("tool", ""),
                "args": obj.get("args", {}) or {},
            }
        elif "content" in obj and not "tool" in obj:
            # 有 content 无 tool → 视为 answer
            return {
                "type": "answer",
                "content": obj.get("content", ""),
            }

        return None

    def _execute_tool_directly(self, tool_name: str, args: Dict,
                               context: Dict) -> Dict:
        """直接执行工具（绕过 requires_confirm 检查，用于确认后的执行）。

        优先走本地 Python executor，避免内部 HTTP 认证问题。

        Args:
            tool_name: 工具名
            args: 参数
            context: 上下文

        Returns:
            工具执行结果
        """
        tool = self.registry.get(tool_name)
        if not tool:
            return {"ok": False, "error": f"工具 '{tool_name}' 未注册"}

        # 校验参数
        validated = self.executor._validate_args(tool, args)
        if validated.get("error"):
            return {"ok": False, "error": validated["error"]}
        args = validated.get("args", args)

        # 优先本地 Python 函数执行
        if tool.executor:
            try:
                return tool.executor(args, context)
            except Exception as e:
                _logger.exception(f"确认执行本地工具失败 {tool_name}: {e}")

        # 回退到内部 HTTP
        api_path = self.executor._resolve_path(tool.api_path, args)
        url = f"{self.executor.base_url}{api_path}"
        request_headers = context.get("request_headers", {})

        try:
            if tool.api_method.upper() == "GET":
                return self.executor._call_get(url, args, request_headers)
            elif tool.api_method.upper() == "POST":
                return self.executor._call_post(url, args, request_headers)
            else:
                return {"ok": False, "error": f"不支持的 HTTP 方法: {tool.api_method}"}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {e.code} {e.reason}: {body}", "status": e.code}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"网络错误: {str(e.reason)}", "error_category": "network"}
        except Exception as e:
            return {"ok": False, "error": f"执行异常: {str(e)}"}


def get_agent() -> AIAgent:
    """获取 AIAgent 单例（复用 AIPredictor + 默认注册表）。"""
    predictor = AIPredictor()
    registry = create_default_registry()
    executor = ToolExecutor(registry)
    session_mgr = SessionManager()
    return AIAgent(predictor, executor, session_mgr)
