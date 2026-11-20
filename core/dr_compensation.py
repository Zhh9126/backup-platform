# -*- coding: utf-8 -*-
"""失败切换的补偿建议（2026-11-20）。

真实事故里最耗时的往往不是「切换失败」本身，而是「不知道下一步做什么」：
值班看到 failed 之后，得自己判断卡在哪一步、该不该重试、需不需要人工介入。

本模块基于切换作业**已落库的步骤明细**（``dr_switch_runs.steps``）做判定：
- 判定依据只有 steps 里的真实步骤名与成败，不做任何推测；
- 认不出的步骤 → 如实标注「需人工判定」，**不编造**补偿动作；
- 可执行的补偿（如「DNS 牵引未完成 → 补做仅 DNS 牵引」）会给出
  ``scope`` / ``direction`` 参数，由前端二次确认后复用既有切换端点
  （``POST /api/disaster-links/<id>/switch``），不另造一条切换通道。

诚实边界：本模块只做**建议**，不代替人工决策；除「仅 DNS 牵引」外的动作
（解除旧主只读、处理 GTID 分叉）平台目前没有对应能力，如实标注需人工。
"""
from __future__ import annotations

import json

import core.db as db


def _link_dns(link: dict) -> dict:
    """取链路 failover_config 中的 DNS 配置（只读，不解密任何口令）。"""
    raw = (link or {}).get("failover_config") or ""
    if not str(raw).strip():
        return {}
    try:
        cfg = json.loads(raw)
    except Exception:
        return {}
    return cfg.get("dns") or {}


def _act(aid: str, label: str, hint: str, executable: bool = False,
         scope: str = "", direction: str = "") -> dict:
    return {"id": aid, "label": label, "hint": hint,
            "executable": bool(executable), "scope": scope,
            "direction": direction}


def analyze(run: dict, link: dict = None) -> dict:
    """分析一次失败/中止的切换作业，给出补偿建议。

    参数：
    - ``run``  切换作业记录（需含 steps 明细与 status）
    - ``link`` 对应链路（用于判断是否配置了 DNS → 补偿动作能否自动执行）

    返回::

        {"run_id", "status", "direction", "failed_step", "last_ok_step",
         "diagnosis", "actions": [...], "generated_at"}
    """
    run = run or {}
    steps = run.get("steps") or []
    failed = [s for s in steps if not s.get("ok")]
    last_failed = failed[-1] if failed else None
    last_ok = None
    for s in steps:
        if s.get("ok"):
            last_ok = s

    status = str(run.get("status") or "")
    direction = str(run.get("direction") or "failover")
    has_dns = bool(_link_dns(link or {}).get("domain"))
    name = str((last_failed or {}).get("name") or "")
    actions = []
    diagnosis = ""

    def _dns_only() -> dict:
        if has_dns:
            return _act("dns_only", "补做仅 DNS 牵引",
                        "业务已切换、只差流量牵引：执行「仅 DNS 牵引」把解析切到新主，不触碰数据库。",
                        True, "dns_only", direction)
        return _act("manual", "人工牵引 DNS",
                    "该链路未配置 DNS（domain），无法自动牵引——请在「容灾链路」页补配 DNS 后重试。",
                    False)

    if last_failed is not None:
        if "主库故障探测" in name:
            diagnosis = ("切换在第一步（主库探测）被拦下，**尚未对任何实例做变更**，拓扑保持原状。")
            actions.append(_act(
                "manual", "人工确认主库状态",
                "主库仍可达时切换被防误操作拦截（这是保护行为）。若确认主库假死/已隔离，"
                "请在「一键切换」页勾选「强制切换」重新发起。"))
        elif "Fencing" in name or "进入只读" in name:
            diagnosis = ("旧主写入冻结环节失败——**旧主当前是否可写入需人工确认**："
                         "若已冻结而业务未切换，需人工解除只读。")
            actions.append(_act(
                "manual", "人工核对旧主只读状态",
                "平台暂无「解除只读」能力，请在数据库侧执行 SET GLOBAL read_only=0 / super_read_only=0 后再重试切换。"))
        elif "备库提升为主库" in name:
            diagnosis = ("备库提升失败（常见原因：复制未就绪、复制账号权限不足、GTID 位点异常），"
                         "**主备角色通常尚未变更**。")
            actions.append(_act(
                "manual", "检查复制状态后重试",
                "先在备库确认 SHOW REPLICA STATUS 的 IO/SQL 均为 Yes、Seconds_Behind=0，再重新发起切换。"))
        elif "DNS 切换" in name or "DNS 切回" in name:
            diagnosis = ("**业务层已完成（备库已提升为主库），仅 DNS 牵引未完成**——"
                         "此时流量仍指向旧端点，是可补偿的典型场景。")
            actions.append(_dns_only())
        elif "可达验证" in name:
            diagnosis = ("DNS 已牵引，但新主可达性验证未通过——**流量已切走而新主可能不可用，属高危状态**，"
                         "请优先确认业务连接。")
            actions.append(_act(
                "manual", "人工核验新主连通性",
                "请立即在应用侧验证连通性；如新主确有问题，可重新发起一次「回切」把流量切回原端点。"))
        elif "GTID 分叉检测" in name:
            diagnosis = ("回切被 GTID 分叉检测拦截：旧主存在现主缺失的事务段，强行回切会丢数据——"
                         "**这是保护行为，不要绕过**。")
            actions.append(_act(
                "manual", "人工决定分叉处理方式",
                "请在「一键切换」页预检查看分叉段数，由 DBA 决定丢弃该段或先导出合并，再重新回切。"))
        elif "追平" in name:
            diagnosis = ("旧主未能追平现主（复制未真正追平），回切被拒——**拒绝回切是为了防止数据缺失**。")
            actions.append(_act(
                "manual", "修复复制后重试",
                "请先排查复制链路（IO/SQL 是否 Yes、账号权限、位点是否报错），追平后再发起回切。"))
        elif "转为从库" in name:
            diagnosis = ("主备角色已互换，但**现主转从 / 复制重建未完成**，此时复制关系可能不完整。")
            actions.append(_act(
                "manual", "人工确认复制关系",
                "请在两端核对 SHOW REPLICA STATUS，确认复制已重新建立后再做后续操作。"))
        else:
            diagnosis = "未识别的失败步骤，需人工判定。"
            actions.append(_act(
                "manual", "人工判定",
                "请查看该作业的步骤明细与操作日志确认实际卡点。"))
    elif status == "cancelled":
        stage = str((last_ok or {}).get("name") or "")
        diagnosis = ("作业在「%s」完成后被中止（步骤边界生效），"
                     "**已完成的步骤全部保留，未完成的步骤未执行**。" % (stage or "-"))
        if "备库提升为主库" in stage or "提升原主为主库" in stage:
            actions.append(_dns_only())
        else:
            actions.append(_act(
                "manual", "人工确认当前拓扑后决定是否重试",
                "中止时点决定了哪些变更已生效，请先核对当前主备角色与 DNS 解析，再决定重试或人工接管。"))
    else:
        diagnosis = "该作业未处于失败/中止状态，无需补偿。"
        actions.append(_act("manual", "无需处理", "作业状态为 %s。" % (status or "-")))

    return {
        "run_id": run.get("id"),
        "status": status,
        "direction": direction,
        "failed_step": name,
        "last_ok_step": str((last_ok or {}).get("name") or ""),
        "diagnosis": diagnosis,
        "actions": actions,
        "generated_at": db.now_iso(),
    }


