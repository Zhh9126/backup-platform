# -*- coding: utf-8 -*-
"""插件版本矩阵与升级建议。

## 为什么需要

插件装没装、装的是哪个版本、跟平台期望版本差多少——这三件事此前只能逐个点开
看。现场排查"为什么物理备份用不了"时，最常见原因是**版本不匹配**（例如
XtraBackup 2.4 对 MySQL 8.0 不适用）。这里给出机器可读的矩阵：

    每行 = 一个插件：期望版本 / 各主机下已装版本 / 状态 / 升级建议

## 状态口径（诚实优先）

    ok          已装版本与期望一致（或期望未声明版本）
    outdated    已装版本低于期望 → 需升级
    newer       已装版本高于期望（可用，但记录在案）
    missing     文件层面未安装
    unknown     版本串无法比较（如 "builtin"/空），只提示人工确认

版本比较按数字段逐段比（1.2.10 > 1.2.9），非数字段退化为字符串比较。
"""
from __future__ import annotations

import json
import re

import core.db as db
from core import plugin_catalog


def _ver_tuple(v: str):
    """把版本串转成可比较元组；无法解析返回 None。"""
    s = str(v or "").strip()
    if not s:
        return None
    nums = re.findall(r"\d+", s)
    if not nums:
        return None
    return tuple(int(x) for x in nums[:4])


def compare(installed: str, expected: str) -> tuple:
    """返回 (状态, 说明)。"""
    a, b = _ver_tuple(installed), _ver_tuple(expected)
    if not installed:
        return "missing", "未安装"
    if not expected:
        return "ok", "平台未声明期望版本（不做比较）"
    if str(installed).lower() == "builtin" or str(expected).lower() == "builtin":
        # 内置/随数据库服务端安装的工具（如 LVM 的 lvs、Oracle 的 rman）
        # 没有独立版本号，不做版本比较，避免误报 outdated/unknown
        return "ok", "内置工具（随系统/服务端安装），不做版本比较"
    if a is None or b is None:
        return "unknown", "版本串无法比较（已装 %s / 期望 %s），请人工确认" % (
            installed, expected)
    # 补齐长度，避免 (8,) 与 (8,0,35) 比较时误判
    n = max(len(a), len(b))
    a2 = a + (0,) * (n - len(a))
    b2 = b + (0,) * (n - len(b))
    if a2 == b2:
        return "ok", "版本一致"
    if a2 < b2:
        return "outdated", "已装 %s 低于期望 %s，建议升级" % (installed, expected)
    return "newer", "已装 %s 高于期望 %s（可用）" % (installed, expected)


def _host_states(host_id=None) -> dict:
    """{(host_key, plugin_id): state_row}"""
    sql = "SELECT * FROM plugin_host_state"
    params = ()
    if host_id:
        sql += " WHERE host_id=?"
        params = (int(host_id),)
    out = {}
    try:
        for r in db.query(sql, params) or []:
            out[(r.get("host_key") or "", r.get("plugin_id") or "")] = dict(r)
    except Exception:
        pass
    return out


def matrix(host_id=None) -> dict:
    """插件 × 主机 的版本矩阵 + 升级建议。"""
    manifests = plugin_catalog.load_all()
    states = _host_states(host_id)
    by_plugin = {}
    for (hk, pid), st in states.items():
        by_plugin.setdefault(pid, []).append({
            "host_key": hk, "host_id": st.get("host_id"),
            "status": st.get("status"), "version": st.get("version") or "",
            "method": st.get("method") or "", "updated_at": st.get("updated_at") or "",
        })

    # 缺口补全：plugin_host_state 为空（从未记录过）时，用**本机实时检测**兜底，
    # 并把检测到的版本写回该表——这就是"记录每台主机的插件版本"的落地点。
    # 否则矩阵会把本机明明装着的 zstd/lvm-snapshot 全显示成 missing（假阴性）。
    try:
        live = {p.get("id"): p for p in (plugin_catalog.list_plugins() or [])}
    except Exception:
        live = {}
    for pid, man in manifests.items():
        if by_plugin.get(pid):
            continue
        lp = live.get(pid) or {}
        if not lp.get("installed"):
            continue
        ver = str(lp.get("version") or "")
        by_plugin[pid] = [{
            "host_key": "local", "host_id": 0, "status": "installed",
            "version": ver, "method": "detected",
            "updated_at": db.now_iso(),
        }]
        try:
            db.upsert_plugin_host_state("local", pid, {
                "status": "installed", "version": ver, "method": "detected",
                "updated_at": db.now_iso(),
            })
        except Exception:
            pass

    rows, suggestions = [], []
    for pid, man in sorted(manifests.items()):
        expected = str(man.get("version") or "")
        hosts = by_plugin.get(pid) or []
        # 汇总优先级：最需要关注的状态胜出（此前写成"取最小 index"，
        # 导致 ok 永远无法覆盖初始的 missing —— 本机已装却显示 missing 的假阴性）
        _priority = {"outdated": 0, "missing": 1, "unknown": 2, "newer": 3, "ok": 4}
        best_status, notes = ("ok" if hosts else "missing"), []
        for h in hosts:
            st, why = compare(h["version"], expected)
            h["compare"] = st
            h["advice"] = why
            if st == "outdated":
                suggestions.append({
                    "plugin": pid, "name": man.get("name") or pid,
                    "host_key": h["host_key"], "installed": h["version"],
                    "expected": expected, "action": "升级",
                    "command": _upgrade_hint(pid, expected),
                    "reason": why,
                })
            elif st == "missing" and h.get("status") in ("installed", "ok"):
                notes.append("%s 记录为已装但版本为空，建议重装校准" % h["host_key"])
            if _priority.get(st, 9) < _priority.get(best_status, 9):
                best_status = st
        if not hosts:
            best_status = "missing"
            suggestions.append({
                "plugin": pid, "name": man.get("name") or pid, "host_key": "",
                "installed": "", "expected": expected, "action": "安装",
                "command": _install_hint(pid),
                "reason": "该主机尚未安装此插件",
            })
        rows.append({
            "plugin": pid, "name": man.get("name") or pid,
            "category": man.get("category") or "",
            "expected": expected, "status": best_status,
            "hosts": hosts, "notes": notes,
        })
    summary = {k: sum(1 for r in rows if r["status"] == k)
               for k in ("ok", "outdated", "newer", "missing", "unknown")}
    return {"rows": rows, "summary": summary, "suggestions": suggestions,
            "generated_at": db.now_iso()}


def _install_hint(pid: str) -> str:
    return ("在「备份插件」页安装 %s；离线现场可用「离线插件包」一次导入 "
            "（POST /api/plugins/bundle/import）" % pid)


def _upgrade_hint(pid: str, expected: str) -> str:
    return ("先卸载旧版本再安装期望版本 %s（在「备份插件」页操作），"
            "或导入包含该版本的离线插件包；升级后回到本页确认状态变为 ok" % expected)


def suggestions(host_id=None) -> list:
    return matrix(host_id).get("suggestions") or []
