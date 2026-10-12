# -*- coding: utf-8 -*-
"""不可变备份与保留锁定（WORM / Immutability，对标 Veeam Hardened Repository、
Commvault Immutable Storage、云祺防勒索不可变备份）。

设计说明书的 G13 缺口：此前备份可被删除/覆盖，加密只防读不防删，
抗勒索能力存在结构性缺口。本模块提供四层防护：

1. 保留锁定（应用层 WORM）：锁定期内删除/覆盖一律拒绝（含管理员误操作），
   解锁必须带操作者与原因并留审计；
2. 产物写保护：锁定时把产物文件 chmod 为只读（0444）——即使绕过应用层
   （如运维误 rm 需 -f 才能删），部分文件系统/容器卷可能不支持，失败时
   如实标记 write_protected=0，绝不静默假装已保护；
3. 完整性基线 + 周期复检：以备份记录的 sha256 为基线，周期任务重算比对，
   漂移即告警（篡改/位 rot 早期发现）；
4. 删除路径全面拦截：定期清理、存储保留策略、GFS 策略、任务级联删除
   全部尊重保留锁。

诚实边界（沿用设计原则 6）：应用层锁定防的是「平台自身与凭据泄露」场景；
chmod 只读防的是普通误删；真正的物理不可变需要文件系统 immutability 或
S3 Object Lock，本环境若不支持会显式报告而非假装。隔离副本（air-gap）
由磁带 D2T + 摆渡 ferry_inbox 承接（物理隔离）。
"""
import os
from datetime import datetime, timedelta

import core.db as db
from core import models

_log_target = "immutability"


class ImmutabilityViolation(Exception):
    """试图删除/覆盖保留锁内的备份。"""


def now_iso() -> str:
    return db.now_iso()


def is_active(record: dict, now: str = None) -> bool:
    """记录当前是否处于保留锁保护期。"""
    if not record or not record.get("locked"):
        return False
    until = record.get("retention_until") or ""
    # retention_until 为空视为永久锁（显式永久保留）
    if not until:
        return True
    now = now or now_iso()
    try:
        return now <= until
    except TypeError:
        return False


def locked_paths() -> set:
    """当前处于锁保护期的产物路径集合（删除路径守卫用）。"""
    try:
        rows = db.query(
            "SELECT backup_path, locked, retention_until FROM backup_records "
            "WHERE locked=1 AND backup_path IS NOT NULL AND backup_path!=''")
        out = set()
        for r in rows:
            if is_active(r):
                out.add(r["backup_path"])
        return out
    except Exception:  # noqa: BLE001 - 查询失败绝不放行拦截逻辑之外
        return set()


def _artifact_paths(record: dict) -> list:
    p = (record or {}).get("backup_path") or ""
    return [p] if p and os.path.isfile(p) else []


def apply_write_protection(paths: list) -> tuple:
    """把产物文件置为只读（0444）。返回 (成功清单, 失败清单[(path, err)])。"""
    ok, failed = [], []
    for p in paths or []:
        try:
            cur = os.stat(p).st_mode
            os.chmod(p, cur & 0o555)          # 去掉写位，保留读/执行位
            ok.append(p)
        except OSError as e:
            failed.append((p, str(e)))
    return ok, failed


def release_write_protection(paths: list) -> tuple:
    """恢复产物文件可写（解锁/到期清理时调用）。"""
    ok, failed = [], []
    for p in paths or []:
        if not os.path.isfile(p):
            continue
        try:
            cur = os.stat(p).st_mode
            os.chmod(p, cur | 0o200)
            ok.append(p)
        except OSError as e:
            failed.append((p, str(e)))
    return ok, failed


def lock_record(record_id: int, days: int, operator: str = "",
                reason: str = "") -> dict:
    """对备份记录施加保留锁定（可重复调用以延长/缩短，取更晚到期时间）。

    days=0 表示永久锁定（retention_until 置空）。返回锁摘要。
    """
    record = models.get_record(int(record_id))
    if not record:
        raise ValueError(f"备份记录不存在: {record_id}")
    if record.get("status") != "success":
        raise ValueError("只有成功的备份才能锁定")
    if record.get("is_simulated"):
        raise ValueError("演示记录不参与保留锁定")
    paths = _artifact_paths(record)
    if not paths:
        raise ValueError("产物文件不在磁盘上（幽灵记录），无法锁定")

    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("锁定天数必须为整数")
    if days < 0:
        raise ValueError("锁定天数不能为负")

    until = "" if days == 0 else (
        datetime.fromisoformat(now_iso().replace("Z", "+00:00"))
        + timedelta(days=days)).isoformat(timespec="seconds")
    # 已有锁取更晚到期（只能延长，不能通过重复加锁提前缩短）
    if record.get("locked") and (record.get("retention_until") or ""):
        until = max(until, record["retention_until"]) if until \
            else record["retention_until"]

    protected, protect_failed = apply_write_protection(paths)
    note = (f"锁定 by {operator or 'unknown'}：{reason or '未填写原因'}"
            + (f"（永久）" if days == 0 else f"（{days} 天，至 {until}）"))
    if record.get("lock_note"):
        note = record["lock_note"] + " | " + note
    db.execute(
        "UPDATE backup_records SET locked=1, locked_at=?, retention_until=?, "
        "write_protected=?, lock_note=? WHERE id=?",
        (now_iso(), until or None, 1 if protected else 0, note, record_id))
    db.add_log("WARNING", _log_target,
               f"备份记录 #{record_id} 保留锁定：{note}；"
               f"写保护 {len(protected)}/{len(paths)} 个文件"
               + (f"，失败 {protect_failed}" if protect_failed else ""))
    return {"record_id": record_id, "locked": True, "retention_until": until,
            "write_protected": len(protected), "protect_failed": protect_failed,
            "note": note}