# ------------------------------------------------------------------ 剧本化

def build_runbook(run: dict, link: dict = None) -> dict:
    """把补偿建议编排成一份**可审批的恢复剧本定义**。

    返回::

        {"name", "description", "steps": [...], "warnings": [...],
         "run_id", "link_id", "link_name", "diagnosis"}

    编排原则（诚实）：
    - **第一步永远是 notify**：先把诊断结论广播出去——人必须先知道发生了什么，
      再谈处置；不要让剧本一上来就动数据库或 DNS。
    - 平台确有能力的补偿（当前仅「仅 DNS 牵引」）→ ``dr_compensate`` 步骤，
      且**必须带审批闸**（``require_confirm``）：DNS 牵引会改变线上流量入口。
    - 平台没有能力自动做的动作（修复制、解 GTID 分叉、解除只读等）
      → 编成 ``notify`` 步骤并带审批闸，作用是「人工处置确认点」：
      值班按指引处理完再放行后续步骤。**绝不生成假的「自动修复」步骤**。
    - 剧本**不自动执行**，只生成定义；执行仍需人在「恢复剧本」页发起并审批。

    诚实边界：本函数只做编排，不做任何数据库/DNS 变更。
    """
    run = run or {}
    ana = analyze(run, link)
    run_id = int(run.get("id") or 0)
    link_id = int(run.get("link_id") or 0)
    link_name = str(run.get("link_name") or (link or {}).get("name") or "")
    direction = ana.get("direction") or "failover"

    steps: list = []
    # 步骤 1：先播报事实（不等审批，人要先看到）
    steps.append({
        "type": "notify",
        "name": "播报失败诊断",
        "params": {
            "title": "容灾切换失败",
            "message": ("作业 #%s（%s）%s。诊断：%s"
                        % (run_id, link_name or "-",
                           {"failover": "切换", "failback": "回切"}.get(direction, direction),
                           ana.get("diagnosis") or "-")),
            "severity": "critical",
        },
        "require_confirm": False,
        "continue_on_fail": True,
    })

    warnings: list = []
    for act in ana.get("actions") or []:
        label = str(act.get("label") or "")
        hint = str(act.get("hint") or "")
        if act.get("executable"):
            steps.append({
                "type": "dr_compensate",
                "name": label,
                "params": {"run_id": run_id, "action_id": act.get("id") or "",
                           "scope": act.get("scope") or "",
                           "direction": act.get("direction") or direction},
                "require_confirm": True,      # 会改变流量入口 → 人工放行
                "continue_on_fail": False,
            })
        else:
            steps.append({
                "type": "notify",
                "name": "人工处置确认：%s" % label,
                "params": {"title": label, "message": hint, "severity": "warning"},
                "require_confirm": True,      # 人工确认已按指引处理
                "continue_on_fail": True,
            })
            warnings.append("「%s」需人工处理，平台当前无对应自动能力。" % label)

    name = "处置剧本 #%s %s %s" % (run_id, link_name or "", direction)
    desc = ("由切换作业 #%s 的补偿建议自动生成（卡在「%s」）。"
            "请逐步骤核对后执行；带审批闸的步骤需人工放行。"
            % (run_id, ana.get("failed_step") or "-"))
    return {
        "name": name[:120],
        "description": desc,
        "steps": steps,
        "warnings": warnings,
        "run_id": run_id,
        "link_id": link_id,
        "link_name": link_name,
        "direction": direction,
        "diagnosis": ana.get("diagnosis") or "",
        "executable": bool([a for a in (ana.get("actions") or []) if a.get("executable")]),
    }
