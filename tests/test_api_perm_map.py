# -*- coding: utf-8 -*-
"""写端点权限映射回归：全量覆盖 / 精确豁免 / 角色矩阵 / 强制改密闸。"""
import re

import pytest
from flask import jsonify

from api import perm_map
from core import rbac


def _concretize(rule: str) -> str:
    """把 Flask 路由模板变成可匹配的真实路径。"""
    p = re.sub(r"<int:[^>]+>", "1", rule)
    p = re.sub(r"<path:[^>]+>", "example.test", p)
    p = re.sub(r"<[^:>]+>", "x", p)
    return p


@pytest.fixture(scope="module")
def app():
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    return a


def _write_rules(a):
    out = []
    for r in a.url_map.iter_rules():
        ms = sorted(set(r.methods or []) & set(perm_map.WRITE_METHODS))
        if ms and str(r.rule).startswith("/api"):
            out.append((str(r.rule), ms))
    return out


def test_all_write_endpoints_are_mapped(app):
    """全站写端点必须全部映射到合法权限点或精确豁免，不允许 fallback。"""
    rules = _write_rules(app)
    assert len(rules) >= 200, f"写端点数量异常: {len(rules)}"
    problems = []
    for rule, methods in rules:
        p = _concretize(rule)
        for m in methods:
            perm, reason = perm_map.resolve(p, m)
            if reason == "fallback":
                problems.append((m, p, "未映射"))
            elif perm is not None and perm not in rbac.PERM_KEYS:
                problems.append((m, p, f"非法权限点 {perm}"))
    assert not problems, f"权限映射缺口: {problems[:20]}"


@pytest.mark.parametrize("method,path,expected_perm,expected_reason", [
    ("POST", "/api/tasks/1/run", "backup.run", "action"),
    ("POST", "/api/tasks/quick-backup", "backup.run", "action"),
    ("POST", "/api/tasks/1/stop", "backup.run", "action"),
    ("POST", "/api/rbac/me/password", None, "self_service"),
    ("POST", "/api/meta-db/test", "system.settings", "force_check"),
    ("POST", "/api/datamining/export", "data_compare.run", "force_check"),
    ("DELETE", "/api/datamining/exports/1", "data_compare.run", "segment"),
    ("POST", "/api/clone/1/verify", None, "exempt"),
    ("POST", "/api/records/1/restore-precheck", None, "exempt"),
    ("POST", "/api/tasks/1/precheck", None, "exempt"),
    ("POST", "/api/backup-cleanup/preview/1", None, "exempt"),
    ("POST", "/api/restores", "restore.run", "segment"),
    ("POST", "/api/tokens", "security.manage", "segment"),
    ("PUT", "/api/system/concurrency", "system.settings", "segment"),
    ("GET", "/api/tasks", None, "read"),
    ("POST", "/api/source-guard/config", "system.settings", "segment"),
    ("POST", "/api/source-guard/sample/1", None, "exempt"),
    ("POST", "/api/source-guard/window/1/apply", "backup.task.manage", "action"),
])
def test_key_resolutions(method, path, expected_perm, expected_reason):
    perm, reason = perm_map.resolve(path, method)
    assert perm == expected_perm, (method, path, perm, reason)
    assert reason == expected_reason, (method, path, perm, reason)


def test_export_substring_not_exempt_and_v1_alias_normalized():
    # v1 别名必须与 legacy 端点得到同样的判定
    assert perm_map.resolve("/api/v1/tasks/1/run", "POST") == \
           perm_map.resolve("/api/tasks/1/run", "POST")
    # 子串白名单不得再放行 DELETE（旧 gate 的 "export" 子串误判）
    perm, reason = perm_map.resolve("/api/datamining/exports/1", "DELETE")
    assert perm == "data_compare.run" and reason == "segment"
    # 真正有写副作用的 export/scan/classify 强制要求权限
    for tail in ("export", "scan", "classify"):
        perm, reason = perm_map.resolve(f"/api/datamining/{tail}", "POST")
        assert perm == "data_compare.run" and reason == "force_check"


# --------------------------------------------------------------------------
# 运行时角色矩阵：把真实视图替换为空响应，只验证全局 gate 的判定
# --------------------------------------------------------------------------

def _stub(app, monkeypatch, paths_and_methods):
    adapter = app.url_map.bind("localhost")
    names = set()
    for p, m in paths_and_methods:
        endpoint, _ = adapter.match(p, method=m)
        names.add(endpoint)
    for name in names:
        monkeypatch.setitem(app.view_functions, name,
                            lambda *a, **k: jsonify({"ok": True}))
    return names


def _client(app, role="viewer", extra=None):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": 1, "username": "u", "display_name": "u", "role": role}
        if extra:
            s.update(extra)
    return c


def test_role_matrix(app, monkeypatch):
    _stub(app, monkeypatch, [
        ("/api/tasks", "POST"),
        ("/api/tasks/1/precheck", "POST"),
        ("/api/rbac/me/password", "POST"),
        ("/api/datamining/exports/1", "DELETE"),
        ("/api/tokens", "POST"),
        ("/api/tasks/1/run", "POST"),
        ("/api/system/concurrency", "PUT"),
        ("/api/restore-verify-policies", "POST"),
        ("/api/rbac/me", "GET"),
    ])
    viewer = _client(app, "viewer")
    assert viewer.post("/api/tasks", json={}).status_code == 403
    assert viewer.post("/api/tasks/1/precheck", json={}).status_code == 200
    assert viewer.post("/api/rbac/me/password", json={}).status_code == 200
    assert viewer.delete("/api/datamining/exports/1").status_code == 403

    operator = _client(app, "operator")
    assert operator.post("/api/tokens", json={}).status_code == 403
    assert operator.post("/api/tasks/1/run", json={}).status_code == 200
    assert operator.put("/api/system/concurrency", json={}).status_code == 403

    admin = _client(app, "admin")
    assert admin.put("/api/system/concurrency", json={}).status_code == 200

    audit = _client(app, "audit_admin")
    assert audit.post("/api/restore-verify-policies", json={}).status_code == 200


def test_legacy_string_session_no_attribute_error(app, monkeypatch):
    _stub(app, monkeypatch, [("/api/tasks", "POST")])
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = "admin"          # 旧版字符串 session
    assert c.post("/api/tasks", json={}).status_code == 200


def test_force_password_change_gate(app, monkeypatch):
    _stub(app, monkeypatch, [
        ("/api/rbac/me", "GET"),
        ("/api/rbac/me/password", "POST"),
        ("/api/tasks", "GET"),
    ])
    c = _client(app, "admin", {"pwd_change_required": 1})
    assert c.get("/api/rbac/me").status_code == 200
    assert c.post("/api/rbac/me/password", json={}).status_code == 200
    r = c.get("/api/tasks")
    assert r.status_code == 428
    assert r.get_json()["password_change_required"] is True


def test_api_token_role_can_be_tightened(app, monkeypatch):
    """外部令牌默认等价 admin，收紧为 viewer 后不得再越权写任务。"""
    import auth
    _stub(app, monkeypatch, [("/api/tasks", "POST")])
    monkeypatch.setattr(auth, "_valid_api_token", lambda t: True)
    monkeypatch.setattr(rbac, "external_auth_role", lambda: "viewer")
    c = app.test_client()
    r = c.post("/api/tasks", json={}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 403
    assert r.get_json()["required_permission"] == "backup.task.manage"
