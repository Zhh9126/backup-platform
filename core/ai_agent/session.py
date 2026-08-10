# -*- coding: utf-8 -*-
"""
会话管理：AI Agent 会话和消息的 CRUD + 上下文构建。

SessionManager 封装 models 层的 CRUD，提供：
- 创建/列出/获取/删除会话
- 添加用户消息、助手消息、工具消息
- 加载会话历史构建 LLM 输入上下文
"""

import json
import uuid
from typing import Dict, List, Any, Optional

import core.db as db
import core.models as models

_logger = db.get_logger("ai_agent.session")

# 上下文滑窗最大消息条数
MAX_CONTEXT_MESSAGES = 20


class SessionManager:
    """AI 会话管理器：封装会话和消息的 CRUD 操作。"""

    def create(self, title: str = "新对话") -> str:
        """创建新会话，返回 session_id。

        Args:
            title: 会话标题，默认"新对话"

        Returns:
            session_id (UUID 字符串)
        """
        session_id = str(uuid.uuid4())
        now = db.now_iso()
        models.create_ai_session({
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        })
        _logger.info(f"创建会话: {session_id} 标题='{title}'")
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict]:
        """获取会话信息。

        Args:
            session_id: 会话 ID

        Returns:
            会话 dict 或 None
        """
        return models.get_ai_session(session_id)

    def list_sessions(self) -> List[Dict]:
        """列出所有会话（按更新时间倒序）。

        Returns:
            会话列表
        """
        return models.list_ai_sessions()

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其所有消息。

        Args:
            session_id: 会话 ID

        Returns:
            是否成功删除
        """
        # 先删消息再删会话
        models.delete_ai_messages(session_id)
        result = models.delete_ai_session(session_id)
        _logger.info(f"删除会话: {session_id}")
        return result

    def update_session(self, session_id: str, **kwargs) -> bool:
        """更新会话属性（如标题、updated_at）。

        Args:
            session_id: 会话 ID
            **kwargs: 要更新的字段

        Returns:
            是否成功更新
        """
        return models.update_ai_session(session_id, **kwargs)

    def add_user_message(self, session_id: str, content: str) -> Dict:
        """添加用户消息。

        Args:
            session_id: 会话 ID
            content: 用户消息文本

        Returns:
            消息 dict
        """
        now = db.now_iso()
        msg_id = models.add_ai_message({
            "session_id": session_id,
            "role": "user",
            "content": content,
            "created_at": now,
        })
        # 更新会话的 updated_at 和 message_count
        self._touch_session(session_id)
        # 若是第一条消息且标题是"新对话"，用内容前 20 字作为标题
        session = self.get_session(session_id)
        if session and session.get("title") == "新对话":
            short_title = content[:20] + ("..." if len(content) > 20 else "")
            self.update_session(session_id, title=short_title)

        return {"id": msg_id, "session_id": session_id, "role": "user", "content": content}

    def add_assistant_message(self, session_id: str, content: str,
                              tool_calls: Optional[List[Dict]] = None) -> Dict:
        """添加助手消息。

        Args:
            session_id: 会话 ID
            content: 助手回答文本
            tool_calls: 工具调用列表 [{"name": "...", "args": {...}}]

        Returns:
            消息 dict
        """
        now = db.now_iso()
        tool_calls_str = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        msg_id = models.add_ai_message({
            "session_id": session_id,
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls_str,
            "created_at": now,
        })
        self._touch_session(session_id)
        return {
            "id": msg_id, "session_id": session_id, "role": "assistant",
            "content": content, "tool_calls": tool_calls,
        }

    def add_tool_message(self, session_id: str, tool_name: str,
                         tool_result: Any, content: str = "") -> Dict:
        """添加工具消息（工具执行结果）。

        Args:
            session_id: 会话 ID
            tool_name: 工具名
            tool_result: 工具返回结果（dict 或 str）
            content: 可选的文本描述

        Returns:
            消息 dict
        """
        now = db.now_iso()
        result_str = json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, (dict, list)) else str(tool_result)
        msg_id = models.add_ai_message({
            "session_id": session_id,
            "role": "tool",
            "content": content or f"工具 {tool_name} 执行结果",
            "tool_name": tool_name,
            "tool_result": result_str,
            "created_at": now,
        })
        self._touch_session(session_id)
        return {
            "id": msg_id, "session_id": session_id, "role": "tool",
            "content": content or f"工具 {tool_name} 执行结果",
            "tool_name": tool_name, "tool_result": tool_result,
        }

    def get_history(self, session_id: str, max_messages: int = MAX_CONTEXT_MESSAGES) -> List[Dict]:
        """加载会话历史消息（含 role/content/tool_calls/tool_name/tool_result）。

        Args:
            session_id: 会话 ID
            max_messages: 最大消息条数

        Returns:
            消息列表 [{"role", "content", "tool_calls", "tool_name", "tool_result"}]
        """
        raw_msgs = models.list_ai_messages(session_id, limit=max_messages)
        history = []
        for msg in raw_msgs:
            entry = {
                "role": msg.get("role", ""),
                "content": msg.get("content", "") or "",
            }
            # 解析 tool_calls（JSON 字符串 → list）
            tc = msg.get("tool_calls")
            if tc:
                try:
                    entry["tool_calls"] = json.loads(tc)
                except (json.JSONDecodeError, TypeError):
                    entry["tool_calls"] = None
            # 解析 tool_result（JSON 字符串 → dict）
            tr = msg.get("tool_result")
            if tr:
                try:
                    entry["tool_result"] = json.loads(tr)
                except (json.JSONDecodeError, TypeError):
                    entry["tool_result"] = tr
            # tool_name
            if msg.get("tool_name"):
                entry["tool_name"] = msg.get("tool_name")

            history.append(entry)

        return history

    def build_messages_for_llm(self, session_id: str, system_prompt: str,
                                new_user_msg: str,
                                max_messages: int = MAX_CONTEXT_MESSAGES) -> List[Dict]:
        """构建 LLM 输入消息列表（OpenAI chat/completions 格式）。

        包含：
        1. system prompt
        2. 最近历史消息（滑窗截断）
        3. 当前用户消息

        Args:
            session_id: 会话 ID
            system_prompt: ReAct system prompt
            new_user_msg: 当前用户输入
            max_messages: 历史滑窗大小

        Returns:
            [{"role": "system"/"user"/"assistant"/"tool", "content": str}]
        """
        messages = [{"role": "system", "content": system_prompt}]

        # 加载历史（不含当前消息，当前消息由 add_user_message 在调用前写入）
        history = self.get_history(session_id, max_messages=max_messages)

        for entry in history:
            role = entry.get("role", "")
            content = entry.get("content", "")

            if role == "user":
                messages.append({"role": "user", "content": content})
            elif role == "assistant":
                # 如果有 tool_calls，按 OpenAI function calling 格式呈现
                if entry.get("tool_calls"):
                    # assistant 消息含工具调用
                    assistant_msg = {"role": "assistant", "content": content}
                    # 转换为 OpenAI function calling 格式（虽然 ReAct 不需要，
                    # 但统一格式方便后续切 Function Calling）
                    assistant_msg["tool_calls"] = [
                        {
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
                            },
                        }
                        for tc in entry["tool_calls"]
                    ]
                    messages.append(assistant_msg)
                else:
                    messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                # 工具结果消息
                tool_msg = {
                    "role": "tool",
                    "content": content,
                    "name": entry.get("tool_name", ""),
                }
                # 如果有结构化的 tool_result，将其作为 content
                if entry.get("tool_result") and isinstance(entry["tool_result"], dict):
                    tool_msg["content"] = json.dumps(entry["tool_result"], ensure_ascii=False)
                messages.append(tool_msg)

        return messages

    def _touch_session(self, session_id: str) -> None:
        """更新会话的 updated_at 和 message_count。"""
        now = db.now_iso()
        # 计算当前消息数
        msgs = models.list_ai_messages(session_id, limit=10000)
        count = len(msgs)
        self.update_session(session_id, updated_at=now, message_count=count)
