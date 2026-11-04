# -*- coding: utf-8 -*-
"""数据库日常监控（对标 DSG/鼎甲「自定义 SQL 监控指标 + 图表化」）。

用户为任意已纳管实例定义自定义监控 SQL（如 MySQL 的
``SHOW GLOBAL STATUS LIKE 'Threads_connected'``、PG 的
``SELECT count(*) FROM pg_stat_activity``、Oracle 的
``SELECT count(*) FROM v$session``），平台按 interval_min 周期经原生
Python 驱动直连执行，取第一行第一列作为指标值落时间序列
（db_monitor_points），前端 /monitor 图表化展示。

诚实边界：
- 仅取结果集第一行第一列（标量指标）；多行结果只记第一行。
- 数值可解析时存 value_num（用于折线图），否则存 value_text。
- 采集失败如实记 last_error，不伪造数据点。
"""
import json
import time
import traceback
from datetime import datetime

from core import db


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_metrics(enabled_only: bool = False) -> list:
    sql = ("SELECT id, name, db_type, host, port, db_name, username, sql_text, "
           "interval_min, unit, enabled, last_run_at, last_value, last_error, "
           "created_at FROM db_monitor_metrics")
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    rows = db.query(sql)
    for r in rows:
        r["has_password"] = bool(r.get("username"))
    return rows


def get_metric(mid: int) -> dict:
    rows = db.query(
        "SELECT * FROM db_monitor_metrics WHERE id=?", (mid,))
    return rows[0] if rows else None


def create_metric(data: dict) -> int:
    pw = data.get("password") or ""
    return db.execute(
        "INSERT INTO db_monitor_metrics (name, db_type, host, port, db_name, "
        "username, password_enc, sql_text, interval_min, unit, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (data.get("name") or "未命名指标", data.get("db_type") or "mysql",
         data.get("host") or "127.0.0.1", int(data.get("port") or 0),
         data.get("db_name") or "", data.get("username") or "",
         db.encrypt_secret(pw) if pw else "",
         data.get("sql_text") or "", int(data.get("interval_min") or 5),
         data.get("unit") or "", 1 if data.get("enabled", 1) else 0, _now()))


def update_metric(mid: int, data: dict) -> bool:
    cur = get_metric(mid)
    if not cur:
        return False
    sets, args = [], []
    for k in ("name", "db_type", "host", "db_name", "username",
              "sql_text", "unit"):
        if k in data:
            sets.append("%s=?" % k)
            args.append(data.get(k) or "")
    if "port" in data:
        sets.append("port=?")
        args.append(int(data.get("port") or 0))
    if "interval_min" in data:
        sets.append("interval_min=?")
        args.append(max(1, int(data.get("interval_min") or 5)))
    if "enabled" in data:
        sets.append("enabled=?")
        args.append(1 if data.get("enabled") else 0)
    if data.get("password"):
        sets.append("password_enc=?")
        args.append(db.encrypt_secret(data.get("password")))
    if not sets:
        return True
    args.append(mid)
    db.execute("UPDATE db_monitor_metrics SET " + ",".join(sets) + " WHERE id=?",
               tuple(args))
    return True


def delete_metric(mid: int) -> bool:
    db.execute("DELETE FROM db_monitor_points WHERE metric_id=?", (mid,))
    db.execute("DELETE FROM db_monitor_metrics WHERE id=?", (mid,))
    return True


# ---------------------------------------------------------------------------
# 采集执行
# ---------------------------------------------------------------------------

def _decrypt(pw_enc: str) -> str:
    if not pw_enc:
        return ""
    try:
        return db.decrypt_secret(pw_enc) or ""
    except Exception:
        return pw_enc if pw_enc.startswith("sm4v1:") is False else ""


