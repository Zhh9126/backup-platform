# -*- coding: utf-8 -*-
"""功能项按需启用 / 权限分离回归。

覆盖：默认零破坏、平台停用、角色分配、用户覆盖、受保护项、
管理接口豁免、损坏配置容错、API 与设置页契约。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from core import features  # noqa: E402


@pytest.fixture
def mem(monkeypatch):
    """把 system_config 读写换成内存字典，不碰真实元数据库。"""
    store = {}
    monkeypatch.setattr(features.db, "get_system_config",
                        lambda k, default=None: store.get(k, default))
    monkeypatch.setattr(features.db, "set_system_config",
                        lambda k, v: store.__setitem__(k, v))
    return store


def _admin():
    return {"role": "admin", "username": "admin"}


def _viewer(name="v"):
    return {"role": "viewer", "username": name}


def test_default_is_zero_breakage(mem):
    assert len(features.enabled_features()) == len(features.catalog())
    u = _viewer()
    for f in features.catalog():
        assert features.can_access(u, f["paths"][0])[0], f["id"]
    assert features.perm_allowed(u, "restore.run") is True
    assert features.can_access(u, "/some/unmanaged/page")[0] is True


def test_disabling_feature_blocks_page_and_api(mem):
    features.save_config({"enabled": {"clone": False}})
    u = _admin()
    assert features.can_access(u, "/clone")[0] is False
    assert features.can_access(u, "/api/clone")[0] is False
    assert features.can_access(u, "/api/v1/clone/1/snapshots")[0] is False
    assert features.can_access(u, "/tasks")[0] is True
    assert features.perm_allowed(u, "dr.clone.manage") is False
    assert features.perm_allowed(u, "backup.run") is True


def test_role_assignment_is_enforced(mem):
    features.save_config({"roles": {"viewer": ["dashboard", "backup_task"]}})
    v = _viewer()
    assert features.can_access(v, "/tasks")[0] is True
    assert features.can_access(v, "/clone")[0] is False
    assert features.can_access(v, "/settings")[0] is True   # 受保护
    # 未配置角色不额外限制
    assert features.can_access(_admin(), "/clone")[0] is True


def test_user_override_beats_role(mem):
    features.save_config({
        "roles": {"viewer": ["dashboard"]},
        "users": {"v": ["dashboard", "clone"]},
    })
    assert features.can_access(_viewer(), "/clone")[0] is True
    assert features.can_access(_viewer("other"), "/clone")[0] is False


def test_protected_features_cannot_be_disabled(mem):
    cfg = features.save_config({"enabled": {"system": False, "dashboard": False}})
    assert cfg["enabled"]["system"] is True
    assert cfg["enabled"]["dashboard"] is True
    assert features.can_access(_viewer(), "/settings")[0] is True


def test_management_paths_are_exempt(mem):
    features.save_config({"enabled": {f["id"]: False for f in features.catalog()}})
    u = _viewer()
    assert features.can_access(u, "/api/rbac/me")[0] is True
    assert features.can_access(u, "/api/rbac/features")[0] is True
    assert features.can_access(u, "/api/health")[0] is True
    assert features.can_access(u, "/settings")[0] is True


def test_bad_or_unknown_config_is_ignored(mem):
    mem[features.CONFIG_KEY] = "{not json"
    assert len(features.enabled_features()) == len(features.catalog())
    features.save_config({"enabled": {"not_exist": True},
                          "roles": {"viewer": ["not_exist"]}})
    assert features.get_config()["roles"]["viewer"] == []
    assert "not_exist" not in features.get_config()["enabled"]


def test_permission_filter_keeps_unregistered_perms(mem):
    features.save_config({"enabled": {"clone": False}})
    out = features.filter_permissions(
        _admin(), ["dr.clone.manage", "backup.run", "custom.perm"])
    assert out == ["backup.run", "custom.perm"]


def test_api_and_ui_contract():
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()}
    assert "/api/rbac/features" in rules
    tpl = open(os.path.join(ROOT, "templates/settings.html"),
               encoding="utf-8").read()
    assert 'id="faRoot"' in tpl
    m = re.search(r"feature_access\.js\?v=(\d{8,10})", tpl)
    assert m and int(m.group(1)) >= 2026111109
    base = open(os.path.join(ROOT, "templates/base.html"),
                encoding="utf-8").read()
    m2 = re.search(r"bkp-core\.js\?v=(\d{8,10})", base)
    assert m2 and int(m2.group(1)) >= 2026111109
    core_js = open(os.path.join(ROOT, "static/js/bkp-core.js"),
                   encoding="utf-8").read()
    assert "feature_catalog" in core_js and "_featureByPath" in core_js
