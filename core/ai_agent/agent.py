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
import os
import re
import time
import uuid
import urllib.error
from typing import Dict, List, Any, Optional

import core.db as db
from core.ai_alert import AIPredictor

from .tools import ToolRegistry, create_default_registry, needs_confirm
from .executor import ToolExecutor
from .session import SessionManager
from .skills import SKILLS, FALLBACK_HELP
from .router import SkillRouter
from .skills import KNOWLEDGE, SKILLS, FALLBACK_HELP
from .router import SkillRouter

# 本地意图路由器（惰性单例）。
# 构建索引需要遍历数百条示例语句，放到首次使用时初始化，避免拖慢平台启动；
# 平台的目标交付形态是完全离线环境，这条通道必须不依赖任何网络与模型。
_LOCAL_ROUTER: Optional[SkillRouter] = None


def get_local_router() -> SkillRouter:
    """获取（首次调用时构建）本地意图路由器。"""
    global _LOCAL_ROUTER
    if _LOCAL_ROUTER is None:
        _LOCAL_ROUTER = SkillRouter(SKILLS)
    return _LOCAL_ROUTER

# 本地直答阈值：只接管高置信度的元问题 / 概念答疑，避免抢答复杂问题。
# 这类问题交给 LLM 既费 token 又费时延，离线时还必然失败。
_LOCAL_ANSWER_MIN_SCORE = 0.80
_LOCAL_KB_MIN_SCORE = 0.86

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

# 自然语言动作的跨轮槽位记忆（session_id → 上次成功使用的槽位）。
# 用于「再备份一次」「同样查一下」这类指代，避免每轮都要求重复报编号。
_NL_CONTEXT: Dict[str, Dict] = {}

# 本地意图引擎开关：命中高置信技能时跳过 LLM 直接执行/直答。
#
# 为什么要本地优先（offline-first）：平台交付形态为完全离线环境，LLM endpoint
# 常常不可达（典型：DNS 失败的 Errno -2）。把所有请求先过模型，等于让「核心功能」
# 的可用性完全押在一条外网上。本地引擎覆盖到高置信场景时直接响应，既能保证
# 离线可用，也让常见指令从「等 3~60 秒的 LLM 往返」变成「毫秒级」。
#
# 设为 false 则回到「LLM 优先、本地兜底」的旧行为（排查/对比用时）。
AGENT_LOCAL_FIRST = os.getenv(
    "BP_AGENT_LOCAL_FIRST", "1").strip().lower() not in ("0", "false", "no", "off")

# 自然语言动作层开关。执行依赖「透传真实会话给内部 API」，因此只有真实
# Web 请求（chat 的 request_headers 非空）才启用；单元测试直接调用 agent.chat
# 时没有会话头，自动回到既有工具链路，不破坏旧契约。设为 0 可整体回退。
AGENT_NL_ACTIONS = os.getenv(
    "BP_AGENT_NL_ACTIONS", "1").strip().lower() not in ("0", "false", "no", "off")

# ---- ReAct System Prompt ----

