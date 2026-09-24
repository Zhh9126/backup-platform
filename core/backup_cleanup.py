# -*- coding: utf-8 -*-
"""
备份产物定期清理（Retention Cleanup）——数据库多了、备份久了如何自动瘦身。

与既有能力的分工：
- core.storage.StorageManager.apply_retention：每次备份成功后按任务
  retention_days / retention_count 清理该任务目录下的文件；只在备份成功时
  触发，任务停用后历史产物就再也不会被清理，且不动 backup_records（记录
  会变成指向已删文件的"幽灵记录"）。
- core.retention_gfs：GFS（日/周/月/年）保留，默认只标记 expired_gfs 不删文件。
- 本模块：按任务保留策略的周期清理总账——每天一次扫描全部任务的成功备份
  记录，既按天龄（retention_days）也按份数（retention_count）判定过期，删除
  产物文件并把记录置为 expired_cleanup，可选连记录一并清除；支持 dry-run
  预览，让"会删哪些、能释放多少空间"在执行前可见。

安全护栏：
1. 每任务最近 keep_min 份成功备份无条件保留（默认 1），任何策略都不能删掉
   最后一个可用备份；
2. 只删除位于受管备份根目录（config.get_backup_root() 及其派生目录）内的
   产物，路径为空、不在受管目录或远程 URL 一律跳过；文件已不在磁盘的如实
   计入 missing，不谎报释放空间；
3. retention_days 与 retention_count 都为空/0 的任务视为未配置保留策略，
   不做任何清理（不替用户做决定）；
4. 所有删除动作写日志（logger=cleanup），并落盘最近一次执行结果
   （system_config.cleanup_last_*）供界面展示。

入口：build_plan()（扫描/预览）、run_cleanup()（执行）；
由 core.scheduler 的 backup_cleanup 周期任务每日调用。
"""
import json
import os
from datetime import datetime

import config
import core.db as db
import core.models as models

# 每任务无条件保留的最近成功备份份数（安全兜底）
KEEP_MIN_DEFAULT = 1
DEFAULT_CRON = "10 3 * * *"          # 默认每日 03:10（错开默认备份窗口）

CFG_ENABLED = "cleanup_enabled"
CFG_CRON = "cleanup_cron"
CFG_KEEP_MIN = "cleanup_keep_min"
CFG_PURGE = "cleanup_purge_records"
CFG_LAST_RUN = "cleanup_last_run"
CFG_LAST_SUMMARY = "cleanup_last_summary"

# 清理后记录落的状态（界面显示为"已清理（过期）"）
STATUS_EXPIRED = "expired_cleanup"

_logger = None


def _log():
    global _logger
    if _logger is None:
        _logger = db.get_logger("cleanup")
    return _logger


# ---------------------------- 配置 ----------------------------
def _as_bool(v, default=False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_int(v, default=0) -> int:
    try:
        return int(str(v).strip() or default)
    except (TypeError, ValueError):
        return default


def get_config() -> dict:
    """读取定期清理配置（缺失时返回默认值）。"""
    try:
        summary = json.loads(db.get_system_config(CFG_LAST_SUMMARY) or "{}")
    except Exception:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "enabled": _as_bool(db.get_system_config(CFG_ENABLED, "1"), True),
        "cron": db.get_system_config(CFG_CRON) or DEFAULT_CRON,
        "keep_min": max(0, _as_int(db.get_system_config(CFG_KEEP_MIN, KEEP_MIN_DEFAULT),
                                   KEEP_MIN_DEFAULT)),
        "purge_records": _as_bool(db.get_system_config(CFG_PURGE, "0"), False),
        "last_run_at": db.get_system_config(CFG_LAST_RUN) or "",
        "last_summary": summary,
    }


def save_config(data: dict) -> dict:
    """保存定期清理配置（键不存在时不改动）。"""
    data = data or {}
    if "enabled" in data:
        db.set_system_config(CFG_ENABLED, "1" if _as_bool(data["enabled"], True) else "0")
    if "cron" in data:
        cron = str(data["cron"] or "").strip() or DEFAULT_CRON
        if len(cron.split()) != 5:
            raise ValueError("清理时间必须是 5 段 cron 表达式（分 时 日 月 周）")
        db.set_system_config(CFG_CRON, cron)
    if "keep_min" in data:
        db.set_system_config(CFG_KEEP_MIN, max(0, _as_int(data["keep_min"], KEEP_MIN_DEFAULT)))
    if "purge_records" in data:
        db.set_system_config(CFG_PURGE, "1" if _as_bool(data["purge_records"]) else "0")
    return get_config()


# ---------------------------- 工具 ----------------------------
def _parse_ts(s):
    """平台时间戳（带时区 ISO / 纯 ISO）转本地朴素 datetime。"""
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone().replace(tzinfo=None)
        except Exception:
            dt = dt.replace(tzinfo=None)
    return dt