def unlock_record(record_id: int, operator: str = "", reason: str = "") -> dict:
    """解除保留锁定（必须填写原因；强制审计——这是抗勒索的关键闸门）。"""
    if not (reason or "").strip():
        raise ValueError("解锁必须填写原因（审计要求）")
    record = models.get_record(int(record_id))
    if not record:
        raise ValueError(f"备份记录不存在: {record_id}")
    if not record.get("locked"):
        return {"record_id": record_id, "locked": False, "note": "未锁定"}
    paths = _artifact_paths(record)
    release_write_protection(paths)
    note = (record.get("lock_note") or "") + \
        f" | 解锁 by {operator or 'unknown'}：{reason}"
    db.execute(
        "UPDATE backup_records SET locked=0, retention_until=NULL, "
        "write_protected=0, lock_note=? WHERE id=?",
        (note, record_id))
    db.add_log("WARNING", _log_target,
               f"备份记录 #{record_id} 解除保留锁定 by {operator or 'unknown'}："
               f"{reason}（原到期 {record.get('retention_until') or '永久'}）")
    return {"record_id": record_id, "locked": False, "note": note}


def assert_deletable(record: dict) -> None:
    """删除路径守卫：锁保护期内抛 ImmutabilityViolation。"""
    if is_active(record):
        raise ImmutabilityViolation(
            f"备份记录 #{record['id']} 处于保留锁保护期"
            f"（至 {record.get('retention_until') or '永久'}），禁止删除/覆盖；"
            f"如确需删除请先解锁（留审计）")


def verify_integrity(record_id: int) -> dict:
    """完整性复检：重算产物 sha256 与记录基线比对。

    首次复检时若记录无 checksum，则以当前文件计算为基线（如实标注
    baseline_new）；不一致即篡改/位 rot，记 ERROR 日志供告警采集。
    """
    record = models.get_record(int(record_id))
    if not record:
        raise ValueError(f"备份记录不存在: {record_id}")
    paths = _artifact_paths(record)
    if not paths:
        return {"record_id": record_id, "status": "missing",
                "message": "产物文件不在磁盘上"}
    from core.scheduler import _compute_checksum
    digest = _compute_checksum(paths[0])
    if not digest:
        return {"record_id": record_id, "status": "error",
                "message": "校验和计算失败"}
    baseline = record.get("checksum") or ""
    if not baseline:
        db.execute("UPDATE backup_records SET checksum=?, checksum_ok=1, "
                   "checksum_verified_at=? WHERE id=?",
                   (digest, now_iso(), record_id))
        return {"record_id": record_id, "status": "baseline_new",
                "message": "记录原无校验和，已以当前文件建立基线",
                "checksum": digest}
    ok = digest == baseline
    db.execute("UPDATE backup_records SET checksum_ok=?, checksum_verified_at=? "
               "WHERE id=?", (1 if ok else 0, now_iso(), record_id))
    if not ok:
        db.add_log("ERROR", _log_target,
                   f"备份记录 #{record_id} 完整性复检不一致（疑似篡改/损坏）："
                   f"基线 {baseline[:16]}… 实际 {digest[:16]}…")
    return {"record_id": record_id,
            "status": "match" if ok else "drift",
            "message": ("校验和一致" if ok else
                        "校验和不一致：产物可能被篡改或损坏，请立即排查"),
            "checksum": digest}


def recheck_all() -> dict:
    """对全部处于锁保护期的记录做完整性复检（周期任务入口）。"""
    rows = db.query(
        "SELECT id FROM backup_records WHERE locked=1 ORDER BY id DESC")
    report = {"total": 0, "match": 0, "drift": 0, "baseline_new": 0,
              "missing": 0, "error": 0}
    for r in rows:
        rec = models.get_record(r["id"])
        if not is_active(rec):
            continue
        report["total"] += 1
        try:
            res = verify_integrity(r["id"])
        except Exception as e:  # noqa: BLE001
            report["error"] += 1
            db.add_log("ERROR", _log_target,
                       f"备份记录 #{r['id']} 完整性复检异常: {e}")
            continue
        st = res.get("status")
        if st in report:
            report[st] += 1
    if report["drift"]:
        db.add_log("ERROR", _log_target,
                   f"完整性复检发现 {report['drift']} 个产物校验和不一致，"
                   "疑似篡改或损坏，请立即排查")
    return report


def maybe_autolock(task: dict, record_id: int) -> dict:
    """按任务配置自动锁定（备份成功后调用）。

    任务 extra_options.retention_lock_days > 0 时自动加锁；为产品提供
    「备份即上锁」的防勒索默认姿势。
    """
    import json as _json
    try:
        raw = (task or {}).get("extra_options")
        extra = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        days = int(extra.get("retention_lock_days") or 0)
    except Exception:  # noqa: BLE001
        return {}
    if days <= 0:
        return {}
    try:
        return lock_record(record_id, days, operator="auto",
                           reason=f"任务「{task.get('name')}」自动保留锁定")
    except Exception as e:  # noqa: BLE001 - 自动锁定失败不影响备份结果
        db.add_log("WARNING", _log_target,
                   f"备份记录 #{record_id} 自动锁定失败: {e}")
        return {"error": str(e)}
