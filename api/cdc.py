# -*- coding: utf-8 -*-
"""行级 CDC API：捕获流管理 + 事件查询 + CDP 任意时间点回放/回滚。

路由前缀 /api/cdc（通过共享 api_bp 注册）。
"""
import json

import core.db as db
from auth import login_required
from core.cdc import rowlevel as rl
from flask import jsonify, request

from . import api_bp, contract


def _row_public(row: dict) -> dict:
    d = dict(row)
    d.pop("password", None)
    try:
        d["position"] = json.loads(d.get("position_json") or "{}")
    except Exception:
        d["position"] = {}
    return d


@api_bp.route("/cdc/capabilities", methods=["GET"])
@login_required
def api_cdc_capabilities():
    """平台侧 CDC 客户端能力自检。"""
    import shutil
    return jsonify({
        "mysqlbinlog": bool(shutil.which("mysqlbinlog")),
        "pg_recvlogical": bool(shutil.which("pg_recvlogical")
                               or __import__("os").path.exists(
                                   "/pgdb/pgsql/bin/pg_recvlogical")),
        "supported": ["mysql", "mariadb", "postgresql"],
    })


@api_bp.route("/cdc/probe", methods=["POST"])
@login_required
def api_cdc_probe():
    """创建流前的 CDC 前置条件检查（对标 DTS 预检查）。"""
    data = request.get_json(silent=True) or {}
    res = rl.probe_cdc_capability(
        data.get("db_type") or "", data.get("host") or "",
        int(data.get("port") or 0), data.get("username") or "",
        data.get("password") or "", data.get("db_name") or "")
    return jsonify(res)


@api_bp.route("/cdc/streams", methods=["GET"])
@login_required
def api_cdc_list():
    rows = db.query("SELECT * FROM cdc_streams ORDER BY id DESC LIMIT 200")
    out = []
    for r in rows:
        d = _row_public(r)
        st = rl.manager.get(int(r["id"]))
        d["alive"] = bool(st and st.is_alive())
        out.append(d)
    return jsonify(out)


@api_bp.route("/cdc/streams", methods=["POST"])
@login_required
def api_cdc_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    db_type = (data.get("db_type") or "").strip().lower()
    host = (data.get("host") or "").strip()
    if not name:
        return jsonify({"error": "名称必填"}), 400
    if db_type not in ("mysql", "mariadb", "postgresql"):
        return jsonify({"error": "行级 CDC 当前支持 mysql / mariadb / postgresql"}), 400
    if not host:
        return jsonify({"error": "主机必填"}), 400
    now = db.now_iso()
    sid = db.execute(
        "INSERT INTO cdc_streams (name, db_type, host, port, username, password,"
        " db_name, purpose, ref_id, include_tables, exclude_tables, status,"
        " events_total, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'stopped',0,?,?)",
        (name, db_type, host, int(data.get("port") or 0) or None,
         (data.get("username") or "").strip(),
         db.encrypt_secret(data.get("password") or ""),
         (data.get("db_name") or "").strip(),
         (data.get("purpose") or "cdp").strip(),
         int(data.get("ref_id") or 0) or None,
         (data.get("include_tables") or "").strip(),
         (data.get("exclude_tables") or "").strip(), now, now))
    return jsonify({"id": sid, "ok": True}), 201


@api_bp.route("/cdc/streams/<int:sid>", methods=["DELETE"])
@login_required
def api_cdc_delete(sid):
    rl.manager.stop(sid)
    db.execute("DELETE FROM cdc_events WHERE stream_id=?", (sid,))
    db.execute("DELETE FROM cdc_streams WHERE id=?", (sid,))
    return jsonify({"ok": True})


@api_bp.route("/cdc/streams/<int:sid>/start", methods=["POST"])
@login_required
def api_cdc_start(sid):
    ok, msg = rl.manager.start(sid)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@api_bp.route("/cdc/streams/<int:sid>/stop", methods=["POST"])
@login_required
def api_cdc_stop(sid):
    ok, msg = rl.manager.stop(sid)
    return jsonify({"ok": ok, "message": msg})


@api_bp.route("/cdc/streams/<int:sid>/status", methods=["GET"])
@login_required
def api_cdc_status(sid):
    return jsonify(rl.manager.status(sid))


@api_bp.route("/cdc/streams/<int:sid>/events", methods=["GET"])
@login_required
def api_cdc_events(sid):
    """读取行级变更事件（分页见 docs/api_conventions.md §2）。"""
    _page, size, offset = contract.pagination_args(default_size=100, max_size=500)
    events = rl.STORE.list_events(
        sid, limit=size, op=request.args.get("op") or "",
        table=request.args.get("table") or "",
        since=request.args.get("since") or "",
        until=request.args.get("until") or "",
        offset=offset)
    total = rl.STORE.count(sid)
    return contract.list_response(events, total=total,
                                  legacy={"total": total, "events": events})


@api_bp.route("/cdc/streams/<int:sid>/replay", methods=["POST"])
@login_required
def api_cdc_replay(sid):
    """按时间范围生成回放 / 回滚 SQL，可选真实应用到目标库。

    body::

        {
          "mode": "redo" | "undo",     # redo=重放变更，undo=撤销变更（回滚到起始点）
          "since": "2026-09-15 10:00:00", "until": "2026-09-15 11:00:00",
          "tables": ["t1"],            # 可选
          "target": {...},             # 可选；给出且 apply=true 时真实执行
          "apply": false
        }
    """
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "redo").strip().lower()
    if mode not in ("redo", "undo"):
        return jsonify({"error": "mode 应为 redo 或 undo"}), 400
    stream = db.query_one("SELECT * FROM cdc_streams WHERE id=?", (sid,))
    if not stream:
        return jsonify({"error": "CDC 流不存在"}), 404
    events = rl.STORE.fetch_range(
        sid, since=data.get("since") or "", until=data.get("until") or "",
        tables=data.get("tables") or [])
    dialect = "mysql" if stream["db_type"] in ("mysql", "mariadb") else "postgresql"
    sqls = rl.STORE.gen_sql(events, dialect=dialect, mode=mode,
                            schema_name=data.get("schema_name") or "")
    result = {
        "ok": True, "mode": mode, "events": len(events), "sql_count": len(sqls),
        "sqls": sqls[:500],
        "applied": 0, "errors": [],
    }
    if data.get("apply"):
        tgt = data.get("target") or {}
        if not tgt.get("host"):
            return jsonify({"error": "apply=true 时必须提供 target 目标库信息"}), 400
        r = rl.apply_sql_to_target(tgt, sqls)
        result.update({"ok": r.get("ok"), "applied": r.get("applied", 0),
                       "errors": r.get("errors", []), "message": r.get("message", "")})
        db.add_log("INFO" if r.get("ok") else "ERROR", "cdc",
                   f"CDC 流 {sid} {mode} 应用: {r.get('message', '')}")
    return jsonify(result)
