# -*- coding: utf-8 -*-
"""Oracle 事务级精准还原 API（P1-2）：

- GET  /api/oracle-txn/segments?task_id=          实时任务 LogMiner 段清单
- GET  /api/oracle-txn/transactions?task_id&seg   事务时间轴（聚合，无 SQL 正文）
- GET  /api/oracle-txn/transaction?task_id&seg&xid 事务事件明细（redo/undo）
- POST /api/oracle-txn/rollback                   回滚（preview=true 出计划 /
      preview=false 执行——高危操作：confirm + 操作验证码双闸）
"""
import os

from flask import jsonify, request

import core.db as db
from auth import login_required, verify_code_gate
from . import api_bp


def _load_segment(task_id: int, seg: str):
    """按段名定位文件（只允许仓库目录内文件名，防路径穿越）。"""
    from core import oracle_txn as ot
    if not seg or os.path.basename(seg) != seg or not seg.endswith(".jsonl"):
        raise ot.OracleTxnError("段名非法")
    for s in ot.segments_for_task(task_id):
        if s["name"] == seg:
            return s
    raise ot.OracleTxnError(f"段不存在或已清理: {seg}")


@api_bp.route("/oracle-txn/segments", methods=["GET"])
@login_required
def api_txn_segments():
    from core import oracle_txn as ot
    try:
        task_id = int(request.args.get("task_id") or 0)
        segs = ot.segments_for_task(task_id)
    except ot.OracleTxnError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "段扫描失败: %s" % e}), 500
    for s in segs:
        s.pop("path", None)  # 不透出服务器路径
    return jsonify({"ok": True, "segments": segs})


@api_bp.route("/oracle-txn/transactions", methods=["GET"])
@login_required
def api_txn_list():
    from core import oracle_txn as ot
    try:
        task_id = int(request.args.get("task_id") or 0)
        s = _load_segment(task_id, request.args.get("seg") or "")
        txns = ot.transactions(ot.load_events(s["path"]))
    except ot.OracleTxnError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "事务聚合失败: %s" % e}), 500
    return jsonify({"ok": True, "segment": s["name"], "transactions": txns})


@api_bp.route("/oracle-txn/transaction", methods=["GET"])
@login_required
def api_txn_detail():
    from core import oracle_txn as ot
    try:
        task_id = int(request.args.get("task_id") or 0)
        s = _load_segment(task_id, request.args.get("seg") or "")
        xid = request.args.get("xid") or "-"
        evs = ot.txn_events(ot.load_events(s["path"]), xid)
        if not evs:
            return jsonify({"error": f"段中不存在事务 {xid}"}), 404
    except ot.OracleTxnError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "事务读取失败: %s" % e}), 500
    return jsonify({"ok": True, "segment": s["name"], "xid": xid,
                    "events": evs})


@api_bp.route("/oracle-txn/rollback", methods=["POST"])
@login_required
def api_txn_rollback():
    """回滚指定事务。preview=true 出计划；false 执行（confirm+验证码双闸）。"""
    from core import oracle_txn as ot
    from core import models
    data = request.get_json(force=True, silent=True) or {}
    preview = bool(data.get("preview", True))
    if not preview:
        gate = verify_code_gate(data)
        if gate:
            return jsonify(gate[0]), gate[1]
        if not data.get("confirm"):
            return jsonify({"error": "缺少 confirm=true（回滚直接修改生产库，"
                                     "必须显式确认）"}), 400
    try:
        task = ot._rt_task(int(data.get("task_id") or 0))
        s = _load_segment(task["id"], data.get("segment") or "")
        events = ot.load_events(s["path"])
        plan = ot.rollback_plan(events, data.get("xid") or "-")
        if preview:
            return jsonify({"ok": True, "preview": True, "plan": plan})
        result = ot.rollback_execute(task, plan,
                                     stop_on_error=bool(data.get("stop_on_error")))
        db.add_log("WARNING", "oracle_txn",
                   "Oracle 事务回滚执行: task=%s segment=%s xid=%s "
                   "executed=%s failed=%s" % (task["id"], s["name"],
                                              data.get("xid"),
                                              result["executed"],
                                              result["failed"]),
                   task_id=task["id"])
        return jsonify({"ok": result["failed"] == 0, "result": result,
                        "plan_summary": {"n_undo": plan["n_undo"],
                                         "n_skip": plan["n_skip"]}})
    except ot.OracleTxnError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "回滚失败: %s" % e}), 500
