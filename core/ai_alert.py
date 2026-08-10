# -*- coding: utf-8 -*-
"""
AI 预测告警引擎（AIPredictor）。

基于规则 + 轻量统计（滑动窗口失败率、线性趋势外推）预测五类风险：
  backup_fail   备份失败
  storage_full  存储容量将满
  link_degraded 容灾链路劣化
  drill_overdue 演练超期
  rpo_breach    RPO 目标未达标

对 risk_level >= medium 的预测写入 alert_predictions 表；
critical 级别自动触发 notifier 告警。
预留 predict_with_model() 插拔点对接外部 ML 服务（model_uri 为 None 时走规则引擎）。
DEMO_MODE 下所有外部数据（真实专线延迟、ML 服务）均以仿真兜底。
"""
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import core.db as db
import core.models as models
from core.ai_secret import encrypt_api_key, decrypt_api_key


_logger = db.get_logger("ai_alert")


# ---- 提供商预设（前端快速选择 + 端点自动填充） ----
PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI 官方",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model_examples": ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"],
    },
    "moma": {
        "label": "中国移动 MoMA",
        "endpoint": "http://moma.hq.cmcc/largemodel/moma/api/v3/chat/completions",
        "model_examples": ["z.ai/glm-5.2", "jiutian/jiutian-lan-35b", "qwen/qwen3-235b-a22b-instruct"],
    },
    "zhipu": {
        "label": "智谱 Zhipu",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model_examples": ["glm-4.5", "glm-4.6", "glm-4-flash"],
    },
    "qwen": {
        "label": "通义千问 Qwen",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model_examples": ["qwen-turbo", "qwen-plus", "qwen-max"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "model_examples": ["deepseek-chat", "deepseek-reasoner"],
    },
    "custom": {
        "label": "自定义",
        "endpoint": "",
        "model_examples": [],
    },
}


# 风险等级阈值（0-100 分）：左闭右开
RISK_LEVELS = {
    "low": (0, 40),
    "medium": (40, 65),
    "high": (65, 85),
    "critical": (85, 101),
}

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---- 失败原因 → 建议动作 关键词映射（纯规则，不调 LLM） ----
# 顺序即优先级：命中第一条即返回，未命中任何条目走 DEFAULT_SUGGESTION。
SUGGESTION_RULES = [
    (("connection refused", "can't connect", "cannot connect", "timed out",
      "unreachable", "no route to host", "拒绝", "连接失败", "无法连接"),
     "检查源库端口可达性与网络策略"),
    (("access denied", "permission", "authentication", "auth failed",
      "权限", "密码", "认证"),
     "检查备份账号权限与凭据有效性"),
    (("no space", "disk full", "quota", "磁盘", "空间不足", "容量"),
     "清理 L1 暂存目录或扩容备份分区"),
    (("timeout", "lock wait", "超时", "锁等待"),
     "调大超时阈值或避开业务高峰重试"),
]

DEFAULT_SUGGESTION = "查看任务日志定位失败原因"

# 失败原因摘要的最大字符数（超出截断）
ERROR_SUMMARY_MAX_CHARS = 80

# 任务级明细最多下发条数（按风险分倒序取前 N）
TASK_DETAIL_TOP_N = 10

# evidence.record_ids 最多下发条数（防止 details JSON 过大）
EVIDENCE_RECORD_LIMIT = 50


def suggest_action(error_text: str) -> str:
    """按错误关键词映射建议动作（大小写不敏感，中英文双语关键词）。

    Args:
        error_text: 失败记录的 message / verify_msg 文本，可为空。

    Returns:
        中文建议动作文案；无法归类时返回 DEFAULT_SUGGESTION。
    """
    text = str(error_text or "").lower()
    if not text:
        return DEFAULT_SUGGESTION
    for keywords, advice in SUGGESTION_RULES:
        for kw in keywords:
            if kw in text:
                return advice
    return DEFAULT_SUGGESTION


def _level_from_score(score: float) -> str:
    score = max(0.0, min(100.0, float(score)))
    for lvl, (lo, hi) in RISK_LEVELS.items():
        if lo <= score < hi:
            return lvl
    return "critical"


# 模型输出上限（tokens）默认值。
# 该默认值服务于「AI 预测告警」场景（输出为一个短 JSON 评估结果），
# 历史行为即为硬编码 1024，此处提取为常量后行为保持完全一致。
# 需要更长输出的调用方（如 AI 对话 Agent 要把长列表塞进 JSON content）
# 应显式通过 _call_model(..., max_tokens=N) 传入，不影响本默认值。
DEFAULT_MODEL_MAX_TOKENS = 1024

# 模型请求超时（秒）默认值。
# 该默认值服务于「AI 预测告警」场景（后台调度、单次短 JSON 输出），
# 历史行为即为 30s，此处提取为常量后行为保持完全一致。
# 需要更长等待的调用方（如 AI 对话 Agent：一轮对话要串行 2~3 次 LLM 调用，
# 30s 单点超时会直接毁掉整轮）应显式通过 _call_model(..., timeout=N) 传入，
# 不影响本默认值，也不影响预测告警链路（零回归）。
DEFAULT_MODEL_TIMEOUT_SEC = 30

DEFAULT_AI_CONFIG = {
    "enabled": True,
    "min_risk_level_to_record": "medium",  # 仅记录该级别及以上
    "notify_on": "critical",                # 该级别及以上自动通知
    "ai_alert_interval_hours": 6,           # 调度周期（小时）
    "ai_model": {
        "enabled": False,                    # 是否启用真实模型推理（关闭时永远走规则引擎）
        "provider": "openai",               # 厂商标识：openai / anthropic / ollama / local / custom
        "endpoint": "",                     # 远程 API base URL（OpenAI 兼容格式优先）
        "api_key": "",                      # API 密钥（加密存储；GET 时不回显原文）
        "model_name": "",                   # 模型名（如 gpt-4o-mini / claude-3-5-sonnet / qwen2.5:7b）
        "local_model_path": "",             # 本地模型路径（仅 provider=local 时用）
        "request_timeout_sec": DEFAULT_MODEL_TIMEOUT_SEC,
        "max_input_chars": 8000,            # 发送给模型的最大字符数（超过截断）
        "prompt_template": "",              # 自定义提示词模板（留空用内置默认模板）
    },
    "backup_fail": {
        "window_days_7": 7,
        "window_days_30": 30,
        "fail_rate_high": 0.4,   # 30 天失败率 >= 0.4 → 高分
        "fail_rate_warn": 0.2,
        "consecutive_fail_high": 3,
        "duration_spike_x": 2.0,  # 单次耗时 > 平均 * 2
        "size_low_pct": 10.0,    # 体积 < 平均 * 10%
        "size_high_pct": 300.0,
    },
    "verify_fail": {
        "l1_enabled": True,          # L1 完整性：sha256 与落库 checksum 比对
        "l2_enabled": True,          # L2 可用性：gzip 魔数 / SQL dump 标记探测
        "l3_enabled": False,         # L3 抽样恢复演练（P2，本期空实现且默认关）
        "verify_sample_limit": 20,   # 单次最多校验的最近记录条数（IO 限流）
        "verify_max_file_mb": 512,   # 超过该体积跳过全量 sha256，退化轻指纹
        "unverified_ratio_warn": 0.3,  # verified=0 占比 ≥ 该值 → 55 分
        "stale_days": 7,             # 距上次成功验证超过该天数 → 45 分
    },
    "storage_full": {
        "l1_warn_pct": 85.0,
        "l1_critical_pct": 95.0,
        "bucket_warn_pct": 85.0,
        "bucket_critical_pct": 95.0,
        "forecast_days": 30,
    },
    "link_degraded": {
        "switch_count_high": 5,   # 近 7 天切路次数 >= 5 → 劣化
        "switch_window_days": 7,
        "consistency_fail_score": 60,
    },
    "drill_overdue": {
        "interval_days": 90,      # 距上次演练 > 90 天 → overdue
        "rto_target_sec": 900,
        "rpo_target_sec": 14400,
    },
}