def _first_value(metric: dict):
    """执行监控 SQL，返回 (value_num, value_text)。仅取第一行第一列。"""
    from core import native_conn
    conn = native_conn.connect(
        metric.get("db_type"), metric.get("host"),
        int(metric.get("port") or 0), metric.get("db_name") or "",
        metric.get("username") or "", _decrypt(metric.get("password_enc") or ""),
        timeout=30)
    try:
        cur = conn.cursor()
        try:
            cur.execute(metric.get("sql_text") or "")
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row or row[0] is None:
        raise ValueError("SQL 未返回数据（需要第一行第一列为指标值）")
    # 取值策略：单列直接取；多列时优先取第一个可解析为数值的列
    # （兼容 SHOW GLOBAL STATUS LIKE 'x' → (Variable_name, Value) 两列形态），
    # 全非数值则取最后一列文本。
    vals = list(row)
    if len(vals) == 1:
        v = vals[0]
    else:
        v = None
        for cand in vals[1:] if len(vals) > 1 else vals:
            try:
                float(cand)
                v = cand
                break
            except (TypeError, ValueError):
                continue
        if v is None:
            v = vals[-1]
    try:
        return float(v), None
    except (TypeError, ValueError):
        return None, str(v)


def run_metric(mid: int, keep: int = 2000) -> dict:
    """立即执行一次采集并落点。返回 {ok, value, error}。"""
    metric = get_metric(mid)
    if not metric:
        return {"ok": False, "error": "指标不存在"}
    ts = _now()
    try:
        num, text = _first_value(metric)
        db.execute(
            "INSERT INTO db_monitor_points (metric_id, ts, value_num, value_text) "
            "VALUES (?,?,?,?)", (mid, ts, num, text))
        db.execute(
            "UPDATE db_monitor_metrics SET last_run_at=?, last_value=?, "
            "last_error=NULL WHERE id=?",
            (ts, ("%g" % num) if num is not None else (text or ""), mid))
        _trim_points(mid, keep)
        return {"ok": True, "value": num if num is not None else text, "ts": ts}
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:300])
        db.execute(
            "UPDATE db_monitor_metrics SET last_run_at=?, last_error=? WHERE id=?",
            (ts, err, mid))
        return {"ok": False, "error": err, "ts": ts}


def _trim_points(mid: int, keep: int):
    try:
        db.execute(
            "DELETE FROM db_monitor_points WHERE metric_id=? AND id NOT IN "
            "(SELECT id FROM db_monitor_points WHERE metric_id=? "
            "ORDER BY id DESC LIMIT ?)", (mid, mid, int(keep)))
    except Exception:
        pass


def run_due_metrics() -> dict:
    """调度入口：跑所有到期（last_run_at 距今 >= interval_min）的启用指标。"""
    rows = db.query(
        "SELECT id, interval_min, last_run_at FROM db_monitor_metrics "
        "WHERE enabled=1")
    ran = ok = fail = 0
    for r in rows:
        if _is_due(r.get("last_run_at"), int(r.get("interval_min") or 5)):
            ran += 1
            res = run_metric(r["id"])
            if res.get("ok"):
                ok += 1
            else:
                fail += 1
    if ran:
        db.get_logger("db_monitor").info(
            "[db_monitor] 采集 %d 项：成功 %d 失败 %d", ran, ok, fail)
    return {"ran": ran, "ok": ok, "fail": fail}


def _is_due(last_run_at: str, interval_min: int) -> bool:
    if not last_run_at:
        return True
    try:
        last = datetime.strptime(str(last_run_at)[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last).total_seconds() >= max(60, interval_min * 60)
    except Exception:
        return True


def points(mid: int, hours: int = 24, limit: int = 2000) -> list:
    rows = db.query(
        "SELECT ts, value_num, value_text FROM db_monitor_points "
        "WHERE metric_id=? ORDER BY id DESC LIMIT ?", (mid, int(limit)))
    rows.reverse()
    if hours and rows:
        cutoff = _cutoff_ts(hours)
        rows = [r for r in rows if str(r.get("ts") or "") >= cutoff]
    return rows


def _cutoff_ts(hours: int) -> str:
    dt = datetime.fromtimestamp(time.time() - hours * 3600)
    return dt.strftime("%Y-%m-%d %H:%M:%S")
