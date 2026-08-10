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

SYSTEM_PROMPT_TEMPLATE = """你是数据备份管理平台的 AI 智能助手。你可以回答运维知识问题，也可以通过工具执行备份/巡检/查询等操作。

## 可用工具

{tools_description}

## 输出格式

你必须严格按以下 JSON 格式输出（不要包含任何其他文字）：

### 纯问答（不调用工具）：
```json
{{"type": "answer", "content": "你的回答文本"}}
```

### 调用工具：
```json
{{"type": "tool_call", "tool": "工具名", "args": {{参数对象}}}}
```

### 需要确认的危险操作：
```json
{{"type": "confirm_required", "tool": "工具名", "args": {{参数对象}}, "reason": "需要确认的原因"}}
```

## Few-shot 示例

用户: "最近备份有没有失败？"
助手: ```json
{{"type": "tool_call", "tool": "list_recent_records", "args": {{"limit": 10}}}}
```

用户: "帮我跑一次生产库巡检"
助手: ```json
{{"type": "confirm_required", "tool": "run_inspection", "args": {{"scope": "quick"}}, "reason": "巡检操作会影响数据库性能，请确认"}}
```

用户: "什么是RPO？"
助手: ```json
{{"type": "answer", "content": "RPO（Recovery Point Objective）是恢复点目标，指灾难发生后允许丢失的数据量时间窗口…"}}
```

## 约束
1. 一次只调用一个工具
2. 涉及执行操作（备份/巡检）时必须先确认
3. 不确定参数时回答"请提供更多信息"
4. 绝不虚构 task_id，不确定时先用 list_tasks 查询"""


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
            result = self._react_loop(session_id, system_prompt, context)

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
            # 保存助手消息（确认执行意图）
            self.session_mgr.add_assistant_message(
                session_id,
                content=f"用户已确认执行 {tool_name}，正在执行...",
                tool_calls=[{"name": tool_name, "args": args}],
            )

            # 执行工具（此时不再拦截）
            # 为确认执行，临时将 requires_confirm 标记为 False
            tool = self.registry.get(tool_name)
            if tool and tool.requires_confirm:
                # 直接通过 executor 的内部 HTTP 调用执行
                result = self._execute_tool_directly(tool_name, args, context)
            else:
                result = self.executor.execute(tool_name, args, context)

            # 保存工具结果
            self.session_mgr.add_tool_message(session_id, tool_name, result)

            # 再次调用 LLM 综合回答
            system_prompt = self._build_system_prompt()
            messages = self.session_mgr.build_messages_for_llm(
                session_id, system_prompt, "")

            llm_result = self._call_model_with_messages(messages)
            assistant_content = self._extract_answer_from_llm(llm_result)

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
                    context: Dict) -> Dict[str, Any]:
        """ReAct 循环：最多 MAX_REACT_ROUNDS 轮工具调用后强制输出。

        Args:
            session_id: 会话 ID
            system_prompt: system prompt
            context: 执行上下文

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
                # LLM 返回空内容，降级为错误消息
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

                # 保存助手消息（含工具调用意图）
                self.session_mgr.add_assistant_message(
                    session_id,
                    content=f"正在调用工具 {tool_name}...",
                    tool_calls=[{"name": tool_name, "args": args}],
                )

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

        # 超过最大轮数，强制输出
        _logger.warning(f"ReAct 循环超过 {MAX_REACT_ROUNDS} 轮，强制输出")
        self.session_mgr.add_assistant_message(
            session_id, "经过多轮工具调用后，以上是相关信息汇总。如需进一步操作，请继续提问。")
        return {
            "ok": True,
            "type": "answer",
            "content": "经过多轮工具调用后，以上是相关信息汇总。如需进一步操作，请继续提问。",
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
            return {"ok": False, "error": "AI 模型未启用"}

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
        return POST_ACTION_FALLBACK

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

        # 构造 HTTP 请求
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
