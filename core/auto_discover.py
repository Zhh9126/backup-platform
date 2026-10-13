# -*- coding: utf-8 -*-
"""新建库自动纳管（对标 AXUS「备新建库」，2026-10-13）。

管理软件用户常在系统内新建业务库，却忘记把新库加进备份计划——数据裸奔。
本模块在任务开启 extra_options.auto_discover_new_dbs 时：

1. 每次任务执行前枚举源实例全部数据库（复用各引擎 list_databases 通道），
   与上次快照（extra_options.discovered_dbs，JSON 数组）比对；
2. 发现新库时：
   - 全实例（use_all_db/库名留空）任务：新库天然被覆盖，仅记录与提示；
   - 单库任务：**自动克隆原任务**为新库建独立备份任务（保留调度/保留策略/
     存储配置），并发 webhook/mail 通知，杜绝遗忘；
3. 快照随发现更新；发现失败不影响本次备份执行（失败安全）。

手动触发：POST /api/tasks/<id>/discover。
"""
import json
import logging

import config
from core import db, models

logger = logging.getLogger("auto_discover")

# 支持自动发现（有 list_databases 实现）的数据库类型
SUPPORTED_DB_TYPES = {"mysql", "mariadb", "postgresql", "kingbase",
                      "dameng", "opengauss", "panwei", "oracle", "sqlserver",
                      "mongodb", "redis"}


def _is_full_instance(task: dict) -> bool:
    return not (task.get("db_name") or "").strip()


def _load_seen(extra: dict) -> list:
    try:
        v = extra.get("discovered_dbs")
        if isinstance(v, str):
            v = json.loads(v)
        return [str(x) for x in (v or [])]
    except Exception:
        return []


def _save_seen(task_id: int, extra: dict, seen: list) -> None:
    extra = dict(extra or {})
    extra["discovered_dbs"] = seen
    models.update_task(task_id, {"extra_options": json.dumps(extra, ensure_ascii=False)})


def list_instance_databases(task: dict) -> tuple:
    """枚举源实例数据库列表。返回 (dbs, note)；异常返回 ([], 原因)。"""
    from core.engines import get_engine
    try:
        eng = get_engine(task.get("db_type"), task, config.BACKUP_ROOT)
        if not hasattr(eng, "list_databases"):
            return [], "该引擎未实现列库通道"
        dbs = eng.list_databases() or []
        return [str(x) for x in dbs if x and str(x).strip()], ""
    except Exception as e:  # noqa: BLE001 —— 发现失败不阻断备份
        return [], f"枚举失败: {str(e) or type(e).__name__}"


def discover_for_task(task_id: int, operator: str = "") -> dict:
    """对单个任务执行新库发现。返回发现结果摘要（供 API/调度共用）。"""
    task = models.get_task(task_id, include_secret=True)
    if not task:
        return {"ok": False, "message": "任务不存在"}
    db_type = (task.get("db_type") or "").lower()
    if db_type not in SUPPORTED_DB_TYPES:
        return {"ok": False, "message": f"{db_type} 不支持新建库自动发现"}

    extra = task.get("extra_options") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}

    dbs, note = list_instance_databases(task)
    if not dbs:
        return {"ok": False, "message": note or "未枚举到任何数据库"}

    sys_like = {"information_schema", "performance_schema", "mysql", "sys",
                "postgres", "template0", "template1", "template2", "security",
                "test", "master", "model", "msdb", "tempdb"}
    seen = set(_load_seen(extra))
    # 首次发现：把当前全部库记为基线，不批量建任务（避免误建一批）
    if not seen:
        seen = set(dbs)
        _save_seen(task_id, extra, sorted(seen))
        return {"ok": True, "discovered": [], "baseline": sorted(seen),
                "message": f"已建立基线快照（{len(seen)} 个库），后续新库将自动纳管"}

    new_dbs = sorted(d for d in dbs
                     if d not in seen and d.lower() not in sys_like
                     and not d.startswith("template"))
    created = []
    if new_dbs and not _is_full_instance(task):
        for ndb in new_dbs:
            nt = dict(task)
            nt.pop("id", None)
            nt["db_name"] = ndb
            nt["name"] = f"{task.get('name') or 'task'}_{ndb}"[:120]
            nt["extra_options"] = json.dumps(
                {k: v for k, v in extra.items()
                 if k != "discovered_dbs"}, ensure_ascii=False)
            try:
                new_id = models.create_task(nt)
                created.append({"task_id": new_id, "db_name": ndb})
                logger.info("[auto_discover] 任务 #%s 发现新库 %s → 自动创建任务 #%s",
                            task_id, ndb, new_id)
            except Exception as e:  # noqa: BLE001
                logger.error("[auto_discover] 新库 %s 建任务失败: %s", ndb, e)

    seen_now = sorted(set(dbs) | seen)
    _save_seen(task_id, extra, seen_now)

    msg = (f"发现新库: {', '.join(new_dbs)}（已自动创建备份任务）"
           if created else
           (f"发现新库: {', '.join(new_dbs)}（全实例任务已自动覆盖）"
            if new_dbs else "未发现新库"))
    # 通知（邮件/webhook 走既有通道，成功失败皆不阻断）
    if new_dbs:
        try:
            from core import webhooks
            webhooks.emit_async("tasks.discovered", {
                "task_id": task_id, "task_name": task.get("name"),
                "new_databases": new_dbs, "created_tasks": created,
                "operator": operator,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("[auto_discover] 通知失败: %s", e)

    return {"ok": True, "discovered": new_dbs, "created": created,
            "message": msg}
