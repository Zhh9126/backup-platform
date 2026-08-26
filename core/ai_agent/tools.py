# -*- coding: utf-8 -*-
"""
工具注册表 + 7 个 MVP 工具定义。

每个工具的 executor 现在直接调用本地 Python API（models / scheduler /
inspection / db），不再走内部 HTTP 调用，避免 session/cookie 认证问题并
显著降低延迟。
"""

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Any


@dataclass
class Tool:
    """单个工具定义。

    Attributes:
        name: 工具名（唯一标识）
        description: 工具描述（给 LLM 看的）
        parameters: 参数 schema（OpenAI tools JSON Schema 格式）
        requires_confirm: 是否需要用户二次确认才执行
        api_method: HTTP 方法（兼容旧字段，保留）
        api_path: 内部 API 端点路径（兼容旧字段，保留）
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


# ======================== 本地工具执行器 ========================

def _safe_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _run_backup_task_executor(args: Dict, context: Dict) -> Dict:
    """立即运行指定备份任务；返回启动结果/记录。"""
    from core import scheduler
    task_id = _safe_int(args.get("task_id"))
    if not task_id:
        return {"ok": False, "error": "缺少 task_id", "tool": "run_backup_task", "args": args}
    backup_type = args.get("task_type") or args.get("backup_type") or "full"
    try:
        record = scheduler.run_task_now(task_id, backup_type=backup_type, operator="ai_agent")
        if not record:
            return {"ok": False, "error": f"任务 #{task_id} 不存在或启动失败", "tool": "run_backup_task", "args": args}
        # 文件备份是异步 accepted 模式
        if isinstance(record, dict) and record.get("accepted"):
            return {
                "ok": True,
                "message": f"文件备份任务 #{task_id} 已提交后台执行",
                "data": {"task_id": task_id, "status": "running", "accepted": True},
                "tool": "run_backup_task",
                "args": args,
            }
        status = (record.get("status") or "unknown").lower()
        ok = status == "success"
        return {
            "ok": ok,
            "message": f"任务 #{task_id} 执行结果: {record.get('status')}",
            "data": record,
            "tool": "run_backup_task",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "run_backup_task", "args": args}


def _run_inspection_executor(args: Dict, context: Dict) -> Dict:
    """执行巡检并返回汇总结果。"""
    from core import inspection
    scope = args.get("scope", "quick")
    raw_ids = args.get("task_ids") or ""
    task_ids = None
    if raw_ids:
        ids = []
        for part in str(raw_ids).split(","):
            part = part.strip()
            if part:
                try:
                    ids.append(int(part))
                except ValueError:
                    pass
        if len(ids) == 1:
            task_ids = ids[0]
        elif len(ids) > 1:
            # inspection.run_inspection 只支持单个 task_id 或 None
            # 这里取第一个并提示
            task_ids = ids[0]
    try:
        summary = inspection.run_inspection(task_id=task_ids, triggered_by="ai_agent")
        # 巡检"执行成功"与"巡检发现的问题"是两回事：只要巡检流程跑完、拿到汇总，
        # 就算执行成功（ok=True）；具体通过/警告/失败项数由 message/data 体现给用户。
        ok = summary is not None and "total" in summary
        total = summary.get("total", 0)
        passed = summary.get("pass", 0)
        warned = summary.get("warn", 0)
        failed = summary.get("fail", 0)
        return {
            "ok": ok,
            "message": f"巡检已执行完成：共 {total} 项，通过 {passed}，警告 {warned}，失败 {failed}",
            "data": summary,
            "tool": "run_inspection",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "run_inspection", "args": args}


def _list_recent_records_executor(args: Dict, context: Dict) -> Dict:
    """查询最近的备份执行记录。"""
    from core import models
    task_id = _safe_int(args.get("task_id"))
    limit = _safe_int(args.get("limit"), 20)
    try:
        rows = models.list_records(task_id=task_id, limit=limit)
        # 精简字段，避免 LLM token 过大
        simplified = []
        for r in rows:
            simplified.append({
                "id": r.get("id"),
                "task_name": r.get("task_name") or r.get("biz_label") or "-",
                "db_type": r.get("db_type_display") or r.get("db_type"),
                "backup_type": r.get("backup_type_display") or r.get("backup_type"),
                "status": r.get("status"),
                "size_bytes": r.get("size_bytes"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "is_simulated": bool(r.get("is_simulated")),
                "message": r.get("message"),
            })
        return {
            "ok": True,
            "message": f"查询到 {len(simplified)} 条最近备份记录",
            "data": simplified,
            "tool": "list_recent_records",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_recent_records", "args": args}


def _list_alert_predictions_executor(args: Dict, context: Dict) -> Dict:
    """查询 AI 预测告警列表。"""
    from core import models
    metric = args.get("metric")
    days = _safe_int(args.get("days"), 7)
    try:
        rows = models.list_alert_predictions(metric=metric, limit=200)
        # 按 predicted_at 过滤最近 N 天
        if days and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            filtered = []
            for r in rows:
                pa = r.get("predicted_at")
                if pa:
                    try:
                        # 兼容带 Z / +00:00 的 ISO 格式
                        if isinstance(pa, str):
                            pa_dt = datetime.fromisoformat(pa.replace("Z", "+00:00"))
                            if pa_dt.tzinfo is None:
                                pa_dt = pa_dt.replace(tzinfo=timezone.utc)
                            if pa_dt >= cutoff:
                                filtered.append(r)
                        else:
                            filtered.append(r)
                    except Exception:
                        filtered.append(r)
                else:
                    filtered.append(r)
            rows = filtered
        simplified = []
        for r in rows[:50]:
            simplified.append({
                "id": r.get("id"),
                "metric": r.get("metric"),
                "risk_level": r.get("risk_level"),
                "risk_score": r.get("risk_score"),
                "predicted_at": r.get("predicted_at"),
                "predicted_content": r.get("predicted_content"),
                "basis": r.get("basis"),
            })
        return {
            "ok": True,
            "message": f"查询到 {len(simplified)} 条 AI 预测告警",
            "data": simplified,
            "tool": "list_alert_predictions",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_alert_predictions", "args": args}


def _get_storage_usage_executor(args: Dict, context: Dict) -> Dict:
    """查询本地存储空间用量；如指定 target_id 则查该目标，否则查默认本地目标。"""
    from core import db
    target_id = _safe_int(args.get("target_id"))
    try:
        if target_id:
            row = db.query_one("SELECT endpoint, type, name FROM storage_targets WHERE id=?", (target_id,))
        else:
            row = db.query_one(
                "SELECT endpoint, type, name FROM storage_targets WHERE type='local' AND enabled=1 "
                "ORDER BY is_default DESC, id LIMIT 1"
            )
        path = "./backups"
        target_name = "默认本地存储"
        if row:
            path = row.get("endpoint") or path
            target_name = row.get("name") or target_name
        path = os.path.abspath(path)
        du = shutil.disk_usage(path)
        used_percent = round(du.used / du.total * 100, 1) if du.total else 0
        data = {
            "target_id": target_id,
            "target_name": target_name,
            "path": path,
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "total_gb": round(du.total / (1024 ** 3), 2),
            "used_gb": round(du.used / (1024 ** 3), 2),
            "free_gb": round(du.free / (1024 ** 3), 2),
            "used_percent": used_percent,
        }
        return {
            "ok": True,
            "message": f"存储 {target_name} 使用率 {used_percent}%",
            "data": data,
            "tool": "get_storage_usage",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "get_storage_usage", "args": args}


def _list_tasks_executor(args: Dict, context: Dict) -> Dict:
    """列出所有备份任务，可按类型/启用状态过滤。"""
    from core import models
    db_type = args.get("type") or args.get("db_type")
    enabled_raw = args.get("enabled")
    enabled = None
    if enabled_raw is not None:
        enabled = str(enabled_raw).strip() in ("1", "true", "True", "是", "yes")
    try:
        rows = models.list_tasks(db_type=db_type, enabled=enabled)
        simplified = []
        for r in rows:
            simplified.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "db_type": r.get("db_type"),
                "backup_mode": r.get("backup_mode"),
                "host": r.get("host"),
                "port": r.get("port"),
                "enabled": bool(r.get("enabled")),
                "last_status": r.get("last_status"),
                "last_run_at": r.get("last_run_at"),
                "policy_name": r.get("policy_name"),
            })
        return {
            "ok": True,
            "message": f"共有 {len(simplified)} 个备份任务",
            "data": simplified,
            "tool": "list_tasks",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_tasks", "args": args}


def _get_inspection_report_executor(args: Dict, context: Dict) -> Dict:
    """获取最新巡检报告详情。"""
    from core import models, db
    record_id = _safe_int(args.get("record_id"))
    try:
        if record_id:
            row = db.query_one("SELECT * FROM inspection_records WHERE id=?", (record_id,))
        else:
            row = db.query_one("SELECT * FROM inspection_records ORDER BY id DESC LIMIT 1")
        if not row:
            return {"ok": False, "error": "暂无巡检记录", "tool": "get_inspection_report", "args": args}
        detail = row.get("detail") or "{}"
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                pass
        data = {
            "id": row.get("id"),
            "task_name": row.get("task_name"),
            "db_type": row.get("db_type"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "triggered_by": row.get("triggered_by"),
            "detail": detail,
        }
        return {
            "ok": True,
            "message": f"最新巡检记录 #{data['id']} 状态: {data['status']}",
            "data": data,
            "tool": "get_inspection_report",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "get_inspection_report", "args": args}


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
                    "description": "备份类型（full/incremental/log），默认 full",
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
        requires_confirm=True,
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
        description="查询本地存储空间用量，不指定 target_id 时查默认本地存储",
        parameters={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "存储目标 ID，可选（不指定则查默认本地存储）",
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
        description="列出所有备份任务，可按数据库类型/启用状态过滤",
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
