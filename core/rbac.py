# -*- coding: utf-8 -*-
"""RBAC：用户、角色、权限点、密码哈希。

设计要点
--------
- 表 ``users``（db.py 迁移块创建）：username/password_hash/role/permissions/...
- 角色三档：admin / operator / viewer；权限点 22 个，覆盖所有一级菜单功能
- 密码哈希：PBKDF2-HMAC-SHA256（标准库 hashlib），格式 ``pbkdf2_sha256$<iter>$<saltB64>$<hashB64>``
- 兼容老部署：首次启动如 users 表为空，从 config.WEB_USERNAME/WEB_PASSWORD
  种子建一个 admin 用户（保留内置账号作为 fallback）
- 外部 API Token 仍走 api_tokens，与 RBAC 解耦
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

import config
import core.db as db


# ============== 权限点（22 个，覆盖所有一级菜单） ==============
PERMISSIONS: list[tuple[str, str, str]] = [
    # (perm_key,    显示名,                 所属菜单/分组)
    ("dashboard.view",   "查看仪表盘",        "概览"),
    ("backup.task.view", "查看备份任务",      "备份管理"),
    ("backup.task.manage", "管理备份任务",    "备份管理"),
    ("backup.run",       "执行备份",          "备份管理"),
    ("backup.policy.manage", "管理备份策略",  "备份管理"),
    ("backup.storage.manage", "管理存储",     "备份管理"),
    ("backup.plugins.manage", "管理备份插件",  "备份管理"),
    ("backup.dbtypes.manage", "管理数据库类型", "备份管理"),
    ("rt.manage",        "管理实时备份",      "备份管理"),
    ("restore.run",      "执行恢复",          "恢复"),
    ("restore.verify.manage", "管理恢复校验", "恢复"),
    ("data_compare.run", "执行数据对比",      "数据对比"),
    ("deploy.manage",    "管理数据库部署",    "部署"),
    ("dr.migration.manage", "管理数据迁移",   "灾备"),
    ("dr.sync.manage",   "管理数据同步",      "灾备"),
    ("dr.link.manage",   "管理容灾链路",      "灾备"),
    ("dr.clone.manage",  "管理克隆服务",      "灾备"),
    ("inspection.manage", "管理巡检",         "运维"),
    ("agent.use",        "使用 AI 助手",      "运维"),
    ("alert.manage",     "管理智能告警",      "运维"),
    ("logs.view",        "查看日志",          "运维"),
    ("operations.view",  "查看运维分析",      "运维"),
    ("system.settings",  "系统设置",          "系统"),
    ("users.manage",     "管理用户",          "系统"),
]

PERM_KEYS = [p[0] for p in PERMISSIONS]

# 角色 → 默认权限（admin = 全部；operator = 备份/恢复/同步/克隆/对比/部署/巡检；
# 不含 system.settings / users.manage / backup.dbtypes.manage；viewer = 只读）
_OPERATOR_KEYS = [
    "dashboard.view", "backup.task.view", "backup.task.manage", "backup.run",
    "backup.policy.manage", "backup.storage.manage", "backup.plugins.manage",
    "rt.manage",
    "restore.run", "restore.verify.manage",
    "data_compare.run", "deploy.manage",
    "dr.migration.manage", "dr.sync.manage", "dr.link.manage", "dr.clone.manage",
    "inspection.manage", "agent.use", "alert.manage",
    "logs.view", "operations.view",
]
_VIEWER_KEYS = [
    "dashboard.view", "backup.task.view",
    "logs.view", "operations.view",
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": list(PERM_KEYS),  # 全部
    "operator": _OPERATOR_KEYS,
    "viewer": _VIEWER_KEYS,
}
VALID_ROLES = list(ROLE_PERMISSIONS.keys())


# ============== 密码哈希（PBKDF2-HMAC-SHA256，标准库） ==============
_PBKDF2_ITER = 200_000
_PBKDF2_ALGO = "sha256"


def hash_password(plain: str) -> str:
    """返回 'pbkdf2_sha256$<iter>$<saltB64>$<hashB64>'。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, plain.encode("utf-8"), salt,
                             _PBKDF2_ITER)
    return ("pbkdf2_sha256$%d$%s$%s"
            % (_PBKDF2_ITER,
               base64.urlsafe_b64encode(salt).rstrip(b"=").decode(),
               base64.urlsafe_b64encode(dk).rstrip(b"=").decode()))


def verify_password(plain: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iters_i = int(iters)
        salt = base64.urlsafe_b64decode(salt_b64 + "==")
        expect = base64.urlsafe_b64decode(hash_b64 + "==")
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, plain.encode("utf-8"), salt, iters_i)
    return hmac.compare_digest(dk, expect)


# ============== 用户 CRUD ==============
_FIELDS = (
    "username", "display_name", "email", "password_hash", "password_algo",
    "role", "permissions", "enabled", "must_change_password",
    "last_login_at", "last_login_ip",
    "created_at", "updated_at", "created_by",
)
_BOOL = ("enabled", "must_change_password")


def _row(r: Optional[dict]) -> Optional[dict]:
    if not r:
        return None
    out = dict(r)
    for k in _BOOL:
        out[k] = 1 if out.get(k) else 0
    return out


