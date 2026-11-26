# -*- coding: utf-8 -*-
"""Agent 统一发布物与能力矩阵（2026-11-26）。

用户口径：「**只安装一次，所有功能都支持，不区分类型**」。本文件守住三条：

1. **统一发布物**：默认源码包（一个包通吃所有架构与系统），ELF 仅在目标机
   没有 python3 时兜底；
2. **能力矩阵**：平台维护期望清单，安装后逐项核对——缺哪项、为什么缺，
   落库并如实展示，不虚报「装完就什么都会」；
3. **自更新闭环**：平台提供清单与下载端点（令牌鉴权），Agent 装一次即可
   长期跟随平台版本。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="agent_caps_test_")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as _db  # noqa: E402
_db.init_schema()

from core import agent_capabilities as CAP  # noqa: E402
from core import agent_hosts  # noqa: E402


FULL_CAPS = {
    "version": "1.4.0",
    "distribution": "source",
    "os": "Linux",
    "arch": "x86_64",
    "enc": True,
    "features": list(CAP.EXPECTED_FEATURES),
    "plugins": [
        {"name": "block-cdp", "version": "1.2.0", "supported": True, "reason": ""},
        {"name": "bmr", "version": "1.3.0", "supported": True, "reason": ""},
        {"name": "app-dr", "version": "1.2.0", "supported": True, "reason": ""},
        {"name": "block-carve", "version": "1.0.0", "supported": True, "reason": ""},
    ],
}


# --------------------------------------------------------------------------- #
# 能力矩阵
# --------------------------------------------------------------------------- #
def test_full_capability_matrix():
    matrix = CAP.evaluate(FULL_CAPS)
    assert matrix["total"] == len(CAP.EXPECTED_FEATURES) + len(CAP.EXPECTED_PLUGINS)
    assert matrix["ok"] == matrix["total"]
    assert matrix["missing"] == []
    assert "能力齐全" in matrix["summary"]


def test_missing_plugin_reports_reason():
    caps = json.loads(json.dumps(FULL_CAPS))
    caps["plugins"][0]["supported"] = False
    caps["plugins"][0]["reason"] = "目标机未开启 CBT"
    matrix = CAP.evaluate(caps)
    assert matrix["ok"] == matrix["total"] - 1
    miss = [m for m in matrix["missing"] if m["item"] == "block-cdp"]
    assert miss and "CBT" in miss[0]["reason"]
    assert miss[0]["label"] == CAP.PLUGIN_LABELS["block-cdp"]


def test_old_agent_without_plugins_is_reported_honestly():
    """旧 ELF 包（能力全量之前的快照）：如实报「未内置该插件」。"""
    caps = {"version": "1.0.0", "features": ["exec", "exec-stream"], "plugins": []}
    matrix = CAP.evaluate(caps)
    assert matrix["ok"] < matrix["total"]
    plugins_missing = [m for m in matrix["missing"] if m["kind"] == "plugin"]
    assert len(plugins_missing) == len(CAP.EXPECTED_PLUGINS)
    assert all("未内置" in m["reason"] for m in plugins_missing)
    feats_missing = [m for m in matrix["missing"] if m["kind"] == "feature"]
    assert feats_missing and all(m["label"] for m in feats_missing)


def test_evaluate_handles_empty_caps():
    matrix = CAP.evaluate({})
    assert matrix["ok"] == 0
    assert matrix["total"] > 0
    assert matrix["missing"]


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def test_capabilities_persisted_and_exposed():
    aid = agent_hosts.create_host({"hostname": "10.0.0.9", "port": 8466,
                                   "transport": "auto", "enabled": 1})
    matrix = agent_hosts.save_capabilities(aid, FULL_CAPS)
    assert matrix["ok"] == matrix["total"]
    rec = agent_hosts.get_by_id(aid)
    assert rec.get("capabilities")
    assert rec.get("capabilities_at")
    listed = [r for r in agent_hosts.list_hosts() if r["id"] == aid][0]
    assert listed["cap_matrix"]["ok"] == listed["cap_matrix"]["total"]
    # 列表里不得暴露明文令牌
    assert "token" not in listed and listed.get("token_masked")


# --------------------------------------------------------------------------- #
# API 契约
# --------------------------------------------------------------------------- #
def _client():
    import app as app_module
    client = app_module.create_app().test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"id": 1, "username": "admin", "role": "admin"}
        sess["username"] = "admin"
    return client


def test_packages_api_declares_unified_distribution():
    client = _client()
    r = client.get("/api/v1/agents/packages")
    assert r.status_code == 200
    data = r.get_json()
    assert data["unified"] is True
    assert data["default_mode"] == "python"
    unified = [p for p in data["packages"] if p["mode"] == "python"][0]
    assert unified["arch"] == "any"
    assert "统一发布物" in unified["note"]


def test_manifest_and_download_require_agent_token():
    """Agent 端无会话：端点自身必须校验注册令牌（放行会话闸不等于放行令牌闸）。"""
    import app as app_module
    anon = app_module.create_app().test_client()
    assert anon.get("/api/v1/agents/manifest").status_code == 401
    assert anon.get("/api/v1/agents/download").status_code == 401
    assert anon.get("/api/v1/agents/manifest?token=bad-token").status_code == 401
    assert anon.get("/api/v1/agents/download?token=bad-token").status_code == 401


def test_manifest_and_download_work_with_valid_token():
    aid = agent_hosts.create_host({"hostname": "10.0.0.10", "port": 8466,
                                   "transport": "auto", "enabled": 1})
    token = agent_hosts.get_token(agent_hosts.get_by_id(aid))
    client = _client()
    r = client.get("/api/v1/agents/manifest?token=%s" % token)
    assert r.status_code == 200
    data = r.get_json()
    assert data["sha256"] and data["version"]
    assert data["agent_id"] == aid
    r = client.get("/api/v1/agents/download?token=%s" % token)
    assert r.status_code == 200
    assert r.headers.get("X-Artifact-SHA256") == data["sha256"]


def test_install_cmd_defaults_to_unified_source_package():
    aid = agent_hosts.create_host({"hostname": "10.0.0.11", "port": 8466,
                                   "transport": "auto", "enabled": 1})
    client = _client()
    r = client.get("/api/v1/agents/%d/install-cmd" % aid)
    assert r.status_code == 200
    data = r.get_json()
    assert data["mode"] == "python"
    assert "aidbm-agent-src.tar.gz" in data["script"]
    assert "backup_agent.py" in data["script"]
    assert "AIDBM_PLATFORM_URL" in data["script"]
    assert data["package_url"].endswith("arch=source")


# --------------------------------------------------------------------------- #
# 回连平台地址（自更新前提）
# --------------------------------------------------------------------------- #
def test_platform_url_prefers_explicit_override():
    from api import agents as agents_api
    client = _client()
    with client.session_transaction() as sess:
        sess["user"] = {"id": 1, "username": "admin", "role": "admin"}
    from flask import request
    with client.application.test_request_context("/api/v1/agents/remote-install",
                                                 base_url="http://127.0.0.1:8080"):
        url = agents_api._platform_url_for_agent(request, "http://10.1.2.3:8080/")
        assert url == "http://10.1.2.3:8080"


def test_platform_url_falls_back_to_detected_ip_when_host_is_loopback():
    """浏览器在平台机上打开时不能把 127.0.0.1 写给目标机。"""
    from api import agents as agents_api
    from flask import request
    client = _client()
    with client.application.test_request_context("/api/v1/agents/remote-install",
                                                 base_url="http://127.0.0.1:8080"):
        url = agents_api._platform_url_for_agent(request, "")
        assert "127.0.0.1" not in url
        assert url.startswith("http://")


def test_platform_url_config_can_be_set_and_cleared():
    from core import db as _db
    client = _client()
    r = client.put("/api/v1/agents/platform-url", json={"platform_url": "http://10.9.9.9:8080"})
    assert r.status_code == 200
    assert _db.get_system_config("agent_platform_url") == "http://10.9.9.9:8080"
    r = client.get("/api/v1/agents/platform-url")
    assert r.get_json()["platform_url"] == "http://10.9.9.9:8080"
    r = client.put("/api/v1/agents/platform-url", json={"platform_url": "10.9.9.9"})
    assert r.status_code == 400
    client.put("/api/v1/agents/platform-url", json={"platform_url": ""})
    assert not _db.get_system_config("agent_platform_url")
