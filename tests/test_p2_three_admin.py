# -*- coding: utf-8 -*-
"""三员管理 + 登录强化自测（P2，2026-11）。

覆盖：
- 三员角色权限矩阵（sys_admin/sec_admin/audit_admin 三权分立边界）
- 账号级登录锁定：连续 5 次失败锁 15 分钟、正确密码在锁定期内仍拒绝、
  过期自动解锁、成功登录清零
- 强口令策略：开关关闭只要求非空；开启后 8 位+大小写数字校验
- HTTP 全流程：锁定期内登录 429、创建用户弱口令 400
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="three_admin_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["LOGIN_LOCK_ENABLED"] = "0"   # 关闭 IP 级锁定（避免干扰账号级测试）

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()                                # noqa: E402

import core.rbac as rbac                        # noqa: E402
from flask import Flask                         # noqa: E402


# ---------------------------------------------------------------------------
# 1. 三员权限矩阵
# ---------------------------------------------------------------------------
def test_three_admin_matrix():
    r = rbac.ROLE_PERMISSIONS
    # 系统管理员：业务全权（含库型管理），不能管用户与安全
    assert "backup.dbtypes.manage" in r["sys_admin"]
    assert "users.manage" not in r["sys_admin"]
    assert "security.manage" not in r["sys_admin"]
    # 安全保密管理员：管用户/系统设置/安全，不碰备份执行细节外全站查看
    assert "users.manage" in r["sec_admin"]
    assert "system.settings" in r["sec_admin"]
    assert "security.manage" in r["sec_admin"]
    assert "backup.run" not in r["sec_admin"]
    # 审计管理员：全站只读 + 审计查看，不能执行任何管理动作
    assert "logs.view" in r["audit_admin"]
    assert "backup.run" not in r["audit_admin"]
    assert "users.manage" not in r["audit_admin"]
    assert "system.settings" not in r["audit_admin"]
    # 三员在合法角色清单里
    for role in ("sys_admin", "sec_admin", "audit_admin"):
        assert role in rbac.VALID_ROLES


def test_three_admin_effective_permissions():
    for role, perm in (("sys_admin", "backup.run"),
                       ("sec_admin", "users.manage"),
                       ("audit_admin", "logs.view")):
        u = {"id": 1, "username": "t", "role": role, "permissions": ""}
        assert rbac.has_permission(u, perm)
    # 越权负面
    u = {"id": 1, "username": "t", "role": "sys_admin", "permissions": ""}
    assert not rbac.has_permission(u, "users.manage")
    u = {"id": 1, "username": "t", "role": "audit_admin", "permissions": ""}
    assert not rbac.has_permission(u, "restore.run")


# ---------------------------------------------------------------------------
# 2. 账号级登录锁定
# ---------------------------------------------------------------------------
def _mkuser(name, pw="Init@12345", role="operator"):
    return rbac.create({"username": name, "display_name": name, "role": role,
                        "enabled": True}, pw)


def test_account_lock_flow():
    name = "lockee1"
    _mkuser(name)
    for i in range(4):
        r = rbac.record_login_failure(name)
        assert r["locked"] is False, r
    r = rbac.record_login_failure(name)          # 第 5 次 → 锁定
    assert r["locked"] is True and r.get("locked_until")
    locked, remain = rbac.account_locked(name)
    assert locked is True and 0 < remain <= 15 * 60
    # 锁定过期（把 until 拨回过去）→ 自动解锁
    db.execute("UPDATE users SET locked_until=? WHERE username=?",
               ("2000-01-01 00:00:00", name))
    locked, remain = rbac.account_locked(name)
    assert locked is False and remain == 0
    # 成功登录清零
    for _ in range(3):
        rbac.record_login_failure(name)
    rbac.reset_login_failures(name)
    row = db.query_one("SELECT failed_count, locked_until FROM users WHERE username=?", (name,))
    assert int(row["failed_count"] or 0) == 0 and not row["locked_until"]


def test_password_policy():
    # 开关关闭：只要求非空
    assert rbac.validate_password("123")[0] is True
    assert rbac.validate_password("")[0] is False
    rbac.set_password_policy(True)
    assert rbac.validate_password("short1A")[0] is False          # <8 位
    assert rbac.validate_password("alllowercase1")[0] is False    # 无大写
    assert rbac.validate_password("ALLUPPERCASE1")[0] is False    # 无小写
    assert rbac.validate_password("NoDigitsHere")[0] is False     # 无数字
    assert rbac.validate_password("Str0ngPass")[0] is True
    rbac.set_password_policy(False)
    assert rbac.validate_password("123")[0] is True               # 关闭恢复


# ---------------------------------------------------------------------------
# 3. HTTP 全流程
# ---------------------------------------------------------------------------
def _client():
    from app import create_app
    app = create_app()
    return app.test_client()


def test_http_lock_and_policy():
    rbac.set_password_policy(True)
    c = _client()
    # 登录超管
    r = c.post("/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    # 弱口令创建用户 → 400
    r = c.post("/api/rbac/users", json={
        "username": "weakpw", "display_name": "w", "role": "viewer",
        "password": "123"})
    assert r.status_code == 400 and "强口令" in r.get_json().get("error", "")
    # 合规口令创建 → ok
    r = c.post("/api/rbac/users", json={
        "username": "sec1", "display_name": "安全员", "role": "sec_admin",
        "password": "Sec@Pass2026"})
    assert r.status_code == 200, r.get_json()
    # 重置密码弱口令 → 400
    uid = db.query_one("SELECT id FROM users WHERE username='sec1'")["id"]
    r = c.post(f"/api/rbac/users/{uid}/password", json={"password": "abc"})
    assert r.status_code == 400
    # 账号锁定 HTTP：错误密码 5 次 → 429（且此时正确密码也 429）
    c2 = _client()
    for _ in range(5):
        c2.post("/login", json={"username": "sec1", "password": "wrong"})
    r = c2.post("/login", json={"username": "sec1", "password": "Sec@Pass2026"})
    assert r.status_code == 429, (r.status_code, r.get_data(as_text=True)[:150])
