# -*- coding: utf-8 -*-
"""功能项注册表：按需启用 + 权限分离（按角色 / 按用户分配）。

设计约束：
- 零破坏：system_config 里没有配置时，所有功能项启用、角色不额外限制，
  现有 RBAC 行为完全不变；只有管理员显式保存配置后才生效。
- 强制生效：不只隐藏菜单，页面路由与 API（含 GET）都在服务端拦截。
- 防自锁：仪表盘 / 系统设置与用户管理是受保护项，恒启用且不可停用；
  /api/rbac/* 等管理接口豁免，保证管理员随时能改回来。

配置存于 system_config.feature_access（JSON）：
{
  "enabled": {"<feature_id>": true/false, ...},
  "roles":   {"<role>": ["<feature_id>", ...], ...},
  "users":   {"<username>": ["<feature_id>", ...], ...}
}
未出现在 roles/users 里的角色/用户 = 跟随平台开关，不额外限制。
"""
import json
from typing import Dict, List, Optional, Tuple

import core.db as db

CONFIG_KEY = "feature_access"

# (id, 名称, 分组, 页面路径, 权限点, API 一级段, 是否受保护)
CATALOG: List[dict] = [
    dict(id="dashboard", name="仪表盘", group="概览",
         paths=["/"], perms=["dashboard.view"], api=[], protected=True),
    dict(id="backup_task", name="备份任务与记录", group="备份管理",
         paths=["/tasks", "/file_backup", "/records", "/scan-fs",
                "/netdev", "/synology", "/ai_backup"],
         perms=["backup.task.view", "backup.task.manage", "backup.run"],
         api=["tasks", "records", "scan-fs", "netdev", "synology",
              "file-sync", "hosts", "backup-cleanup", "win-protect"]),
    dict(id="storage", name="存储与介质", group="备份管理",
         paths=["/storage", "/tape", "/object_storage"],
         perms=["backup.storage.manage"],
         api=["storage", "tape", "pool", "remote-pull", "object-storage",
              "lifecycle", "dedup", "synthesize"]),
    dict(id="policy", name="备份策略", group="备份管理",
         paths=["/protection"], perms=["backup.policy.manage"],
         api=["policy"]),
    dict(id="plugins", name="备份插件与库型", group="备份管理",
         paths=["/plugins", "/db-adapters"],
         perms=["backup.plugins.manage", "backup.dbtypes.manage"],
         api=["plugins", "jdbc", "db-adapters"]),
    dict(id="realtime", name="实时备份", group="备份管理",
         paths=["/realtime"], perms=["rt.manage"], api=["rt", "cdc"]),
    dict(id="restore", name="数据恢复", group="恢复",
         paths=["/restore", "/restore_records"], perms=["restore.run"],
         api=["restores", "oracle-txn"]),
    dict(id="restore_verify", name="恢复校验", group="恢复",
         paths=["/restore-verify"], perms=["restore.verify.manage"],
         api=["restore-test-reports", "restore-verify-policies"]),
    dict(id="data_compare", name="数据对比与价值挖掘", group="数据对比",
         paths=["/data-compare", "/datamining"], perms=["data_compare.run"],
         api=["data-compare-tasks", "data-compare-reports", "datamining"]),
    dict(id="deploy", name="数据库部署", group="部署",
         paths=["/deploy"], perms=["deploy.manage"], api=["deploy"]),
    dict(id="migration", name="数据迁移", group="灾备",
         paths=["/migration"], perms=["dr.migration.manage"],
         api=["migration", "migration-plans", "db-migrate",
              "migration-protection-plans"]),
    dict(id="sync", name="数据同步", group="灾备",
         paths=["/sync"], perms=["dr.sync.manage"],
         api=["sync", "sync-tasks"]),
    dict(id="dr_link", name="容灾链路", group="灾备",
         paths=["/dr-link"], perms=["dr.link.manage"],
         api=["disaster-links", "dr-dns"]),
    dict(id="clone", name="克隆服务", group="灾备",
         paths=["/clone", "/vdb"], perms=["dr.clone.manage"],
         api=["clone", "vdb"]),
    dict(id="inspection", name="巡检 / 监控 / 报表", group="运维",
         paths=["/inspection", "/monitor", "/reports"],
         perms=["inspection.manage", "operations.view"],
         api=["inspection", "drills", "job-monitor", "report-center"]),
    dict(id="alert", name="智能告警", group="运维",
         paths=["/alert"], perms=["alert.manage"],
         api=["alerts", "notify-config"]),
    dict(id="logs", name="日志与运维分析", group="运维",
         paths=["/logs", "/operations"], perms=["logs.view"],
         api=["logs", "operations"]),
    dict(id="agent", name="Agent 管理", group="运维",
         paths=["/agentless"], perms=["agent.use"],
         api=["agents", "agentless"]),
    dict(id="ai_assistant", name="AI 智能助手", group="运维",
         paths=["/agent"], perms=["agent.use"], api=["agent"]),
    dict(id="vm", name="虚拟机备份", group="备份管理",
         paths=["/vm"], perms=["backup.task.view"], api=["vm"]),
    dict(id="system", name="系统设置与用户管理", group="系统",
         paths=["/settings", "/users"],
         perms=["system.settings", "users.manage", "security.manage",
                "dashboard.view"],
         api=["system", "settings", "rbac", "op-guard", "tokens",
              "webhooks", "access-keys", "snmp", "pool-crypto",
              "meta-db", "scheduler", "itsm", "source-guard",
              "ransomware", "license"],
         protected=True),
]

