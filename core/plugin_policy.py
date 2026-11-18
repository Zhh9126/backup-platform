# -*- coding: utf-8 -*-
"""备份插件使用策略（任务级「是否使用插件」+ 能力协商）。

## 设计目标

平台对所有备份类型都提供**两条通道**：

- **零安装通道**：平台自带能力（mysqldump/pg_dump/tar/S3 协议客户端…），
  开箱即用，但只有逻辑/文件级能力；
- **插件增强通道**：装上对应插件后获得物理备份、一致性快照、并行压缩、
  块级导出等能力。

用户按需选择（任务级 ``extra_options.use_plugin``）：

    auto（默认）  有插件就用增强通道，没有就自动回落零安装通道
    off           强制零安装通道（例如现场不允许在数据库主机装东西）
    require       必须走插件通道；插件缺失时**预检直接失败**并说明缺什么

## 诚实边界

本模块只做"策略判定 + 能力协商"，不安装任何东西；安装由
``core/plugin_installer`` 负责，检测由 ``core/plugin_catalog.check_installed``
负责。未检测到的插件一律按"未安装"处理，不假设可用。
"""
from __future__ import annotations

import json

import core.models as models
from core import plugin_catalog

MODE_AUTO = "auto"
MODE_OFF = "off"
MODE_REQUIRE = "require"
MODES = (MODE_AUTO, MODE_OFF, MODE_REQUIRE)


def _extra(task: dict) -> dict:
    raw = (task or {}).get("extra_options") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def task_mode(task: dict) -> str:
    """读取任务级插件模式（默认 auto）。"""
    mode = str(_extra(task).get("use_plugin") or MODE_AUTO).strip().lower()
    aliases = {"true": MODE_REQUIRE, "1": MODE_REQUIRE, "yes": MODE_REQUIRE,
               "false": MODE_OFF, "0": MODE_OFF, "no": MODE_OFF,
               "on": MODE_AUTO, "": MODE_AUTO}
    mode = aliases.get(mode, mode)
    return mode if mode in MODES else MODE_AUTO


def set_task_mode(task_id: int, mode: str) -> dict:
    """写入任务级插件模式（保存到 extra_options.use_plugin）。"""
    mode = str(mode or "").strip().lower()
    if mode == "true":
        mode = MODE_REQUIRE
    if mode == "false":
        mode = MODE_OFF
    if mode == "on":
        mode = MODE_AUTO
    if mode not in MODES:
        return {"ok": False, "message": "模式非法（auto/off/require）"}
    task = models.get_task(int(task_id), include_secret=True)
    if not task:
        return {"ok": False, "message": "任务不存在"}
    extra = _extra(task)
    extra["use_plugin"] = mode
    models.update_task(int(task_id), {"extra_options": json.dumps(extra, ensure_ascii=False)})
    return {"ok": True, "task_id": int(task_id), "mode": mode}


def candidates_for(db_type: str) -> list:
    """该库型可用的插件候选（含通用插件如 zstd/pigz）。"""
    return plugin_catalog.recommended_for_db_type(str(db_type or "").lower()) or []


def resolve(task: dict, ssh_host: dict = None) -> dict:
    """解析任务实际使用的通道。

    Returns::

        {"ok": bool, "mode": "auto|off|require", "use_plugin": bool,
         "installed": [{"id","name","category"}], "missing": [...],
         "reason": "...", "message": "..."（ok=False 时给用户看）}
    """
    db_type = str((task or {}).get("db_type") or "").lower()
    mode = task_mode(task or {})
    cands = candidates_for(db_type)
    installed, missing = [], []
    for p in cands:
        item = {"id": p.get("id"), "name": p.get("name"),
                "category": p.get("category"),
                "install_cmd": p.get("install_cmd") or ""}
        (installed if p.get("installed") else missing).append(item)
    if mode == MODE_OFF:
        return {"ok": True, "mode": mode, "use_plugin": False,
                "installed": installed, "missing": missing,
                "reason": "任务显式关闭插件（走零安装通道）",
                "message": "已按任务设置使用零安装通道"}
    if mode == MODE_REQUIRE and not installed:
        need = "、".join(x["name"] for x in missing[:4]) or "（该库型暂无插件）"
        return {"ok": False, "mode": mode, "use_plugin": False,
                "installed": [], "missing": missing,
                "reason": "任务要求必须使用插件，但本机未检测到任何可用插件",
                "message": "插件缺失，备份已阻止：需要安装 %s。可在「备份插件」页一键安装，"
                           "或把任务插件模式改为 auto/off" % need}
    return {"ok": True, "mode": mode, "use_plugin": bool(installed),
            "installed": installed, "missing": missing,
            "reason": ("已检测到插件，使用增强通道" if installed
                       else "未检测到插件，自动回落零安装通道"),
            "message": ""}


