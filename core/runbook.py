# -*- coding: utf-8 -*-
"""Runbook 恢复编排引擎（对标鼎甲 Live Recovery / 中科热备 Runbook）。

把「备份 → 恢复 → 校验 → 通知 → 切换」串成**可复用的恢复剧本**，
一键执行、RTO 计时、高危步骤审批闸、失败如实中止。

设计要点：
  - 步骤类型（v1 八类）：
      wait           等待 N 秒（等复制追平/应用重启窗口）
      run_backup     立即执行一次备份任务（scheduler.run_task_now）
      restore_latest 取任务最近一次成功备份做异名恢复（run_restore_now）
      verify_rows    直连目标实例逐表 COUNT(*) 核对行数下限（native_conn）
      notify         事件中心广播（webhook + SNMP trap，core.webhooks.emit）
      failover       容灾切换（dr_failover.run_failover，RTO 步骤透传）
      failback       容灾回切（dr_failover.run_failback）
      clone          从备份记录拉起测试克隆（clone_service）
  - 审批闸：步骤 ``require_confirm=true`` 时执行线程挂起等待
    POST /api/runbook-runs/<id>/approve 放行，超时如实失败。
  - 失败策略：步骤默认失败即中止整本剧本（continue_on_fail=true 可放行）。
  - RTO：整本剧本 start→finish 计时，逐步骤 duration 落 steps_log。
  - 并发护栏：同一剧本同时只允许一个运行实例。

只读 core.models / core.db，不自建 sqlite3 连接（与 T01/T02/T03 约定一致）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import core.db as db
import core.models as models

logger = logging.getLogger("runbook")

# ---------------------------------------------------------------- 常量
STEP_TYPES = ("wait", "run_backup", "restore_latest", "verify_rows",
              "notify", "failover", "failback", "clone")
APPROVE_TIMEOUT_SEC = 3600        # 审批等待上限（1 小时）
POLL_INTERVAL = 2.0               # 审批/停止轮询间隔
MAX_STEPS = 50

STEP_TYPE_LABELS = {
    "wait": "等待",
    "run_backup": "执行备份",
    "restore_latest": "恢复最近备份",
    "verify_rows": "行数校验",
    "notify": "事件通知",
    "failover": "容灾切换",
    "failback": "容灾回切",
    "clone": "测试克隆",
}

_RUNLOCK = threading.Lock()
_RUNNING: set = set()             # 正在执行的 runbook_id
_STOPPED_RUNS: set = set()        # 收到停止指令的 run id

TABLES_DDL = """
CREATE TABLE IF NOT EXISTS runbooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  steps TEXT DEFAULT '[]',
  enabled INTEGER DEFAULT 1,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runbook_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  runbook_id INTEGER,
  runbook_name TEXT,
  status TEXT DEFAULT 'pending',
  current_seq INTEGER DEFAULT 0,
  steps_log TEXT DEFAULT '[]',
  approved_seqs TEXT DEFAULT '[]',
  rto_sec REAL,
  operator TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT
);
"""

_TABLES_READY = False


def _ensure_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    for stmt in [s.strip() for s in TABLES_DDL.split(";") if s.strip()]:
        db.execute(stmt)
    _TABLES_READY = True


# ---------------------------------------------------------------- 序列化
def _norm_steps(steps: Any) -> List[dict]:
    """校验/规范化 steps JSON：补 seq、剔除未知类型、校验 DAG 依赖。

    ``depends_on``：依赖的 seq 列表（可为空 = 无依赖）。配置后执行器按拓扑序
    推进；全部为空则退化为原「按 seq 顺序执行」，零行为变化。
    依赖非法（引用不存在/自依赖/成环）直接拒绝，避免运行到一半才炸。
    """
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            raise ValueError("steps 不是合法 JSON")
    if not isinstance(steps, list):
        raise ValueError("steps 必须是数组")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"步骤数超上限（最多 {MAX_STEPS}）")
    out = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise ValueError(f"第 {i} 个步骤必须是对象")
        stype = str(s.get("type") or "").strip()
        if stype not in STEP_TYPES:
            raise ValueError(f"第 {i} 个步骤类型不支持: {stype}（支持: "
                             f"{', '.join(STEP_TYPES)}）")
        raw_deps = s.get("depends_on") or []
        if isinstance(raw_deps, (int, str)):
            raw_deps = [raw_deps]
        deps = []
        for d in raw_deps:
            try:
                di = int(d)
            except (TypeError, ValueError):
                raise ValueError(f"第 {i} 个步骤的依赖项非法: {d}")
            if di not in deps:
                deps.append(di)
        out.append({
            "seq": i,
            "type": stype,
            "name": str(s.get("name") or STEP_TYPE_LABELS.get(stype, stype)),
            "params": s.get("params") if isinstance(s.get("params"), dict) else {},
            "require_confirm": bool(s.get("require_confirm")),
            "continue_on_fail": bool(s.get("continue_on_fail")),
            "depends_on": sorted(deps),
        })
    _validate_dag(out)
    return out


def _validate_dag(steps: List[dict]) -> None:
    """依赖校验：自依赖、引用不存在、成环。"""
    seqs = {s["seq"] for s in steps}
    for s in steps:
        for d in s.get("depends_on") or []:
            if d == s["seq"]:
                raise ValueError(f"步骤 {s['seq']}「{s['name']}」不能依赖自己")
            if d not in seqs:
                raise ValueError(f"步骤 {s['seq']}「{s['name']}」依赖的步骤 {d} 不存在")
    # Kahn 拓扑排序检测环
    indeg = {s["seq"]: len(s.get("depends_on") or []) for s in steps}
    children = {s["seq"]: [] for s in steps}
    for s in steps:
        for d in s.get("depends_on") or []:
            children[d].append(s["seq"])
    queue = [q for q, n in indeg.items() if n == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for ch in children[cur]:
            indeg[ch] -= 1
            if indeg[ch] == 0:
                queue.append(ch)
    if seen != len(steps):
        raise ValueError("步骤依赖存在环（循环依赖），请检查 depends_on")


def topo_order(steps: List[dict]) -> List[dict]:
    """按依赖生成执行顺序（同层按 seq 升序，结果稳定）。"""
    by_seq = {s["seq"]: s for s in steps}
    indeg = {s["seq"]: len(s.get("depends_on") or []) for s in steps}
    children = {s["seq"]: [] for s in steps}
    for s in steps:
        for d in s.get("depends_on") or []:
            if d in children:
                children[d].append(s["seq"])
    ready = sorted([q for q, n in indeg.items() if n == 0])
    order = []
    while ready:
        cur = ready.pop(0)
        order.append(by_seq[cur])
        for ch in sorted(children[cur]):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                ready.append(ch)
        ready.sort()
    return order


def has_dag(steps: List[dict]) -> bool:
    """是否配置了依赖（否则走原顺序执行路径）。"""
    return any(s.get("depends_on") for s in steps or [])


def _row_to_dict(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    row = dict(row)
    for key in ("steps", "steps_log", "approved_seqs"):
        if key in row and isinstance(row[key], str):
            try:
                row[key] = json.loads(row[key] or "[]")
            except Exception:
                row[key] = []
    return row


# ---------------------------------------------------------------- CRUD
def list_runbooks() -> List[dict]:
    _ensure_tables()
    rows = db.query("SELECT * FROM runbooks ORDER BY id DESC")
    return [_row_to_dict(r) for r in rows]


def get_runbook(runbook_id: int) -> Optional[dict]:
    _ensure_tables()
    return _row_to_dict(db.query_one(
        "SELECT * FROM runbooks WHERE id=?", (int(runbook_id),)))


def create_runbook(data: dict, operator: str = "") -> dict:
    _ensure_tables()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("剧本名称不能为空")
    steps = _norm_steps(data.get("steps"))
    rid = db.execute(
        "INSERT INTO runbooks(name, description, steps, enabled, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (name, str(data.get("description") or ""), json.dumps(steps, ensure_ascii=False),
         1 if data.get("enabled", 1) else 0, db.now_iso(), db.now_iso()))
    db.add_log("info", "runbook", f"创建恢复剧本 #{rid}「{name}」（{len(steps)} 步骤，"
               f"操作人 {operator or '-'}）")
    return get_runbook(rid)


def update_runbook(runbook_id: int, data: dict) -> Optional[dict]:
    _ensure_tables()
    old = get_runbook(runbook_id)
    if not old:
        raise ValueError(f"剧本 {runbook_id} 不存在")
    name = str(data.get("name") or old["name"]).strip() or old["name"]
    steps = _norm_steps(data.get("steps")) if "steps" in data else old["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps)
    db.execute(
        "UPDATE runbooks SET name=?, description=?, steps=?, enabled=?, updated_at=? WHERE id=?",
        (name, str(data.get("description") if "description" in data else old["description"]),
         json.dumps(steps, ensure_ascii=False),
         1 if data.get("enabled", old.get("enabled", 1)) else 0,
         db.now_iso(), int(runbook_id)))
    return get_runbook(runbook_id)


def delete_runbook(runbook_id: int) -> bool:
    _ensure_tables()
    old = get_runbook(runbook_id)
    if not old:
        return False
    _ensure_tables()
    db.execute("DELETE FROM runbooks WHERE id=?", (int(runbook_id),))
    db.execute("DELETE FROM runbook_runs WHERE runbook_id=?", (int(runbook_id),))
    db.add_log("info", "runbook", f"删除恢复剧本 #{runbook_id}「{old['name']}」")
    return True


# ---------------------------------------------------------------- 运行记录
def list_runs(runbook_id: int = None, limit: int = 100) -> List[dict]:
    _ensure_tables()
    if runbook_id:
        rows = db.query("SELECT * FROM runbook_runs WHERE runbook_id=? "
                        "ORDER BY id DESC LIMIT ?", (int(runbook_id), int(limit)))
    else:
        rows = db.query("SELECT * FROM runbook_runs ORDER BY id DESC LIMIT ?",
                        (int(limit),))
    return [_row_to_dict(r) for r in rows]


def get_run(run_id: int) -> Optional[dict]:
    _ensure_tables()
    return _row_to_dict(db.query_one(
        "SELECT * FROM runbook_runs WHERE id=?", (int(run_id),)))


def _update_run(run_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE runbook_runs SET {sets} WHERE id=?",
               tuple(fields.values()) + (int(run_id),))


# ---------------------------------------------------------------- 执行引擎
def _emit_notify(event: str, data: dict) -> None:
    """事件中心广播（webhook + SNMP trap），失败不影响主流程。"""
    try:
        from core import webhooks
        webhooks.emit(event, data)
    except Exception as e:  # noqa: BLE001
        logger.warning("runbook 事件广播失败: %s", e)


def _latest_success_record(task_id: int) -> Optional[dict]:
    rows = db.query(
        "SELECT id FROM backup_records WHERE task_id=? AND status='success' "
        "ORDER BY id DESC LIMIT 1", (int(task_id),))
    return rows[0] if rows else None


def _step_run_backup(params: dict, operator: str) -> dict:
    from core import scheduler
    task_id = int(params.get("task_id") or 0)
    if task_id <= 0:
        raise ValueError("run_backup 需要 task_id")
    rec = scheduler.run_task_now(task_id, operator=operator)
    if rec is None:
        raise ValueError(f"任务 {task_id} 不存在或未执行")
    if rec.get("accepted"):
        return {"message": f"任务 {task_id} 已提交异步执行", "accepted": True,
                "record_id": rec.get("id")}
    status = str(rec.get("status") or "").lower()
    if status != "success":
        raise ValueError(f"备份未成功: {str(rec.get('message') or status)[:200]}")
    return {"message": f"备份成功（记录 #{rec.get('id')}，"
                       f"{rec.get('size_human') or rec.get('size') or ''}）",
            "record_id": rec.get("id")}


def _step_restore_latest(params: dict, operator: str) -> dict:
    from core import scheduler
    task_id = int(params.get("task_id") or 0)
    if task_id <= 0:
        raise ValueError("restore_latest 需要 task_id")
    rec = _latest_success_record(task_id)
    if not rec:
        raise ValueError(f"任务 {task_id} 没有成功备份记录可恢复")
    kwargs: Dict[str, Any] = {
        "target_db": str(params.get("target_db") or ""),
        "operator": operator or "runbook",
    }
    if params.get("target_host_id"):
        kwargs["target_host_id"] = int(params["target_host_id"])
    if params.get("target_host"):
        kwargs["target_host"] = str(params["target_host"])
    rrec = scheduler.run_restore_now(rec["id"], **kwargs)
    if rrec is None:
        raise ValueError(f"恢复记录 #{rec['id']} 未执行")
    status = str(rrec.get("status") or "").lower()
    if status != "success":
        raise ValueError(f"恢复未成功: {str(rrec.get('message') or status)[:200]}")
    return {"message": f"备份记录 #{rec['id']} 已恢复到"
                       f"{params.get('target_db') or '目标库'}",
            "restore_record_id": rrec.get("id")}


def _step_verify_rows(params: dict, operator: str) -> dict:
    task_id = int(params.get("task_id") or 0)
    if task_id <= 0:
        raise ValueError("verify_rows 需要 task_id")
    task = models.get_task(task_id, include_secret=True)
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    host = str(params.get("host") or task.get("host") or "127.0.0.1")
    port = int(params.get("port") or task.get("port") or 0)
    user = str(params.get("user") or task.get("username") or "")
    password = str(params.get("password") or task.get("password") or "")
    dbname = str(params.get("db") or params.get("target_db") or task.get("db_name") or "")
    tables = params.get("tables") or []
    if isinstance(tables, str):
        tables = [t.strip() for t in tables.split(",") if t.strip()]
    if not tables:
        raise ValueError("verify_rows 需要至少一张表")
    min_rows = int(params.get("min_rows") or 1)

    from core import native_conn
    conn = native_conn.connect(task.get("db_type"), host, port, dbname,
                               user, password, timeout=15)
    details = []
    try:
        cur = conn.cursor()
        for t in tables:
            safe = str(t).replace("`", "").replace(";", "")
            cur.execute(f"SELECT COUNT(*) FROM `{safe}`")
            n = int(cur.fetchone()[0])
            ok = n >= min_rows
            details.append({"table": safe, "rows": n, "ok": ok})
            if not ok:
                raise ValueError(f"表 {safe} 行数 {n} < 预期下限 {min_rows}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"message": "行数校验通过: " + ", ".join(
        f"{d['table']}={d['rows']}" for d in details), "tables": details}


def _step_notify(params: dict, operator: str) -> dict:
    title = str(params.get("title") or "Runbook 通知")
    message = str(params.get("message") or "")
    _emit_notify("runbook.notify", {
        "message": f"{title}: {message}"[:300],
        "severity": str(params.get("severity") or "info"),
        "operator": operator,
    })
    return {"message": f"通知已广播: {title}"}


def _step_failover(params: dict, operator: str) -> dict:
    from core import dr_failover
    link_id = int(params.get("link_id") or 0)
    if link_id <= 0:
        raise ValueError("failover 需要 link_id")
    res = dr_failover.run_failover(link_id, force=bool(params.get("force")))
    rto = (res or {}).get("rto_sec")
    return {"message": f"容灾切换完成（RTO {rto}s）" if rto else "容灾切换完成",
            "rto_sec": rto, "steps": (res or {}).get("steps")}


def _step_failback(params: dict, operator: str) -> dict:
    from core import dr_failover
    link_id = int(params.get("link_id") or 0)
    if link_id <= 0:
        raise ValueError("failback 需要 link_id")
    res = dr_failover.run_failback(link_id)
    rto = (res or {}).get("rto_sec")
    return {"message": f"容灾回切完成（RTO {rto}s）" if rto else "容灾回切完成",
            "rto_sec": rto, "steps": (res or {}).get("steps")}


def _step_clone(params: dict, operator: str) -> dict:
    from core import clone_service
    record_id = int(params.get("record_id") or 0)
    if record_id <= 0:
        raise ValueError("clone 需要 record_id")
    svc = clone_service.CloneService()
    req = svc.request_clone(
        record_id,
        target_env=str(params.get("target_env") or "runbook"),
        requested_by=operator or "runbook",
        note="Runbook 编排克隆",
        purpose=str(params.get("purpose") or "test"))
    if not (req or {}).get("id"):
        raise ValueError(str((req or {}).get("message") or "克隆申请失败"))
    # 等待拉起（最多 60s 轮询状态）
    deadline = time.time() + 60
    rid = int(req["id"])
    status = str(req.get("status") or "pending")
    while time.time() < deadline and status in ("pending", "creating"):
        time.sleep(2)
        cur = db.query_one("SELECT status, note FROM clone_requests WHERE id=?", (rid,))
        status = str((cur or {}).get("status") or status)
    if status != "ready":
        raise ValueError(f"克隆 #{rid} 未就绪（status={status}）: "
                         f"{str((cur or {}).get('note') or '')[:150]}")
    return {"message": f"测试克隆 #{rid} 已就绪", "clone_id": rid}


_STEP_HANDLERS = {
    "run_backup": _step_run_backup,
    "restore_latest": _step_restore_latest,
    "verify_rows": _step_verify_rows,
    "notify": _step_notify,
    "failover": _step_failover,
    "failback": _step_failback,
    "clone": _step_clone,
}


def _wait_for_approval(run_id: int, seq: int, deadline: float) -> str:
    """等待审批放行/停止/超时。返回 'approved'|'stopped'|'timeout'。"""
    while time.time() < deadline:
        row = get_run(run_id)
        if not row or row.get("status") == "stopped" or run_id in _STOPPED_RUNS:
            return "stopped"
        if seq in (row.get("approved_seqs") or []):
            return "approved"
        time.sleep(POLL_INTERVAL)
    return "timeout"


def _check_stopped(run_id: int) -> bool:
    if run_id in _STOPPED_RUNS:
        return True
    row = db.query_one("SELECT status FROM runbook_runs WHERE id=?", (int(run_id),))
    return bool(row and row.get("status") == "stopped")


def _execute_run(run_id: int, runbook_id: int, steps: List[dict],
                 operator: str) -> None:
    """执行线程主体：逐步骤跑，RTO 计时，审批闸与停止检查。"""
    t0 = time.monotonic()
    started_iso = db.now_iso()
    _update_run(run_id, status="running", started_at=started_iso,
                current_seq=0, steps_log=json.dumps([], ensure_ascii=False))
    db.add_log("info", "runbook", f"剧本 #{runbook_id} 开始执行（run #{run_id}，"
               f"操作人 {operator or '-'}）")
    _emit_notify("runbook.start", {"message": f"恢复剧本 run #{run_id} 开始执行",
                                   "severity": "info"})
    log: List[dict] = []
    final_status = "success"
    error = ""
    try:
        # DAG 模式：配了 depends_on 才启用（否则保持原顺序执行，零行为变化）
        _use_dag = has_dag(steps)
        _exec_steps = topo_order(steps) if _use_dag else steps
        _dep_status = {}          # seq -> 实际终态（ok/fail_cont/skipped/fail）
        _dag_failed = False
        for step in _exec_steps:
            seq, stype = step["seq"], step["type"]
            if _use_dag:
                _bad = [d for d in (step.get("depends_on") or [])
                        if _dep_status.get(d) not in ("ok", "fail_cont")]
                if _bad:
                    _entry = {"seq": seq, "type": stype, "name": step["name"],
                              "status": "skipped",
                              "message": "上游步骤 %s 未成功，已跳过" % _bad,
                              "duration_sec": 0.0, "at": db.now_iso()}
                    log.append(_entry)
                    _dep_status[seq] = "skipped"
                    _update_run(run_id,
                                steps_log=json.dumps(log, ensure_ascii=False))
                    continue
            if _check_stopped(run_id):
                final_status = "stopped"
                error = "执行已被人工停止"
                break
            # ---- 审批闸 ----
            if step.get("require_confirm"):
                _update_run(run_id, status="waiting_approval", current_seq=seq)
                verdict = _wait_for_approval(run_id, seq,
                                             time.time() + APPROVE_TIMEOUT_SEC)
                _update_run(run_id, status="running")
                if verdict == "stopped":
                    final_status = "stopped"
                    error = f"步骤 {seq} 等待期间被人工停止"
                    break
                if verdict == "timeout":
                    entry = {"seq": seq, "type": stype, "name": step["name"],
                             "status": "timeout", "message": "审批等待超时（1 小时）",
                             "duration_sec": 0, "at": db.now_iso()}
                    log.append(entry)
                    _update_run(run_id, steps_log=json.dumps(log, ensure_ascii=False))
                    final_status = "failed"
                    error = f"步骤 {seq}「{step['name']}」审批等待超时"
                    break
            # ---- 执行 ----
            _update_run(run_id, current_seq=seq)
            s0 = time.monotonic()
            entry = {"seq": seq, "type": stype, "name": step["name"],
                     "status": "ok", "message": "", "duration_sec": 0.0,
                     "at": db.now_iso()}
            try:
                if stype == "wait":
                    secs = min(float(step["params"].get("seconds") or 0), 600)
                    time.sleep(max(secs, 0))
                    entry["message"] = f"等待 {secs}s"
                else:
                    handler = _STEP_HANDLERS[stype]
                    result = handler(step["params"], operator)
                    entry["message"] = str((result or {}).get("message") or "完成")
            except Exception as e:  # noqa: BLE001
                entry["status"] = "fail"
                entry["message"] = str(e)[:300]
            entry["duration_sec"] = round(time.monotonic() - s0, 3)
            log.append(entry)
            _update_run(run_id, steps_log=json.dumps(log, ensure_ascii=False))
            _dep_status[seq] = entry["status"]
            if entry["status"] == "fail":
                if step.get("continue_on_fail"):
                    entry["status"] = "fail_cont"
                    _dep_status[seq] = "fail_cont"
                    continue
                if _use_dag:
                    # DAG：记录失败但继续推进其它分支（依赖它的步骤会被 skipped）
                    _dag_failed = True
                    if not error:
                        error = (f"步骤 {seq}「{step['name']}」失败: "
                                 f"{entry['message'][:200]}")
                    continue
                final_status = "failed"
                error = f"步骤 {seq}「{step['name']}」失败: {entry['message'][:200]}"
                break
        if _use_dag and _dag_failed and final_status == "success":
            final_status = "failed"
        # ---- 收尾 ----
        rto = round(time.monotonic() - t0, 3)
        _update_run(run_id, status=final_status, rto_sec=rto,
                    error=error, finished_at=db.now_iso(),
                    steps_log=json.dumps(log, ensure_ascii=False))
        _verdict = dict(success="执行成功", failed="执行失败",
                        stopped="已停止").get(final_status, final_status)
        _emit_notify("runbook.finish" if final_status == "success"
                     else "runbook.fail", {
            "message": (f"恢复剧本 run #{run_id} {_verdict}"
                        f"（RTO {rto}s）{'：' + error if error else ''}"),
            "severity": "info" if final_status == "success" else "critical",
            "rto_sec": rto, "run_id": run_id,
        })
        db.add_log("info" if final_status == "success" else "error", "runbook",
                   f"剧本 #{runbook_id} 执行{_verdict}，RTO {rto}s"
                   f"{'：' + error if error else ''}")
    except Exception as exc:  # noqa: BLE001
        rto = round(time.monotonic() - t0, 3)
        _update_run(run_id, status="failed", rto_sec=rto,
                    error=str(exc)[:300], finished_at=db.now_iso())
        logger.exception("runbook 执行异常")
    finally:
        with _RUNLOCK:
            _RUNNING.discard(runbook_id)
            _STOPPED_RUNS.discard(run_id)


def run_runbook(runbook_id: int, operator: str = "") -> dict:
    """启动一次剧本执行（后台线程），返回 run 记录。"""
    _ensure_tables()
    rb = get_runbook(runbook_id)
    if not rb:
        raise ValueError(f"剧本 {runbook_id} 不存在")
    steps = rb.get("steps") or []
    if not steps:
        raise ValueError("剧本没有步骤")
    if not rb.get("enabled"):
        raise ValueError("剧本已停用")
    with _RUNLOCK:
        if runbook_id in _RUNNING:
            raise ValueError("该剧本正在执行中，请等待完成或先停止")
        _RUNNING.add(runbook_id)
    run_id = db.execute(
        "INSERT INTO runbook_runs(runbook_id, runbook_name, status, operator, created_at)"
        " VALUES(?,?,?,?,?)",
        (int(runbook_id), rb["name"], "pending", operator or "", db.now_iso()))
    th = threading.Thread(target=_execute_run,
                          args=(run_id, int(runbook_id), steps, operator or ""),
                          name=f"runbook-run-{run_id}", daemon=True)
    th.start()
    return get_run(run_id)


def approve_run(run_id: int, seq: int, operator: str = "") -> dict:
    """审批放行某步骤（幂等）。"""
    _ensure_tables()
    run = get_run(run_id)
    if not run:
        raise ValueError(f"运行记录 {run_id} 不存在")
    seq = int(seq)
    if seq < 1:
        raise ValueError("seq 非法")
    approved = set(run.get("approved_seqs") or [])
    approved.add(seq)
    _update_run(run_id, approved_seqs=json.dumps(sorted(approved)))
    db.add_log("info", "runbook",
               f"run #{run_id} 步骤 {seq} 审批放行（操作人 {operator or '-'}）")
    return get_run(run_id)


def stop_run(run_id: int, operator: str = "") -> dict:
    """停止一次运行（当前步骤跑完后中止）。"""
    _ensure_tables()
    run = get_run(run_id)
    if not run:
        raise ValueError(f"运行记录 {run_id} 不存在")
    if run.get("status") in ("success", "failed", "stopped"):
        return run
    _STOPPED_RUNS.add(int(run_id))
    _update_run(run_id, status="stopped",
                error=run.get("error") or "人工停止",
                finished_at=db.now_iso())
    db.add_log("warning", "runbook", f"run #{run_id} 已被人工停止"
               f"（操作人 {operator or '-'}）")
    return get_run(run_id)
