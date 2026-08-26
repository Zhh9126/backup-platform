# -*- coding: utf-8 -*-
"""
工具执行器：通过内部 HTTP 请求调用业务 API。

ToolExecutor 负责：
1. 调本机 127.0.0.1:8080 的后端 API（透传前端鉴权 header）
2. 危险工具（requires_confirm=True）返回 needs_confirm 标记，不直接执行
3. 错误处理：HTTP 错误时返回结构化错误
"""

import json
import urllib.request
import urllib.error
import urllib.parse

from typing import Dict, Any, Optional

import core.db as db

from .tools import ToolRegistry, Tool

_logger = db.get_logger("ai_agent.executor")

# 内部 API 基地址
_DEFAULT_BASE_URL = "http://127.0.0.1:8080"


class ToolExecutor:
    """工具执行器：通过内部 HTTP 调用业务 API。

    Args:
        registry: 工具注册表
        base_url: 内部 API 基地址，默认 http://127.0.0.1:8080
    """

    def __init__(self, registry: ToolRegistry, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.registry = registry
        self.base_url = base_url.rstrip("/")

    def execute(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """执行指定工具。

        Args:
            tool_name: 工具名
            args: 工具参数
            context: 执行上下文（含 request_headers 用于鉴权透传）

        Returns:
            执行结果 dict。危险工具返回 {"needs_confirm": True, "message": "..."}。
            HTTP 错误返回 {"ok": False, "error": str, "status": int}。
        """
        tool = self.registry.get(tool_name)
        if tool is None:
            _logger.warning(f"工具 '{tool_name}' 未注册")
            return {"ok": False, "error": f"工具 '{tool_name}' 未注册"}

        # 危险工具拦截：不直接执行，返回确认标记
        if tool.requires_confirm:
            reason = self._build_confirm_reason(tool, args)
            _logger.info(f"工具 '{tool_name}' 需要确认: {reason}")
            return {
                "needs_confirm": True,
                "tool_name": tool_name,
                "args": args,
                "message": reason,
            }

        # 参数校验
        validated = self._validate_args(tool, args)
        if validated.get("error"):
            return {"ok": False, "error": validated["error"]}
        args = validated.get("args", args)

        # 优先本地 Python 函数执行：避免内部 HTTP 的 session/cookie 认证问题，更快更稳定
        if tool.executor:
            try:
                _logger.info(f"工具 '{tool_name}' 本地执行，参数: {args}")
                return tool.executor(args, context)
            except Exception as e:
                _logger.exception(f"工具 '{tool_name}' 本地执行异常，回退到 HTTP: {e}")

        # 回退：内部 HTTP 调用（兼容旧逻辑；需 request_headers 中的 Cookie）
        api_path = self._resolve_path(tool.api_path, args)
        url = f"{self.base_url}{api_path}"
        request_headers = context.get("request_headers", {})

        try:
            if tool.api_method.upper() == "GET":
                result = self._call_get(url, args, request_headers)
            elif tool.api_method.upper() == "POST":
                result = self._call_post(url, args, request_headers)
            else:
                return {"ok": False, "error": f"不支持的 HTTP 方法: {tool.api_method}"}

            _logger.info(f"工具 '{tool_name}' HTTP 执行成功: {url}")
            return result

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            _logger.error(f"工具 '{tool_name}' HTTP 错误: {e.code} {e.reason}")
            return {"ok": False, "error": f"HTTP {e.code} {e.reason}: {body}", "status": e.code}

        except urllib.error.URLError as e:
            reason_str = str(e.reason)
            _logger.error(f"工具 '{tool_name}' 网络错误: {reason_str}")
            if "timed out" in reason_str.lower():
                return {"ok": False, "error": "内部 API 请求超时", "error_category": "timeout"}
            return {"ok": False, "error": f"网络错误: {reason_str}", "error_category": "network"}

        except Exception as e:
            _logger.error(f"工具 '{tool_name}' 执行异常: {e}")
            return {"ok": False, "error": f"执行异常: {str(e)}"}

    def _call_get(self, url: str, args: Dict, headers: Dict) -> Dict[str, Any]:
        """发起 GET 请求，参数作为 query string。"""
        # GET 工具：参数中不在 path 占位符里的字段作为 query string
        query_params = {}
        for k, v in args.items():
            if v is not None:
                query_params[k] = str(v)

        if query_params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(query_params)

        req = urllib.request.Request(url, method="GET")
        # 透传鉴权 header
        for h_key in ("Cookie", "Authorization", "X-Session-Token"):
            if h_key in headers:
                req.add_header(h_key, headers[h_key])

        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return {"ok": True, "raw": body}
            # 列表/标量响应（多数工具端点 /api/tasks、/api/backup-records 等
            # 返回 JSON 数组）统一包装为 dict，避免调用方对 list 调 .get 崩溃
            if isinstance(parsed, dict):
                return parsed
            return {"ok": True, "data": parsed, "is_collection": isinstance(parsed, list)}

    def _call_post(self, url: str, args: Dict, headers: Dict) -> Dict[str, Any]:
        """发起 POST 请求，参数作为 JSON body（排除 path 占位符已用的字段）。"""
        # POST 工具：path 中的占位符参数已解析，剩余参数放 JSON body
        body_data = json.dumps(args).encode("utf-8")
        req = urllib.request.Request(url, data=body_data, method="POST")
        req.add_header("Content-Type", "application/json")
        # 透传鉴权 header
        for h_key in ("Cookie", "Authorization", "X-Session-Token"):
            if h_key in headers:
                req.add_header(h_key, headers[h_key])

        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return {"ok": True, "raw": body}
            # 列表/标量响应统一包装为 dict，避免调用方对 list 调 .get 崩溃
            if isinstance(parsed, dict):
                return parsed
            return {"ok": True, "data": parsed, "is_collection": isinstance(parsed, list)}

    def _resolve_path(self, api_path: str, args: Dict) -> str:
        """解析 API 路径中的占位符（如 {task_id}）。"""
        path = api_path
        for key, value in args.items():
            placeholder = "{" + key + "}"
            if placeholder in path:
                path = path.replace(placeholder, str(value))
        return path

    def _validate_args(self, tool: Tool, args: Dict) -> Dict:
        """校验工具参数（检查 required 字段是否存在）。

        Returns:
            {"args": cleaned_args} 或 {"error": str}
        """
        required = tool.parameters.get("required", [])
        for field_name in required:
            if field_name not in args or args[field_name] is None or args[field_name] == "":
                return {"error": f"缺少必填参数 '{field_name}'"}

        # 清理参数：只保留 schema 中定义的 properties
        props = tool.parameters.get("properties", {})
        cleaned = {}
        for k, v in args.items():
            if k in props:
                cleaned[k] = v
        # 填充 default 值
        for k, pdef in props.items():
            if k not in cleaned and "default" in pdef:
                cleaned[k] = pdef["default"]

        return {"args": cleaned}

    def _build_confirm_reason(self, tool: Tool, args: Dict) -> str:
        """构造危险操作的确认提示文本。"""
        if tool.name == "run_backup_task":
            task_id = args.get("task_id", "未知")
            return f"即将执行备份任务 {task_id}，该操作会对数据库产生实际影响，请确认是否继续？"
        elif tool.name == "run_inspection":
            scope = args.get("scope", "quick")
            if scope == "full":
                return "全量巡检会对数据库性能产生较大影响，请确认是否继续？"
            return "巡检操作会短暂影响数据库性能，请确认是否继续？"
        else:
            return f"操作 '{tool.name}' 需要二次确认，请确认是否继续？"