def assert_task(task: dict, ssh_host: dict = None) -> dict:
    """供调度/引擎入口调用：require 且缺失时返回 ok=False（由调用方阻止执行）。"""
    return resolve(task, ssh_host=ssh_host)


def coverage() -> dict:
    """备份类型 × 插件覆盖矩阵（页面用）。

    一行 = 一个备份类型（db/file/object/vm/common），列出该类型下的插件与
    已装数量，便于回答"这类备份到底有没有插件增强能力"。
    """
    rows = plugin_catalog.list_plugins()
    groups: dict[str, dict] = {}
    for p in rows:
        types = list(p.get("db_types") or []) or [p.get("category") or "common"]
        for t in types:
            g = groups.setdefault(t, {"type": t, "total": 0, "installed": 0,
                                      "plugins": []})
            g["total"] += 1
            if p.get("installed"):
                g["installed"] += 1
            g["plugins"].append({"id": p.get("id"), "name": p.get("name"),
                                 "installed": bool(p.get("installed")),
                                 "category": p.get("category")})
    order = ["mysql", "mariadb", "postgresql", "oracle", "dameng", "kingbase",
             "redis", "mongodb", "file", "mount", "object_storage", "s3", "minio",
             "vm", "common"]
    out = sorted(groups.values(),
                 key=lambda x: (order.index(x["type"]) if x["type"] in order else 99,
                                x["type"]))
    return {"rows": out, "total_plugins": len(rows),
            "installed_plugins": sum(1 for p in rows if p.get("installed"))}


def bundle_for_tasks(task_ids: list) -> dict:
    """对外"一个包"：按一组任务推导所需插件（去重），供一键安装。"""
    want, tasks = {}, []
    for tid in task_ids or []:
        t = models.get_task(int(tid))
        if not t:
            continue
        tasks.append({"id": t["id"], "name": t.get("name"), "db_type": t.get("db_type")})
        for p in candidates_for(t.get("db_type")):
            if not p.get("installed"):
                want[p["id"]] = {"id": p["id"], "name": p.get("name"),
                                 "install_cmd": p.get("install_cmd") or ""}
    return {"tasks": tasks, "plugins": list(want.values()),
            "count": len(want)}

def physical_blocked(task: dict) -> tuple:
    """物理备份通道是否被插件策略禁用。返回 (blocked, reason)。

    2026-11-14：策略此前只做"判定"，引擎仍会跑物理分支——``use_plugin=off``
    的任务照样去调 xtrabackup/RMAN。这里给出统一的"是否禁用物理"结论，
    由引擎 preflight/调度入口共同调用，做到策略真正生效。
    """
    res = resolve(task or {})
    if not res.get("ok"):
        return True, res.get("message") or "插件策略未满足"
    if res.get("mode") == MODE_OFF:
        return True, ("任务已设置「不使用备份插件」（use_plugin=off）：物理备份依赖 "
                      "xtrabackup / RMAN / sys_basebackup / dmrman 等客户端或插件，"
                      "该通道已禁用。请改用逻辑备份，或把插件模式改为 auto/require。")
    # auto 模式**不在这里拦**：auto 的语义是"有插件就用、没有就回落"，
    # 而"到底有没有可用工具"由物理预检自己探测（本机/远端），更权威。
    # 若在这里依据插件目录状态拦，会把"目录信息未就绪/临时库"误判成"没法物理备份"。
    # （降级为逻辑备份由 effective_task 在调度入口完成。）
    return False, ""


def effective_task(task: dict) -> dict:
    """按插件策略调整任务的执行形态（返回浅拷贝，不写库）。

    - ``off`` / ``auto 无插件`` + 物理备份 → **降级为逻辑备份**（并标记
      ``plugin_downgraded``，让备份记录里能看到"为什么走了逻辑"）；
    - ``require`` 且无插件 → 抛 ``PluginPolicyError``（由调用方转成 400）。
    """
    t = dict(task or {})
    if str(t.get("backup_mode") or "").lower() != "physical":
        return t
    blocked, reason = physical_blocked(t)
    if not blocked:
        return t
    res = resolve(t)
    if res.get("mode") == MODE_REQUIRE:
        raise PluginPolicyError(reason)
    t["backup_mode"] = "logical"
    t["_plugin_downgraded"] = reason
    return t


class PluginPolicyError(RuntimeError):
    """插件策略要求无法满足（require 但缺插件）。"""
