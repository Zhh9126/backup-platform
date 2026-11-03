# -*- coding: utf-8 -*-
"""容灾自动切换监控（P1-1）：

调度器周期调用 check_once()，对启用自动策略的容灾链路探测主库：
- 连续 N 次失败 → 告警（require_manual_confirm=true，默认，只告警不切换）
- 或自动执行 failover（require_manual_confirm=false，主库不可达场景无需 force）

状态持久化在 system_config（key=dr_auto_monitor_state），重启不丢计数；
告警去重：同一次故障窗口只告警一次（恢复后重置）。
设计参照 failover 三步法共识：缺 fence 的自动切换就是新事故源——因此
自动切换复用 FailoverEngine（fencing→提升→DNS→验证），绝不跳步。
"""
import json
import logging
import threading
import time

import core.db as db

_logger = logging.getLogger("dr_monitor")

STATE_KEY = "dr_auto_monitor_state"
_lock = threading.Lock()


def _load_state() -> dict:
    try:
        raw = db.get_system_config(STATE_KEY) or "{}"
        st = json.loads(raw)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    db.set_system_config(STATE_KEY, json.dumps(st, ensure_ascii=False))


def reset_state(link_id: int = None) -> None:
    """清空监控状态（策略变更/人工处置后调用）。"""
    with _lock:
        st = _load_state()
        if link_id is None:
            st = {}
        else:
            st.pop(str(link_id), None)
        _save_state(st)


def state_view() -> dict:
    """监控状态视图（API 展示用）。"""
    return _load_state()


def _alert(link, st: dict, snap: dict) -> None:
    """主库连续失败告警（系统日志 WARNING + webhook 事件）。"""
    msg = ("容灾链路「%s」主库 %s:%s 连续 %s 次探测失败（阈值 %s）——"
           "最后错误: %s。自动策略=告警待人工确认，请尽快确认是否执行切换"
           % (link.get("name"), snap["primary"].get("host"),
              snap["primary"].get("port"), st["fails"],
              st.get("threshold"), (snap["primary"].get("error") or "-")))
    _logger.warning("[dr_monitor] %s", msg)
    try:
        db.add_log("WARNING", "dr_monitor", msg, detail=msg)
    except Exception:
        pass
    try:
        from core import webhooks
        webhooks.emit("dr.primary_down", {
            "link_id": link.get("id"), "link_name": link.get("name"),
            "consecutive_failures": st["fails"],
            "primary": "%s:%s" % (snap["primary"].get("host"),
                                  snap["primary"].get("port")),
            "error": snap["primary"].get("error"),
            "action": "manual_confirm_required",
        })
    except Exception:
        pass


def check_once() -> list:
    """对所有启用自动策略的链路执行一轮探测。返回本轮处置摘要。"""
    from core import dr_failover
    rows = db.query("SELECT * FROM disaster_links ORDER BY id")
    out = []
    now = time.time()
    for row in rows or []:
        link = dict(row)
        cfg = dr_failover._load_config(link)
        policy = dr_failover.auto_policy(cfg)
        if not policy["enabled"]:
            continue
        lid = str(link.get("id"))
        # 按链路自身的探测间隔节流
        with _lock:
            st_all = _load_state()
            st = st_all.get(lid) or {}
            last_ts = st.get("last_check_ts") or 0
            if now - last_ts < policy["check_interval_sec"]:
                continue
        try:
            snap = dr_failover.health_snapshot(link.get("id"))
        except Exception as e:
            snap = {"primary": {"reachable": False, "error": str(e)[:160],
                                "host": "-", "port": "-"},
                    "standby": None, "dns": None}
        primary_ok = bool(snap.get("primary", {}).get("reachable"))
        item = {"link_id": link.get("id"), "link_name": link.get("name"),
                "primary_reachable": primary_ok, "action": "none"}
        if primary_ok:
            if st.get("fails") or st.get("alerted") or st.get("auto_running"):
                reset_state(link.get("id"))
            item["action"] = "ok"
            out.append(item)
            with _lock:
                st_all = _load_state()
                st_all[lid] = {"fails": 0, "last_check_ts": now,
                               "last_ok_at": db.now_iso(),
                               "threshold": policy["consecutive_failures"]}
                _save_state(st_all)
            continue

        # 主库不可达：累计连续失败
        with _lock:
            st_all = _load_state()
            st = st_all.get(lid) or {}
            fails = int(st.get("fails") or 0) + 1
            st.update({"fails": fails, "last_check_ts": now,
                       "last_error": (snap["primary"].get("error") or "")[:200],
                       "threshold": policy["consecutive_failures"],
                       "last_check_at": db.now_iso()})
            st_all[lid] = st
            _save_state(st_all)

        if fails < policy["consecutive_failures"]:
            item["action"] = "counting(%d/%s)" % (fails,
                                                  policy["consecutive_failures"])
            out.append(item)
            continue

        if st.get("auto_running"):
            item["action"] = "auto_running"
            out.append(item)
            continue

        if policy["require_manual_confirm"]:
            # 只告警一次，直到恢复重置
            if not st.get("alerted"):
                with _lock:
                    st_all = _load_state()
                    st_all[lid]["alerted"] = True
                    st_all[lid]["alerted_at"] = db.now_iso()
                    _save_state(st_all)
                _alert(link, st_all[lid], snap)
            item["action"] = "alerted"
            out.append(item)
            continue

        # 自动切换：复用 FailoverEngine 全流程（fencing→提升→DNS→验证）
        with _lock:
            st_all = _load_state()
            st_all[lid]["auto_running"] = True
            _save_state(st_all)
        try:
            _logger.warning(
                "[dr_monitor] 链路「%s」主库连续 %s 次探测失败，自动策略=自动切换，开始 failover",
                link.get("name"), fails)
            res = dr_failover.run_failover(link.get("id"), force=False)
            item["action"] = "auto_failover"
            item["result"] = {"rto_sec": res.get("rto_sec"),
                              "to": res.get("to"),
                              "success": True}
            with _lock:
                st_all = _load_state()
                st_all[lid]["auto_running"] = False
                st_all[lid]["last_failover_at"] = db.now_iso()
                _save_state(st_all)
        except Exception as e:
            _logger.error("[dr_monitor] 链路「%s」自动切换失败: %s",
                          link.get("name"), e)
            item["action"] = "auto_failover_failed"
            item["result"] = {"success": False, "error": str(e)[:200]}
            with _lock:
                st_all = _load_state()
                st_all[lid]["auto_running"] = False
                st_all[lid]["auto_failover_error"] = str(e)[:200]
                _save_state(st_all)
        out.append(item)
    return out
