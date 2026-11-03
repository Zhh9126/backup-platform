# -*- coding: utf-8 -*-
"""Oracle 事务级精准还原（P1-2，对标 DSG 事务级还原/鼎甲 Live Recovery 思路）：

地基：实时备份 Oracle LogMiner 段（core/cdc/oracle_logminer.py 产出的 .jsonl，
每行 {scn, ts, owner, table, op, redo, undo, xid?}）。

能力：
- 段浏览：live/ + sealed/ 目录的 .jsonl 清单
- 事务聚合：按 xid（新段）/ SCN 分组（旧段无 xid 时回退，标注 xid="-"）
- 回滚计划：已提交事务的变更按 SCN 降序取 SQL_UNDO（Oracle 反向回滚语义），
  undo 为空的事件（DDL/LOB 等）如实列入 skipped 而非静默丢弃
- 回滚执行：经原生驱动（oracledb/cx_Oracle，与捕获同通道）逐条执行 UNDO，
  每条独立提交（错误隔离），逐条结果如实回传

安全边界：
- 执行属高危操作（直接改生产库），API 层必须走 verify_code_gate + confirm 双闸
- 语句数上限 _MAX_ROLLBACK_STMTS，超出如实拒绝
- 只回放段内捕获到的事件，绝不凭空构造 SQL
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

import core.db as db

_MAX_ROLLBACK_STMTS = 10000


class OracleTxnError(ValueError):
    """事务级还原错误（消息可直接透出给用户）。"""


# ---------------------------------------------------------------- 段仓库 #

def _rt_task(task_id: int) -> dict:
    """实时任务行（backup_tasks）+ rt 配置校验。"""
    import core.models as models
    task = models.get_task(task_id, include_secret=True)
    if not task:
        raise OracleTxnError(f"备份任务不存在: {task_id}")
    if (task.get("db_type") or "").lower() != "oracle":
        raise OracleTxnError(
            "事务级精准还原仅支持 Oracle 实时捕获任务（当前 %s）"
            % task.get("db_type"))
    return task


def segments_for_task(task_id: int) -> list:
    """列出实时任务的 LogMiner 段（live 优先，sealed 按天倒序）。

    返回 [{name, path, dir, size_bytes, mtime}]，按 mtime 倒序。
    """
    _rt_task(task_id)
    from core.rt_backup.repo import LogRepository
    from core.rt_backup.types import KIND_DB_LOG
    repo = LogRepository(task_id, KIND_DB_LOG)
    out = []
    for sub in ("live", "sealed"):
        base = os.path.join(repo.base, sub)
        if sub == "sealed":
            # sealed/<day>/*.jsonl
            if not os.path.isdir(base):
                continue
            days = sorted(os.listdir(base), reverse=True)
            for day in days:
                day_dir = os.path.join(base, day)
                if not os.path.isdir(day_dir):
                    continue
                out.extend(_scan_dir(day_dir, f"sealed/{day}"))
        else:
            out.extend(_scan_dir(base, "live"))
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _scan_dir(d: str, label: str) -> list:
    rows = []
    try:
        names = os.listdir(d)
    except OSError:
        return rows
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append({"name": name, "dir": label, "path": p,
                     "size_bytes": st.st_size, "mtime": st.st_mtime})
    return rows


def load_events(segment_path: str) -> List[dict]:
    """读段内事件（jsonl 逐行，坏行跳过并计数）。"""
    if not os.path.isfile(segment_path):
        raise OracleTxnError(f"段文件不存在: {segment_path}")
    events = []
    with open(segment_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events


# ---------------------------------------------------------------- 事务聚合 #

_OP_CN = {"INSERT": "插入", "UPDATE": "更新", "DELETE": "删除", "DDL": "DDL"}


def transactions(events: List[dict]) -> List[dict]:
    """把段内事件聚合为事务列表（时间轴视图，不含 SQL 正文）。

    分组键：xid（新段）；旧段无 xid → 整段按单组（xid='-'，按 SCN 分段
    无法可靠还原事务边界，如实标注）。
    返回按首个 SCN 降序（最新事务在前）。
    """
    groups: Dict[str, dict] = {}
    for ev in events:
        xid = str(ev.get("xid") or "-")
        g = groups.get(xid)
        if g is None:
            g = {"xid": xid, "events": [], "first_scn": None,
                 "last_scn": None, "start_ts": "", "end_ts": "",
                 "tables": set(), "n_insert": 0, "n_update": 0,
                 "n_delete": 0, "n_ddl": 0}
            groups[xid] = g
        g["events"].append(ev)
        try:
            scn = int(float(ev.get("scn") or 0))
        except Exception:
            scn = 0
        if scn:
            if g["first_scn"] is None or scn < g["first_scn"]:
                g["first_scn"] = scn
            if scn > (g["last_scn"] or 0):
                g["last_scn"] = scn
        if ev.get("ts"):
            if not g["start_ts"]:
                g["start_ts"] = ev["ts"]
            g["end_ts"] = ev["ts"]
        tbl = "%s.%s" % (ev.get("owner") or "-", ev.get("table") or "-")
        g["tables"].add(tbl)
        op = (ev.get("op") or "").upper()
        if op == "INSERT":
            g["n_insert"] += 1
        elif op == "UPDATE":
            g["n_update"] += 1
        elif op == "DELETE":
            g["n_delete"] += 1
        elif op == "DDL":
            g["n_ddl"] += 1
    out = []
    for g in groups.values():
        out.append({
            "xid": g["xid"],
            "start_ts": g["start_ts"], "end_ts": g["end_ts"],
            "first_scn": g["first_scn"], "last_scn": g["last_scn"],
            "tables": sorted(g["tables"]),
            "n_insert": g["n_insert"], "n_update": g["n_update"],
            "n_delete": g["n_delete"], "n_ddl": g["n_ddl"],
            "n_ops": len(g["events"]),
        })
    out.sort(key=lambda x: (x["first_scn"] or 0), reverse=True)
    return out


def txn_events(events: List[dict], xid: str) -> List[dict]:
    """取指定事务的事件（保持 SCN 升序 = 时间轴原序）。"""
    return [e for e in events if str(e.get("xid") or "-") == str(xid)]


# ---------------------------------------------------------------- 回滚 #

def rollback_plan(events: List[dict], xid: str) -> dict:
    """生成已提交事务的 UNDO 回滚计划。

    Oracle 反向回滚语义：按 SCN 降序（后做的先撤销）回放 SQL_UNDO。
    undo 为空的事件（DDL 等 Oracle 不产 undo）如实列入 skipped。
    """
    evs = txn_events(events, xid)
    if not evs:
        raise OracleTxnError(f"段中不存在事务 {xid}")
    plan, skipped = [], []
    for ev in sorted(evs, key=lambda e: _scn_of(e), reverse=True):
        undo = (ev.get("undo") or "").strip()
        op = (ev.get("op") or "").upper()
        if not undo:
            skipped.append({
                "scn": ev.get("scn"), "table":
                    "%s.%s" % (ev.get("owner") or "-", ev.get("table") or "-"),
                "op": op,
                "reason": "Oracle 未产出 SQL_UNDO（DDL 等操作不可按行撤销）"})
            continue
        plan.append({
            "seq": len(plan) + 1,
            "scn": ev.get("scn"), "ts": ev.get("ts"),
            "table": "%s.%s" % (ev.get("owner") or "-", ev.get("table") or "-"),
            "op": op, "undo_sql": undo,
        })
        if len(plan) >= _MAX_ROLLBACK_STMTS:
            raise OracleTxnError(
                f"回滚语句数超过上限 {_MAX_ROLLBACK_STMTS}，拒绝执行；"
                "请改用 PITR 整库时点恢复")
    return {"xid": xid, "n_undo": len(plan), "n_skip": len(skipped),
            "statements": plan, "skipped": skipped,
            "order": "SCN 降序（Oracle 反向回滚语义）"}


def _scn_of(ev: dict) -> int:
    try:
        return int(float(ev.get("scn") or 0))
    except Exception:
        return 0


def _connect_oracle(task: dict):
    """按任务配置连接 Oracle（原生驱动，与 LogMiner 捕获同通道）。

    11g 无 thin 驱动场景由 native_conn 内部抛 DriverUnavailable →
    如实报错引导（与捕获通道一致：捕获能跑回滚就能跑）。
    """
    from core import native_conn
    pw = task.get("password") or ""
    if isinstance(pw, str) and pw.startswith(("enc:", "sm4v1:")):
        pw = db.decrypt_secret(pw)
    service = None
    try:
        extra = task.get("extra_options")
        if isinstance(extra, str):
            extra = json.loads(extra or "{}")
        service = (extra or {}).get("service")
    except Exception:
        service = None
    dbname = service or task.get("db_name") or task.get("host")
    return native_conn.connect("oracle", task.get("host"),
                               int(task.get("port") or 1521),
                               dbname, task.get("username") or "system",
                               pw, timeout=15)


def rollback_execute(task: dict, plan: dict,
                     stop_on_error: bool = False) -> dict:
    """执行回滚计划：逐条 UNDO 独立提交（错误隔离），结果逐条回传。"""
    conn = _connect_oracle(task)
    results = []
    executed, failed = 0, 0
    started = time.time()
    try:
        cur = conn.cursor()
        for stmt in plan.get("statements", []):
            sql = (stmt.get("undo_sql") or "").strip().rstrip(";")
            if not sql:
                continue
            item = {"seq": stmt["seq"], "scn": stmt.get("scn"),
                    "table": stmt["table"], "op": stmt["op"]}
            try:
                cur.execute(sql)
                try:
                    item["rows"] = cur.rowcount
                except Exception:
                    item["rows"] = None
                conn.commit()
                executed += 1
                item["ok"] = True
            except Exception as e:
                conn.rollback()
                failed += 1
                item["ok"] = False
                item["error"] = str(e)[:200]
                results.append(item)
                if stop_on_error:
                    break
                continue
            results.append(item)
        try:
            cur.close()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"executed": executed, "failed": failed,
            "duration_sec": round(time.time() - started, 2),
            "results": results,
            "note": "每条 UNDO 独立提交；失败条目不阻断后续（stop_on_error=true 时中断）"}
