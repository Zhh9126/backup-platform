# -*- coding: utf-8 -*-
"""备份数据防勒索统一编排：检测 → 保护 → 响应 → 可观测。

设计前提（与 AIDBM 的产品约束一致）：
- 完全离线：无病毒库、无云查杀，检测只能是启发式（体积突变/熵值/结构嗅探）
  + sha256 完整性基线；**不宣称病毒查杀**。
- 零客户端安装：防护全部落在备份平台侧，靠应用层锁 + 文件权限 +
  （能力可用时）内核 immutable，绝不要求被备份机装 agent。
- 诚实失败：任何探测/加固失败都如实记录，不静默降级、不伪造成功。

闭环：
1. 备份成功 → 自动安全检测并落库（security_level / security_detail）。
2. suspect / 完整性漂移 → 逻辑隔离（quarantined=1）+ 取证锁定 + 告警 webhook
   + 审计事件，禁止恢复与下载。
3. 恢复前 → assert_restorable() 统一安全门（覆盖 API / 调度 / 演练全部入口），
   高危记录默认拒绝；确需恢复须显式 force，并落 critical 审计。
4. 清理 / 保留策略 → 每个任务至少保留一个「可信恢复点」，防止勒索后
   把全部可用备份一并清掉。
5. summary() → 防勒索态势（锁定率、检测分布、隔离数、事件流、能力状态）。

所有写动作都经过 db.execute / db.add_log；任何异常由调用方兜底，
绝不因防勒索逻辑失败而把一次正常备份变成失败。
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from core import db, models

_LOG_TARGET = "ransomware_guard"

# system_config 键 → 默认值。默认开启的都是不影响正常备份/恢复/E2E 的能力；
# fs_immutable 默认关（需要 CAP_LINUX_IMMUTABLE，且会妨碍产物清理，需显式启用）。
DEFAULTS = {
    "ransomware_auto_scan": 1,
    "ransomware_block_suspect_restore": 1,
    "ransomware_auto_quarantine": 1,
    "ransomware_keep_last_clean": 1,
    "ransomware_alert_webhook": 1,
    "ransomware_fs_immutable": 0,
}


class RansomwareBlocked(ValueError):
    """恢复安全门拒绝（继承 ValueError 以兼容既有调用方的业务异常捕获）。"""

    def __init__(self, message: str, reasons: list = None,
                 record_id: int = None):
        super().__init__(message)
        self.reasons = reasons or []
        self.record_id = record_id


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 配置 / 审计表
# --------------------------------------------------------------------------

def get_config() -> dict:
    out = {}
    for key, default in DEFAULTS.items():
        try:
            raw = db.get_system_config(key)
            val = default if raw is None or raw == "" else int(raw)
        except Exception:  # noqa: BLE001 —— 脏配置不打断服务
            val = default
        out[key] = 1 if val else 0
    return out


def set_config(data: dict) -> dict:
    for key in DEFAULTS:
        if key in data:
            val = data[key]
            on = 1 if val is True or str(val).lower() in ("1", "true", "on", "yes") else 0
            db.set_system_config(key, on)
    return get_config()


def enabled(key: str) -> bool:
    return bool(get_config().get(key, DEFAULTS.get(key, 0)))


def _ensure_table() -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ransomware_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "at TEXT, event TEXT, level TEXT, record_id INTEGER, task_id INTEGER, "
        "operator TEXT, detail TEXT)")


def _event(kind: str, level: str, detail: str, record_id=None,
           task_id=None, operator: str = "") -> None:
    """统一审计：事件表 + system_logs + webhook（任一失败不影响主流程）。"""
    try:
        _ensure_table()
        db.execute(
            "INSERT INTO ransomware_events "
            "(at, event, level, record_id, task_id, operator, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (now_iso(), kind, level, record_id, task_id,
             operator or "system", detail[:2000]))
    except Exception:  # noqa: BLE001
        pass
    try:
        db.add_log("ERROR" if level == "critical" else "WARNING", _LOG_TARGET,
                   f"[{kind}] record={record_id} task={task_id} {detail[:600]} "
                   f"(operator={operator or 'system'})")
    except Exception:  # noqa: BLE001
        pass
    if enabled("ransomware_alert_webhook"):
        try:
            from core import webhooks
            webhooks.emit_async("security." + kind, {
                "event": kind, "level": level, "record_id": record_id,
                "task_id": task_id, "operator": operator or "system",
                "detail": detail[:600],
            })
        except Exception:  # noqa: BLE001
            pass


def audit(kind: str, level: str, detail: str, record_id=None,
          task_id=None, operator: str = "") -> None:
    """公开审计入口：解锁、强制恢复等外部动作统一留痕 + 告警。"""
    _event(kind, level, detail, record_id, task_id, operator)


def events(limit: int = 50) -> list:
    _ensure_table()
    try:
        n = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        n = 50
    return [dict(r) for r in db.query(
        "SELECT * FROM ransomware_events ORDER BY id DESC LIMIT ?", (n,))]


# --------------------------------------------------------------------------
# 检测：结果落库（此前只实时返回、不留存）
# --------------------------------------------------------------------------

def scan_record(record_id: int, operator: str = "", auto: bool = False) -> dict:
    """对一条备份记录做启发式检测并持久化结论。

    suspect 时按配置自动隔离（取证锁定 + 告警），但绝不删除/移动产物，
    避免破坏备份链与审计证据。
    """
    from core import backup_anomaly
    rid = int(record_id)
    rec = models.get_record(rid)
    if not rec:
        raise ValueError(f"备份记录不存在: {rid}")
    res = backup_anomaly.check_record_by_id(rid)
    level = res.get("level") or "unknown"
    detail = json.dumps(res.get("checks") or [], ensure_ascii=False)
    db.execute(
        "UPDATE backup_records SET security_level=?, security_checked_at=?, "
        "security_detail=? WHERE id=?",
        (level, now_iso(), detail, rid))
    out = {"record_id": rid, "level": level,
           "checks": res.get("checks") or [], "auto": bool(auto),
           "actions": []}
    if level == "suspect":
        bad = [f"{c.get('item')}: {c.get('message')}" for c in out["checks"]
               if c.get("level") == "suspect"]
        _event("ransomware_suspect", "critical",
               f"恢复点安全检测发现可疑特征（{'；'.join(bad)[:600]}）",
               rid, rec.get("task_id"), operator)
        if enabled("ransomware_auto_quarantine"):
            try:
                q = quarantine_record(rid, reason="自动隔离：安全检测判定 suspect",
                                      operator=operator or "auto-scan")
                out["actions"].append("quarantined")
                out["quarantined"] = True
                out["lock"] = q.get("lock")
            except Exception as exc:  # noqa: BLE001
                out["actions"].append(f"quarantine_failed: {exc}")
    return out


def after_backup(record_id: int, task: dict = None,
                 operator: str = "") -> dict:
    """备份成功钩子（scheduler 调用）。任何异常都不影响备份结果。"""
    if not enabled("ransomware_auto_scan"):
        return {"skipped": "auto_scan_disabled"}
    try:
        return scan_record(record_id, operator=operator, auto=True)
    except Exception as exc:  # noqa: BLE001
        db.add_log("ERROR", _LOG_TARGET,
                   f"备份后自动安全检测失败 record={record_id}: {exc}")
        return {"error": str(exc)}


def scan_recent(limit: int = 100, operator: str = "") -> dict:
    """补扫历史记录（安全门上线前的存量备份），按 id 倒序。"""
    try:
        n = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        n = 100
    rows = db.query(
        "SELECT id FROM backup_records WHERE status='success' "
        "AND COALESCE(is_simulated,0)=0 "
        "AND (security_level IS NULL OR security_level='') "
        "ORDER BY id DESC LIMIT ?", (n,))
    stats = {"scanned": 0, "clean": 0, "suspect": 0, "unknown": 0,
             "errors": 0}
    for r in rows:
        try:
            res = scan_record(r["id"], operator=operator, auto=True)
            stats["scanned"] += 1
            stats[res["level"]] = stats.get(res["level"], 0) + 1
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
    return stats


# --------------------------------------------------------------------------
# 响应：逻辑隔离 / 完整性漂移
# --------------------------------------------------------------------------

def quarantine_record(record_id: int, reason: str, operator: str = "") -> dict:
    """逻辑隔离可疑产物：标记 + 永久取证锁 + 审计 + 告警。

    只做标记与加锁，不移动/删除文件（保留证据链，也避免大文件复制占满磁盘）。
    """
    rid = int(record_id)
    rec = models.get_record(rid)
    if not rec:
        raise ValueError(f"备份记录不存在: {rid}")
    db.execute(
        "UPDATE backup_records SET quarantined=1, security_level='suspect' "
        "WHERE id=?", (rid,))
    lock = None
    if not rec.get("locked"):
        try:
            from core import immutability
            lock = immutability.lock_record(
                rid, days=0, operator=operator or "ransomware-guard",
                reason=f"防勒索取证锁定：{reason}")
        except Exception as exc:  # noqa: BLE001 —— 幽灵记录等如实记录
            lock = {"error": str(exc)}
    _event("quarantine", "critical",
           f"可疑备份产物已隔离（禁止恢复/下载）：{reason}；"
           f"取证锁：{lock.get('error') if isinstance(lock, dict) and lock.get('error') else '已施加/已存在'}",
           rid, rec.get("task_id"), operator)
    return {"record_id": rid, "quarantined": True, "lock": lock}


def release_quarantine(record_id: int, reason: str, operator: str = "") -> dict:
    """人工解除隔离（必须填原因）。保留原有取证锁，需另行解锁，防绕过。"""
    if not (reason or "").strip():
        raise ValueError("解除隔离必须填写原因（审计要求）")
    rid = int(record_id)
    rec = models.get_record(rid)
    if not rec:
        raise ValueError(f"备份记录不存在: {rid}")
    db.execute(
        "UPDATE backup_records SET quarantined=0, security_level=NULL, "
        "security_checked_at=NULL WHERE id=?", (rid,))
    _event("quarantine_released", "warning",
           f"人工解除隔离（需重新检测）：{reason}；原有保留锁保持不变",
           rid, rec.get("task_id"), operator)
    return {"record_id": rid, "quarantined": False,
            "note": "已解除隔离；安全结论清空，建议重新检测后再恢复"}


def on_integrity_drift(record_id: int, digest: str = "",
                       operator: str = "integrity-check") -> dict:
    """完整性复检发现 sha256 漂移：升级为 suspect + 隔离 + 告警 + 拦截恢复。"""
    rid = int(record_id)
    rec = models.get_record(rid)
    if not rec:
        return {"ok": False, "error": "记录不存在"}
    db.execute(
        "UPDATE backup_records SET security_level='suspect', "
        "security_checked_at=? WHERE id=?", (now_iso(), rid))
    _event("integrity_drift", "critical",
           f"产物 sha256 与基线不一致，疑似被篡改/加密（digest={digest[:32]}）",
           rid, rec.get("task_id"), operator)
    if enabled("ransomware_auto_quarantine") and not rec.get("quarantined"):
        return quarantine_record(rid, reason="完整性基线漂移",
                                 operator=operator)
    return {"ok": True, "record_id": rid, "security_level": "suspect"}


# --------------------------------------------------------------------------
# 恢复前安全门
# --------------------------------------------------------------------------

def assert_restorable(record: dict, force: bool = False,
                      operator: str = "") -> dict:
    """恢复统一安全门（scheduler.run_restore_now 调用，覆盖所有恢复入口）。

    默认拒绝：隔离中 / 检测 suspect / 完整性漂移。未检测过的记录现场快扫
    （unknown 不阻断——远端产物本地不可见是正常形态，不能误伤）。
    force=True 时不阻断，但落 critical 审计 + webhook，绝不静默放行。
    """
    rec = dict(record or {})
    rid = int(rec.get("id") or 0)
    if not enabled("ransomware_block_suspect_restore") and not force:
        return {"allowed": True, "skipped": "guard_disabled"}
    if not rec.get("security_level") and enabled("ransomware_auto_scan") and rid:
        try:
            res = scan_record(rid, operator=operator, auto=True)
            rec["security_level"] = res["level"]
            rec["quarantined"] = 1 if res.get("quarantined") else rec.get("quarantined")
        except Exception:  # noqa: BLE001 —— 检测失败不伪装结论，也不阻断恢复
            rec["security_level"] = "unknown"

    reasons = []
    if rec.get("quarantined"):
        reasons.append("产物已被防勒索隔离（quarantined=1）")
    if rec.get("security_level") == "suspect":
        reasons.append("恢复点安全检测判定可疑（疑似加密/篡改）")
    if rec.get("checksum_ok") == 0:
        reasons.append("完整性复检发现 sha256 与基线不一致")

    if not reasons:
        return {"allowed": True, "level": rec.get("security_level") or "unknown"}
    detail = "；".join(reasons)
    if force:
        _event("unsafe_restore_override", "critical",
               f"人工强制恢复存在安全风险的备份产物：{detail}",
               rid, rec.get("task_id"), operator)
        return {"allowed": True, "forced": True, "reasons": reasons}
    raise RansomwareBlocked(
        f"恢复安全门拒绝：备份记录 {rid} {detail}。"
        "该产物可能已被勒索加密或篡改；请先排查、用「恢复点安全检测」复核，"
        "或由管理员在明确风险后显式强制恢复（会留 critical 审计）。",
        reasons=reasons, record_id=rid)


# --------------------------------------------------------------------------
# 最后可信恢复点保护（清理/保留策略调用）
# --------------------------------------------------------------------------

def trusted_point_ids() -> set:
    """每个任务最近一个「可信恢复点」：成功、未隔离、非 suspect、无漂移、
    且产物文件确实还在磁盘上（可恢复）。

    - 不依赖 security_level 已存在（历史记录 NULL 也视为可信），上线即有保护。
    - 不给记录加锁，避免影响既有任务删除与 E2E 流程。
    - 幽灵记录（backup_path 非空但文件已丢失）不算恢复点：它本来就恢复不了，
      若拿它占位反而会让清理预览把真实可清理项误判为"必须保留"。
    """
    if not enabled("ransomware_keep_last_clean"):
        return set()
    try:
        rows = db.query(
            "SELECT id, task_id, backup_path FROM backup_records "
            "WHERE status='success' AND COALESCE(is_simulated,0)=0 "
            "AND COALESCE(quarantined,0)=0 "
            "AND COALESCE(security_level,'')!='suspect' "
            "AND COALESCE(checksum_ok,1)!=0 ORDER BY id DESC")
        best = {}
        for r in rows:
            p = r.get("backup_path") or ""
            if p and not os.path.isfile(p):
                continue
            tid = r.get("task_id")
            if tid is not None and tid not in best:
                best[tid] = int(r["id"])
        return set(best.values())
    except Exception:  # noqa: BLE001 —— 表结构未升级时如实返回空集
        return set()


def is_last_trusted_point(record_id: int) -> bool:
    try:
        return int(record_id) in trusted_point_ids()
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# 内核级不可变（能力探测 + 可选启用）
# --------------------------------------------------------------------------

def fs_immutable_capability() -> dict:
    """真实探测 chattr +i 是否可用（不猜、不虚报）。"""
    if os.name != "posix":
        return {"available": False, "reason": "当前不是 Linux/Unix 文件系统"}
    chattr = shutil.which("chattr")
    lsattr = shutil.which("lsattr")
    if not chattr or not lsattr:
        return {"available": False,
                "reason": "缺少 chattr/lsattr（e2fsprogs 未安装）"}
    root = None
    tmp = None
    try:
        import config
        root = str(config.BACKUP_ROOT)
        os.makedirs(root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".rw_probe_", dir=root)
        os.close(fd)
        p = subprocess.run([chattr, "+i", tmp], capture_output=True,
                           text=True, timeout=5)
        if p.returncode != 0:
            return {"available": False,
                    "reason": f"chattr +i 失败（容器通常缺 CAP_LINUX_IMMUTABLE）："
                              f"{(p.stderr or p.stdout).strip()[:180]}"}
        ls = subprocess.run([lsattr, "-d", tmp], capture_output=True,
                            text=True, timeout=5)
        parts = (ls.stdout or "").split()
        ok = bool(parts) and "i" in parts[0]
        return ({"available": True, "enabled": enabled("ransomware_fs_immutable"),
                 "reason": "内核 immutable 能力可用（ext4/xfs 等支持）"}
                if ok else {"available": False,
                            "reason": f"chattr +i 未生效，lsattr={ls.stdout.strip()[:80]}"})
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:180]}
    finally:
        if tmp:
            try:
                subprocess.run([chattr, "-i", tmp], capture_output=True,
                               timeout=5)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass


def apply_fs_immutable(paths: list) -> tuple:
    """对产物文件施加 chattr +i。返回 (成功清单, 失败清单[(path, err)])。"""
    ok, failed = [], []
    if not enabled("ransomware_fs_immutable"):
        return ok, failed
    chattr = shutil.which("chattr")
    if not chattr:
        return ok, [(p, "chattr 不可用") for p in paths or []]
    for p in paths or []:
        try:
            r = subprocess.run([chattr, "+i", p], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                ok.append(p)
            else:
                failed.append((p, (r.stderr or r.stdout).strip()[:180]))
        except Exception as exc:  # noqa: BLE001
            failed.append((p, str(exc)[:180]))
    return ok, failed


def release_fs_immutable(paths: list) -> tuple:
    """解锁前移除 immutable 标志（必须在 chmod 之前）。"""
    ok, failed = [], []
    chattr = shutil.which("chattr")
    if not chattr:
        return ok, failed
    for p in paths or []:
        if not os.path.isfile(p):
            continue
        try:
            r = subprocess.run([chattr, "-i", p], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                ok.append(p)
            else:
                failed.append((p, (r.stderr or "").strip()[:180]))
        except Exception as exc:  # noqa: BLE001
            failed.append((p, str(exc)[:180]))
    return ok, failed


# --------------------------------------------------------------------------
# 态势
# --------------------------------------------------------------------------

def summary() -> dict:
    _ensure_table()
    row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN COALESCE(locked,0)=1 THEN 1 ELSE 0 END) AS locked, "
        "SUM(CASE WHEN COALESCE(quarantined,0)=1 THEN 1 ELSE 0 END) AS quarantined, "
        "SUM(CASE WHEN security_level='clean' THEN 1 ELSE 0 END) AS clean, "
        "SUM(CASE WHEN security_level='suspect' THEN 1 ELSE 0 END) AS suspect, "
        "SUM(CASE WHEN security_level IS NULL OR security_level='' THEN 1 ELSE 0 END) AS unscanned, "
        "SUM(CASE WHEN checksum_ok=0 THEN 1 ELSE 0 END) AS drift, "
        "SUM(CASE WHEN COALESCE(fs_immutable,0)=1 THEN 1 ELSE 0 END) AS fs_immutable "
        "FROM backup_records WHERE status='success' AND COALESCE(is_simulated,0)=0")
    s = dict(row or {})
    for k in ("total", "locked", "quarantined", "clean", "suspect",
              "unscanned", "drift", "fs_immutable"):
        s[k] = int(s.get(k) or 0)
    s["lock_rate"] = round(s["locked"] * 100.0 / s["total"], 1) if s["total"] else 0.0
    s["trusted_points"] = len(trusted_point_ids())
    s["config"] = get_config()
    s["fs_capability"] = fs_immutable_capability()
    s["recent_events"] = events(20)
    try:
        s["suspects"] = [dict(r) for r in db.query(
            "SELECT id, task_id, db_type, status, security_level, quarantined, "
            "security_checked_at, checksum_ok, locked, backup_path "
            "FROM backup_records WHERE security_level='suspect' "
            "OR COALESCE(quarantined,0)=1 ORDER BY id DESC LIMIT 20")]
    except Exception:  # noqa: BLE001
        s["suspects"] = []
    return s