def list_users(include_disabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM users"
    if not include_disabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id ASC"
    rows = db.query(sql)
    return [_row(r) for r in rows]


def get_by_username(username: str, include_disabled: bool = True) -> Optional[dict]:
    if not username:
        return None
    sql = "SELECT * FROM users WHERE username=?"
    if not include_disabled:
        sql += " AND enabled=1"
    return _row(db.query_one(sql, (username,)))


def get_by_id(uid: int) -> Optional[dict]:
    return _row(db.query_one("SELECT * FROM users WHERE id=?", (uid,)))


def create(data: dict, password: str, created_by: str = "") -> int:
    """创建用户。password 必须明文；此处哈希后入库。"""
    username = (data.get("username") or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if not password or len(password) < 6:
        raise ValueError("密码至少 6 位")
    if get_by_username(username):
        raise ValueError(f"用户 {username!r} 已存在")
    role = data.get("role") or "viewer"
    if role not in VALID_ROLES:
        raise ValueError(f"非法角色 {role!r}")
    extra = (data.get("permissions") or "").strip()
    now = db.now_iso()
    sql = ("INSERT INTO users (username, display_name, email, password_hash, "
           "password_algo, role, permissions, enabled, must_change_password, "
           "created_at, updated_at, created_by) "
           "VALUES (?, ?, ?, ?, 'pbkdf2_sha256', ?, ?, ?, ?, ?, ?, ?)")
    return db.execute(sql, (
        username,
        (data.get("display_name") or username).strip(),
        (data.get("email") or "").strip(),
        hash_password(password),
        role,
        extra,
        1 if data.get("enabled", True) else 0,
        1 if data.get("must_change_password") else 0,
        now, now, created_by or "",
    ))


def update(uid: int, data: dict) -> bool:
    u = get_by_id(uid)
    if not u:
        return False
    if "role" in data and data["role"] not in VALID_ROLES:
        raise ValueError(f"非法角色 {data['role']!r}")
    sets, vals = [], []
    for k in ("display_name", "email", "role", "permissions"):
        if k in data:
            sets.append(k + "=?")
            vals.append(data[k] or "")
    for k in ("enabled", "must_change_password"):
        if k in data:
            sets.append(k + "=?")
            vals.append(1 if data[k] else 0)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(db.now_iso())
    vals.append(uid)
    db.execute("UPDATE users SET " + ",".join(sets) + " WHERE id=?",
               tuple(vals))
    return True


def set_password(uid: int, new_password: str) -> bool:
    if not new_password or len(new_password) < 6:
        raise ValueError("密码至少 6 位")
    db.execute(
        "UPDATE users SET password_hash=?, password_algo='pbkdf2_sha256', "
        "must_change_password=0, updated_at=? WHERE id=?",
        (hash_password(new_password), db.now_iso(), uid))
    return True


def delete(uid: int) -> tuple[bool, str]:
    u = get_by_id(uid)
    if not u:
        return False, "用户不存在"
    if u.get("username") == (config.WEB_USERNAME or "admin"):
        return False, "不能删除内置管理员账号"
    # 防最后一超管被删
    admins = db.query_one("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND enabled=1")
    n = admins.get("c", 0) if admins else 0
    if u.get("role") == "admin" and n <= 1:
        return False, "不能删除最后一个启用的 admin 用户"
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    return True, ""


def record_login(uid: int, ip: str = "") -> None:
    db.execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE id=?",
               (db.now_iso(), (ip or "")[:64], uid))


# ============== 权限解析 ==============
def effective_permissions(user: dict) -> set:
    """返回该用户实际拥有的权限集合：角色权限 + users.permissions 附加。"""
    if not user:
        return set()
    role = user.get("role") or ""
    base = set(ROLE_PERMISSIONS.get(role, []))
    extra = (user.get("permissions") or "").strip()
    if extra:
        for k in extra.split(","):
            k = k.strip()
            if k and (k in PERM_KEYS or k in ROLE_PERMISSIONS):
                base.add(k)
    return base


def has_permission(user: Optional[dict], perm: str) -> bool:
    """是否拥有某个权限：admin 直接放行。"""
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return perm in effective_permissions(user)


# ============== 启动种子（兼容老部署） ==============
def seed_admin_if_empty() -> Optional[dict]:
    """users 表为空时，用 config.WEB_USERNAME/WEB_PASSWORD 建首个 admin。"""
    n = db.query_one("SELECT COUNT(*) AS c FROM users")
    cnt = (n or {}).get("c", 0) if isinstance(n, dict) else (n[0] if n else 0)
    if cnt and cnt > 0:
        return get_by_username(config.WEB_USERNAME or "admin")
    username = (config.WEB_USERNAME or "admin").strip()
    password = config.WEB_PASSWORD or "admin123"
    try:
        create({
            "username": username,
            "display_name": "内置管理员",
            "role": "admin",
            "enabled": True,
            "must_change_password": 1,
        }, password, created_by="system")
        return get_by_username(username)
    except Exception as e:
        # 已有同名记录（极端竞态）—— 返回它
        return get_by_username(username)


# ============== 内置账号兼容（config 旧账号回退登录） ==============
def verify_builtin(username: str, password: str) -> bool:
    """config.WEB_USERNAME/WEB_PASSWORD 直接比对（仅当 users 表无同名用户时生效）。"""
    if get_by_username(username):
        return False
    cu = (config.WEB_USERNAME or "").strip()
    cp = config.WEB_PASSWORD or ""
    if not cu or not cp:
        return False
    return hmac.compare_digest(username.strip(), cu) and hmac.compare_digest(password, cp)