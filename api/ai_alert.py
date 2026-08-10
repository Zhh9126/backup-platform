# -*- coding: utf-8 -*-
"""
AI 预测告警 API（AIPredictor）。

路由前缀: /api/alerts（通过共享 api_bp 注册）
- GET    /api/alerts/predictions   预测列表（支持 ?metric= 过滤）
- POST   /api/alerts/run            手动触发一次全量分析
- GET    /api/alerts/stats          统计概览（?days=7）
- GET    /api/alerts/config         当前 AI 配置（阈值/开关，api_key 掩码）
- POST   /api/alerts/config         保存配置
- POST   /api/alerts/model/test     测试模型连接
- GET    /api/alerts/model/status   模型配置状态
"""
import json
import time

import core.db as db
import core.models as models
from auth import login_required
from core import ai_alert as ai_alert_engine
from core.ai_alert import PROVIDER_PRESETS
from . import api_bp
from flask import request, jsonify


def _predictor():
    return ai_alert_engine.AIPredictor()


@api_bp.route("/alerts/predictions", methods=["GET"])
@login_required
def api_list_predictions():
    metric = request.args.get("metric")
    limit = request.args.get("limit", default=200, type=int)
    rows = models.list_alert_predictions(metric=metric, limit=limit)
    # 将 basis（JSON str）解析为 list[str]，predicted_content 确保非 None
    parsed = [models._ap_to_dict(r) for r in rows]
    return jsonify({"predictions": parsed})


@api_bp.route("/alerts/run", methods=["POST"])
@login_required
def api_run_alerts():
    summary = _predictor().run_all_checks()
    return jsonify({"ok": True, "summary": summary})


@api_bp.route("/alerts/stats", methods=["GET"])
@login_required
def api_alert_stats():
    days = request.args.get("days", default=7, type=int)
    stats = _predictor().get_prediction_stats(days=days)
    return jsonify(stats)


@api_bp.route("/alerts/config", methods=["GET"])
@login_required
def api_alert_config():
    """返回 AI 告警配置（api_key 掩码，不回显明文）。"""
    return jsonify(_predictor().get_safe_config())


@api_bp.route("/alerts/config", methods=["POST"])
@login_required
def api_alert_save_config():
    data = request.get_json(silent=True) or {}
    predictor = _predictor()
    cfg = predictor.save_config(data)
    # 返回时用 safe config（api_key 掩码）
    safe_cfg = predictor.get_safe_config()
    return jsonify({"ok": True, "config": safe_cfg})


@api_bp.route("/alerts/model/test", methods=["POST"])
@login_required
def api_alert_model_test():
    """测试模型连接：用当前已保存配置 + 提交覆盖，发起一次最小推理。

    请求体可选覆盖字段：endpoint, api_key, model_name, provider 等。
    返回 {ok, status_code, latency_ms, sample_response} 或 {ok:false, error}。
    """
    predictor = _predictor()
    cfg = predictor.get_config()
    ai_model = cfg.get("ai_model", {})

    # 合并提交的覆盖字段（不持久化）
    override = request.get_json(silent=True) or {}
    for k, v in override.items():
        if k == "api_key" and not v:
            # 空字符串视为不修改，保留已保存的密钥
            continue
        if k in ("enabled", "provider", "endpoint", "api_key", "model_name",
                 "local_model_path", "request_timeout_sec", "max_input_chars",
                 "prompt_template"):
            ai_model[k] = v
    cfg["ai_model"] = ai_model

    # 构造极简测试提示词
    test_prompt = "请以 JSON 格式返回：{\"risk_score\": 0, \"risk_level\": \"low\", \"predicted_content\": \"测试连接成功\", \"basis\": [\"测试\"]}"

    # 截断到 max_input_chars
    max_chars = int(ai_model.get("max_input_chars", 8000) or 8000)
    test_prompt = test_prompt[:max_chars]

    start_ms = time.time() * 1000
    raw = predictor._call_model(test_prompt, cfg)

    if raw.get("ok"):
        latency = raw.get("latency_ms", round(time.time() * 1000 - start_ms, 1))
        resp_body = raw.get("response_body", "")
        # 尝试解析
        sample = ""
        try:
            resp_json = json.loads(resp_body)
            content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            sample = content[:200]
        except Exception:
            sample = resp_body[:200] if resp_body else ""
        return jsonify({
            "ok": True,
            "status_code": raw.get("status_code", 200),
            "latency_ms": latency,
            "sample_response": sample,
        })
    else:
        latency = raw.get("latency_ms", round(time.time() * 1000 - start_ms, 1))
        result = {
            "ok": False,
            "error": raw.get("error", "未知错误"),
            "latency_ms": latency,
        }
        # 透传错误分类字段（error_category / hint）
        if raw.get("error_category"):
            result["error_category"] = raw["error_category"]
        if raw.get("hint"):
            result["hint"] = raw["hint"]
        return jsonify(result)


@api_bp.route("/alerts/model/status", methods=["GET"])
@login_required
def api_alert_model_status():
    """返回模型配置状态：是否配置、是否启用、厂商、端点、密钥是否已设置、上次测试结果。"""
    predictor = _predictor()
    safe_cfg = predictor.get_safe_config()
    ai_model = safe_cfg.get("ai_model", {})

    configured = bool(ai_model.get("endpoint") or ai_model.get("local_model_path"))
    # 如果 provider=local，只要有路径就算配置了
    if ai_model.get("provider") == "local":
        configured = bool(ai_model.get("local_model_path"))

    # 尝试读取上次测试结果（从 system_config）
    last_test_raw = db.get_system_config("ai_model_last_test")
    last_test = None
    if last_test_raw:
        try:
            last_test = json.loads(last_test_raw)
        except Exception:
            pass

    return jsonify({
        "configured": configured,
        "enabled": bool(ai_model.get("enabled")),
        "provider": ai_model.get("provider", "openai"),
        "endpoint": ai_model.get("endpoint", ""),
        "model_name": ai_model.get("model_name", ""),
        "api_key_set": bool(ai_model.get("api_key_set")),
        "local_model_path": ai_model.get("local_model_path", ""),
        "last_test": last_test,
        "provider_presets": PROVIDER_PRESETS,
    })
