# -*- coding: utf-8 -*-
"""首次登录口令治理回归：零破坏、自助端点、告警、随机口令、防 500 回归。"""
import pytest

import config
import core.rbac as rbac


@pytest.fixture()
def client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": 1, "username": "admin",
                     "display_name": "admin", "role": "admin"}
    return c


def test_me_endpoint_contract_no_self_reference(client):
    """/api/rbac/me 曾在字典字面量里引用自身 payload → 500，这里锁死回归。"""
    r = client.get("/api/rbac/me")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    for key in ("username", "role", "permissions", "must_change_password",
                "password_change_required", "force_password_change_enabled",
                "default_password_in_use"):
        assert key in body, key
    assert isinstance(body["permissions"], list)


def test_default_login_still_works(monkeypatch):
    """默认关闭强制改密：admin/admin123 登录行为零破坏（E2E/离线包依赖）。"""
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    monkeypatch.setattr(config, "FORCE_PASSWORD_CHANGE", False)
    monkeypatch.setattr(rbac, "force_password_change_enabled", lambda: False)
    monkeypatch.setattr(rbac, "account_locked", lambda u: (False, 0))
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code in (200, 302), r.get_data(as_text=True)[:200]
    with c.session_transaction() as s:
        assert not s.get("pwd_change_required")


def test_force_policy_switch_sources(monkeypatch):
    monkeypatch.setattr(config, "FORCE_PASSWORD_CHANGE", False)
    monkeypatch.setattr(rbac.db, "get_system_config",
                        lambda k, d=None: "1" if k == "force_password_change" else d)
    assert rbac.force_password_change_enabled() is True
    monkeypatch.setattr(config, "FORCE_PASSWORD_CHANGE", True)
    monkeypatch.setattr(rbac.db, "get_system_config", lambda k, d=None: "0")
    assert rbac.force_password_change_enabled() is True   # env 优先


def test_default_password_detection_and_cache(monkeypatch):
    monkeypatch.setattr(config, "WEB_PASSWORD", "admin123")
    rbac._DEFAULT_PWD_CACHE.update({"ts": 0.0, "value": None})
    calls = []
    monkeypatch.setattr(rbac, "get_by_username", lambda name: {
        "username": name, "password_hash": "fake"})
    monkeypatch.setattr(rbac, "verify_password",
                        lambda plain, stored: calls.append(plain) or True)
    assert rbac.default_admin_password_in_use() is True
    assert rbac.default_admin_password_in_use() is True
    assert len(calls) == 1, "同一缓存窗口内不应重复计算 PBKDF2"
    rbac._DEFAULT_PWD_CACHE["value"] = None
    monkeypatch.setattr(config, "WEB_PASSWORD", "strong-password")
    assert rbac.default_admin_password_in_use() is False


def test_random_admin_password_written_with_0600(monkeypatch, tmp_path):
    """显式开启随机口令时，首次部署不再用 admin123，且明文文件是 0600。"""
    import os
    import stat
    captured = {}

    def fake_create(data, password, created_by=""):
        captured["password"] = password
        return 1

    monkeypatch.setattr(config, "RANDOM_ADMIN_PASSWORD", True)
    monkeypatch.setattr(config, "WEB_PASSWORD", "admin123")
    monkeypatch.setattr(config, "INSTANCE_DIR", tmp_path)
    monkeypatch.setattr(rbac.db, "query_one", lambda sql: {"c": 0})
    monkeypatch.setattr(rbac, "create", fake_create)
    monkeypatch.setattr(rbac, "get_by_username", lambda name: {"username": name})
    rbac.seed_admin_if_empty()
    assert captured.get("password") not in (None, "admin123")
    f = tmp_path / "initial_admin_password.txt"
    assert f.exists()
    assert stat.S_IMODE(os.stat(str(f)).st_mode) == 0o600
    assert captured["password"] in f.read_text(encoding="utf-8")


def test_password_change_policy_api(client, monkeypatch):
    writes = []
    monkeypatch.setattr(rbac, "set_force_password_change",
                        lambda on: writes.append(on))
    monkeypatch.setattr(rbac, "force_password_change_enabled", lambda: True)
    r = client.post("/api/rbac/password-change-policy", json={"enabled": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["enabled"] is True
    assert writes == [True]
    assert client.get("/api/rbac/password-change-policy").status_code == 200
