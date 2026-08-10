# -*- coding: utf-8 -*-
"""
工具注册表 + 7 个 MVP 工具定义。

Tool dataclass 存 OpenAI tools JSON Schema 格式的参数定义，
ToolRegistry 提供注册/查找/导出为 OpenAI tools JSON 的能力。
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any


@dataclass
class Tool:
    """单个工具定义。

    Attributes:
        name: 工具名（唯一标识）
        description: 工具描述（给 LLM 看的）
        parameters: 参数 schema（OpenAI tools JSON Schema 格式）
        requires_confirm: 是否需要用户二次确认才执行
        api_method: HTTP 方法（GET / POST）
        api_path: 内部 API 端点路径（可能含 {param} 占位符）
        executor: 实际执行函数 (args: dict, context: dict) -> dict
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    requires_confirm: bool = False
    api_method: str = "GET"
    api_path: str = ""
    executor: Optional[Callable[[Dict, Dict], Dict]] = None


class ToolRegistry:
    """工具注册表：注册、查找、导出工具列表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。重复注册会覆盖。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """按名称查找工具，不存在返回 None。"""
        return self._tools.get(name)

    def list_all(self) -> List[Tool]:
        """返回所有已注册工具列表。"""
        return list(self._tools.values())

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """导出为 OpenAI function calling 格式的 tools 列表。

        Returns:
            [{"type": "function", "function": {"name", "description", "parameters"}}]
        """
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return result

    def tools_description_for_prompt(self) -> str:
        """生成给 ReAct prompt 的工具描述文本段。

        格式：
        - tool_name: 描述（⚠需确认）。参数: {param_schema}
        """
        lines = []
        for tool in self._tools.values():
            confirm_mark = "（⚠需确认）" if tool.requires_confirm else ""
            # 从 parameters 中提取简洁的参数摘要
            props = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            param_parts = []
            for pname, pdef in props.items():
                ptype = pdef.get("type", "string")
                req_mark = "(必填)" if pname in required else ""
                desc = pdef.get("description", "")
                param_parts.append(f"{pname}:{ptype}{req_mark}")
            param_str = ", ".join(param_parts) if param_parts else "无参数"
            lines.append(f"- {tool.name}: {tool.description}{confirm_mark}。参数: {param_str}")
        return "\n".join(lines)


# ======================== 7 个 MVP 工具定义 ========================

# ---- 工具执行器占位（实际实现由 executor.py 调内部 HTTP API） ----

def _run_backup_task_executor(args: Dict, context: Dict) -> Dict:
    """执行 run_backup_task：POST /api/tasks/{task_id}/run。"""
    # executor.py 的 ToolExecutor 会在实际执行时替换此占位
    return {"ok": True, "placeholder": True, "tool": "run_backup_task", "args": args}


def _run_inspection_executor(args: Dict, context: Dict) -> Dict:
    """执行 run_inspection：POST /api/inspection/run。"""
    return {"ok": True, "placeholder": True, "tool": "run_inspection", "args": args}


def _list_recent_records_executor(args: Dict, context: Dict) -> Dict:
    """执行 list_recent_records：GET /api/backup-records。"""
    return {"ok": True, "placeholder": True, "tool": "list_recent_records", "args": args}


def _list_alert_predictions_executor(args: Dict, context: Dict) -> Dict:
    """执行 list_alert_predictions：GET /api/alerts/predictions。"""
    return {"ok": True, "placeholder": True, "tool": "list_alert_predictions", "args": args}


def _get_storage_usage_executor(args: Dict, context: Dict) -> Dict:
    """执行 get_storage_usage：GET /api/storage/usage。"""
    return {"ok": True, "placeholder": True, "tool": "get_storage_usage", "args": args}


def _list_tasks_executor(args: Dict, context: Dict) -> Dict:
    """执行 list_tasks：GET /api/tasks。"""
    return {"ok": True, "placeholder": True, "tool": "list_tasks", "args": args}


def _get_inspection_report_executor(args: Dict, context: Dict) -> Dict:
    """执行 get_inspection_report：GET /api/inspection/records。"""
    return {"ok": True, "placeholder": True, "tool": "get_inspection_report", "args": args}


# ---- 工具实例 ----

TOOL_DEFINITIONS: List[Tool] = [
    Tool(
        name="run_backup_task",
        description="立即运行指定备份任务，返回执行状态和启动时间",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "备份任务 ID",
                },
                "task_type": {
                    "type": "string",
                    "description": "备份类型（full/incremental/log），可选",
                },
            },
            "required": ["task_id"],
        },
        requires_confirm=True,
        api_method="POST",
        api_path="/api/tasks/{task_id}/run",
        executor=_run_backup_task_executor,
    ),
    Tool(
        name="run_inspection",
        description="立即执行巡检，可指定任务或全量巡检",
        parameters={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "string",
                    "description": "巡检任务 ID 列表（逗号分隔），可选",
                },
                "scope": {
                    "type": "string",
                    "enum": ["quick", "full"],
                    "description": "巡检范围：quick 快速巡检 / full 全量巡检",
                },
            },
        },
        requires_confirm=True,  # scope=full 时需确认，此处统一标记，Agent 内部细化判断
        api_method="POST",
        api_path="/api/inspection/run",
        executor=_run_inspection_executor,
    ),
    Tool(
        name="list_recent_records",
        description="查询最近的备份执行记录",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "按任务 ID 过滤，可选",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录数量上限，默认 20",
                    "default": 20,
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/records",
        executor=_list_recent_records_executor,
    ),
    Tool(
        name="list_alert_predictions",
        description="查询 AI 预测告警列表",
        parameters={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "按指标类型过滤（backup_fail/storage_full/link_degraded/drill_overdue/rpo_breach），可选",
                },
                "days": {
                    "type": "integer",
                    "description": "查询最近 N 天的预测，默认 7",
                    "default": 7,
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/alerts/predictions",
        executor=_list_alert_predictions_executor,
    ),
    Tool(
        name="get_storage_usage",
        description="查询存储空间用量",
        parameters={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "存储目标 ID，可选（不指定则查所有）",
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/storage/usage",
        executor=_get_storage_usage_executor,
    ),
    Tool(
        name="list_tasks",
        description="列出所有备份任务，可按类型/启用状态过滤",
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "按数据库类型过滤（mysql/postgresql/oracle 等），可选",
                },
                "enabled": {
                    "type": "string",
                    "description": "按启用状态过滤（1=启用/0=禁用），可选",
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/tasks",
        executor=_list_tasks_executor,
    ),
    Tool(
        name="get_inspection_report",
        description="获取最新巡检报告详情",
        parameters={
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "巡检记录 ID，可选（不指定则取最新一条）",
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/inspection/records",
        executor=_get_inspection_report_executor,
    ),
]


def create_default_registry() -> ToolRegistry:
    """创建并注册 7 个 MVP 工具的默认注册表。"""
    registry = ToolRegistry()
    for tool_def in TOOL_DEFINITIONS:
        registry.register(tool_def)
    return registry
