# -*- coding: utf-8 -*-
"""写端点 → RBAC 权限点统一映射（全站兜底，替代逐端点手写装饰器）。

背景
----
全站有 239 个唯一写端点（另有 /api/v1 别名），逐条补 @permission_required
既不现实又容易漏。这里在 api_bp.before_request 统一按「一级模块 + 精确动作」
判定所需权限点；已有 @permission_required 的端点仍保有更细粒度的判定。

判定顺序
--------
1. 非写方法（GET/HEAD/OPTIONS）不检查。
2. 自助类精确豁免（改自己的密码）。
3. FORCE_CHECK：看起来像探测、实则有写副作用的端点强制校验权限。
4. 探测/预览/校验类：仅 POST + 末段为白名单动作词（精确匹配，不是子串），
   或命中显式正则，才豁免。DELETE/PUT/PATCH 一律不豁免。
5. 精确动作覆盖（如 /tasks/<id>/run → backup.run）。
6. 一级模块前缀映射；未覆盖时回落 system.settings 并记 WARNING（不静默放行）。

诚实边界：这是「菜单级权限」兜底，不替代高危操作的双闸（验证码/确认）。
"""
import logging
import re

logger = logging.getLogger("aidbm.perm_map")

WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_V1_PREFIX = "/api/v1/"


def normalize(path: str) -> str:
    """/api/v1/xxx 与 /api/xxx 视为同一端点。"""
    if path.startswith(_V1_PREFIX):
        return "/api/" + path[len(_V1_PREFIX):]
    return path or ""


# ---- 一级模块（/api/<seg>）→ 权限点，覆盖 url_map 全部写端点 ----
SEGMENT_PERMS = {
    # 备份管理
    "tasks": "backup.task.manage",
    # 源端资源保护配置（阈值/开关）属系统级设置
    "source-guard": "system.settings",
    # 无备份目录扫描恢复（pull 会拉回数据，按任务管理权限校验）
    "scan-fs": "backup.task.manage",
    "file-sync": "backup.task.manage",
    "hosts": "backup.task.manage",
    "vm": "backup.task.manage",
    "netdev": "backup.task.manage",
    "synology": "backup.task.manage",
    "win-protect": "backup.task.manage",
    "records": "backup.task.manage",
    "backup-cleanup": "backup.task.manage",
    "policy": "backup.policy.manage",
    "storage": "backup.storage.manage",
    "lifecycle": "backup.storage.manage",
    "dedup": "backup.storage.manage",
    "tape": "backup.storage.manage",
    "pool": "backup.storage.manage",
    "remote-pull": "backup.storage.manage",
    "object-storage": "backup.storage.manage",
    "synthesize": "backup.storage.manage",
    "plugins": "backup.plugins.manage",
    "db-adapters": "backup.dbtypes.manage",
    "jdbc": "backup.dbtypes.manage",
    "rt": "rt.manage",
    "cdc": "rt.manage",
    # 恢复
    "restores": "restore.run",
    "restore-test-reports": "restore.verify.manage",
    "restore-verify-policies": "restore.verify.manage",
    "oracle-txn": "restore.run",
    # 数据对比 / 价值挖掘
    "data-compare-tasks": "data_compare.run",
    "data-compare-reports": "data_compare.run",
    "datamining": "data_compare.run",
    # 部署
    "deploy": "deploy.manage",
    # 灾备
    "migration": "dr.migration.manage",
    "migration-plans": "dr.migration.manage",
    "db-migrate": "dr.migration.manage",
    "migration-protection-plans": "dr.migration.manage",
    "sync": "dr.sync.manage",
    "sync-tasks": "dr.sync.manage",
    "disaster-links": "dr.link.manage",
    "dr-dns": "dr.link.manage",
    "clone": "dr.clone.manage",
    "vdb": "dr.clone.manage",
    # 运维
    "drills": "inspection.manage",
    "inspection": "inspection.manage",
    "agent": "agent.use",
    "agents": "agent.use",
    "ai-plans": "agent.use",
    "alerts": "alert.manage",
    "notify-config": "alert.manage",
    "logs": "system.settings",
    "itsm": "system.settings",
    # 系统 / 安全
    "system": "system.settings",
    "settings": "system.settings",
    "scheduler": "system.settings",
    "meta-db": "system.settings",
    "rbac": "users.manage",
    "tokens": "security.manage",
    "webhooks": "security.manage",
    "op-guard": "security.manage",
    "access-keys": "security.manage",
    "snmp": "security.manage",
    "pool-crypto": "security.manage",
    "ransomware": "system.settings",
}