def _deep_update(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（仅合并 dict 子树，标量直接覆盖）。"""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


# ------------------------- 内部默认提示词模板 -------------------------
DEFAULT_PROMPT_TEMPLATE = """你是一个备份管理平台的 AI 风险预测引擎。请根据以下数据对 {metric} 风险进行评估。

风险指标类型：{metric}
当前分析器输出：
- 预测内容：{predicted_content}
- 依据因子：{basis_json}
- 详细数据：{details_json}

请以 JSON 格式返回评估结果，格式如下（不要包含任何其他文字）：
{{"risk_score": <float 0-100>, "risk_level": "<low|medium|high|critical>", "predicted_content": "<str>", "basis": ["<str>", ...]}}

注意：
1. risk_score 为 0-100 的浮点数
2. risk_level 按阈值划分：0-40=low, 40-65=medium, 65-85=high, 85-100=critical
3. predicted_content 为中文人类可读的预测结论
4. basis 为人类可读依据因子列表"""


class AIPredictor:
    """AI 预测告警引擎：规则 + 轻量统计，可插拔外部 ML。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 配置 -------------------------
    def get_config(self) -> dict:
        """获取完整配置（内部使用，api_key 为解密后的明文）。"""
        raw = db.get_system_config("ai_alert_config")
        cfg = json.loads(json.dumps(DEFAULT_AI_CONFIG))  # 深拷贝默认值
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    _deep_update(cfg, loaded)
            except (json.JSONDecodeError, TypeError):
                pass
        # 解密 api_key
        if cfg.get("ai_model", {}).get("api_key"):
            cfg["ai_model"]["api_key"] = decrypt_api_key(cfg["ai_model"]["api_key"])
        return cfg

    def get_safe_config(self) -> dict:
        """对外安全配置（GET 接口用）：api_key 替换为掩码 + api_key_set 标记。"""
        cfg = self.get_config()
        ai_model = cfg.get("ai_model", {})
        api_key_plain = ai_model.get("api_key", "")
        ai_model["api_key"] = "***hidden***"
        ai_model["api_key_set"] = bool(api_key_plain)
        cfg["ai_model"] = ai_model
        return cfg

    def save_config(self, data: dict) -> dict:
        """保存配置（data 中 api_key 为明文，存储前加密；api_key 留空表示不改）。"""
        cfg = self.get_config()  # 读取当前配置（含解密 api_key）
        top = {"enabled", "min_risk_level_to_record", "notify_on",
               "ai_alert_interval_hours"}
        for k, v in (data or {}).items():
            if k in top:
                cfg[k] = v
        # ai_model 子段：只覆盖键名命中的子段
        if "ai_model" in (data or {}) and isinstance(data["ai_model"], dict):
            ai_model_data = data["ai_model"]
            current_ai_model = cfg.get("ai_model", {})
            for sub_key, sub_val in ai_model_data.items():
                if sub_key == "api_key":
                    # api_key 留空表示不修改
                    if sub_val:
                        current_ai_model["api_key"] = encrypt_api_key(sub_val)
                    # 留空则保留原值（已加密）
                else:
                    current_ai_model[sub_key] = sub_val
            cfg["ai_model"] = current_ai_model
        # 子表规则
        for sub in ("backup_fail", "verify_fail", "storage_full",
                    "link_degraded", "drill_overdue"):
            if sub in (data or {}) and isinstance(data[sub], dict):
                # 老库存的配置可能缺少新增子表，缺失时先补默认值再覆盖
                if not isinstance(cfg.get(sub), dict):
                    cfg[sub] = json.loads(json.dumps(DEFAULT_AI_CONFIG.get(sub, {})))
                cfg[sub].update(data[sub])
        # 存入数据库（api_key 已加密）
        db.set_system_config("ai_alert_config", json.dumps(cfg, ensure_ascii=False))
        # 返回时 api_key 仍为加密值，调用方需自行解密或用 get_safe_config
        return cfg

    # ------------------------- 内部加密辅助 -------------------------
    def _encrypt_api_key(self, plain: str) -> str:
        """加密 API Key（调用 ai_secret 实现）。"""
        return encrypt_api_key(plain)

    def _decrypt_api_key(self, cipher: str) -> str:
        """解密 API Key（调用 ai_secret 实现）。"""
        return decrypt_api_key(cipher)

    # ------------------------- 模型推理 -------------------------
    def _get_model_uri(self) -> str:
        """从配置拼装模型服务 URI。返回空字符串表示未配置。

        智能识别规则（避免路径双拼）：
        1. 去除尾部斜杠后判断
        2. 如果 endpoint 已包含 /chat/completions 结尾（大小写不敏感）→ 直接用原 URL
        3. 如果 endpoint 已包含 /v1 结尾 → 只追加 /chat/completions（不重复 /v1）
        4. 否则（基础 URL）→ 追加 /v1/chat/completions
        5. endpoint 为空字符串 → 返回 ""
        """
        cfg = self.get_config()
        ai_model = cfg.get("ai_model", {})
        endpoint = ai_model.get("endpoint", "")
        provider = ai_model.get("provider", "openai")
        if not endpoint:
            return ""
        # 去除尾部斜杠
        base = endpoint.rstrip("/")
        # local 不需要 URI
        if provider == "local":
            return ""
        # openai / anthropic / ollama / custom：均用 OpenAI 兼容路径
        if provider in ("openai", "anthropic", "ollama", "custom"):
            # 规则 1：endpoint 已含 /chat/completions → 直接使用（避免双拼）
            if base.lower().endswith("/chat/completions"):
                return base
            # 规则 2：endpoint 已含 /v1 → 只追加 /chat/completions
            if base.lower().endswith("/v1"):
                return f"{base}/chat/completions"
            # 规则 3：基础 URL → 追加 /v1/chat/completions
            return f"{base}/v1/chat/completions"
        return ""

    def _compose_prompt(self, metric: str, data: dict, cfg: dict) -> str:
        """构造发送给模型的提示词。受 max_input_chars 截断约束。"""
        ai_model = cfg.get("ai_model", {})
        max_chars = int(ai_model.get("max_input_chars", 8000) or 8000)
        template = ai_model.get("prompt_template", "")
        if not template:
            template = DEFAULT_PROMPT_TEMPLATE

        # 准备占位符内容
        predicted_content = data.get("predicted_content", "")
        basis = data.get("basis", [])
        basis_json = json.dumps(basis, ensure_ascii=False)
        details = data.get("details", {})
        details_json = json.dumps(details, ensure_ascii=False)

        # 替换占位符
        prompt = template.replace("{metric}", str(metric))
        prompt = prompt.replace("{predicted_content}", str(predicted_content))
        prompt = prompt.replace("{basis_json}", basis_json)
        prompt = prompt.replace("{details_json}", details_json)

        # 截断到最大字符数
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars]

        return prompt

    @staticmethod
    def _resolve_max_tokens(cfg: dict, override: int = None) -> int:
        """解析本次调用使用的 max_tokens。

        优先级：调用方显式传参 > 配置项 ai_model.max_tokens > DEFAULT_MODEL_MAX_TOKENS。

        AI 预测告警不传 override、配置里也没有该键时，返回 1024，
        与历史硬编码行为完全一致（零回归）。

        Args:
            cfg: 完整配置 dict
            override: 调用方显式指定的 max_tokens（None 表示不指定）

        Returns:
            正整数 max_tokens
        """
        ai_model = (cfg or {}).get("ai_model", {})
        if not isinstance(ai_model, dict):
            ai_model = {}
        for candidate in (override, ai_model.get("max_tokens")):
            if candidate is None:
                continue
            try:
                value = int(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return DEFAULT_MODEL_MAX_TOKENS

    @staticmethod
    def _resolve_timeout(cfg: dict, override: int = None) -> int:
        """解析本次调用使用的请求超时秒数。

        优先级：调用方显式传参 > 配置项 ai_model.request_timeout_sec >
        DEFAULT_MODEL_TIMEOUT_SEC —— 与 _resolve_max_tokens 完全同款设计。

        AI 预测告警不传 override、配置里也没有该键时返回 30，
        与历史硬编码行为完全一致（零回归）；AI 对话 Agent 显式传 60，
        避免上游抖动时单点 30s 超时毁掉整轮多次 LLM 调用。

        Args:
            cfg: 完整配置 dict
            override: 调用方显式指定的 timeout 秒数（None 表示不指定）

        Returns:
            正整数超时秒数
        """
        ai_model = (cfg or {}).get("ai_model", {})
        if not isinstance(ai_model, dict):
            ai_model = {}
        for candidate in (override, ai_model.get("request_timeout_sec")):
            if candidate is None:
                continue
            try:
                value = int(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return DEFAULT_MODEL_TIMEOUT_SEC

    def _call_model(self, prompt: str, cfg: dict, max_tokens: int = None,
                    timeout: int = None) -> dict:
        """调用外部模型服务。返回原始响应或错误信息。

        Args:
            prompt: 提示词文本
            cfg: 完整配置 dict
            max_tokens: 本次调用的输出上限；None 表示沿用配置/默认值(1024)
            timeout: 本次调用的请求超时秒数；None 表示沿用配置/默认值(30s)
        """
        import urllib.request
        import urllib.error

        ai_model = cfg.get("ai_model", {})
        provider = ai_model.get("provider", "openai")
        endpoint = ai_model.get("endpoint", "").rstrip("/")
        model_name = ai_model.get("model_name", "")
        api_key = ai_model.get("api_key", "")
        # 三级优先级：调用方传参 > cfg.ai_model.request_timeout_sec > 30s 默认
        resolved_timeout = self._resolve_timeout(cfg, timeout)

        # provider=local：仅校验路径存在（优先检查，因为 local 不需要 URI）
        if provider == "local":
            local_path = ai_model.get("local_model_path", "")
            if local_path and os.path.exists(local_path):
                self.logger.info("[ai_alert] provider=local，路径存在但本期未实现本地推理: %s", local_path)
                return {"error": "未实现本地推理", "local_path_exists": True}
            else:
                self.logger.warning("[ai_alert] provider=local，本地模型路径不存在: %s", local_path)
                return {"error": "本地模型路径不存在", "local_path_exists": False}

        uri = self._get_model_uri()
        if not uri:
            return {"error": "模型端点未配置"}

        # 入口日志：记录请求关键信息（便于排查），api_key 脱敏显示前 6 位 + ***
        masked_key = (api_key[:6] + "***") if api_key and len(api_key) >= 6 else ("***" if api_key else "")
        prompt_preview = prompt[:80] if prompt else ""
        self.logger.info(
            "[ai_alert] 模型调用请求: endpoint=%s, uri=%s, model=%s, key=%s, prompt_preview=%s",
            endpoint, uri, model_name, masked_key, prompt_preview,
        )

        # 构造 OpenAI 兼容请求体（强制非流式响应，避免 SSE delta 格式）
        resolved_max_tokens = self._resolve_max_tokens(cfg, max_tokens)
        body_dict = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": resolved_max_tokens,
            "stream": False,  # 强制非流式，确保返回完整 JSON（choices[0].message.content）
        }
        body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        self.logger.debug("[ai_alert] 请求体大小: %d bytes", len(body))

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        start_ms = time.time() * 1000
        try:
            req = urllib.request.Request(uri, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=resolved_timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                latency_ms = round(time.time() * 1000 - start_ms, 1)
                self.logger.info("[ai_alert] 模型调用成功: %s, latency=%.1fms", uri, latency_ms)
                return {
                    "ok": True,
                    "status_code": resp.status,
                    "latency_ms": latency_ms,
                    "response_body": resp_body,
                }
        except urllib.error.HTTPError as e:
            latency_ms = round(time.time() * 1000 - start_ms, 1)
            # 尝试读取错误响应体（远端 API 可能返回详细错误信息如 "invalid model name"）
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                err_body = "(无法读取错误响应体)"
            # 尝试从响应体中提取 error.message 作为详细信息
            detail_msg = ""
            try:
                err_json = json.loads(err_body)
                detail_msg = (err_json.get("error", {}) or {}).get("message", "")
                if not detail_msg and isinstance(err_json.get("error"), str):
                    detail_msg = err_json["error"]
            except (json.JSONDecodeError, TypeError):
                pass
            # 按 HTTP 状态码分类错误
            category_map = {
                401: {"error_category": "auth", "error": "API Key 校验未通过", "hint": "请检查密钥或密钥是否有该模型权限"},
                403: {"error_category": "forbidden", "error": "无权限访问该模型", "hint": "该模型可能需要单独申请权限"},
                404: {"error_category": "endpoint", "error": "端点地址不存在", "hint": "请检查 URL 是否正确"},
                429: {"error_category": "rate_limit", "error": "请求过于频繁", "hint": "请降低调用频率或稍后重试"},
                503: {"error_category": "provider_full", "error": "提供商并发已满", "hint": "该模型/提供商当前排队较多，请稍后重试或切换其他模型"},
            }
            if e.code in category_map:
                classified = category_map[e.code]
                # 如果响应体有更详细的错误信息，补充到 error 字段
                if detail_msg:
                    classified["error"] = f"{classified['error']}（{detail_msg}）"
            elif 500 <= e.code < 600:
                classified = {"error_category": "server_error", "error": f"远端服务异常({e.code})", "hint": "请稍后重试"}
                if detail_msg:
                    classified["error"] = f"远端服务异常({e.code})（{detail_msg}）"
            else:
                classified = {"error_category": "http_error", "error": f"HTTP {e.code} {e.reason}", "hint": "请检查配置"}
                if detail_msg:
                    classified["error"] = f"HTTP {e.code} {e.reason}（{detail_msg}）"
            self.logger.warning(
                "[ai_alert] 模型调用 HTTP 错误: %s %s, category=%s, latency=%.1fms, response_body_preview=%s",
                e.code, e.reason, classified["error_category"], latency_ms, err_body,
            )
            result = {"ok": False, "latency_ms": latency_ms, "response_body_preview": err_body}
            result.update(classified)
            return result
        except urllib.error.URLError as e:
            latency_ms = round(time.time() * 1000 - start_ms, 1)
            # 区分 timeout 与其他网络错误
            reason_str = str(e.reason) if e.reason else ""
            is_timeout = "timed out" in reason_str.lower()
            if is_timeout:
                classified = {
                    "error_category": "timeout",
                    "error": f"请求超时（{resolved_timeout}s）",
                    "hint": "可能是远端排队中或网络抖动，请稍后重试",
                }
            else:
                classified = {
                    "error_category": "network",
                    "error": f"网络连接失败（{reason_str}）",
                    "hint": "请检查网络或 endpoint 是否可达",
                }
            self.logger.warning(
                "[ai_alert] 模型调用 URL 错误: %s, category=%s, latency=%.1fms",
                e.reason, classified["error_category"], latency_ms,
            )
            result = {"ok": False, "latency_ms": latency_ms}
            result.update(classified)
            return result
        except Exception as e:
            latency_ms = round(time.time() * 1000 - start_ms, 1)
            classified = {
                "error_category": "unknown",
                "error": f"异常: {e}",
                "hint": "请检查配置或联系管理员",
            }
            self.logger.warning("[ai_alert] 模型调用异常: %s, category=%s, latency=%.1fms", e, classified["error_category"], latency_ms)
            result = {"ok": False, "latency_ms": latency_ms}
            result.update(classified)
            return result

    def _parse_response(self, raw: dict, metric: str, rule_result: dict) -> dict:
        """解析模型响应。解析失败则降级用规则引擎结果。

        容错策略：
        1. 优先读 choices[0].message.content（标准非流式 JSON）
        2. 若为空，回退读 choices 列表中所有 delta.content 并拼接（SSE 流式增量累积）
        3. 仍为空 → 降级规则引擎 + warn 日志
        """
        if not raw.get("ok"):
            self.logger.warning("[ai_alert] 模型调用失败，降级到规则引擎: %s", raw.get("error", "未知"))
            rule_result["model_source"] = "规则引擎(降级)"
            rule_result["model_error"] = raw.get("error", "")
            return rule_result

        resp_body = raw.get("response_body", "")
        try:
            resp_json = json.loads(resp_body)
            choices = resp_json.get("choices", [])

            # ---- 优先路径：标准非流式 JSON（choices[0].message.content） ----
            content = ""
            if choices:
                first_choice = choices[0]
                message_obj = first_choice.get("message", {})
                content = message_obj.get("content", "") if isinstance(message_obj, dict) else ""

            # ---- 回退路径：SSE 流式增量累积（choices[*].delta.content 拼接） ----
            # 有些 API 即使收到 stream:false 仍返回流式格式，
            # 此时 content 字段为空，需遍历所有 choices 的 delta.content 拼接
            if not content and choices:
                delta_parts = []
                for chunk in choices:
                    delta = chunk.get("delta", {})
                    if isinstance(delta, dict):
                        delta_content = delta.get("content", "")
                        if delta_content:
                            delta_parts.append(delta_content)
                if delta_parts:
                    content = "".join(delta_parts)
                    self.logger.info(
                        "[ai_alert] 流式响应回退拼接(delta): 拼接 %d 个 delta chunk, 总长度=%d",
                        len(delta_parts), len(content),
                    )

            # ---- 仍然为空 → 降级规则引擎 ----
            if not content:
                self.logger.warning(
                    "[ai_alert] 模型响应 content 为空（message 和 delta 均无内容），降级到规则引擎: resp_body=%s",
                    resp_body[:300],
                )
                rule_result["model_source"] = "规则引擎(降级-空响应)"
                rule_result["model_error"] = "模型响应 content 为空"
                return rule_result
            # 从 content 中提取 JSON
            # 尝试直接解析 content 为 JSON
            parsed = None
            # 尝试提取 JSON 块（可能被 markdown code block 包裹）
            json_str = content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            json_str = json_str.strip()
            parsed = json.loads(json_str)

            if parsed and isinstance(parsed, dict):
                risk_score = float(parsed.get("risk_score", 0))
                risk_level = parsed.get("risk_level", "")
                predicted_content = parsed.get("predicted_content", "")
                basis = parsed.get("basis", [])
                if isinstance(basis, str):
                    basis = [basis]

                # 校验 risk_level
                if risk_level not in ("low", "medium", "high", "critical"):
                    risk_level = _level_from_score(risk_score)

                ai_model_cfg = self.get_config().get("ai_model", {})
                provider = ai_model_cfg.get("provider", "openai")
                model_name = ai_model_cfg.get("model_name", "")
                if provider == "local":
                    model_source = f"本地({model_name})"
                else:
                    model_source = f"{provider}:{model_name}" if model_name else provider

                result = {
                    "metric": metric,
                    "risk_score": round(max(0.0, min(100.0, risk_score)), 1),
                    "risk_level": risk_level,
                    "details": rule_result.get("details", {}),
                    "predicted_content": predicted_content or rule_result.get("predicted_content", ""),
                    "basis": basis or rule_result.get("basis", []),
                    "model_source": model_source,
                }
                self.logger.info("[ai_alert] 模型推理成功: %s, score=%.1f, level=%s", metric, risk_score, risk_level)
                return result
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as e:
            self.logger.warning("[ai_alert] 模型响应解析失败，降级到规则引擎: %s, content=%s", e, resp_body[:200])

        # 降级到规则引擎
        rule_result["model_source"] = "规则引擎(降级)"
        return rule_result

    def predict_with_ai(self, metric: str) -> dict:
        """先规则引擎 → compose_prompt → call_model → parse_response。
        模型失败时自动降级到规则引擎。"""
        cfg = self.get_config()
        ai_model = cfg.get("ai_model", {})
        if not ai_model.get("enabled"):
            # 模型未启用，纯规则引擎
            fn_map = {
                "backup_fail": self.analyze_backup_failure_risk,
                "verify_fail": self.analyze_backup_verify_risk,
                "storage_full": self.analyze_storage_risk,
                "link_degraded": self.analyze_link_health,
                "drill_overdue": self.analyze_drill_compliance,
            }
            fn = fn_map.get(metric)
            result = fn() if fn else self._empty_metric(metric, "未知指标")
            result["model_source"] = "规则引擎"
            return result

        provider = ai_model.get("provider", "openai")
        # local provider 特殊处理
        if provider == "local":
            local_path = ai_model.get("local_model_path", "")
            fn_map = {
                "backup_fail": self.analyze_backup_failure_risk,
                "verify_fail": self.analyze_backup_verify_risk,
                "storage_full": self.analyze_storage_risk,
                "link_degraded": self.analyze_link_health,
                "drill_overdue": self.analyze_drill_compliance,
            }
            fn = fn_map.get(metric)
            rule_result = fn() if fn else self._empty_metric(metric, "未知指标")
            if local_path and os.path.exists(local_path):
                rule_result["model_source"] = "本地(未实现)"
                self.logger.info("[ai_alert] provider=local 路径存在，本期不真推理，标记'本地(未实现)'")
            else:
                rule_result["model_source"] = "规则引擎(本地路径不存在)"
                self.logger.warning("[ai_alert] provider=local 路径不存在: %s", local_path)
            return rule_result

        # 远程模型推理
        fn_map = {
            "backup_fail": self.analyze_backup_failure_risk,
            "verify_fail": self.analyze_backup_verify_risk,
            "storage_full": self.analyze_storage_risk,
            "link_degraded": self.analyze_link_health,
            "drill_overdue": self.analyze_drill_compliance,
        }
        fn = fn_map.get(metric)
        rule_result = fn() if fn else self._empty_metric(metric, "未知指标")

        # 构造提示词
        prompt = self._compose_prompt(metric, rule_result, cfg)

        # 调用模型
        raw_response = self._call_model(prompt, cfg)

        # 解析响应
        return self._parse_response(raw_response, metric, rule_result)

    # ------------------------- ML 插拔点 -------------------------
    def predict_with_model(self, metric: str, data: dict, model_uri: str = None) -> dict:
        """ML 插拔点：model_uri 为 None 时走规则引擎或已配置的模型；否则调用外部 ML 服务。

        Args:
            metric: 风险指标名
            data: 输入特征（规则引擎可忽略，外部 ML 服务使用）
            model_uri: 外部 ML 服务地址；为 None 时走配置模型或规则引擎兜底。
        """
        if model_uri is None:
            # 从配置决定走模型还是规则引擎
            return self.predict_with_ai(metric)

        # 外部 ML 服务调用骨架（直接指定 URI，不走配置）
        self.logger.info("[ai_alert] 调用外部 ML 服务 %s 预测 %s", model_uri, metric)
        return {
            "metric": metric,
            "risk_score": 0.0,
            "risk_level": "low",
            "details": {"model_uri": model_uri,
                        "note": "外部 ML 调用骨架（未实现真实推理）"},
            "model_uri": model_uri,
            "predicted_content": "",
            "basis": [],
            "model_source": f"外部ML:{model_uri}",
        }

    # ------------------------- 备份失败风险 -------------------------
    def _group_failures_by_task(self, records: list, cfg: dict) -> tuple:
        """将备份记录按 task_id 分桶，产出任务级明细与证据 ID。

        全局分数计算不受影响，此处只做「附加信息」的旁路聚合。

        Args:
            records: models.list_records() 的原始记录列表（id DESC）。
            cfg: backup_fail 子配置（阈值复用同一套）。

        Returns:
            (task_details, evidence, basis_lines) 三元组：
              * task_details: list[dict]，固定 9 键，按 task_risk_score 倒序 Top N
              * evidence:     {"task_ids": [int], "record_ids": [int]}
              * basis_lines:  list[str]，追加到人类可读依据
        """
        now = datetime.now(timezone.utc).astimezone()
        cutoff_7 = now - timedelta(days=int(cfg.get("window_days_7", 7) or 7))
        cutoff_30 = now - timedelta(days=int(cfg.get("window_days_30", 30) or 30))

        # 一次性建立任务索引，避免 N+1 查询
        task_index: dict = {}
        try:
            for t in models.list_tasks():
                try:
                    task_index[int(t.get("id"))] = t
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            self.logger.warning("[ai_alert] 读取任务列表失败，任务名降级为 ID: %s", exc)

        buckets: dict = {}
        for r in records:
            try:
                tid = int(r.get("task_id"))
            except (TypeError, ValueError):
                continue  # 无归属任务的记录（如手工导入）不参与分组
            buckets.setdefault(tid, []).append(r)

        task_details: list = []
        for tid, recs in buckets.items():
            fail_7d = fail_30d = 0
            total_7d = total_30d = 0
            last_fail_at = None
            last_error = None
            for r in recs:
                ts = self._parse_ts(r.get("started_at"))
                is_fail = r.get("status") == "failed"
                if ts is None or ts >= cutoff_30:
                    total_30d += 1
                    if is_fail:
                        fail_30d += 1
                if ts is None or ts >= cutoff_7:
                    total_7d += 1
                    if is_fail:
                        fail_7d += 1
                if is_fail and last_fail_at is None:
                    # records 已按 id DESC，首个命中即最近一次失败
                    last_fail_at = r.get("started_at") or r.get("finished_at")
                    last_error = str(r.get("message") or "")[:ERROR_SUMMARY_MAX_CHARS]
            if fail_30d == 0 and fail_7d == 0:
                continue  # 该任务窗口内无失败，不进明细

            # 连续失败（该桶头部，records 已 id DESC）
            consecutive = 0
            for r in recs:
                if r.get("status") == "failed":
                    consecutive += 1
                else:
                    break
            rate7 = fail_7d / total_7d if total_7d else 0.0
            rate30 = fail_30d / total_30d if total_30d else 0.0
            task_score = self._failure_score(consecutive, rate7, rate30, cfg)

            task = task_index.get(tid) or {}
            task_details.append({
                "task_id": tid,
                "task_name": task.get("name") or f"任务#{tid}",
                "db_type": task.get("db_type") or None,
                "fail_7d": fail_7d,
                "fail_30d": fail_30d,
                "last_fail_at": last_fail_at,
                "last_error": last_error,
                "task_risk_score": round(task_score, 1),
                "suggestion": suggest_action(last_error),
            })

        task_details.sort(key=lambda d: d["task_risk_score"], reverse=True)
        task_details = task_details[:TASK_DETAIL_TOP_N]

        # 证据记录只保留 Top N 任务的失败记录 ID（顺序按 id 倒序）
        kept_ids = {d["task_id"] for d in task_details}
        record_ids: list = []
        for tid in kept_ids:
            for r in buckets.get(tid, []):
                if r.get("status") == "failed" and r.get("id") is not None:
                    try:
                        record_ids.append(int(r["id"]))
                    except (TypeError, ValueError):
                        continue
        evidence = {
            "task_ids": [d["task_id"] for d in task_details],
            "record_ids": sorted(record_ids, reverse=True)[:EVIDENCE_RECORD_LIMIT],
        }

        basis_lines: list = []
        for d in task_details[:3]:  # 依据列表只摘前 3 个高风险任务，避免刷屏
            reason = f"，最近失败原因：{d['last_error']}" if d["last_error"] else ""
            basis_lines.append(
                f"任务「{d['task_name']}」近7天失败 {d['fail_7d']} 次 / "
                f"近30天失败 {d['fail_30d']} 次{reason}")
        return task_details, evidence, basis_lines

    @staticmethod
    def _failure_score(consecutive: int, rate7: float, rate30: float,
                       cfg: dict) -> float:
        """按同一套阈值计算失败风险分（供任务级分桶复用）。

        Args:
            consecutive: 连续失败次数。
            rate7: 近 7 天失败率（0-1）。
            rate30: 近 30 天失败率（0-1）。
            cfg: backup_fail 子配置。

        Returns:
            0-100 的风险分。
        """
        score = 0.0
        if consecutive >= cfg["consecutive_fail_high"]:
            score = max(score, 85.0)
        elif consecutive > 0:
            score = max(score, 40.0 + consecutive * 10)
        if rate30 >= cfg["fail_rate_high"]:
            score = max(score, 80.0)
        elif rate30 >= cfg["fail_rate_warn"]:
            score = max(score, 55.0)
        if rate7 >= cfg["fail_rate_high"]:
            score = max(score, 88.0)
        return min(100.0, score)

    def analyze_backup_failure_risk(self) -> dict:
        cfg = self.get_config()["backup_fail"]
        records = models.list_records(limit=200)
        if not records:
            return self._empty_metric("backup_fail", "无备份记录")
        recent = records[:min(len(records), 20)]
        # 连续失败（尾部）
        consecutive = 0
        for r in recent:
            if r.get("status") == "failed":
                consecutive += 1
            else:
                break
        now = datetime.now(timezone.utc).astimezone()

        def in_window(recs, days):
            cnt = fail = 0
            cutoff = now - timedelta(days=days)
            for r in recs:
                ts = self._parse_ts(r.get("started_at"))
                if ts and ts < cutoff:
                    continue
                cnt += 1
                if r.get("status") == "failed":
                    fail += 1
            return cnt, fail

        c7, f7 = in_window(records, cfg["window_days_7"])
        c30, f30 = in_window(records, cfg["window_days_30"])
        rate7 = f7 / c7 if c7 else 0.0
        rate30 = f30 / c30 if c30 else 0.0

        durations = [float(r.get("duration_sec") or 0)
                     for r in recent
                     if (r.get("duration_sec") or 0) > 0
                     and r.get("status") in ("success", "simulated")]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        last = recent[0]
        last_dur = float(last.get("duration_sec") or 0)
        dur_spike = avg_dur > 0 and last_dur > avg_dur * cfg["duration_spike_x"]

        sizes = [int(r.get("size_bytes") or 0)
                 for r in recent
                 if (r.get("size_bytes") or 0) > 0
                 and r.get("status") in ("success", "simulated")]
        avg_size = sum(sizes) / len(sizes) if sizes else 0
        last_size = int(last.get("size_bytes") or 0)
        size_anom = avg_size > 0 and (
            last_size < avg_size * cfg["size_low_pct"] / 100.0
            or last_size > avg_size * cfg["size_high_pct"] / 100.0)

        score = 0.0
        if consecutive >= cfg["consecutive_fail_high"]:
            score = max(score, 85.0)
        elif consecutive > 0:
            score = max(score, 40.0 + consecutive * 10)
        if rate30 >= cfg["fail_rate_high"]:
            score = max(score, 80.0)
        elif rate30 >= cfg["fail_rate_warn"]:
            score = max(score, 55.0)
        if rate7 >= cfg["fail_rate_high"]:
            score = max(score, 88.0)
        if dur_spike:
            score = max(score, max(score, 50.0))
        if size_anom:
            score = max(score, max(score, 45.0))
        score = min(100.0, score)
        # ---------- 人类可读预测内容 & 依据 ----------
        basis: list[str] = []
        predicted_content = ""
        if consecutive >= cfg["consecutive_fail_high"]:
            basis.append(f"连续失败 {consecutive} 次（≥阈值 {cfg['consecutive_fail_high']} 次）")
        elif consecutive > 0:
            basis.append(f"连续失败 {consecutive} 次")
        if rate30 >= cfg["fail_rate_high"]:
            basis.append(f"近30天失败率 {round(rate30*100, 1)}%（≥阈值 {cfg['fail_rate_high']*100}%）")
        elif rate30 >= cfg["fail_rate_warn"]:
            basis.append(f"近30天失败率 {round(rate30*100, 1)}%（≥警告阈值 {cfg['fail_rate_warn']*100}%）")
        if rate7 >= cfg["fail_rate_high"]:
            basis.append(f"近7天失败率 {round(rate7*100, 1)}%（≥阈值 {cfg['fail_rate_high']*100}%）")
        if dur_spike:
            basis.append(f"上次耗时 {round(last_dur, 1)}s 为均值 {round(avg_dur, 1)}s 的 {round(last_dur/avg_dur, 1)} 倍（≥阈值 {cfg['duration_spike_x']}x）")
        if size_anom:
            pct_vs_avg = round(last_size / avg_size * 100, 1) if avg_size > 0 else 0
            basis.append(f"上次体积偏差 {pct_vs_avg}%（阈值范围 {cfg['size_low_pct']}%-{cfg['size_high_pct']}%）")
        if score > 0:
            predicted_content = "预测未来一段时间内备份失败概率上升"

        # ---------- 任务级明细（旁路聚合，只加字段不改既有结构） ----------
        # 注意：evidence 必须放在 details 内而非 basis —— 启用大模型时
        # _parse_response() 会用模型输出覆盖 basis，但原样保留 details。
        try:
            task_details, evidence, task_basis = self._group_failures_by_task(records, cfg)
        except Exception as exc:
            self.logger.warning("[ai_alert] 备份失败任务级分组异常，降级为全局结论: %s", exc)
            task_details, evidence, task_basis = [], {"task_ids": [], "record_ids": []}, []
        basis.extend(task_basis)
        if task_details and not predicted_content:
            predicted_content = f"检测到 {len(task_details)} 个任务近期存在备份失败"

        return {
            "metric": "backup_fail",
            "risk_score": round(score, 1),
            "risk_level": _level_from_score(score),
            "details": {
                "consecutive_failures": consecutive,
                "fail_rate_7d": round(rate7, 3),
                "fail_rate_30d": round(rate30, 3),
                "avg_duration_sec": round(avg_dur, 1),
                "last_duration_sec": round(last_dur, 1),
                "duration_spike": bool(dur_spike),
                "avg_size_bytes": avg_size,
                "last_size_bytes": last_size,
                "size_anomaly": bool(size_anom),
                "sample_count": len(records),
                "task_details": task_details,
                "evidence": evidence,
            },
            "predicted_content": predicted_content,
            "basis": basis,
        }

    # ------------------------- 备份数据验证风险 -------------------------
    @staticmethod
    def _light_fingerprint(path: str, size: int) -> str:
        """大文件轻指纹：头 8KB + 尾 8KB + 文件大小 的 sha256。

        用于 size > verify_max_file_mb 的场景，避免全量 sha256 打满磁盘 IO。
        注意轻指纹与落库的全量 sha256 不同源，**不可直接比对**，
        仅作为「本次已抽查过」的留痕。

        Args:
            path: 备份文件绝对路径。
            size: 文件字节数。

        Returns:
            十六进制摘要；读取失败返回空字符串。
        """
        import hashlib
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read(8192))
                if size > 16384:
                    f.seek(-8192, os.SEEK_END)
                    h.update(f.read(8192))
            h.update(str(size).encode("utf-8"))
            return h.hexdigest()
        except Exception:
            return ""

    @staticmethod
    def _probe_archive_usable(path: str) -> tuple:
        """L2 可用性探测：gzip 魔数 / tar / SQL dump 关键标记扫描。

        与 scheduler._verify_backup 的探测口径保持一致（同一套判定），
        差别是本函数在告警侧只读不写。

        Args:
            path: 备份文件绝对路径。

        Returns:
            (ok, note) 二元组；ok=False 表示文件格式无法识别。
        """
        try:
            with open(path, "rb") as f:
                head = f.read(8192)
        except Exception as exc:
            return False, f"文件不可读: {exc}"
        if not head:
            return False, "文件内容为空"
        if head[:2] == b"\x1f\x8b":
            return True, "gzip 魔数正常"
        if head[:4] == b"PK\x03\x04":
            return True, "zip 魔数正常"
        upper = head.upper()
        for marker in (b"CREATE", b"INSERT", b"-- MYSQL DUMP", b"PGDMP",
                       b"POSTGRESQL", b"PG_DUMP", b"REDIS", b"SQLITE FORMAT"):
            if marker in upper:
                return True, "识别到数据库导出标记"
        # tar 归档：偏移 257 处的 magic
        if len(head) > 262 and head[257:262] in (b"ustar",):
            return True, "tar 归档正常"
        return False, "未识别到已知备份格式标记"

    @staticmethod
    def _sample_restore_drill(cfg: dict) -> dict:
        """L3 抽样恢复演练（P2 预留，本期空实现且默认关闭）。

        Args:
            cfg: verify_fail 子配置。

        Returns:
            固定结构 dict，enabled=False 时 checked/failed 恒为 0。
        """
        return {
            "enabled": bool(cfg.get("l3_enabled", False)),
            "checked": 0,
            "failed": 0,
            "note": "抽样恢复演练本期未实现（P2 独立迭代）",
        }

    @staticmethod
    def _verify_suggestion(kind: str) -> str:
        """验证类问题的建议动作映射。

        Args:
            kind: l1_fail | l2_fail | no_checksum | unverified 之一。

        Returns:
            中文建议动作。
        """
        return {
            "l1_fail": "校验和不匹配，备份文件可能已损坏，立即重跑全量备份并排查存储介质",
            "l2_fail": "备份文件格式无法识别，检查备份引擎输出与压缩完整性",
            "no_checksum": "执行 scripts/backfill_checksum.py 回填校验和后重新校验",
            "unverified": "开启备份后自动校验，或对该任务手工触发一次校验",
        }.get(kind, DEFAULT_SUGGESTION)

    def analyze_backup_verify_risk(self) -> dict:
        """备份数据验证风险（metric = verify_fail）。

        三层校验（按配置开关分层启用）：
          * L1 完整性：文件存在 + size>0 + sha256 与落库 checksum 比对。
            **checksum 为空时绝不判失败**，只计入未校验占比（避免存量误报 critical）。
          * L2 可用性：落库 verify_msg 表明失败，或实时文件格式探测失败。
          * L3 可恢复性：抽样 dry-run 恢复，本期空实现默认关闭。
        派生：verified=0 占比 ≥ 阈值 → 55；距上次成功验证超期 → 45。

        IO 限流：单次最多抽查 verify_sample_limit 条最近记录；
        超过 verify_max_file_mb 的文件退化为头尾轻指纹，跳过全量 sha256。

        Returns:
            标准 analyzer 返回体，details 与 backup_fail 同构
            （含 task_details / evidence），供前端复用展开子表组件。
        """
        full_cfg = self.get_config()
        cfg = full_cfg.get("verify_fail")
        if not isinstance(cfg, dict):
            cfg = json.loads(json.dumps(DEFAULT_AI_CONFIG["verify_fail"]))

        try:
            records = models.list_records(limit=200)
        except Exception as exc:
            self.logger.warning("[ai_alert] 读取备份记录失败: %s", exc)
            return self._empty_metric("verify_fail", f"读取备份记录失败: {exc}")
        # 只有成功产出物才谈得上「验证」，失败记录归 backup_fail 维度
        candidates = [r for r in records
                      if r.get("status") in ("success", "simulated")]
        if not candidates:
            return self._empty_metric("verify_fail", "无可验证的备份记录")

        sample_limit = max(1, int(cfg.get("verify_sample_limit", 20) or 20))
        max_bytes = max(1, int(cfg.get("verify_max_file_mb", 512) or 512)) * 1024 * 1024
        l1_enabled = bool(cfg.get("l1_enabled", True))
        l2_enabled = bool(cfg.get("l2_enabled", True))
        stale_days = max(1, int(cfg.get("stale_days", 7) or 7))
        ratio_warn = float(cfg.get("unverified_ratio_warn", 0.3) or 0.3)

        sample = candidates[:sample_limit]
        layers = {
            "l1": {"checked": 0, "failed": 0, "skipped": 0},
            "l2": {"checked": 0, "failed": 0, "skipped": 0},
            "l3": self._sample_restore_drill(cfg),
        }
        unverified = 0
        no_checksum = 0
        bad_records: list = []          # (record, kind, note)
        evidence_records: list = []

        for r in sample:
            rec_id = r.get("id")
            checksum = str(r.get("checksum") or "").strip()
            path = str(r.get("backup_path") or "").strip()
            is_sim = int(r.get("is_simulated") or 0) == 1
            verified = int(r.get("verified") or 0)
            verify_msg = str(r.get("verify_msg") or "").strip()

            if verified != 1:
                unverified += 1
            if not checksum:
                no_checksum += 1

            # ---- L1 完整性 ----
            # 仿真记录无真实文件，checksum 为空无基准可比 —— 两者均不判失败
            if l1_enabled and not is_sim and checksum and path:
                if not os.path.isfile(path):
                    layers["l1"]["checked"] += 1
                    layers["l1"]["failed"] += 1
                    bad_records.append((r, "l1_fail", f"备份文件已丢失: {path}"))
                else:
                    try:
                        real_size = os.path.getsize(path)
                    except OSError as exc:
                        real_size = -1
                        self.logger.warning("[ai_alert] 读取文件大小失败 %s: %s", path, exc)
                    if real_size == 0:
                        layers["l1"]["checked"] += 1
                        layers["l1"]["failed"] += 1
                        bad_records.append((r, "l1_fail", "备份文件大小为 0"))
                    elif real_size < 0:
                        layers["l1"]["skipped"] += 1
                    elif real_size > max_bytes:
                        # 大文件退化轻指纹：只留痕不比对，不参与失败判定
                        layers["l1"]["skipped"] += 1
                        self._light_fingerprint(path, real_size)
                    else:
                        layers["l1"]["checked"] += 1
                        try:
                            actual = db.sha256_file(path)
                        except Exception as exc:
                            actual = ""
                            self.logger.warning("[ai_alert] 计算 sha256 失败 %s: %s", path, exc)
                        if actual and actual != checksum:
                            layers["l1"]["failed"] += 1
                            bad_records.append((r, "l1_fail", "sha256 与落库校验和不一致"))
            elif l1_enabled and not checksum:
                layers["l1"]["skipped"] += 1

            # ---- L2 可用性 ----
            if l2_enabled:
                if verified == 0 and verify_msg:
                    # 落库校验结论即为失败（scheduler 写入的错误文案）
                    layers["l2"]["checked"] += 1
                    layers["l2"]["failed"] += 1
                    bad_records.append((r, "l2_fail",
                                        verify_msg[:ERROR_SUMMARY_MAX_CHARS]))
                elif not is_sim and path and os.path.isfile(path):
                    layers["l2"]["checked"] += 1
                    ok, note = self._probe_archive_usable(path)
                    if not ok:
                        layers["l2"]["failed"] += 1
                        bad_records.append((r, "l2_fail", note))
                else:
                    layers["l2"]["skipped"] += 1

            if rec_id is not None and (verified != 1 or not checksum):
                try:
                    evidence_records.append(int(rec_id))
                except (TypeError, ValueError):
                    pass

        sample_count = len(sample)
        unverified_ratio = round(unverified / sample_count, 3) if sample_count else 0.0

        # 距上次成功验证的天数（全量候选里找最近一条 verified=1）
        last_verified_at = None
        for r in candidates:
            if int(r.get("verified") or 0) == 1:
                last_verified_at = r.get("finished_at") or r.get("started_at")
                break
        stale_days_actual = None
        if last_verified_at:
            ts = self._parse_ts(last_verified_at)
            if ts:
                try:
                    stale_days_actual = (
                        datetime.now(timezone.utc).astimezone() - ts).days
                except (TypeError, ValueError):
                    stale_days_actual = None
        is_stale = (stale_days_actual is None
                    or stale_days_actual > stale_days)

        # ---------- 评分 ----------
        score = 0.0
        if layers["l1"]["failed"] > 0:
            score = max(score, 90.0)
        if layers["l2"]["failed"] > 0:
            score = max(score, 70.0)
        if unverified_ratio >= ratio_warn:
            score = max(score, 55.0)
        if is_stale:
            score = max(score, 45.0)
        score = min(100.0, score)
        if score == 0.0:
            return self._empty_metric("verify_fail", "备份验证通过")

        # ---------- 任务级明细（与 backup_fail 同构，前端子表可复用） ----------
        task_index: dict = {}
        try:
            for t in models.list_tasks():
                try:
                    task_index[int(t.get("id"))] = t
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            self.logger.warning("[ai_alert] 读取任务列表失败，任务名降级为 ID: %s", exc)

        now = datetime.now(timezone.utc).astimezone()
        cutoff_7 = now - timedelta(days=7)
        cutoff_30 = now - timedelta(days=30)
        buckets: dict = {}
        for rec, kind, note in bad_records:
            try:
                tid = int(rec.get("task_id"))
            except (TypeError, ValueError):
                continue
            buckets.setdefault(tid, []).append((rec, kind, note))
        # 未校验但未判失败的记录，也按任务聚合（占比风险的归属）
        unverified_buckets: dict = {}
        for r in sample:
            if int(r.get("verified") or 0) == 1:
                continue
            try:
                tid = int(r.get("task_id"))
            except (TypeError, ValueError):
                continue
            unverified_buckets.setdefault(tid, []).append(r)

        task_details: list = []
        for tid in set(list(buckets.keys()) + list(unverified_buckets.keys())):
            bad = buckets.get(tid, [])
            unv = unverified_buckets.get(tid, [])
            fail_7d = fail_30d = 0
            last_fail_at = None
            last_error = None
            kind_of_worst = "unverified"
            for rec, kind, note in bad:
                ts = self._parse_ts(rec.get("started_at"))
                if ts is None or ts >= cutoff_30:
                    fail_30d += 1
                if ts is None or ts >= cutoff_7:
                    fail_7d += 1
                if last_fail_at is None:
                    last_fail_at = rec.get("finished_at") or rec.get("started_at")
                    last_error = str(note or "")[:ERROR_SUMMARY_MAX_CHARS]
                    kind_of_worst = kind
                elif kind == "l1_fail" and kind_of_worst != "l1_fail":
                    kind_of_worst = "l1_fail"
            if not bad and unv:
                first = unv[0]
                last_fail_at = first.get("finished_at") or first.get("started_at")
                has_checksum = bool(str(first.get("checksum") or "").strip())
                kind_of_worst = "unverified" if has_checksum else "no_checksum"
                last_error = ("该任务备份未经校验"
                              if has_checksum else "该任务备份无校验和记录")

            task_score = 0.0
            if any(k == "l1_fail" for _, k, _ in bad):
                task_score = 90.0
            elif any(k == "l2_fail" for _, k, _ in bad):
                task_score = 70.0
            elif unv:
                task_score = 55.0 if len(unv) >= 2 else 45.0

            task = task_index.get(tid) or {}
            task_details.append({
                "task_id": tid,
                "task_name": task.get("name") or f"任务#{tid}",
                "db_type": task.get("db_type") or None,
                "fail_7d": fail_7d,
                "fail_30d": fail_30d,
                "last_fail_at": last_fail_at,
                "last_error": last_error,
                "task_risk_score": round(task_score, 1),
                "suggestion": self._verify_suggestion(kind_of_worst),
            })
        task_details.sort(key=lambda d: d["task_risk_score"], reverse=True)
        task_details = task_details[:TASK_DETAIL_TOP_N]

        # ---------- 人类可读预测内容 & 依据 ----------
        basis: list = []
        if layers["l1"]["failed"] > 0:
            basis.append(
                f"L1 完整性校验失败 {layers['l1']['failed']} 条"
                f"（已校验 {layers['l1']['checked']} 条），备份文件可能已损坏")
        if layers["l2"]["failed"] > 0:
            basis.append(
                f"L2 可用性探测失败 {layers['l2']['failed']} 条"
                f"（已探测 {layers['l2']['checked']} 条）")
        if unverified_ratio >= ratio_warn:
            basis.append(
                f"抽样 {sample_count} 条中 {unverified} 条未通过校验"
                f"（占比 {round(unverified_ratio * 100, 1)}%，≥阈值 {round(ratio_warn * 100, 1)}%）")
        if no_checksum > 0:
            basis.append(
                f"{no_checksum} 条记录无校验和，建议执行 scripts/backfill_checksum.py 回填")
        if is_stale:
            if stale_days_actual is None:
                basis.append("从未有过成功的备份验证记录")
            else:
                basis.append(
                    f"距上次成功验证已 {stale_days_actual} 天（超期阈值 {stale_days} 天）")
        if layers["l1"]["skipped"] > 0:
            basis.append(
                f"{layers['l1']['skipped']} 条因无校验和或文件过大跳过 L1 全量比对")

        if layers["l1"]["failed"] > 0:
            predicted_content = f"预测 {layers['l1']['failed']} 个备份文件已损坏，恢复将失败"
        elif layers["l2"]["failed"] > 0:
            predicted_content = f"预测 {layers['l2']['failed']} 个备份文件可用性存疑"
        else:
            predicted_content = "预测备份可恢复性存在风险（校验覆盖不足）"

        return {
            "metric": "verify_fail",
            "risk_score": round(score, 1),
            "risk_level": _level_from_score(score),
            "details": {
                "layers": layers,
                "sample_count": sample_count,
                "unverified_count": unverified,
                "unverified_ratio": unverified_ratio,
                "no_checksum_count": no_checksum,
                "last_verified_at": last_verified_at,
                "stale_days": stale_days_actual,
                "task_details": task_details,
                "evidence": {
                    "task_ids": [d["task_id"] for d in task_details],
                    "record_ids": sorted(set(evidence_records),
                                         reverse=True)[:EVIDENCE_RECORD_LIMIT],
                },
            },
            "predicted_content": predicted_content,
            "basis": basis,
        }

    # ------------------------- 存储容量风险 -------------------------
    def analyze_storage_risk(self) -> dict:
        cfg = self.get_config()["storage_full"]
        l1 = self._l1_usage()
        score = 0.0
        signals = []
        if l1 and not l1.get("error"):
            used = float(l1.get("used_percent", 0))
            if used >= cfg["l1_critical_pct"]:
                score = max(score, 95.0)
                signals.append(("L1磁盘", used, "critical"))
            elif used >= cfg["l1_warn_pct"]:
                score = max(score, 80.0)
                signals.append(("L1磁盘", used, "warn"))
            elif used >= 70:
                score = max(score, 50.0)
                signals.append(("L1磁盘", used, "elevated"))

        targets = db.query(
            "SELECT * FROM storage_targets WHERE enabled=1 AND type IN ('minio','s3')")
        for t in targets:
            extra = {}
            if t.get("extra_options"):
                try:
                    extra = json.loads(t["extra_options"])
                except Exception:
                    extra = {}
            used_pct = float(extra.get("used_pct") or 0)
            if used_pct >= cfg["bucket_critical_pct"]:
                score = max(score, 92.0)
                signals.append((t["name"], used_pct, "critical"))
            elif used_pct >= cfg["bucket_warn_pct"]:
                score = max(score, 78.0)
                signals.append((t["name"], used_pct, "warn"))
            growth = float(extra.get("growth_per_day_pct") or 0)
            if used_pct > 0 and growth > 0:
                days_to_full = (100 - used_pct) / growth
                if days_to_full <= cfg["forecast_days"]:
                    score = max(score, 75.0)
                    signals.append((f"{t['name']}趋势", round(days_to_full, 1), "forecast"))
            if t.get("last_error") and self._recent_error(t.get("last_test_at")):
                if any(k in (t.get("last_error") or "")
                       for k in ("容量", "quota", "满", "空间", "full")):
                    score = max(score, 70.0)
                    signals.append((f"{t['name']}错误", t["last_error"], "error"))

        if not signals and score == 0.0:
            return self._empty_metric("storage_full", "存储用量正常")
        score = min(100.0, score)
        # ---------- 人类可读预测内容 & 依据 ----------
        basis: list[str] = []
        predicted_content = ""
        if l1 and not l1.get("error"):
            l1_pct = float(l1.get("used_percent", 0))
            l1_label = l1.get("label", "L1(MinIO)")
            if l1_pct >= cfg["l1_critical_pct"]:
                basis.append(f"{l1_label} 使用率 {l1_pct}%（≥临界阈值 {cfg['l1_critical_pct']}%）")
            elif l1_pct >= cfg["l1_warn_pct"]:
                basis.append(f"{l1_label} 使用率 {l1_pct}%（≥警告阈值 {cfg['l1_warn_pct']}%）")
            elif l1_pct >= 70:
                basis.append(f"{l1_label} 使用率 {l1_pct}%（偏高）")
        for t in targets:
            extra = {}
            if t.get("extra_options"):
                try:
                    extra = json.loads(t["extra_options"])
                except Exception:
                    extra = {}
            used_pct = float(extra.get("used_pct") or 0)
            if used_pct >= cfg["bucket_critical_pct"]:
                basis.append(f"存储目标 {t['name']} 使用率 {used_pct}%（≥临界阈值 {cfg['bucket_critical_pct']}%）")
            elif used_pct >= cfg["bucket_warn_pct"]:
                basis.append(f"存储目标 {t['name']} 使用率 {used_pct}%（≥警告阈值 {cfg['bucket_warn_pct']}%）")
            growth = float(extra.get("growth_per_day_pct") or 0)
            if used_pct > 0 and growth > 0:
                days_to_full = (100 - used_pct) / growth
                if days_to_full <= cfg["forecast_days"]:
                    basis.append(f"存储目标 {t['name']} 预计 {round(days_to_full, 1)} 天内填满（日增 {growth}%）")
            if t.get("last_error") and self._recent_error(t.get("last_test_at")):
                if any(k in (t.get("last_error") or "")
                       for k in ("容量", "quota", "满", "空间", "full")):
                    basis.append(f"存储目标 {t['name']} 近期错误：{t['last_error']}")
        if score > 0:
            predicted_content = "预测存储容量将达临界"
        return {
            "metric": "storage_full",
            "risk_score": round(score, 1),
            "risk_level": _level_from_score(score),
            "details": {
                "l1_used_percent": (l1 or {}).get("used_percent"),
                "signals": [{"name": s[0], "value": s[1], "kind": s[2]}
                            for s in signals],
            },
            "predicted_content": predicted_content,
            "basis": basis,
        }

    # ------------------------- 链路劣化风险 -------------------------
    # 源任务失败时追加的劣化分（低于 switch_count_high(70) 与
    # consistency_fail_score(60)，不压过既有权重）
    SOURCE_FAILED_PENALTY = 25.0

    def _link_source_degraded(self, link: dict) -> tuple:
        """判断链路引用的数据源当前是否处于失败/劣化状态。

        Args:
            link: models.list_disaster_links() 的单条链路 dict。

        Returns:
            (degraded, source_name, reason) 三元组；无引用源时 degraded=False。
        """
        kind = str(link.get("source_kind") or "manual")
        source_id = link.get("source_id")
        if kind == "manual" or not source_id:
            return False, "", ""
        try:
            source_id = int(source_id)
        except (TypeError, ValueError):
            return False, "", ""
        try:
            if kind == "sync_task":
                src = models.get_sync_task(source_id)
                if not src:
                    return True, f"同步任务#{source_id}", "源同步任务已被删除"
                name = src.get("name") or f"同步任务#{source_id}"
                if str(src.get("last_status") or "").lower() == "failed":
                    return True, name, "源同步任务最近一次同步失败"
                return False, name, ""
            if kind == "rt_task":
                task = models.get_task(source_id)
                if not task:
                    return True, f"实时任务#{source_id}", "源实时保护任务已被删除"
                name = task.get("name") or f"实时任务#{source_id}"
                if str(task.get("last_status") or "").lower() == "failed":
                    return True, name, "源实时保护任务最近一次执行失败"
                rt = models.get_rt_task(source_id)
                if rt and str(rt.get("health_status") or "") in ("degraded", "stopped"):
                    return True, name, f"源实时保护任务健康度 {rt.get('health_status')}"
                return False, name, ""
        except Exception as exc:
            self.logger.warning("[ai_alert] 读取链路数据源状态失败 kind=%s id=%s: %s",
                                kind, source_id, exc)
        return False, "", ""

    def analyze_link_health(self) -> dict:
        cfg = self.get_config()["link_degraded"]
        links = models.list_disaster_links()
        if not links:
            return self._empty_metric("link_degraded", "无容灾链路")
        score = 0.0
        worst = None
        degraded_sources = 0
        for link in links:
            link_score = 0.0
            cr = link.get("consistency_result")
            if cr == "fail":
                link_score = max(link_score, cfg["consistency_fail_score"])
            elif cr == "warn":
                link_score = max(link_score, 35.0)
            switches = self._recent_route_switches(link["id"], cfg["switch_window_days"])
            if switches >= cfg["switch_count_high"]:
                link_score = max(link_score, 70.0)
            elif switches >= 2:
                link_score = max(link_score, 45.0)
            # 源同步/实时任务失败 → 追加劣化因子（不压过既有权重）
            degraded, _src_name, _reason = self._link_source_degraded(link)
            if degraded:
                link_score = min(100.0, link_score + self.SOURCE_FAILED_PENALTY)
                degraded_sources += 1
            if link_score > score:
                score = link_score
                worst = link.get("name")
        if score == 0.0:
            return self._empty_metric("link_degraded", "链路健康")
        # ---------- 人类可读预测内容 & 依据 ----------
        basis: list[str] = []
        predicted_content = ""
        for link in links:
            link_score = 0.0
            cr = link.get("consistency_result")
            if cr == "fail":
                basis.append(f"链路 {link.get('name', '?')} 一致性校验失败")
            elif cr == "warn":
                basis.append(f"链路 {link.get('name', '?')} 一致性校验告警")
            switches = self._recent_route_switches(link["id"], cfg["switch_window_days"])
            if switches >= cfg["switch_count_high"]:
                basis.append(f"链路 {link.get('name', '?')} 近 {cfg['switch_window_days']} 天选路切换 {switches} 次（≥阈值 {cfg['switch_count_high']} 次）")
            elif switches >= 2:
                basis.append(f"链路 {link.get('name', '?')} 近 {cfg['switch_window_days']} 天选路切换 {switches} 次")
            degraded, src_name, reason = self._link_source_degraded(link)
            if degraded:
                basis.append(
                    f"链路 {link.get('name', '?')} 的数据源「{src_name}」异常："
                    f"{reason}（劣化 +{int(self.SOURCE_FAILED_PENALTY)} 分）")
        if score > 0:
            predicted_content = "预测容灾链路将出现劣化或中断风险"
        return {
            "metric": "link_degraded",
            "risk_score": round(score, 1),
            "risk_level": _level_from_score(score),
            "details": {"worst_link": worst, "link_count": len(links),
                        "degraded_source_count": degraded_sources},
            "predicted_content": predicted_content,
            "basis": basis,
        }

    # ------------------------- 演练合规 / RPO 风险 -------------------------
    def analyze_drill_compliance(self) -> dict:
        cfg = self.get_config()["drill_overdue"]
        drills = models.list_drills()
        if not drills:
            return self._empty_metric("drill_overdue", "无演练记录")
        last = drills[0]  # id DESC，最近一次
        last_finished = last.get("finished_at") or last.get("created_at")
        overdue_days = 0
        if last_finished:
            ts = self._parse_ts(last_finished)
            if ts:
                overdue_days = (datetime.now(timezone.utc).astimezone() - ts).days
        overdue = overdue_days >= cfg["interval_days"]
        rto_breach = (last.get("rto_actual_sec") or 0) > cfg["rto_target_sec"] and (
            last.get("rto_actual_sec") or 0) > 0
        rpo_breach = (last.get("rpo_actual_sec") or 0) > cfg["rpo_target_sec"] and (
            last.get("rpo_actual_sec") or 0) > 0

        score = 0.0
        if overdue:
            score = max(score, 70.0)
        if rto_breach:
            score = max(score, 65.0)
        if rpo_breach:
            score = max(score, 80.0)
        if score == 0.0:
            return self._empty_metric("drill_overdue", "演练合规")
        metric = "rpo_breach" if rpo_breach else "drill_overdue"
        # ---------- 人类可读预测内容 & 依据 ----------
        basis: list[str] = []
        predicted_content = ""
        if overdue:
            basis.append(f"距上次演练已 {overdue_days} 天（超期阈值 {cfg['interval_days']} 天）")
        if rto_breach:
            actual_rto = last.get("rto_actual_sec") or 0
            basis.append(f"RTO 实际 {actual_rto}s 超过目标 {cfg['rto_target_sec']}s")
        if rpo_breach:
            actual_rpo = last.get("rpo_actual_sec") or 0
            basis.append(f"RPO 实际 {actual_rpo}s 超过目标 {cfg['rpo_target_sec']}s")
        if metric == "rpo_breach":
            predicted_content = "预测 RPO 目标将持续未达标"
        else:
            predicted_content = "预测演练合规风险上升"
        return {
            "metric": metric,
            "risk_score": round(score, 1),
            "risk_level": _level_from_score(score),
            "details": {
                "last_drill": last.get("name"),
                "overdue_days": overdue_days,
                "interval_days": cfg["interval_days"],
                "rto_actual_sec": last.get("rto_actual_sec"),
                "rpo_actual_sec": last.get("rpo_actual_sec"),
                "rto_target_sec": cfg["rto_target_sec"],
                "rpo_target_sec": cfg["rpo_target_sec"],
                "rto_breach": bool(rto_breach),
                "rpo_breach": bool(rpo_breach),
            },
            "predicted_content": predicted_content,
            "basis": basis,
        }

    # ------------------------- 全量分析 -------------------------
    def run_all_checks(self) -> dict:
        if not self.get_config().get("enabled", True):
            return {"skipped": True, "reason": "disabled"}
        analyzers = [
            ("backup_fail", self.analyze_backup_failure_risk),
            ("verify_fail", self.analyze_backup_verify_risk),
            ("storage_full", self.analyze_storage_risk),
            ("link_degraded", self.analyze_link_health),
            ("drill_overdue", self.analyze_drill_compliance),
        ]
        min_level = self.get_config().get("min_risk_level_to_record", "medium")
        recorded = 0
        critical_fired = 0
        results = []
        for metric, fn in analyzers:
            try:
                # 通过 predict_with_ai 路由：模型启用则走模型，否则走规则引擎
                res = self.predict_with_ai(metric)
            except Exception as e:
                self.logger.warning("[ai_alert] %s 分析异常: %s", fn.__name__, e)
                continue
            results.append(res)
            if res.get("empty"):
                continue
            level = res.get("risk_level")
            if _LEVEL_RANK.get(level, 0) >= _LEVEL_RANK.get(min_level, 1):
                self._record_prediction(res)
                recorded += 1
                if level == "critical":
                    critical_fired += 1
                    self._fire_critical(res)
        summary = {
            "recorded": recorded,
            "critical_fired": critical_fired,
            "results": results,
            "run_at": db.now_iso(),
        }
        self.logger.info("[ai_alert] 全量分析完成: %s", summary)
        return summary

    # ------------------------- 查询 -------------------------
    def get_recent_predictions(self, limit: int = 50) -> list:
        return models.list_alert_predictions(limit=limit)

    def get_prediction_stats(self, days: int = 7) -> dict:
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=days)).isoformat()
        rows = db.query(
            "SELECT metric, risk_level, COUNT(*) AS cnt FROM alert_predictions "
            "WHERE predicted_at >= ? GROUP BY metric, risk_level", (cutoff,))
        by_metric = {}
        for r in rows:
            m = r["metric"]
            by_metric.setdefault(m, {"low": 0, "medium": 0, "high": 0, "critical": 0})
            by_metric[m][r["risk_level"]] = r["cnt"]
        # 每个 metric 最近一次的最高风险
        latest = {}
        all_rows = db.query(
            "SELECT metric, risk_level, risk_score FROM alert_predictions "
            "ORDER BY id DESC")
        seen = set()
        for r in all_rows:
            m = r["metric"]
            if m in seen:
                continue
            seen.add(m)
            latest[m] = {"risk_level": r["risk_level"],
                        "risk_score": r["risk_score"]}
        trend = db.query(
            "SELECT substr(predicted_at,1,10) AS day, COUNT(*) AS cnt "
            "FROM alert_predictions WHERE predicted_at >= ? GROUP BY day ORDER BY day",
            (cutoff,))
        return {
            "window_days": days,
            "by_metric": by_metric,
            "latest": latest,
            "trend": [{"day": t["day"], "count": t["cnt"]} for t in trend],
        }

    # ------------------------- 内部工具 -------------------------
    def _record_prediction(self, res: dict) -> None:
        # 将 model_source 嵌入到 details 中（因 DB 无独立列，通过 details JSON 持久化）
        details = res.get("details", {}) or {}
        details["model_source"] = res.get("model_source", "规则引擎")
        models.create_alert_prediction({
            "metric": res["metric"],
            "risk_score": res["risk_score"],
            "risk_level": res["risk_level"],
            "details": details,
            "predicted_content": res.get("predicted_content", ""),
            "basis": res.get("basis", []),
        })

    def _fire_critical(self, res: dict) -> None:
        """critical 级别触发通知（DEMO 下 notifier 不真实发送但记录日志）。"""
        text = (f"AI 预测告警（{res['metric']}）：风险等级 {res['risk_level']}，"
                f"风险评分 {res['risk_score']}\n"
                f"详情: {json.dumps(res.get('details', {}), ensure_ascii=False)}")
        try:
            from core import notifier
            notifier.Notifier(None, self.logger).notify(
                "failure", f"[AI告警-critical] {res['metric']}", text=text)
        except Exception:
            pass
        db.add_log("ERROR", "ai_alert",
                   f"critical 风险触发通知: {res['metric']} score={res['risk_score']}")

    def _recent_route_switches(self, link_id: int, window_days: int) -> int:
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=window_days)).isoformat()
        rows = db.query(
            "SELECT COUNT(*) AS cnt FROM system_logs WHERE source='disaster_link' "
            "AND message LIKE ? AND ts >= ?",
            (f"链路#{link_id} 智能选路%", cutoff))
        return rows[0]["cnt"] if rows else 0

    def _parse_ts(self, ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            try:
                return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None

    def _recent_error(self, ts) -> bool:
        if not ts:
            return False
        t = self._parse_ts(ts)
        if not t:
            return False
        return (datetime.now(timezone.utc).astimezone() - t).days <= 7

    def _l1_usage(self) -> dict:
        """获取 L1 层存储用量。

        L1 = MinIO 热数据（TYPE_META tier=1）。优先从 storage_targets 中 type='minio'
        的目标读取 extra_options 中缓存的用量指标；若无法获取（DEMO / 网络异常 / 无 MinIO 目标），
        则回退测量本机备份暂存目录磁盘并标注 label 为 'L1(暂存回退)'，
        不误把 local(L3) 当 L1。
        """
        try:
            import os as _os
            import shutil
            # 1. 优先尝试读取 MinIO 目标的缓存用量（来自 extra_options）
            minio_row = db.query_one(
                "SELECT endpoint, extra_options FROM storage_targets "
                "WHERE type='minio' AND enabled=1 ORDER BY is_default DESC, id LIMIT 1")
            if minio_row:
                extra = {}
                if minio_row.get("extra_options"):
                    try:
                        extra = json.loads(minio_row["extra_options"])
                    except Exception:
                        extra = {}
                used_pct = float(extra.get("used_pct") or 0)
                if used_pct > 0:
                    total_bytes = int(extra.get("total_bytes") or 0)
                    used_bytes = int(extra.get("used_bytes") or 0)
                    free_bytes = total_bytes - used_bytes if total_bytes else 0
                    return {
                        "label": "L1(MinIO)",
                        "total_bytes": total_bytes,
                        "used_bytes": used_bytes,
                        "free_bytes": free_bytes,
                        "used_percent": round(used_pct, 1),
                    }
                # used_pct 未设置或为 0 → 尝试连接 MinIO 实时查询
                try:
                    from core.storage_backends.minio import MinIOStorageBackend
                    backend_cfg = {
                        "endpoint": minio_row.get("endpoint") or "",
                        "access_key": minio_row.get("access_key") or "",
                        "secret_key": minio_row.get("secret_key") or "",
                        "bucket": minio_row.get("bucket") or "backup",
                        "prefix": minio_row.get("prefix") or "",
                        "region": minio_row.get("region") or "",
                    }
                    extra_opts = {}
                    if minio_row.get("extra_options"):
                        try:
                            extra_opts = json.loads(minio_row["extra_options"])
                        except Exception:
                            pass
                    backend_cfg.setdefault("skip_tls", extra_opts.get("skip_tls", False))
                    backend_cfg.setdefault("path_style", extra_opts.get("path_style", True))
                    backend = MinIOStorageBackend(backend_cfg, self.logger)
                    # 尝试通过 MinIO client 获取 bucket 磁盘用量
                    bucket_info = backend.get_bucket_usage()
                    if bucket_info and bucket_info.get("used_percent", 0) > 0:
                        return {
                            "label": "L1(MinIO)",
                            **bucket_info,
                        }
                except Exception as e:
                    self.logger.info("[ai_alert] MinIO 远程用量获取失败(%s)，回退暂存目录", e)
            # 2. 回退：测量本机备份暂存目录磁盘（标注为暂存回退，不混淆层级）
            fallback_row = db.query_one(
                "SELECT endpoint FROM storage_targets WHERE type='local' "
                "AND enabled=1 ORDER BY is_default DESC, id LIMIT 1")
            fallback_path = (fallback_row and fallback_row["endpoint"]) or "./backups"
            fallback_path = _os.path.abspath(fallback_path)
            du = shutil.disk_usage(fallback_path)
            return {
                "label": "L1(暂存回退)",
                "path": fallback_path,
                "total_bytes": du.total,
                "used_bytes": du.used,
                "free_bytes": du.free,
                "used_percent": round(du.used / du.total * 100, 1),
            }
        except Exception as e:
            return {"error": str(e)}

    def _level_rank(self, level: str) -> int:
        return _LEVEL_RANK.get(level, 0)

    def _empty_metric(self, metric: str, note: str) -> dict:
        return {
            "metric": metric,
            "risk_score": 0.0,
            "risk_level": "low",
            "details": {"note": note},
            "predicted_content": "",
            "basis": [],
            "empty": True,
            "model_source": "规则引擎",
        }


# 便捷单例，供调度/API 直接调用
ai_predictor = AIPredictor()
