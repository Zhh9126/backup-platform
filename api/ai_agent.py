# -*- coding: utf-8 -*-
"""
AI 智能助手 REST API 路由。

路由前缀: /api/agent（通过共享 api_bp 注册）
- POST   /api/agent/sessions          创建会话
- GET    /api/agent/sessions          列出所有会话
- DELETE /api/agent/sessions/<id>     删除会话
- GET    /api/agent/sessions/<id>/messages 列出消息
- POST   /api/agent/chat             发送消息（返回 answer/confirm_required）
- POST   /api/agent/confirm          确认执行危险操作
"""

import json

from flask import request, jsonify
from auth import login_required
from . import api_bp

from core.ai_agent.agent import AIAgent, get_agent
from core.ai_agent.session import SessionManager
from core.ai_agent.tools import create_default_registry
from core.ai_agent.executor import ToolExecutor
from core.ai_alert import AIPredictor


def _get_agent() -> AIAgent:
    """获取 AIAgent 实例。"""
    return get_agent()


# ---- 会话管理 ----

@api_bp.route("/agent/sessions", methods=["POST"])
@login_required
def api_create_agent_session():
    """创建新会话。"""
    data = request.get_json(silent=True) or {}
    title = data.get("title", "新对话")
    agent = _get_agent()
    session_id = agent.session_mgr.create(title=title)
    session = agent.session_mgr.get_session(session_id)
    return jsonify({"ok": True, "session": session})


@api_bp.route("/agent/sessions", methods=["GET"])
@login_required
def api_list_agent_sessions():
    """列出所有会话。"""
    agent = _get_agent()
    sessions = agent.session_mgr.list_sessions()
    return jsonify({"ok": True, "sessions": sessions})


@api_bp.route("/agent/sessions/<session_id>", methods=["DELETE"])
@login_required
def api_delete_agent_session(session_id: str):
    """删除会话及其消息。"""
    agent = _get_agent()
    success = agent.session_mgr.delete_session(session_id)
    return jsonify({"ok": success})


@api_bp.route("/agent/sessions/<session_id>/messages", methods=["GET"])
@login_required
def api_list_agent_messages(session_id: str):
    """列出会话消息。"""
    limit = request.args.get("limit", default=50, type=int)
    agent = _get_agent()
    messages = agent.session_mgr.get_history(session_id, max_messages=limit)
    return jsonify({"ok": True, "messages": messages})


# ---- 聊天 ----

@api_bp.route("/agent/chat", methods=["POST"])
@login_required
def api_agent_chat():
    """发送消息到 AI Agent。

    请求体: {"session_id": str, "message": str}
    返回: {"ok": True, "type": "answer"/"confirm_required"/"error",
           "content": str, "tool_trace": [...], "pending_confirm": {...}}
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    message = data.get("message", "")

    if not session_id:
        return jsonify({"ok": False, "error": "缺少 session_id"})
    if not message:
        return jsonify({"ok": False, "error": "缺少 message"})

    # 检查会话是否存在
    agent = _get_agent()
    session = agent.session_mgr.get_session(session_id)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在"})

    # 透传前端鉴权 header
    request_headers = {}
    if request.headers.get("Cookie"):
        request_headers["Cookie"] = request.headers.get("Cookie")
    if request.headers.get("Authorization"):
        request_headers["Authorization"] = request.headers.get("Authorization")
    if request.headers.get("X-Session-Token"):
        request_headers["X-Session-Token"] = request.headers.get("X-Session-Token")

    result = agent.chat(session_id, message, request_headers)
    return jsonify(result)


# ---- 确认执行 ----

@api_bp.route("/agent/confirm", methods=["POST"])
@login_required
def api_agent_confirm():
    """确认执行危险操作。

    请求体: {"session_id": str, "tool_call_id": str, "approved": bool}
    返回: {"ok": True, "type": "answer"/"rejected", "content": str}
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    tool_call_id = data.get("tool_call_id", "")
    approved = data.get("approved", False)

    if not session_id:
        return jsonify({"ok": False, "error": "缺少 session_id"})
    if not tool_call_id:
        return jsonify({"ok": False, "error": "缺少 tool_call_id"})

    agent = _get_agent()
    result = agent.confirm_execute(session_id, tool_call_id, approved)
    return jsonify(result)