_BY_ID: Dict[str, dict] = {f["id"]: f for f in CATALOG}
_PROTECTED = {f["id"] for f in CATALOG if f.get("protected")}
_PATH_MAP: Dict[str, str] = {}
_API_MAP: Dict[str, str] = {}
_PERM_OWNERS: Dict[str, List[str]] = {}
for _f in CATALOG:
    for _p in _f["paths"]:
        _PATH_MAP[_p] = _f["id"]
    for _a in _f["api"]:
        _API_MAP[_a] = _f["id"]
    for _p in _f["perms"]:
        _PERM_OWNERS.setdefault(_p, []).append(_f["id"])

# 管理接口与公开路径豁免：禁用功能后仍要能登录、改配置、查看健康状态
EXEMPT_API_PREFIXES = (
    "/api/rbac", "/api/health", "/api/meta", "/api/system/license",
    "/api/alerts/config",  # 设置页 AI 告警配置，归入系统设置
)
EXEMPT_PAGE_PATHS = frozenset({
    "/", "/login", "/logout", "/health", "/settings", "/users", "/favicon.ico",
})


def catalog() -> List[dict]:
    """返回功能项清单（供管理界面与前端菜单使用）。"""
    return [dict(f) for f in CATALOG]


def _default() -> dict:
    return {"enabled": {f["id"]: True for f in CATALOG},
            "roles": {}, "users": {}}


def get_config() -> dict:
    """读取配置；无配置 / 配置损坏时返回全启用（零破坏）。"""
    raw = db.get_system_config(CONFIG_KEY)
    if not raw:
        return _default()
    try:
        cfg = json.loads(raw)
        if not isinstance(cfg, dict):
            return _default()
    except (ValueError, TypeError):
        return _default()
    base = _default()
    enabled = cfg.get("enabled") or {}
    if isinstance(enabled, dict):
        for f in CATALOG:
            fid = f["id"]
            if fid in enabled and not f.get("protected"):
                base["enabled"][fid] = bool(enabled[fid])
    for key in ("roles", "users"):
        val = cfg.get(key) or {}
        if isinstance(val, dict):
            base[key] = {
                str(k): [str(x) for x in v if str(x) in _BY_ID]
                for k, v in val.items() if isinstance(v, list)
            }
    return base


def save_config(data: dict) -> dict:
    """校验并落库；受保护项强制启用，未知功能项直接丢弃。"""
    cur = get_config()
    incoming = data if isinstance(data, dict) else {}
    enabled = incoming.get("enabled")
    if isinstance(enabled, dict):
        for f in CATALOG:
            fid = f["id"]
            if fid in enabled and not f.get("protected"):
                cur["enabled"][fid] = bool(enabled[fid])
    for key in ("roles", "users"):
        val = incoming.get(key)
        if isinstance(val, dict):
            cur[key] = {
                str(k): [str(x) for x in v if str(x) in _BY_ID]
                for k, v in val.items() if isinstance(v, list)
            }
    # 受保护项始终启用
    for fid in _PROTECTED:
        cur["enabled"][fid] = True
    db.set_system_config(CONFIG_KEY, json.dumps(cur, ensure_ascii=False))
    return cur


def enabled_features() -> set:
    cfg = get_config()["enabled"]
    return {fid for fid, on in cfg.items() if on} | _PROTECTED


def feature_for_path(path: str) -> Optional[dict]:
    """页面路径或 API 一级段 → 功能项；未登记返回 None（不拦截）。"""
    p = (path or "").rstrip("/") or "/"
    fid = _PATH_MAP.get(p)
    if fid:
        return _BY_ID[fid]
    if p.startswith("/api/"):
        rest = p[len("/api/"):]
        if rest.startswith("v1/"):
            rest = rest[3:]
        seg = rest.split("/", 1)[0]
        fid = _API_MAP.get(seg)
        if fid:
            return _BY_ID[fid]
    return None


def allowed_features(user: Optional[dict]) -> set:
    """平台启用 ∩ (用户覆盖 > 角色分配 > 不限制)。"""
    base = enabled_features()
    cfg = get_config()
    if not user:
        return _PROTECTED
    username = str(user.get("username") or "")
    role = str(user.get("role") or "")
    if username in cfg["users"]:
        granted = set(cfg["users"][username])
    elif role in cfg["roles"]:
        granted = set(cfg["roles"][role])
    else:
        granted = set(_BY_ID)
    return (base & granted) | _PROTECTED


def perm_allowed(user: Optional[dict], perm: str) -> bool:
    """权限点是否属于该用户可用功能项；未登记权限点一律放行（零破坏）。"""
    owners = _PERM_OWNERS.get(perm)
    if not owners:
        return True
    allowed = allowed_features(user)
    return any(fid in allowed for fid in owners)


def filter_permissions(user: Optional[dict], perms) -> List[str]:
    return [p for p in (perms or []) if perm_allowed(user, p)]


def can_access(user: Optional[dict], path: str) -> Tuple[bool, str]:
    """页面 / API 是否允许访问。返回 (bool, reason)。"""
    p = (path or "").rstrip("/") or "/"
    if p.startswith("/static/") or p in EXEMPT_PAGE_PATHS:
        return True, "exempt"
    if p.startswith("/api/") and any(
            p == x or p.startswith(x + "/") for x in EXEMPT_API_PREFIXES):
        return True, "exempt"
    f = feature_for_path(p)
    if not f:
        return True, "unmanaged"
    if f["id"] not in enabled_features():
        return False, "feature_disabled"
    if f["id"] not in allowed_features(user):
        return False, "feature_denied"
    return True, "ok"
