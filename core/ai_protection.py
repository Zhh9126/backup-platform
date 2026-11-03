# -*- coding: utf-8 -*-
"""AI 工作负载保护计划（跨层一致性组）。

对标「AI 备份七层状态模型」：一个能运行的大模型服务协调着多层相互依赖的
持久化状态——模型权重/检查点、训练数据集、向量索引、关系型元数据与对象存储、
编排配置、版本指针与血缘、Agent 状态。任何一层单独恢复而其他层不同步，
系统都可能"看似启动、实则输出错误"。

核心能力：把多层资产编入**同一个保护计划**，一次运行按序驱动组内各成员
（复用既有备份任务：文件任务保权重/数据集、向量库任务保索引、数据库任务保
元数据、对象存储任务保原始文档），产出**组恢复点报告**：

    epoch = 组内所有成员最近成功备份时间的最小值（跨层一致的公共恢复点）
    drift = 成员备份时间最大偏差

drift 超过阈值（max_drift_sec，默认 300s）→ 如实告警：该恢复点上各层状态
可能不一致（例如向量索引快照在前、关系库在后，外键/grounding 可能断裂）。

诚实边界：
  - 本编排提供"同窗口协调 + 公共恢复点识别"，不承诺存储层原子快照
    （那需要文件系统/存储控制器级一致性组能力，离线零 agent 架构不做）；
  - 时间偏差小于阈值不等于业务级一致（如向导索引与关系库的写入顺序），
    关键场景应配合停写窗口。
"""
import json
import time
from typing import Optional

import core.db as db

PLAN_TABLE = """CREATE TABLE IF NOT EXISTS ai_protection_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    members TEXT DEFAULT '[]',
    max_drift_sec INTEGER DEFAULT 300,
    cron TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_result TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
)"""

# 七层状态模型（前端展示/成员分层用）
LAYERS = [
    ("weights", "模型权重与检查点"),
    ("dataset", "训练与微调数据集"),
    ("vector_index", "向量索引"),
    ("rdb_metadata", "关系型元数据与对象存储"),
    ("orchestration", "编排与运行时配置"),
    ("lineage", "版本指针与数据血缘"),
    ("agent_state", "Agent 执行状态与工具配置"),
]
_LAYER_CN = dict(LAYERS)
_VALID_LAYERS = set(_LAYER_CN)


class PlanError(ValueError):
    pass


def _now():
    return db.now_iso()


def init_schema():
    db.execute(PLAN_TABLE)
    try:
        db.execute("ALTER TABLE ai_protection_plans ADD COLUMN cron TEXT DEFAULT ''")
    except Exception:
        pass


def _normalize_members(members) -> list:
    if isinstance(members, str):
        try:
            members = json.loads(members or "[]")
        except Exception:
            raise PlanError("members 不是合法 JSON")
    if not isinstance(members, list) or not members:
        raise PlanError("保护计划至少需要一个成员资产")
    out = []
    seen = set()
    for i, m in enumerate(members, 1):
        if not isinstance(m, dict) or not m.get("task_id"):
            raise PlanError(f"成员 {i} 缺少 task_id")
        tid = int(m["task_id"])
        if tid in seen:
            continue
        seen.add(tid)
        row = db.query_one(
            "SELECT id, name, db_type FROM backup_tasks WHERE id=?", (tid,))
        if not row:
            raise PlanError(f"成员 {i} 的任务不存在: #{tid}")
        layer = m.get("layer") if m.get("layer") in _VALID_LAYERS else "rdb_metadata"
        out.append({"task_id": tid, "layer": layer,
                    "task_name": row["name"], "db_type": row["db_type"]})
    return out


def list_plans() -> list:
    rows = db.query("SELECT * FROM ai_protection_plans ORDER BY id DESC") or []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["members"] = json.loads(d.get("members") or "[]")
        except Exception:
            d["members"] = []
        try:
            d["last_result"] = json.loads(d.get("last_result") or "") if d.get("last_result") else None
        except Exception:
            d["last_result"] = None
        out.append(d)
    return out


def get_plan(plan_id: int) -> Optional[dict]:
    for p in list_plans():
        if p["id"] == plan_id:
            return p
    return None


def create_plan(data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise PlanError("计划名称必填")
    members = _normalize_members(data.get("members"))
    try:
        drift = int(data.get("max_drift_sec") or 300)
    except (TypeError, ValueError):
        drift = 300
    now = _now()
    return db.execute(
        "INSERT INTO ai_protection_plans(name, members, max_drift_sec, cron, "
        "enabled, remark, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, json.dumps(members, ensure_ascii=False),
         max(30, drift), (data.get("cron") or "").strip(),
         1 if data.get("enabled", True) else 0,
         data.get("remark") or "", now, now))