def _managed_roots():
    """受管删除的根目录白名单（越界不删）。"""
    roots = []
    getter = getattr(config, "get_backup_root", None)
    try:
        if getter:
            roots.append(str(getter()))
    except Exception:
        pass
    for attr in ("RT_LOG_ROOT", "RT_FILE_ROOT"):
        try:
            roots.append(str(getattr(config, attr)))
        except Exception:
            pass
    out = []
    for r in roots:
        try:
            rp = os.path.realpath(str(r))
        except Exception:
            continue
        if rp and rp not in out:
            out.append(rp)
    return out


def _is_managed_path(path: str) -> bool:
    if not path:
        return False
    if "://" in path:                      # 对象存储 / sftp 等非本地路径：不删
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    for root in _managed_roots():
        if rp == root or rp.startswith(root + os.sep):
            return True
    return False


def _size_of(rec: dict) -> int:
    try:
        sz = int(rec.get("size_bytes") or 0)
    except (TypeError, ValueError):
        sz = 0
    if sz <= 0:
        p = rec.get("backup_path")
        try:
            if p and os.path.isfile(p):
                sz = os.path.getsize(p)
        except OSError:
            sz = 0
    return sz


# ---------------------------- 扫描 / 预览 ----------------------------
def _task_rows(task_id: int):
    return db.query(
        "SELECT id, task_id, backup_path, size_bytes, finished_at, started_at, status "
        "FROM backup_records WHERE task_id=? AND status='success' "
        "ORDER BY COALESCE(finished_at, started_at) DESC, id DESC", (task_id,))


def build_plan(task_id: int = None, keep_min: int = None) -> list:
    """按任务保留策略扫描可清理的成功备份（只读，不做任何删除）。

    返回每个任务的计划：总数 / 保留数 / 待清理清单（含大小与原因）。
    """
    km = KEEP_MIN_DEFAULT if keep_min is None else max(0, int(keep_min))
    if task_id:
        one = models.get_task(task_id)
        tasks = [one] if one else []
    else:
        tasks = models.list_tasks() or []
    now = datetime.now()
    plans = []
    for t in tasks:
        if not t:
            continue
        rd = _as_int(t.get("retention_days"), 0)
        rc = _as_int(t.get("retention_count"), 0)
        # 护栏3：未配置保留策略的任务不清理（记录再多也不动）
        if rd <= 0 and rc <= 0:
            continue
        rows = _task_rows(int(t["id"]))
        if not rows:
            continue
        items = []
        for idx, r in enumerate(rows):
            ts = _parse_ts(r.get("finished_at") or r.get("started_at"))
            age_days = (now - ts).days if ts else None
            reason = ""
            if rd > 0 and age_days is not None and age_days > rd:
                reason = "超过保留天数 %d 天（已 %s 天）" % (rd, age_days)
            elif rc > 0 and idx >= rc:
                reason = "超出保留份数 %d 份（第 %d 份）" % (rc, idx + 1)
            if not reason:
                continue
            # 护栏1：最近 keep_min 份无条件保留
            if idx < km:
                continue
            path = r.get("backup_path") or ""
            items.append({
                "record_id": r["id"],
                "finished_at": r.get("finished_at") or r.get("started_at") or "",
                "age_days": age_days,
                "size_bytes": _size_of(r),
                "backup_path": path,
                "has_file": bool(path) and os.path.isfile(str(path)),
                "reason": reason,
            })
        if not items:
            continue
        plans.append({
            "task_id": t["id"],
            "task_name": t.get("name") or "",
            "biz_label": t.get("biz_label") or t.get("biz_system") or t.get("name") or "",
            "db_type": t.get("db_type") or "",
            "retention_days": rd,
            "retention_count": rc,
            "total": len(rows),
            "keep_min": km,
            "clean_count": len(items),
            "keep": len(rows) - len(items),
            # 只统计"文件还在"的部分作为可释放空间；产物已不在磁盘的（幽灵记录）
            # 单独计入 ghost_bytes，避免预览里虚报容量。
            "free_bytes": sum(i["size_bytes"] for i in items if i["has_file"]),
            "ghost_bytes": sum(i["size_bytes"] for i in items if not i["has_file"]),
            "items": items,
        })
    return plans


def summary_of(plans: list) -> dict:
    missing = sum(1 for p in plans for i in p["items"] if not i["has_file"])
    return {
        "tasks": len(plans),
        "records": sum(p["clean_count"] for p in plans),
        "free_bytes": sum(p.get("free_bytes", 0) for p in plans),
        "ghost_bytes": sum(p.get("ghost_bytes", 0) for p in plans),
        "missing_files": missing,
    }


