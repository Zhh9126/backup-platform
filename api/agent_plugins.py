# -*- coding: utf-8 -*-
"""统一 Agent 插件能力 API —— 一个 Agent 支撑块级 CDP / BMR / 应用整机容灾。

用户决策（2026-11-24）：客户端只安装一个统一 Agent，多块能力由插件提供。
本 API 通过纳管 agent_host 或直连参数找到 Agent，调用其 /plugin 路由。
所有能力按 Agent 自报 supported 执行，不支持时如实返回，不伪装成功。
"""
from flask import request, jsonify

from auth import login_required, permission_required
from core import agent_hosts
from core.agent_client import AgentTransport, AgentError
from . import api_bp


def _agent_transport(data: dict) -> AgentTransport:
    """按 agent_id 取纳管 Agent；或直连 host/port/token。"""
    aid = data.get("agent_id")
    if aid:
        host = agent_hosts.get_by_id(int(aid))
        if not host:
            raise ValueError("Agent 不存在")
        token = agent_hosts.get_token(host)
        return AgentTransport(str(host.get("hostname")), int(host.get("port") or 8466),
                              token, label=str(host.get("name") or host.get("hostname")))
    host = str(data.get("host") or "").strip()
    token = str(data.get("token") or "")
    if not host or not token:
        raise ValueError("需要 agent_id 或 host+token")
    return AgentTransport(host, int(data.get("port") or 8466), token,
                          label=host)


@api_bp.route("/agent-plugins", methods=["GET"])
@login_required
def list_agent_plugins():
    """列出纳管 Agent 及其插件能力（供 UI 展示"一个 Agent 多能力"）。"""
    rows = []
    for h in agent_hosts.list_hosts():
        item = {"id": h.get("id"), "name": h.get("name"),
                "hostname": h.get("hostname"), "port": h.get("port"),
                "agent_version": h.get("agent_version"),
                "last_status": h.get("last_status"),
                "plugins": [], "error": ""}
        try:
            # list_hosts() 出于安全 pop 了 token，须按 id 取完整记录
            full = agent_hosts.get_by_id(int(h["id"])) or h
            token = agent_hosts.get_token(full)
            cli = AgentTransport(str(h.get("hostname")),
                                 int(h.get("port") or 8466), token,
                                 label=item["name"])
            item["plugins"] = cli.plugins(refresh=True)
        except Exception as e:
            item["error"] = str(e)[:150]
        rows.append(item)
    return jsonify({"success": True, "agents": rows,
                    "plugin_api": "1.0",
                    "note": "一个 Agent 支撑 block-cdp / bmr / app-dr 多块能力"})


@api_bp.route("/agent-plugins/invoke", methods=["POST"])
@login_required
@permission_required("backup.task.manage")
def invoke_agent_plugin():
    """调用 Agent 插件动作。plugin/action 必填，params 按插件契约传。"""
    data = request.get_json(force=True, silent=True) or {}
    plugin = str(data.get("plugin") or "").strip()
    action = str(data.get("action") or "").strip()
    if not plugin or not action:
        return jsonify({"success": False, "error": "plugin/action 必填"}), 400
    try:
        cli = _agent_transport(data)
        result = cli.plugin_invoke(plugin, action, data.get("params") or {})
        status = 200 if result.get("ok") else 400
        return jsonify(dict(result, success=bool(result.get("ok")))), status
    except (ValueError, AgentError) as e:
        return jsonify({"success": False, "error": str(e)[:300]}), 400
    except Exception as e:
        return jsonify({"success": False, "error": "插件调用异常: %s" % str(e)[:200]}), 500