def update_plan(plan_id: int, data: dict) -> bool:
    if not get_plan(plan_id):
        raise PlanError(f"保护计划不存在: #{plan_id}")
    sets, params = [], []
    if (data.get("name") or "").strip():
        sets.append("name=?"); params.append(data["name"].strip())
    if "members" in data:
        sets.append("members=?")
        params.append(json.dumps(_normalize_members(data["members"]),
                                 ensure_ascii=False))
    if "max_drift_sec" in data:
        try:
            sets.append("max_drift_sec=?")
            params.append(max(30, int(data["max_drift_sec"] or 300)))
        except (TypeError, ValueError):
            pass
    if "cron" in data:
        sets.append("cron=?"); params.append((data.get("cron") or "").strip())
    if "enabled" in data:
        sets.append("enabled=?")
        params.append(1 if data.get("enabled") else 0)
    if "remark" in data:
        sets.append("remark=?"); params.append(data.get("remark") or "")
    if not sets:
        return False
    sets.append("updated_at=?"); params.append(_now())
    params.append(plan_id)
    db.execute("UPDATE ai_protection_plans SET " + ",".join(sets) + " WHERE id=?",
               params)
    return True


def delete_plan(plan_id: int) -> bool:
    if not get_plan(plan_id):
        return False
    db.execute("DELETE FROM ai_protection_plans WHERE id=?", (plan_id,))
    return True


def _member_record_at(task_id: int) -> tuple:
    """成员最近一条成功记录：(record_id, 完成时间 ISO)。无则 (None, None)。"""
    row = db.query_one(
        "SELECT id, started_at, backup_path FROM backup_records "
        "WHERE task_id=? AND status='success' ORDER BY id DESC LIMIT 1",
        (task_id,))
    if not row:
        return None, None
    return row["id"], row["started_at"]


def run_plan(plan_id: int, operator: str = "") -> dict:
    """运行保护计划：按序驱动成员任务备份 → 汇总组恢复点报告。

    成员任务执行复用 run_task_now（同步等待，保证组内备份窗口受控）；
    单个成员失败不中断后续（报告如实标注），但组恢复点 epoch 以成功成员计，
    失败成员会导致 ok=False。
    """
    plan = get_plan(plan_id)
    if not plan:
        raise PlanError(f"保护计划不存在: #{plan_id}")
    from core import scheduler as sched
    started = time.time()
    results = []
    for m in plan["members"]:
        tid = m["task_id"]
        try:
            rec = sched.run_task_now(tid, operator=operator or "ai-plan")
            rec_id = rec.get("id") if isinstance(rec, dict) else None
            status = rec.get("status") if isinstance(rec, dict) else "failed"
            at = rec.get("started_at") if isinstance(rec, dict) else None
            msg = rec.get("message", "") if isinstance(rec, dict) else ""
            # run_task_now 可能异步返回 accepted——等终态或超时
            if isinstance(rec, dict) and status in ("running", "accepted"):
                rec_id, status, at, msg = _wait_record(tid, rec_id)
            # 状态兜底：取该任务最近记录
            if not rec_id:
                rec_id, at = _member_record_at(tid)
        except Exception as e:
            rec_id, at = _member_record_at(tid)
            status, msg = "error", str(e)[:200]
        results.append({"task_id": tid, "task_name": m.get("task_name"),
                        "layer": m.get("layer"), "layer_cn": _LAYER_CN.get(m.get("layer"), ""),
                        "record_id": rec_id, "status": status,
                        "at": at, "message": msg[:200]})

    ats = sorted([r["at"] for r in results if r["at"]])
    epoch = ats[0] if ats else None
    drift = 0
    if len(ats) > 1:
        try:
            from datetime import datetime
            times = [datetime.fromisoformat(a.replace("Z", "").split(".")[0])
                     for a in ats]
            drift = int((max(times) - min(times)).total_seconds())
        except Exception:
            drift = -1
    max_drift = int(plan.get("max_drift_sec") or 300)
    ok = all(r["status"] == "success" for r in results) and len(results) > 0
    drift_ok = drift <= max_drift
    report = {
        "ok": ok, "epoch": epoch, "drift_sec": drift, "drift_ok": drift_ok,
        "max_drift_sec": max_drift,
        "consistency": ("跨层一致" if (ok and drift_ok)
                        else ("偏差超阈值" if ok else "存在失败成员")),
        "members": results,
        "duration_sec": round(time.time() - started, 1),
        "run_at": _now(),
    }
    db.execute("UPDATE ai_protection_plans SET last_result=?, updated_at=? WHERE id=?",
               (json.dumps(report, ensure_ascii=False), _now(), plan_id))
    db.add_log("warning" if not (ok and drift_ok) else "info", "ai_plan",
               f"AI 保护计划「{plan['name']}」运行完成：{report['consistency']}"
               f"（epoch={epoch or '-'}，偏差 {drift}s / 阈值 {max_drift}s）")
    return report


def _wait_record(task_id: int, record_id, timeout: int = 1800,
                 poll: int = 3) -> tuple:
    """轮询记录至终态（run_task_now 异步 accepted 场景）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.query_one(
            "SELECT id, status, started_at, message FROM backup_records "
            "WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
        if row and row["status"] not in ("running",):
            return row["id"], row["status"], row["started_at"], row["message"] or ""
        time.sleep(poll)
    return record_id, "timeout", None, "等待成员任务终态超时"


def list_enabled_plans() -> list:
    return [p for p in list_plans() if p.get("enabled")]