# ---------------------------- 执行 ----------------------------
def run_cleanup(task_id: int = None, dry_run: bool = False,
                keep_min: int = None, purge_records: bool = None) -> dict:
    """执行清理。dry_run=True 只返回计划，不做任何删除。"""
    cfg = get_config()
    do_purge = cfg["purge_records"] if purge_records is None else bool(purge_records)
    plans = build_plan(task_id=task_id, keep_min=keep_min)
    report = {
        "dry_run": bool(dry_run),
        "tasks": len(plans),
        "records": sum(p["clean_count"] for p in plans),
        "deleted_files": 0,
        "freed_bytes": 0,
        "missing_files": 0,
        "skipped_outside": 0,
        "failed": 0,
        "purge_records": bool(do_purge),
        "details": [],
    }
    for p in plans:
        detail = {"task_id": p["task_id"], "task_name": p["task_name"],
                  "clean_count": p["clean_count"], "deleted": 0,
                  "freed_bytes": 0, "failed": []}
        for item in p["items"]:
            path = item["backup_path"]
            if dry_run:
                if item["has_file"]:
                    detail["deleted"] += 1
                    detail["freed_bytes"] += item["size_bytes"]
                else:
                    report["missing_files"] += 1
                continue
            if item["has_file"]:
                ok, err = _remove_artifact(path)
            else:
                ok, err = False, None
                report["missing_files"] += 1
            if err == "outside":
                report["skipped_outside"] += 1
                detail["failed"].append("#%s 路径不在受管备份目录，已跳过删除"
                                        % item["record_id"])
                continue
            if err:
                report["failed"] += 1
                detail["failed"].append("#%s 删除失败: %s" % (item["record_id"], err))
                _log().warning("[cleanup] task=%s record=%s 删除失败: %s",
                               p["task_id"], item["record_id"], err)
                continue
            if ok:
                report["deleted_files"] += 1
                report["freed_bytes"] += item["size_bytes"]
                detail["deleted"] += 1
                detail["freed_bytes"] += item["size_bytes"]
            # 文件删掉（或已丢失）后统一把记录标记为过期清理，杜绝"幽灵记录"
            _mark_expired(item["record_id"], meta=item, purge=do_purge,
                          deleted_file=bool(ok))
        report["details"].append(detail)

    if not dry_run:
        report["finished_at"] = db.now_iso()
        try:
            db.set_system_config(CFG_LAST_RUN, report["finished_at"])
            db.set_system_config(CFG_LAST_SUMMARY, json.dumps({
                "tasks": report["tasks"], "records": report["records"],
                "deleted_files": report["deleted_files"],
                "freed_bytes": report["freed_bytes"],
                "missing_files": report["missing_files"],
            }, ensure_ascii=False))
        except Exception:
            pass
        _log().info("[cleanup] 定期清理完成: 任务 %d 个 / 记录 %d 条 / 删文件 %d 个 / 释放 %.1f MB",
                    report["tasks"], report["records"], report["deleted_files"],
                    report["freed_bytes"] / 1048576.0)
    return report


def _remove_artifact(path: str):
    """删除单个产物文件。返回 (是否删除, 错误原因)；错误=='outside' 表示越界不删。"""
    if not path:
        return False, None
    if not os.path.isfile(path):
        return False, None                      # 文件已不在（幽灵记录）
    if not _is_managed_path(path):
        return False, "outside"
    try:
        os.remove(path)
        return True, None
    except OSError as e:
        return False, str(e)


def _mark_expired(record_id: int, meta: dict = None, purge: bool = False,
                  deleted_file: bool = False):
    """把记录置为过期清理状态（可选物理删除记录行）。"""
    meta = meta or {}
    note = "[定期清理 %s] %s" % (db.now_iso()[:19], meta.get("reason") or "按保留策略过期")
    if deleted_file:
        note += "；产物文件已删除"
    elif meta.get("backup_path"):
        note += "；产物文件已不在磁盘"
    try:
        if purge:
            db.execute("DELETE FROM backup_records WHERE id=?", (record_id,))
        else:
            if db.current_backend() == "mysql":
                # MySQL 不支持 || 拼接（那是逻辑或），用 CONCAT
                db.execute(
                    "UPDATE backup_records SET status=?, backup_path='', "
                    "message=CASE WHEN message IS NULL OR message='' THEN ? "
                    "             ELSE CONCAT(message, ' | ', ?) END WHERE id=?",
                    (STATUS_EXPIRED, note, note, record_id))
            else:
                db.execute(
                    "UPDATE backup_records SET status=?, backup_path='', "
                    "message=CASE WHEN message IS NULL OR message='' THEN ? "
                    "             ELSE message || ' | ' || ? END WHERE id=?",
                    (STATUS_EXPIRED, note, note, record_id))
    except Exception as e:
        _log().warning("[cleanup] 记录 %s 状态更新失败: %s", record_id, e)