# ---- 自助类：任何登录用户都不需要额外权限点 ----
SELF_SERVICE = frozenset({"/api/rbac/me/password"})

# ---- 看似探测、实则有写副作用：必须校验权限（优先于豁免） ----
FORCE_CHECK = tuple(re.compile(p) for p in (
    r"^/api/meta-db/(test|plan|migrate|rollback)$",
    r"^/api/datamining/(export|scan|classify)$",
    # 磁带显式写后校验会 rewind/扫描物理带位，不是只读探测
    r"^/api/tape/verify$",
    # 迁移的 verify / dry-run 会写 phases_json 或 verified 标记，不是只读探测
    r"^/api/(migration|db-migrate|migration-plans|migration-protection-plans)/\d+/(verify|dry-run)$",
    # 同步任务的 validate / verify 有写副作用，不能因动作词被豁免
    r"^/api/sync-tasks/\d+/(validate|verify)$",
))

# ---- 精确动作覆盖：按实际路径（数字 id）匹配 ----
ACTION_RULES = tuple((re.compile(p), perm) for p, perm in (
    (r"^/api/tasks/quick-backup$", "backup.run"),
    (r"^/api/tasks/\d+/run$", "backup.run"),
    (r"^/api/tasks/\d+/stop$", "backup.run"),
    # 防勒索扫描是只读探测（附带检测结论落库/隔离），operator 也要能发起
    (r"^/api/ransomware/(scan/\d+|scan-recent)$", "backup.task.manage"),
    # 把推荐窗口写入任务 cron 属于任务配置变更
    (r"^/api/source-guard/window/\d+/apply$", "backup.task.manage"),
))

# ---- 只读语义动作词（POST 且为路径末段，精确相等；非子串匹配） ----
READ_ACTIONS = frozenset({
    "precheck", "restore-precheck", "preflight", "preview", "probe", "dry-run",
    "security-check", "verify", "check", "check-consistency",
    "takeover-check", "diff",
    "test", "test-connection", "test-ssh", "test-ftp", "batch-test",
    "resolve", "analyze", "estimate", "compare", "query", "search",
    "list-tables", "list-shares", "list-databases", "mask-preview",
    "badblock-check", "validate", "preview-rules", "health",
})

# ---- 额外豁免正则（末段是动态 id / 参数，动作词无法覆盖） ----
EXTRA_EXEMPT = tuple(re.compile(p) for p in (
    r"^/api/backup-cleanup/preview(/\d+)?$",
    r"^/api/sync-tasks/\d+/test/[^/]+$",
    r"^/api/sync/\d+/test/[^/]+$",
    r"^/api/db-adapters/preview$",
    r"^/api/object-storage/preview$",
    # 源端 CPU 实时采样是只读探测（会落一条负载样本，不改业务数据）
    r"^/api/source-guard/sample/\d+$",
    # 无备份目录的扫描是纯只读探测；pull 才会拉回数据，仍走段权限
    r"^/api/scan-fs/scan$",
))

DEFAULT_PERM = "system.settings"


def resolve(path: str, method: str):
    """返回 (所需权限点, 原因)。权限点为 None 表示豁免（仍需登录）。

    reason ∈ self_service / force_check / action / exempt / segment / fallback
    """
    if method not in WRITE_METHODS:
        return None, "read"
    p = normalize((path or "").rstrip("/") or "/")
    if p in SELF_SERVICE:
        return None, "self_service"
    if any(rx.match(p) for rx in FORCE_CHECK):
        perm = _segment_perm(p) or DEFAULT_PERM
        return perm, "force_check"
    if method == "POST":
        if any(rx.match(p) for rx in EXTRA_EXEMPT):
            return None, "exempt"
        if p.rsplit("/", 1)[-1] in READ_ACTIONS:
            return None, "exempt"
    for rx, perm in ACTION_RULES:
        if rx.match(p):
            return perm, "action"
    perm = _segment_perm(p)
    if perm:
        return perm, "segment"
    logger.warning("[perm_map] 写端点未配置权限映射，按 %s 兜底: %s %s",
                   DEFAULT_PERM, method, p)
    return DEFAULT_PERM, "fallback"


def _segment_perm(p: str):
    parts = p.split("/")
    if len(parts) < 3 or parts[1] != "api":
        return None
    return SEGMENT_PERMS.get(parts[2])