SYSTEM_PROMPT_TEMPLATE = """你是 AIDBM（AI 原生智能数据库灾备管理平台）的 AI 智能运维助手，可以用自然语言驱动平台完成备份执行、巡检与信息查询。

## 可用工具

{tools_description}

## 操作分级（重要）

- **可直接执行**（直接返回 tool_call，不要 confirm_required）：
  - 查询类：list_tasks / list_recent_records / get_storage_usage / list_alert_predictions / get_inspection_report
  - run_backup_task：备份是低风险操作（只读取源库、写入平台存储），用户明确表示要备份某个库/任务时，
    直接执行，并把真实结果（成功/失败、大小、耗时、产物路径）回报给用户。
  - run_inspection（scope=quick）
- **必须用户确认**（返回 confirm_required）：run_inspection（scope=full，全量巡检对数据库有性能影响）。

## 任务定位规则（自然语言 → 备份任务）

1. 用户用口语描述目标时（如"帮我执行 bpm1 库的备份"、"备份一下生产 MySQL"、"把附件桶备份一下"），
   把关键描述放进 run_backup_task 的 task_name 参数（如 "bpm1"、"生产 MySQL"、"附件桶"），系统会自动模糊定位任务。
2. 绝不自己编造 task_id。只有当用户明确给出 ID（如"执行任务 12"）时才传 task_id。
3. 工具返回 needs_clarify=true（匹配到多个任务）时，禁止再次调用工具：直接输出 answer，
   用 Markdown 表格列出候选任务（ID / 名称 / 库类型 / 主机）并请用户确认执行哪一个。
4. 工具返回"没有找到匹配任务"时，输出 answer 说明未找到，并给出可用任务示例或建议先"列出所有备份任务"。

## 输出格式（Markdown，面向运维人员）

- 先给一句结论（含关键数字），再按需展开细节；不要复述工具的原始 JSON 字段名。
- 数据 ≥3 条时使用 Markdown 表格；列数不超过 6 列，只保留用户关心的字段。
- 状态值（success / failed / running / never 等）必须用行内代码包裹（如 `success`），前端会渲染成彩色徽章。
- 数值带单位（MB/GB/%），时间统一为 YYYY-MM-DD HH:MM。
- 行数多时只展示最重要的 5~15 行，并注明"共 N 条，仅展示前 M 条"。
- 结尾可给 1 条后续建议（如"如需重跑，回复：执行任务 12"）。
- 全文使用简体中文，禁止出现 type / tool / args 等技术字段，禁止输出 JSON 代码围栏。
- 可使用 **加粗**、`行内代码`、- 列表、### 小标题、表格，但不要滥用。

## JSON 输出协议

你必须严格按以下 JSON 格式输出，JSON 之外的说明文字一律不要输出（Markdown 只能写在 content 字段里）：

### 纯问答（不调用工具）：
```json
{{"type": "answer", "content": "你的回答文本"}}
```

### 直接执行（查询类与低风险执行类）：
```json
{{"type": "tool_call", "tool": "工具名", "args": {{参数对象}}}}
```

### 需要确认的高风险操作（如全量巡检）：
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
{{"type": "tool_call", "tool": "list_recent_records", "args": {{"only_failed": "true", "limit": 10}}}}
```

用户: "bpm1 这个库有哪些备份任务？"
助手: ```json
{{"type": "tool_call", "tool": "list_tasks", "args": {{"keyword": "bpm1"}}}}
```

用户: "帮我执行 bpm1 库的备份"
助手: ```json
{{"type": "tool_call", "tool": "run_backup_task", "args": {{"task_name": "bpm1"}}}}
```

用户: "备份一下生产 MySQL"
助手: ```json
{{"type": "tool_call", "tool": "run_backup_task", "args": {{"task_name": "生产 MySQL"}}}}
```

用户: "立即执行备份任务 5"
助手: ```json
{{"type": "tool_call", "tool": "run_backup_task", "args": {{"task_id": "5"}}}}
```

用户: "帮我跑一次快速巡检"
助手: ```json
{{"type": "tool_call", "tool": "run_inspection", "args": {{"scope": "quick"}}}}
```

用户: "帮我跑一次全量巡检"
助手: ```json
{{"type": "confirm_required", "tool": "run_inspection", "args": {{"scope": "full"}}, "reason": "全量巡检会对数据库性能产生较大影响，请确认是否继续？"}}
```

用户: "什么是RPO？"
助手: ```json
{{"type": "answer", "content": "RPO（Recovery Point Objective）是恢复点目标，指灾难发生后允许丢失的数据量时间窗口。"}}
```

用户: "查询存储用量"
助手: ```json
{{"type": "tool_call", "tool": "get_storage_usage", "args": {{}}}}
```

工具返回: {{"ok": true, "message": "存储 本地存储 使用率 24%，总容量 500.00 GB，已用 120.00 GB，可用 380.00 GB", "data": {{"target_name": "本地存储", "path": "/data/backups", "total_human": "500.00 GB", "used_human": "120.00 GB", "free_human": "380.00 GB", "total_gb": 500, "used_gb": 120, "free_gb": 380, "used_percent": 24}}}}
助手: ```json
{{"type": "answer", "content": "本地存储 **/data/backups** 使用率 24%，空间充足。\\n\\n| 项目 | 数值 |\\n|---|---|\\n| 总空间 | 500.00 GB |\\n| 已用 | 120.00 GB |\\n| 可用 | 380.00 GB |"}}
```

工具返回（执行备份成功）: {{"ok": true, "message": "备份任务「bpm1 每日备份」执行成功", "data": {{"task_id": 12, "task_name": "bpm1 每日备份", "status": "success", "size_human": "12.40 MB", "duration_sec": 3.2, "record_id": 8841}}}}
助手: ```json
{{"type": "answer", "content": "备份任务「bpm1 每日备份」已执行 **成功**，产物 12.40 MB，耗时 3.2 秒。\\n\\n| 项目 | 结果 |\\n|---|---|\\n| 状态 | `success` |\\n| 产物大小 | 12.40 MB |\\n| 耗时 | 3.2 秒 |\\n| 记录 ID | 8841 |\\n\\n如需查看历史记录，回复：最近备份记录。"}}
```

工具返回（名称歧义）: {{"ok": false, "needs_clarify": true, "error": "「备份」匹配到 3 个备份任务", "candidates": [{{"id": 12, "name": "bpm1 每日备份", "db_type": "MySQL", "host": "127.0.0.1"}}, {{"id": 18, "name": "ERP 库备份", "db_type": "PostgreSQL", "host": "10.0.0.8"}}]}}
助手: ```json
{{"type": "answer", "content": "「备份」匹配到多个任务，请确认执行哪一个：\\n\\n| ID | 名称 | 类型 | 主机 |\\n|---|---|---|---|\\n| 12 | bpm1 每日备份 | MySQL | 127.0.0.1 |\\n| 18 | ERP 库备份 | PostgreSQL | 10.0.0.8 |\\n\\n回复「执行任务 12」即可立即执行。"}}
```

## 约束
1. 一次只调用一个工具。
2. 低风险执行类操作（run_backup_task、scope=quick 的巡检）直接返回 tool_call 执行，不要 confirm_required；只有全量巡检等高风险操作才返回 confirm_required。
3. 查询类工具（list_tasks / list_recent_records / get_storage_usage / list_alert_predictions / get_inspection_report）不需要确认，直接调用。
4. 不确定任务时，先使用 list_tasks（可带 keyword）查询，再决定下一步。
5. 绝不虚构 task_id、target_id 等 ID，不确定时先用 list_tasks 查询。
6. get_storage_usage 不指定 target_id 时默认查询实际生效的 BACKUP_ROOT；容量优先使用 total_human / used_human / free_human，禁止把 *_bytes 裸字节直接写给用户。
7. 你给出的 content 要简洁、面向用户，使用 Markdown（表格 / 列表 / 加粗 / 行内代码），不要包含 JSON 结构、代码围栏或 "type"/"tool"/"args" 等技术字段。
8. 当消息历史中已经包含某工具的返回结果（role=tool）时，必须直接输出 answer 总结该结果，禁止再次调用同一工具。
9. 工具返回 needs_clarify=true（任务名有歧义）时，直接输出 answer 列出候选让用户选择，禁止改参数重试。
10. 工具执行失败时如实说明失败原因与建议动作，不要美化或隐瞒结果。"""


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
        # 本地意图引擎（离线决策主干，见 skills.py / router.py）
        self.local_router = get_local_router()
        # 本地快速通道开关：默认取全局配置，可按会话/实例覆盖（测试与灰度用）
        self.local_first = AGENT_LOCAL_FIRST

    # ------------------------------------------------------------------
    # 自然语言动作层（离线优先）：覆盖全站功能域，真实调用平台 API
    # ------------------------------------------------------------------
    @staticmethod
    def _trim_history_payload(payload):
        """历史消息体积护栏：长列表只留前 20 条，并如实标注已截断。"""
        if not isinstance(payload, dict):
            return payload
        out = dict(payload)
        truncated = False
        for k, v in list(out.items()):
            if isinstance(v, list) and len(v) > 20:
                out[k] = v[:20]
                truncated = True
            elif isinstance(v, dict):
                sub = dict(v)
                for k2, v2 in list(sub.items()):
                    if isinstance(v2, list) and len(v2) > 20:
                        sub[k2] = v2[:20]
                        truncated = True
                if sub != v:
                    out[k] = sub
        if truncated:
            out["_truncated"] = "历史消息仅保留前 20 条，完整结果见当次回答"
        return out

    def _persist_nl_answer(self, session_id: str, content: str,
                           trace: List[Dict]) -> None:
        """NL 动作回答落库：补写 tool_calls 与 role=tool 消息。

        此前只落 content，刷新会话后工具结果全丢，步骤卡也随之消失；
        现在历史消息能像 ReAct 路径一样重建完整调用 + 结果轨迹。
        """
        trace = trace or []
        calls = [{"name": t.get("name"), "args": t.get("args") or {}}
                 for t in trace]
        self.session_mgr.add_assistant_message(session_id, content,
                                               tool_calls=calls or None)
        for t in trace:
            self.session_mgr.add_tool_message(
                session_id, t.get("name") or "tool",
                self._trim_history_payload(t.get("result")))

    def _nl_actions_handle(self, session_id: str, message: str,
                           context: Dict) -> Optional[Dict[str, Any]]:
        """把自然语言编译成动作计划；命中则本地执行/请求确认，未命中放行给既有链路。"""
        from core.ai_agent import nl_actions
        txt = (message or "").strip()
        if not txt:
            return None

        pending = _pending_confirms.get(session_id) or {}
        if pending.get("kind") == "nl_actions":
            if any(w in txt for w in ("取消", "放弃", "不要了", "算了")):
                _pending_confirms.pop(session_id, None)
                content = "已取消，未执行任何操作。"
                self.session_mgr.add_assistant_message(session_id, content)
                return {"ok": True, "type": "rejected", "content": content,
                        "local_engine": True}
            if any(w in txt.lower() for w in
                   ("确认", "确定", "执行", "继续", "同意", "好的", "是的", "yes", "ok")):
                return self._confirm_nl_actions(
                    session_id, pending.get("tool_call_id"), True, txt)
            # 用户改说别的：丢弃旧待确认计划，避免误执行
            _pending_confirms.pop(session_id, None)

        anaphora = any(w in txt for w in
                       ("再", "又", "同样", "重复", "刚才", "上次", "它", "这个任务"))
        plan = nl_actions.plan(txt, _NL_CONTEXT.get(session_id) if anaphora else None)
        acts = plan.get("actions") or []
        if not acts:
            return None
        # 上下文只在「真实执行成功」之后记忆（见下方 read 分支与
        # _confirm_nl_actions）；被拦截/取消/待确认的动作不得污染指代。

        def _answer(content: str, **extra) -> Dict[str, Any]:
            self._persist_nl_answer(session_id, content, extra.get("tool_trace"))
            out = {"ok": True, "type": "answer", "content": content,
                   "local_engine": True, "nl_actions": True}
            out.update(extra)
            return out

        if plan.get("clarify"):
            cands = plan["clarify"]
            lines = ["找到多个匹配任务，请指定其一（回复「执行任务 <ID>」）："]
            lines += ["- %s %s" % (c["id"], c["name"]) for c in cands]
            return _answer("\n".join(lines), needs_clarify=True, candidates=cands)
        if plan.get("error"):
            return _answer("未能确定操作对象：%s。请给出明确的编号或名称。" % plan["error"])
        if plan.get("missing"):
            hints = []
            for a in acts:
                for cat in plan["missing"]:
                    if cat.startswith(a["name"] + ":"):
                        act = nl_actions._BY_NAME.get(a["name"]) or {}
                        hints.append(act.get("slot_hint") or "请补充参数 %s" % cat)
            return _answer("；".join(dict.fromkeys(hints)) or "请补充要操作的对象。")

        needs_confirm = any(a.get("risk") in ("write", "high") for a in acts)
        if needs_confirm:
            reason = nl_actions.format_plan_preview(plan)
            tool_call_id = str(uuid.uuid4())
            _pending_confirms[session_id] = {
                "kind": "nl_actions", "tool_call_id": tool_call_id,
                "tool_name": "nl_actions", "args": {}, "reason": reason,
                "context": context, "compiled": plan,
            }
            self.session_mgr.add_assistant_message(session_id, reason)
            return {
                "ok": True, "type": "confirm_required", "content": reason,
                "local_engine": True, "nl_actions": True,
                "pending_confirm": {
                    "tool_call_id": tool_call_id, "tool_name": "自然语言动作",
                    "args": {}, "reason": reason,
                },
            }

        result = nl_actions.execute(plan, context.get("request_headers") or {})
        if result.get("ok"):
            _NL_CONTEXT.setdefault(session_id, {}).update(plan["slots"])
        content = result["content"]
        return _answer(content, tool_trace=[
            {"name": s["name"], "label": s.get("desc") or s["name"],
             "args": s.get("query") or {}, "result": s.get("data"),
             "detail": s.get("detail") or ""}
            for s in result["steps"]])

    def _confirm_nl_actions(self, session_id: str, tool_call_id: str,
                            approved: bool, message: str = "") -> Dict[str, Any]:
        """执行已确认的自然语言动作计划（前端弹窗与「确认」自然语言走同一路径）。"""
        from core.ai_agent import nl_actions
        pending = _pending_confirms.get(session_id)
        if not pending or pending.get("tool_call_id") != tool_call_id:
            return {"ok": False, "error": "确认请求不存在或已过期"}
        _pending_confirms.pop(session_id, None)
        if not approved:
            content = "用户拒绝了该操作，已取消执行。"
            self.session_mgr.add_assistant_message(session_id, content)
            return {"ok": True, "type": "rejected", "content": content,
                    "local_engine": True}
        extra = {}
        if message:
            slots = nl_actions.parse_slots(message)
            if slots.get("verify_code"):
                extra["verify_code"] = slots["verify_code"]
        context = pending.get("context") or {}
        result = nl_actions.execute(
            pending["compiled"], context.get("request_headers") or {}, extra)
        if result.get("ok"):
            _NL_CONTEXT.setdefault(session_id, {}).update(
                pending["compiled"].get("slots") or {})
        content = result["content"]
        trace = [{"name": s["name"], "label": s.get("desc") or s["name"],
                  "args": s.get("query") or {}, "result": s.get("data"),
                  "detail": s.get("detail") or ""}
                 for s in result["steps"]]
        self._persist_nl_answer(session_id, content, trace)
        return {"ok": True, "type": "answer", "content": content,
                "local_engine": True, "nl_actions": True,
                "tool_trace": trace}

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

            # 1.5 自然语言动作层：离线优先，命中全站动作则直接真实执行/请求确认；
            #     未命中（闲聊、概念答疑、复杂问法）放行给既有 SkillRouter / LLM。
            if AGENT_NL_ACTIONS and request_headers:
                try:
                    nl_result = self._nl_actions_handle(
                        session_id, user_message, context)
                    if nl_result is not None:
                        return nl_result
                except Exception as e:
                    _logger.warning("自然语言动作层异常，回退既有链路: %s", e)

            # 2. 构建 system prompt
            system_prompt = self._build_system_prompt()

            # 3. ReAct 循环
            result = self._react_loop(session_id, system_prompt, context, user_message)

            # 离线未配置模型时，把「死胡同」补成可执行的能力引导
            if isinstance(result, dict) and result.get("type") == "error" \
                    and "未配置模型" in str(result.get("content") or ""):
                try:
                    from core.ai_agent import nl_actions
                    result["content"] = (result.get("content") or "") + "\n\n" + \
                        nl_actions.describe_capabilities()
                except Exception:
                    pass
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

        # 自然语言动作计划：前端确认弹窗与「确认」自然语言共用同一执行路径
        if pending.get("kind") == "nl_actions":
            return self._confirm_nl_actions(session_id, tool_call_id, approved)

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
            # 执行工具（用户已确认，直接执行不再拦截）
            tool = self.registry.get(tool_name)
            if tool and needs_confirm(tool, args):
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

    # ------------------------------------------------------------------
    # 本地快速通道（offline-first）：高置信请求跳过 LLM 直接响应
    # ------------------------------------------------------------------

    def _local_fast_path(self, session_id: str, user_message: str,
                         context: Dict) -> Optional[Dict[str, Any]]:
        """本地快速通道：命中高置信技能时跳过 LLM 直接响应。

        为什么要「先本地、后 LLM」：平台交付形态为完全离线环境，把所有请求都先
        过模型，等于把核心功能的可用性押在一条外网上；而常见运维指令（查任务、
        查备份记录、执行备份、跑巡检、问灾备概念）本地引擎已能高置信判定，
        命中即可毫秒级返回，不必等 2~3 次串行 LLM 往返。

        两条本地通道必须分清，否则会互相吞请求：
        - **快速通道**（本方法）：``fast_only=True``，只接受高置信、无缺槽位、
          非多步、无歧义的请求；达不到就放行给 LLM。
        - **兜底通道**（``_fallback_intent_handle``）：LLM 失败后才启用，
          放宽到 ``accept_min``，保证离线时仍有实质回答。
        """
        if not user_message or not user_message.strip():
            return None
        match = get_local_router().route(user_message, fast_only=True)
        if match is None:
            return None
        result = self._dispatch_local_match(session_id, match, context)
        if result is not None:
            _logger.info(
                f"[本地通道] intent={match.name} score={match.score:.3f} "
                f"reason={match.reason}")
        return result

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

        # ---- 本地快速通道（offline-first） ----
        # 高置信、无缺槽位、非多步且无歧义时，直接由本地意图引擎响应：
        # 离线环境也能用，且省掉 2~3 次串行 LLM 往返（原本一轮对话要等 3~60s）。
        # 只有本地拒识，才把请求交给 LLM。
        if getattr(self, "local_first", AGENT_LOCAL_FIRST) and user_message:
            local_fast = self._local_fast_path(session_id, user_message, context)
            if local_fast:
                _logger.info(f"[本地通道] intent={local_fast.get('intent')} "
                             f"type={local_fast.get('type')}")
                return local_fast

        # 构建 LLM 输入消息列表
        # 注意：用户消息已经保存到 session，build_messages_for_llm 会包含它
        # new_user_msg 传空字符串，避免重复添加
        messages = self.session_mgr.build_messages_for_llm(
            session_id, system_prompt, "")

        while round_count < MAX_REACT_ROUNDS:
            round_count += 1

            # 第一轮先看本地能否直接回答（元问题 / 闲聊 / 概念答疑）：
            # 这类问题交给 LLM 纯属浪费 token，且在离线环境下必然失败。
            # 必须由 local_first 统一开关：否则关闭本地通道时无法验证 LLM 路径。
            if round_count == 1 and getattr(self, "local_first", AGENT_LOCAL_FIRST):
                quick = self._local_quick_answer(session_id, user_message)
                if quick:
                    return quick

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
                    self.session_mgr.add_assistant_message(session_id, fallback)
                    return {"ok": True, "type": "answer", "content": fallback, "tool_trace": tool_trace}

                # LLM 完全不可用（网络/超时/未配置）且尚未执行任何工具：
                # 启用本地意图识别兜底，确保用户每次提问都有实质信息返回，
                # 而不是空白或"模型未返回内容"的无意义提示。
                fb = self._fallback_intent_handle(session_id, user_message, context)
                if fb:
                    return fb

                # 本地意图引擎也没把握时才降级为「能力清单 + 技术状态」。
                # 不返回 error 类型：对运维人员而言这不是「操作失败」而是「换种说法」，
                # 仍标记为 degraded，便于排障与前端灰态提示。
                detail = str(llm_result.get("error") or llm_result.get("message") or "").strip()
                hint = str(llm_result.get("hint") or "").strip()
                content = FALLBACK_HELP
                if detail:
                    content += f"\n\n> 当前模型服务状态：{detail}"
                    if hint:
                        content += f"\n> 处置建议：{hint}"
                    content += "（本地意图引擎已生效，不影响上面的查询类操作）"
                self.session_mgr.add_assistant_message(session_id, content)
                return {
                    "ok": True, "type": "answer", "content": content,
                    "degraded": True, "model_error": detail, "source": "local",
                }

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

                # 防御性检查：免确认的工具已经拿到结果就不要再重复调用，
                # 避免 LLM 陷入死循环导致最后只能返回兜底文案
                if not needs_confirm(tool, args) and any(
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

                # 检查是否需要确认（按工具风险等级与参数动态判定）
                if needs_confirm(tool, args):
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
                _step_started = time.time()
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

                tool_trace.append({
                    "name": tool_name,
                    "args": args,
                    "result": exec_result,
                    "risk_level": getattr(tool, "risk_level", "low"),
                    "duration_ms": int((time.time() - _step_started) * 1000),
                })

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

    @staticmethod
    def _md_cell(value) -> str:
        """Markdown 表格单元格：转义竖线、换行与空值占位。"""
        text = "-" if value in (None, "") else str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _status_text(value) -> str:
        """状态值文本（调用处以行内代码包裹，前端渲染为彩色徽章）。"""
        text = str(value or "").strip().lower()
        return text or "unknown"

    @staticmethod
    def _fmt_time(value) -> str:
        """时间统一为 YYYY-MM-DD HH:MM。"""
        text = str(value or "").strip()
        if not text:
            return "-"
        return text[:16].replace("T", " ")

    @staticmethod
    def _size_human(size) -> str:
        """字节数转可读字符串。"""
        try:
            n = float(size or 0)
        except (TypeError, ValueError):
            return "-"
        if n <= 0:
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return "{} B".format(int(n)) if unit == "B" else "{:.2f} {}".format(n, unit)
            n /= 1024
        return "{:.2f} TB".format(n)

    def _format_tool_result_to_answer(self, tool_name: str, result: Dict) -> str:
        """将单个工具结果格式化为面向用户的文本。"""
        if not isinstance(result, dict):
            return f"工具 {tool_name} 返回格式异常，请稍后重试。"

        ok = result.get("ok")
        message = result.get("message") or ""
        data = result.get("data")
        error = result.get("error")

        # 任务名歧义：列出候选让用户澄清（绝不猜测执行）
        if result.get("needs_clarify"):
            cands = result.get("candidates") or []
            lines = ["**{}**".format(error or "匹配到多个备份任务，请确认执行哪一个"), ""]
            lines.append("| ID | 名称 | 类型 | 主机 | 状态 |")
            lines.append("|---|---|---|---|---|")
            for c in cands[:10]:
                host = c.get("host") or "-"
                if host != "-" and c.get("port"):
                    host = "{}:{}".format(host, c.get("port"))
                name = self._md_cell(c.get("name"))
                if c.get("recommended"):
                    name = "**{}**（最相关）".format(name)
                lines.append(
                    "| {} | {} | {} | {} | `{}` |".format(
                        c.get("id", "-"),
                        name,
                        self._md_cell(c.get("db_type")),
                        self._md_cell(host),
                        self._status_text(c.get("last_status")),
                    )
                )
            lines.append("")
            lines.append("点击下方候选按钮直接执行，或回复「执行任务 <ID>」。")
            return "\n".join(lines)

        if not ok:
            hint = result.get("hint")
            text = "操作未成功：{}".format(error or message or "未知错误")
            if hint:
                text += "\n\n> {}".format(hint)
            avail = result.get("available_tasks")
            if avail:
                text += "\n\n可用任务示例：" + "、".join(str(a) for a in avail[:6])
            return text

        # 根据工具类型做格式化展示
        if tool_name == "list_tasks":
            rows = data or []
            if not rows:
                return "当前没有任何备份任务。"
            lines = ["共有 **{}** 个备份任务：".format(len(rows)), ""]
            lines.append("| ID | 名称 | 类型 | 主机 | 状态 |")
            lines.append("|---|---|---|---|---|")
            for r in rows[:15]:
                host = r.get("host") or "-"
                if host != "-" and r.get("port"):
                    host = "{}:{}".format(host, r.get("port"))
                lines.append(
                    "| {} | {} | {} | {} | `{}` |".format(
                        r.get("id", "-"),
                        self._md_cell(r.get("name")),
                        self._md_cell(r.get("db_type") or r.get("db_type_display")),
                        self._md_cell(host),
                        self._status_text(r.get("last_status") or ("enabled" if r.get("enabled") else "never")),
                    )
                )
            if len(rows) > 15:
                lines.append("")
                lines.append("> 共 {} 条，仅展示前 15 条。可按关键字查询，例如：bpm1 有哪些备份任务？".format(len(rows)))
            return "\n".join(lines)

        if tool_name == "list_recent_records":
            rows = data or []
            if not rows:
                return "最近没有符合条件的备份记录。"
            lines = ["最近 **{}** 条备份记录：".format(len(rows)), ""]
            lines.append("| 记录 | 任务 | 类型 | 状态 | 大小 | 开始时间 |")
            lines.append("|---|---|---|---|---|---|")
            for r in rows[:15]:
                lines.append(
                    "| {} | {} | {} | `{}` | {} | {} |".format(
                        r.get("id", "-"),
                        self._md_cell(r.get("task_name")),
                        self._md_cell(r.get("backup_type")),
                        self._status_text(r.get("status")),
                        self._md_cell(r.get("size_human") or self._size_human(r.get("size_bytes"))),
                        self._fmt_time(r.get("started_at")),
                    )
                )
            if len(rows) > 15:
                lines.append("")
                lines.append("> 共 {} 条，仅展示前 15 条。".format(len(rows)))
            failed = [r for r in rows if str(r.get("status") or "").lower() in ("failed", "error")]
            if failed:
                lines.append("")
                lines.append("其中 **{}** 条失败，建议重点检查：{}".format(
                    len(failed), self._md_cell(failed[0].get("task_name"))))
            return "\n".join(lines)

        if tool_name == "get_storage_usage":
            d = data or {}
            used = d.get("used_percent", "-")
            return (
                "存储目标 **{}**（{}）使用率 {}%，空间{}。\n\n"
                "| 项目 | 数值 |\n|---|---|\n"
                "| 总空间 | {} GB |\n| 已用 | {} GB |\n| 可用 | {} GB |".format(
                    d.get("target_name", "-"),
                    d.get("path", "-"),
                    used,
                    "紧张" if str(used).replace("%", "").replace("-", "0").isdigit()
                    and float(str(used).replace("%", "") or 0) >= 85 else "充足",
                    d.get("total_gb", "-"),
                    d.get("used_gb", "-"),
                    d.get("free_gb", "-"),
                )
            )

        if tool_name == "list_alert_predictions":
            rows = data or []
            if not rows:
                return "最近没有 AI 预测告警。"
            lines = ["最近 AI 预测告警（共 **{}** 条）：".format(len(rows)), ""]
            lines.append("| 指标 | 风险等级 | 分数 | 预测时间 | 内容 |")
            lines.append("|---|---|---|---|---|")
            for r in rows[:15]:
                lines.append(
                    "| {} | `{}` | {} | {} | {} |".format(
                        self._md_cell(r.get("metric")),
                        str(r.get("risk_level") or "unknown").lower(),
                        r.get("risk_score") or "-",
                        self._fmt_time(r.get("predicted_at")),
                        self._md_cell(r.get("predicted_content")),
                    )
                )
            if len(rows) > 15:
                lines.append("")
                lines.append("> 共 {} 条，仅展示前 15 条。".format(len(rows)))
            return "\n".join(lines)

        if tool_name == "get_inspection_report":
            d = data or {}
            detail = d.get("detail")
            if isinstance(detail, dict):
                items = detail.get("items") or []
                lines = ["巡检报告 {}（{}）：".format(d.get("id", "-"), d.get("status", "-"))]
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
                return "备份任务已提交后台执行（异步），可在「备份记录」页查看进度。"
            status = self._status_text(d.get("status"))
            succeeded = status == "success"
            name = d.get("task_name") or "任务 {}".format(d.get("task_id", "-"))
            lines = [
                "备份任务「{}」执行{}。".format(self._md_cell(name), "**成功**" if succeeded else "**失败**"),
                "",
                "| 项目 | 结果 |",
                "|---|---|",
                "| 状态 | `{}` |".format(status),
                "| 任务 ID | {} |".format(d.get("task_id", "-")),
                "| 备份类型 | {} |".format(self._md_cell(d.get("backup_type"))),
                "| 产物大小 | {} |".format(self._md_cell(d.get("size_human") or self._size_human(d.get("size_bytes")))),
                "| 耗时 | {} 秒 |".format(d.get("duration_sec", "-")),
                "| 记录 ID | {} |".format(d.get("record_id", "-")),
            ]
            if d.get("backup_path"):
                lines.append("| 产物路径 | `{}` |".format(self._md_cell(d.get("backup_path"))))
            if not succeeded and d.get("message"):
                lines.append("")
                lines.append("失败原因：{}".format(self._md_cell(d.get("message"))))
            elif succeeded:
                lines.append("")
                lines.append("如需查看该任务的历史记录，可以回复：最近备份记录。")
            return "\n".join(lines)

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

        # ---- 7 个自治 Agent：把结构化诊断 / 修复 / 回验讲成人话 ----
        # 这些工具的返回值没有 data 字段，走默认分支会输出字符串 "null"。
        if tool_name == "autonomous_diagnose":
            s = result.get("summary") or {}
            src = {"llm": "已配置大模型解释",
                   "local_rules": "本地规则结论",
                   "llm_degraded": "模型降级 · 结论来自本地规则"}.get(
                       result.get("explanation_source"),
                       result.get("explanation_source") or "-")
            lines = [
                "**7 个自治 Agent 诊断完成**", "",
                "- 正常完成诊断：{}/{} 个 Agent".format(
                    s.get("agents_ok", "-"), s.get("agents_total", "-")),
                "- 发现：共 {} 项（高危 {} / 中危 {}）".format(
                    s.get("findings_total", 0), s.get("high", 0),
                    s.get("medium", 0)),
                "- 可由平台安全动作处理：{} 项".format(s.get("auto_fixable", 0)),
                "", "**结论（{}）**：".format(src),
                str(result.get("explanation") or "")[:800],
            ]
            if result.get("llm_note"):
                lines.append("> " + str(result["llm_note"])[:200])
            fs = result.get("findings") or []
            if fs:
                lines += ["", "**主要发现**", ""]
                for f in fs[:6]:
                    lines.append("- [{}] {} — {}".format(
                        f.get("severity", "-"),
                        str(f.get("title") or "").replace("|", "\\|"),
                        str(f.get("suggestion") or f.get("detail") or "")[:90]))
            if result.get("plan"):
                lines += ["", "可处理动作 {} 项。说「执行自动修复」先预演；"
                          "明确说「确认执行修复」才会真实执行并自动回验。".format(
                              len(result["plan"]))]
            lines += ["", "诊断批次 `{}`，也可在页面「自治诊断」面板查看与操作。".format(
                result.get("run_id") or "-")]
            return "\n".join(lines)

        if tool_name == "autonomous_plan":
            plan = result.get("plan") or []
            lines = ["**自治修复计划**（批次 `{}`）".format(
                result.get("run_id") or "-"), ""]
            if not plan:
                lines.append("本轮没有可自动处理的动作，其余发现需按建议人工处置。")
            for p in plan[:10]:
                lines.append("- [{}] {} → {}（风险 {}，{}）".format(
                    p.get("severity", "-"), p.get("title", "-"),
                    p.get("action_title", p.get("action", "-")),
                    p.get("risk", "-"),
                    "需确认" if p.get("requires_confirm") else "低危自动"))
            lines += ["", "默认只预演；确认执行须显式授权，高危动作另需操作验证码。"]
            return "\n".join(lines)

        if tool_name == "autonomous_execute":
            items = result.get("results") or []
            lines = ["**自治修复{}**".format("预演" if result.get("dry_run")
                                        else "执行与回验"), ""]
            if not items:
                lines.append("没有选中的发现，或诊断批次已过期，请重新诊断。")
            for x in items[:10]:
                rc = x.get("recheck") or {}
                lines.append("- `{}` **{}** → {}｜回验：{}".format(
                    x.get("finding_id", "-"), x.get("status", "-"),
                    str(x.get("message") or x.get("error") or "-")[:90],
                    rc.get("status", "-")))
                if rc.get("note"):
                    lines.append("  - " + str(rc["note"])[:120])
            lines += ["", "平台只执行白名单动作，不自动删除备份、不自动切换主备。"]
            return "\n".join(lines)

        if tool_name == "autonomous_verify":
            res, per, new = (result.get("resolved") or [],
                             result.get("persist") or [],
                             result.get("new") or [])
            return "\n".join([
                "**修复回验（重新诊断对比）**", "",
                "- 已解决：{} 项".format(len(res)),
                "- 仍存在：{} 项".format(len(per)),
                "- 新增：{} 项".format(len(new)),
                ("- 已解决明细：" + "、".join(str(x) for x in res[:8])) if res else "",
                ("- 仍存在明细：" + "、".join(str(x) for x in per[:8])) if per else "",
                "", "回验结论来自重新执行的只读诊断，不是执行动作的返回值。",
            ])

        # 默认：把任意未适配工具的真实返回渲染成明细表格/键值，
        # 绝不退化成「操作已完成」或一段被截断的 JSON。
        try:
            from core.ai_agent import nl_actions
            detail = nl_actions.build_detail(tool_name, result, {}, "")
            if detail:
                return (message + "\n\n" if message else "") + detail
        except Exception:
            pass
        if message:
            return message
        try:
            import json as _json
            return _json.dumps(data, ensure_ascii=False, default=str)[:1000]
        except Exception:
            return "操作已完成。"

    # 内置运维知识库（LLM 不可用时的纯问答兜底）
    # 统一由 skills.KNOWLEDGE 提供，避免两处维护导致答案不一致
    _KNOWLEDGE_BASE = KNOWLEDGE

    def _local_quick_answer(self, session_id: str, user_message: str) -> Optional[Dict]:
        """本地直答：元问题 / 闲聊 / 概念答疑，完全不调用 LLM。

        离线环境下这条通道是刚需：模型不可达时若把它交给 LLM，
        用户看到的只有「AI 模型未返回有效内容」。
        """
        if not user_message or not user_message.strip():
            return None
        match = get_local_router().route(user_message, min_score=_LOCAL_ANSWER_MIN_SCORE)
        if not match or match.kind not in ("answer", "help"):
            return None
        if match.missing_slot or match.ambiguous or not match.skill.reply:
            return None
        # 概念答疑证据要更强（用户常在多轮追问中，语义依赖上下文）
        if match.name.startswith("kb_") and match.score < _LOCAL_KB_MIN_SCORE:
            return None
        self.session_mgr.add_assistant_message(session_id, match.skill.reply)
        return {"ok": True, "type": "answer", "content": match.skill.reply,
                "local_engine": True, "confidence": match.score}

    def _fallback_intent_handle(self, session_id: str, user_message: str,
                               context: Dict) -> Optional[Dict]:
        """LLM 不可用时的本地意图识别（由 core/ai_agent/router.py 驱动）。

        与旧版「关键词 if/else」的差别：
        - 意图 / 示例语句 / 话术集中在 skills.py，扩展技能不必改判断逻辑；
        - 打分 + 阈值拒识（借鉴语义路由），低于阈值不硬猜；
        - 覆盖元问题与帮助类，且最终兜底给出能力清单，
          而不是返回「AI 模型未返回有效内容」。
        """
        if not user_message or not user_message.strip():
            return None
        match = get_local_router().route(user_message)
        if match is not None:
            result = self._dispatch_local_match(session_id, match, context)
            if result is not None:
                return result
        return self._local_answer_fallback(session_id)

    def _dispatch_local_match(self, session_id: str, match,
                              context: Dict) -> Optional[Dict]:
        """按本地意图匹配结果分派：追问 / 直答 / 请求确认 / 执行工具。"""
        skill = match.skill

        # 槽位缺失时追问，绝不猜测后执行
        if match.missing_slot:
            hint = skill.slot_hint or "请补充要操作的业务对象（任务名或 ID）。"
            self.session_mgr.add_assistant_message(session_id, hint)
            return {"ok": True, "type": "answer", "content": hint, "local_engine": True}

        if skill.kind in ("answer", "help"):
            if not skill.reply:
                return None
            self.session_mgr.add_assistant_message(session_id, skill.reply)
            return {"ok": True, "type": "answer", "content": skill.reply,
                    "local_engine": True, "confidence": match.score}

        if skill.kind != "tool" or not skill.tool:
            return None

        # 需要确认的操作不直接执行（与 LLM 通道的审批闸门保持一致）
        if skill.confirm_reason:
            return self._pending_confirm_response(session_id, skill, match, context)

        return self._run_fallback_tool(session_id, skill.tool, match.args, context)

    def _pending_confirm_response(self, session_id: str, skill, match,
                                  context: Dict) -> Dict:
        """构造「需要用户确认」的响应（中高风险工具走这里）。"""
        reason = skill.confirm_reason
        tool_call_id = str(uuid.uuid4())
        _pending_confirms[session_id] = {
            "tool_call_id": tool_call_id,
            "tool_name": skill.tool,
            "args": match.args,
            "reason": reason,
            "context": context,
        }
        return {
            "ok": True,
            "type": "confirm_required",
            "content": reason,
            "local_engine": True,
            "pending_confirm": {
                "tool_call_id": tool_call_id,
                "tool_name": skill.tool,
                "args": match.args,
                "reason": reason,
            },
        }

    def _local_answer_fallback(self, session_id: str) -> Dict:
        """意图拒识时的统一回退：给能力清单，而不是「模型未返回内容」。

        ``degraded=True`` 表示该答复由本地引擎在模型不可用时产出（非模型生成），
        供前端标注「离线应答」、供排障区分数据来源。
        """
        self.session_mgr.add_assistant_message(session_id, FALLBACK_HELP)
        return {"ok": True, "type": "answer", "content": FALLBACK_HELP,
                "local_engine": True, "confidence": 0.0,
                "degraded": True, "source": "local"}

    @staticmethod
    def _extract_task_keyword(message: str) -> str:
        """从自然语言指令中抽取任务关键字（无 LLM 环境下的兜底执行用）。

        例："帮我执行 bpm1 库的备份" → "bpm1"；"备份一下对象存储附件桶" → "附件桶"。
        """
        import re
        text = re.sub(r"[，。！？、,\.\!\?\s：:；;]+", " ", str(message or ""))
        stop_words = ("立即执行", "帮我执行", "帮我", "请帮", "请", "执行", "跑一下", "跑一次", "立即",
                      "马上", "开始", "做一次", "手动", "进行", "来一次", "一次", "备份一下", "备份",
                      "任务", "数据库", "数据", "库", "的", "把", "给", "我要", "我想", "需要",
                      "现在", "这个", "那个", "一下", "吧", "了")
        for word in sorted(stop_words, key=len, reverse=True):
            text = text.replace(word, " ")
        parts = [p.strip() for p in text.split(" ") if p.strip()]
        for p in parts:
            if re.search(r"[A-Za-z0-9_\-]", p):
                return p
        return parts[0] if parts else ""

    def _run_fallback_tool(self, session_id: str, tool_name: str, args: Dict,
                          context: Dict) -> Dict:
        """兜底执行单个工具并格式化结果。"""
        tool = self.registry.get(tool_name)
        if not tool:
            return None
        exec_result = self.executor.execute(tool_name, args, context)
        if not isinstance(exec_result, dict):
            exec_result = {"ok": False, "error": "工具返回格式异常"}
        # 若该次调用被判为需要确认（条件确认工具），转为确认请求
        if exec_result.get("needs_confirm"):
            tool_call_id = str(uuid.uuid4())
            reason = exec_result.get("message") or "该操作需要确认后执行。"
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
            }
        self.session_mgr.add_tool_message(session_id, tool_name, exec_result)
        content = self._format_tool_result_to_answer(tool_name, exec_result)
        self.session_mgr.add_assistant_message(session_id, content)
        return {
            "ok": True,
            "type": "answer",
            "content": content,
            "tool_trace": [{
                "name": tool_name,
                "args": args,
                "result": exec_result,
                "risk_level": getattr(tool, "risk_level", "low"),
            }],
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
