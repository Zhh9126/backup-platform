# -*- coding: utf-8 -*-
"""7 个自治 Agent：自动诊断 → 修复 → 回验闭环（离线优先）。

设计参考：多 Agent 自愈架构（Router 统一调度 + 独立上下文 Worker 并行执行）、
DeepSeek/OpenAI 兼容 LLM 做结论解释。落到 AIDBM 时的三条硬约束：

1. **完全离线**：诊断数据全部来自平台本地元数据库 / 只读 core 模块；
   LLM 只用于「把结构化发现讲成人话」，配置不可用、超时、报错时一律降级
   为规则模板并标注 `explanation_source=local_rules` / `llm_degraded`，
   绝不编造「AI 分析结论」，也绝不因模型不可用而阻塞诊断。
2. **零客户端安装 / 零越权**：修复动作是**白名单枚举**，只调用平台已封装的
   能力（重试备份、清理僵尸态、预检、巡检、启用任务、恢复校验）；
   不接受 LLM 生成的任意命令 / SQL / 脚本，不自动删除备份、不自动切换主备、
   不自动解除安全隔离——这些一律降级为「人工建议」。
3. **闭环可验证**：每个动作执行后重跑对应 Agent 的诊断做回验，
   异步动作如实标 `async_pending`，不把「已发起」伪装成「已修复」。

每个 Agent 独立上下文、异常隔离（单个失败不影响其余 6 个），
结果经黑板去重与冲突仲裁后生成修复计划。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import core.db as db

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
MAX_RUNS = 20


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Finding:
    id: str = ""
    agent: str = ""
    severity: str = "info"
    title: str = ""
    detail: str = ""
    evidence: Dict = field(default_factory=dict)
    action: str = ""                 # 白名单动作；空串 = 仅人工建议
    action_args: Dict = field(default_factory=dict)
    auto_fixable: bool = False
    requires_confirm: bool = False
    confidence: float = 1.0         # 规则诊断恒为 1.0，启发式按证据打折
    target_key: str = ""            # 冲突仲裁键（同目标只保留最高危发现）
    suggestion: str = ""            # 不可自动修复时的处置建议

    def to_dict(self):
        return asdict(self)


@dataclass
class AgentReport:
    agent: str
    title: str
    status: str = "ok"              # ok / skipped / failed
    findings: List[Finding] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0

    def to_dict(self):
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


@dataclass
class Run:
    id: str
    created_at: str
    domains: List[str]
    reports: List[AgentReport]
    findings: List[Finding]
    plan: List[dict]
    explanation: str = ""
    explanation_source: str = "local_rules"
    llm_note: str = ""
    executions: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "id": self.id, "created_at": self.created_at,
            "domains": self.domains,
            "agents": [r.to_dict() for r in self.reports],
            "findings": [f.to_dict() for f in self.findings],
            "plan": self.plan,
            "explanation": self.explanation,
            "explanation_source": self.explanation_source,
            "llm_note": self.llm_note,
            "executions": self.executions,
            "summary": {
                "agents_total": len(self.reports),
                "agents_ok": sum(1 for r in self.reports if r.status == "ok"),
                "agents_skipped": sum(1 for r in self.reports if r.status != "ok"),
                "findings_total": len(self.findings),
                "high": sum(1 for f in self.findings if f.severity == "high"),
                "medium": sum(1 for f in self.findings if f.severity == "medium"),
                "auto_fixable": sum(1 for f in self.findings if f.auto_fixable),
            },
        }


def _finding(agent, key, severity, title, detail, evidence, action="",
             action_args=None, auto_fixable=False, requires_confirm=False,
             suggestion="", target_key="", confidence=1.0) -> Finding:
    return Finding(
        id="%s-%s" % (agent, key), agent=agent, severity=severity,
        title=title, detail=detail, evidence=evidence or {}, action=action,
        action_args=action_args or {}, auto_fixable=auto_fixable,
        requires_confirm=requires_confirm, confidence=confidence,
        target_key=target_key or ("%s:%s" % (agent, key)), suggestion=suggestion,
    )


def _parse_ts(raw) -> Optional[datetime]:
    """兼容带 +08:00 时区与 naive 两种时间戳，统一转 naive 比较。"""
    s = str(raw or "")[:19]
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 7 个专职诊断 Agent（全部只读；异常隔离由 safe_diagnose 统一处理）
# --------------------------------------------------------------------------
class BaseAgent:
    key = ""
    title = ""
    scope = ""

    def diagnose(self) -> List[Finding]:
        raise NotImplementedError

    def safe_diagnose(self) -> AgentReport:
        t0 = time.time()
        try:
            items = self.diagnose() or []
            return AgentReport(agent=self.key, title=self.title, status="ok",
                               findings=items,
                               duration_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return AgentReport(agent=self.key, title=self.title, status="skipped",
                               error="%s: %s" % (type(e).__name__, str(e)[:200]),
                               duration_ms=int((time.time() - t0) * 1000))


class BackupJobAgent(BaseAgent):
    """① 备份作业诊断：连续失败 / 调度逾期 / 启用未跑 / 长期无成功。"""
    key, title = "backup_job", "备份作业健康"
    scope = "连续失败、调度逾期、启用未运行、长期无成功"

    def diagnose(self):
        out = []
        try:
            from core import job_monitor
            summary = job_monitor.summary() or {}
            attention = summary.get("attention") or []
        except Exception:
            attention = []
            summary = {}
        for a in attention[:20]:
            reasons = a.get("reasons") or []
            r0 = str(reasons[0] if reasons else "abnormal")
            tid = a.get("task_id")
            sev = "high" if str(a.get("severity")) == "high" else "medium"
            name = a.get("task_name") or ("任务 %s" % tid)
            note = str(a.get("note") or r0)
            is_fail = "consecutive_fail" in r0 or "连续失败" in note
            out.append(_finding(
                self.key, "job%s" % tid, sev, "任务「%s」%s" % (name, note[:60]),
                "作业监控分诊结论：%s" % note[:200],
                {"task_id": tid, "reasons": reasons, "summary": "job_monitor"},
                action="retry_failed_backup" if (is_fail and tid) else "",
                action_args={"task_id": tid} if tid else {},
                auto_fixable=bool(is_fail and tid),
                requires_confirm=False,
                suggestion=("建议先查看该任务的操作日志定位根因，再决定重试"
                            if not is_fail else ""),
                target_key="task:%s" % tid,
            ))
        if not attention and not out:
            # 降级：job_monitor 不可用时直接读备份记录
            rows = db.query(
                "SELECT task_id, COUNT(*) AS c FROM backup_records "
                "WHERE status='failed' GROUP BY task_id ORDER BY c DESC LIMIT 10")
            for r in rows or []:
                if int(r.get("c") or 0) < 2:
                    continue
                tid = r.get("task_id")
                out.append(_finding(
                    self.key, "fallback%s" % tid, "high",
                    "任务 %s 近期有 %s 条失败记录" % (tid, r["c"]),
                    "作业监控不可用，按备份记录降级统计（只读）。",
                    {"task_id": tid, "failed": r["c"], "degraded": True},
                    action="retry_failed_backup", action_args={"task_id": tid},
                    auto_fixable=True, target_key="task:%s" % tid,
                    confidence=0.8))
        return out


class RecordIntegrityAgent(BaseAgent):
    """② 备份产物完整性：失败记录 / 0 字节成功 / 僵尸 running。"""
    key, title = "record_integrity", "备份产物完整性"
    scope = "失败记录、空产物、卡死的 running 记录"

    def diagnose(self):
        out = []
        rows = db.query(
            "SELECT id, task_id, status, size_bytes, started_at "
            "FROM backup_records ORDER BY id DESC LIMIT 500") or []
        failed = [r for r in rows if str(r.get("status")) == "failed"]
        empty = [r for r in rows if str(r.get("status")) == "success"
                 and int(r.get("size_bytes") or 0) <= 0]
        cutoff = datetime.now() - timedelta(minutes=30)
        zombies = []
        for r in rows:
            if str(r.get("status")) != "running":
                continue
            ts = _parse_ts(r.get("started_at"))
            if ts and ts < cutoff:
                zombies.append(r)
        if failed:
            out.append(_finding(
                self.key, "failed", "high", "最近 %d 条备份记录失败" % len(failed),
                "样本窗口：最近 500 条备份记录；失败样例 ID=%s" %
                ",".join(str(r.get("id")) for r in failed[:5]),
                {"failed_count": len(failed),
                 "sample_ids": [r.get("id") for r in failed[:10]]},
                suggestion="查看操作日志定位根因；可在记录页或对话中说「重试任务 #ID」"))
        if empty:
            out.append(_finding(
                self.key, "empty", "high", "%d 条成功记录产物为 0 字节" % len(empty),
                "成功但 size_bytes<=0 属于可疑产物，需人工复核。",
                {"empty_count": len(empty),
                 "record_ids": [r.get("id") for r in empty[:10]]},
                suggestion="对可疑产物做恢复预检与安全检测，不要直接删除"))
        for r in zombies[:10]:
            out.append(_finding(
                # ID 必须绑定真实记录号：用枚举序号会在修掉一条后让后续
                # 记录「顶上来」复用同一个 id，导致回验误判为问题仍存在
                self.key, "zombie%s" % r.get("id"), "medium",
                "记录 %s 卡在 running 超过 30 分钟" % r.get("id"),
                "进程中断/重启后残留的僵尸态会误导「停止」按钮与健康统计。",
                {"record_id": r.get("id"), "started_at": r.get("started_at")},
                action="clear_zombie_running",
                action_args={"record_id": r.get("id")},
                auto_fixable=True, requires_confirm=False,
                suggestion="仅修正记录状态，不删除任何备份文件",
                target_key="record:%s" % r.get("id")))
        return out


class StorageCapacityAgent(BaseAgent):
    """③ 存储容量：备份根目录余量。"""
    key, title = "storage_capacity", "存储容量"
    scope = "备份根目录磁盘余量、剩余空间预警"

    def diagnose(self):
        try:
            import config
            root = str(getattr(config, "BACKUP_ROOT", "") or
                       os.environ.get("BACKUP_ROOT") or "/tmp")
        except Exception:
            root = os.environ.get("BACKUP_ROOT") or "/tmp"
        if not os.path.isdir(root):
            # 目录不存在时如实报配置问题，不能让整个 Agent 抛异常被跳过
            return [_finding(
                self.key, "root_missing", "medium",
                "备份根目录不存在：%s" % root,
                "备份产物无法落盘，请检查 BACKUP_ROOT 配置、挂载与目录权限。",
                {"root": root, "exists": False},
                suggestion="确认挂载后重启平台；平台不会自动创建生产备份根目录",
                target_key="storage:%s" % root)]
        du = shutil.disk_usage(root)
        pct = round(du.used / du.total * 100, 1) if du.total else 0
        free_gb = round(du.free / 1024 ** 3, 2)
        if pct < 80:
            return []
        sev = "high" if pct >= 90 else "medium"
        return [_finding(
            self.key, "disk", sev, "备份存储已用 %s%%，剩余 %s GB" % (pct, free_gb),
            "路径 %s；容量紧张会导致备份落盘失败（ENOSPC）。" % root,
            {"root": root, "used_percent": pct, "free_gb": free_gb,
             "total_gb": round(du.total / 1024 ** 3, 2)},
            suggestion=("平台不会自动删除备份数据。请到「备份记录 → 定期清理」"
                        "先做扫描预览，确认保留策略后再清理，或扩容存储。"),
            target_key="storage:%s" % root)]


class RealtimeLinkAgent(BaseAgent):
    """④ 实时链路：CDC 守护降级 / 失败 / 恢复点缺口。"""
    key, title = "realtime_link", "实时备份链路"
    scope = "CDC 守护健康、降级原因、恢复点连续性"

    def diagnose(self):
        out = []
        rows = db.query(
            "SELECT task_id, daemon_status, degrade_reason "
            "FROM rt_capture_state") or []
        bad = [r for r in rows if str(r.get("daemon_status") or "").lower()
               in ("failed", "degraded", "red", "yellow")]
        if bad:
            out.append(_finding(
                self.key, "daemon", "high" if len(bad) > 1 else "medium",
                "%d 个实时备份守护处于降级/失败" % len(bad),
                "降级原因样例：%s" %
                ", ".join(str(r.get("degrade_reason") or "-")[:60] for r in bad[:3]),
                {"count": len(bad),
                 "samples": [dict(r) for r in bad[:5]]},
                suggestion=("检查源库 binlog/WAL 与客户端工具是否就绪；"
                            "平台不自动重启实时守护，避免掩盖真实故障")))
        return out


class RestoreReadinessAgent(BaseAgent):
    """⑤ 恢复就绪：近期恢复失败 / 恢复校验失败。"""
    key, title = "restore_readiness", "恢复就绪"
    scope = "恢复执行失败、恢复校验失败报告"

    def diagnose(self):
        out = []
        try:
            rows = db.query(
                "SELECT id, status, message FROM restore_records "
                "ORDER BY id DESC LIMIT 200") or []
            failed = [r for r in rows if str(r.get("status")) == "failed"]
        except Exception:
            rows, failed = [], []
        if failed:
            out.append(_finding(
                self.key, "restore_fail", "high",
                "最近 %d 条恢复记录失败" % len(failed),
                "恢复失败样例：%s" % str(failed[0].get("message") or "")[:160],
                {"failed_count": len(failed),
                 "record_ids": [r.get("id") for r in failed[:10]]},
                suggestion="先对源备份记录做恢复预检与产物完整性校验"))
        try:
            reports = db.query(
                "SELECT id, result, task_id FROM restore_test_reports "
                "ORDER BY id DESC LIMIT 100") or []
            bad = [r for r in reports if str(r.get("result") or "").lower()
                   not in ("pass", "success", "ok", "")]
        except Exception:
            bad = []
        if bad:
            out.append(_finding(
                self.key, "verify_fail", "high",
                "%d 份恢复校验报告未通过" % len(bad),
                "恢复校验是真实 RESTORE VALIDATE / SQLFILE 解析，未通过必须处置。",
                {"count": len(bad),
                 "report_ids": [r.get("id") for r in bad[:10]]},
                suggestion="检查备份片完整性与目标实例空间，必要时重新做恢复校验"))
        return out


class DisasterReadinessAgent(BaseAgent):
    """⑥ 容灾就绪：链路缺切换配置 / 保护目标缺口。"""
    key, title = "disaster_readiness", "容灾就绪"
    scope = "容灾链路切换配置、RPO/RTO 目标缺口"

    def diagnose(self):
        out = []
        try:
            links = db.query(
                "SELECT id, name, failover_config FROM disaster_links "
                "WHERE status='active'") or []
            missing = [r for r in links if not str(r.get("failover_config") or "").strip()
                       or str(r.get("failover_config")).strip() in ("{}", "null")]
        except Exception:
            links, missing = [], []
        if missing:
            out.append(_finding(
                self.key, "no_failover_cfg", "medium",
                "%d 条容灾链路未配置切换参数" % len(missing),
                "未配置主备连接与 DNS 的链路无法执行自动/手动切换。",
                {"count": len(missing),
                 "link_ids": [r.get("id") for r in missing[:10]]},
                suggestion="在容灾链路页用「配置」补齐主备连接与 DNS 后做切换演练"))
        try:
            from core import goals
            raw = goals.protection_gaps() or {}
            # protection_gaps() 返回汇总 Dict，不能把键数当成缺口数
            gaps = raw.get("gaps") or [] if isinstance(raw, dict) else list(raw)
            gaps = [g for g in gaps if isinstance(g, dict) and
                    str(g.get("level") or "warn") in ("fail", "warn")]
        except Exception:
            gaps = []
        if gaps:
            out.append(_finding(
                self.key, "goal_gaps", "medium",
                "%d 项保护目标未达标" % len(gaps),
                "目标驱动评估只告警、不自动改配置（既有设计约束）。",
                {"count": len(gaps), "samples": gaps[:3]},
                suggestion="按缺口逐条调整 RPO 目标 / 备份频率 / 异地副本"))
        return out


class SecurityAnomalyAgent(BaseAgent):
    """⑦ 安全异常：产物异常检测结果、锁定记录覆盖。"""
    key, title = "security_anomaly", "备份安全"
    scope = "产物异常检测告警、不可变锁定覆盖"

    def diagnose(self):
        out = []
        try:
            logs = db.query(
                "SELECT id, level, message, ts FROM system_logs "
                "WHERE source='backup_anomaly' AND level IN ('WARNING','ERROR') "
                "ORDER BY id DESC LIMIT 100") or []
            suspects = [r for r in logs if "suspect" in
                        str(r.get("message") or "").lower()]
        except Exception:
            logs, suspects = [], []
        if suspects:
            out.append(_finding(
                self.key, "suspect", "high",
                "%d 条备份产物被标记为可疑" % len(suspects),
                "启发式检测（体积突变/熵/结构）只提示，不拦截恢复、不做病毒查杀。",
                {"count": len(suspects),
                 "messages": [str(r.get("message") or "")[:120]
                              for r in suspects[:5]]},
                suggestion="人工复核可疑产物；平台不会自动解除隔离或删除记录"))
        try:
            total = db.query_one("SELECT COUNT(*) AS c FROM backup_records") or {}
            locked = db.query_one(
                "SELECT COUNT(*) AS c FROM backup_records WHERE locked=1") or {}
            n_total = int(total.get("c") or 0)
            n_locked = int(locked.get("c") or 0)
        except Exception:
            n_total = n_locked = 0
        if n_total >= 10 and n_locked == 0:
            out.append(_finding(
                self.key, "no_lock", "low",
                "%d 条备份记录均未加保留锁" % n_total,
                "未锁定的产物可被清理策略或误删操作移除。",
                {"records": n_total, "locked": 0},
                suggestion="对关键恢复点启用「保留锁定」，或在任务高级选项配置自动锁定"))
        return out


AGENTS: Dict[str, BaseAgent] = {
    a.key: a for a in (
        BackupJobAgent(), RecordIntegrityAgent(), StorageCapacityAgent(),
        RealtimeLinkAgent(), RestoreReadinessAgent(),
        DisasterReadinessAgent(), SecurityAnomalyAgent(),
    )
}


def list_agents() -> List[dict]:
    return [{"key": a.key, "title": a.title, "scope": a.scope}
            for a in AGENTS.values()]


# --------------------------------------------------------------------------
# 动作白名单（红线：LLM 只能选动作，不能生成命令/SQL/脚本）
# --------------------------------------------------------------------------
def _act_retry_backup(args):
    from core import scheduler
    tid = int(args.get("task_id") or 0)
    if not tid:
        raise ValueError("task_id 必填")
    rec = scheduler.run_task_now(tid, operator="ai_agent_autofix")
    if not rec:
        raise RuntimeError("任务不存在或未返回结果")
    return {"task_id": tid, "record_id": rec.get("id"),
            "status": rec.get("status"), "async": bool(rec.get("accepted"))}


def _act_clear_zombie(args):
    rid = int(args.get("record_id") or 0)
    if not rid:
        raise ValueError("record_id 必填")
    row = db.query_one("SELECT status FROM backup_records WHERE id=?", (rid,))
    if not row or str(row.get("status")) != "running":
        raise RuntimeError("记录不存在或已不是 running，跳过（幂等）")
    db.execute(
        "UPDATE backup_records SET status='failed', "
        "message=COALESCE(message,'') || ' [自治修复: 僵尸 running 状态已收敛]' "
        "WHERE id=? AND status='running'", (rid,))
    return {"record_id": rid, "new_status": "failed"}


def _act_precheck(args):
    from core import backup_precheck, models
    tid = int(args.get("task_id") or 0)
    task = models.get_task(tid, include_secret=True)
    if not task:
        raise ValueError("任务不存在")
    rep = backup_precheck.precheck_backup(task)
    return {"task_id": tid, "overall": rep.get("overall"),
            "fail": len(rep.get("fails") or [])}


def _act_inspection(args):
    from core import inspection
    # core.inspection.run_inspection 只接受 (task_id, triggered_by)；
    # 旧代码传 scope="quick" 会抛 TypeError，导致自治修复动作必然 failed。
    res = inspection.run_inspection(triggered_by="ai_agent_autofix")
    if not isinstance(res, dict):
        return {"status": str(res)}
    return {"total": res.get("total", 0), "pass": res.get("pass", 0),
            "warn": res.get("warn", 0), "fail": res.get("fail", 0),
            "scope": "quick"}


def _act_enable_task(args):
    from core import models
    tid = int(args.get("task_id") or 0)
    if not tid:
        raise ValueError("task_id 必填")
    models.update_task(tid, {"enabled": 1})
    return {"task_id": tid, "enabled": 1}


def _act_restore_verify(args):
    from core import restore_verify
    tid = int(args.get("task_id") or 0)
    if not tid:
        raise ValueError("task_id 必填")
    rid = restore_verify.run_restore_verify_policy(
        task_id=tid, name="AI 自治复检", schedule_type="manual", enabled=0)
    return {"policy_id": rid}


ACTIONS = {
    "retry_failed_backup": {
        "title": "重试该任务最近一次失败备份", "risk": "low",
        "async": True, "executor": _act_retry_backup,
    },
    "clear_zombie_running": {
        "title": "收敛僵尸 running 记录", "risk": "low",
        "async": False, "executor": _act_clear_zombie,
    },
    "run_backup_precheck": {
        "title": "对该任务做备份预检", "risk": "low",
        "async": False, "executor": _act_precheck,
    },
    "run_inspection_quick": {
        "title": "执行快速巡检取证", "risk": "medium",
        "async": False, "executor": _act_inspection,
    },
    "enable_task": {
        "title": "重新启用停用的备份任务", "risk": "high",
        "async": False, "executor": _act_enable_task,
    },
    "trigger_restore_verify": {
        "title": "发起一次恢复校验", "risk": "medium",
        "async": True, "executor": _act_restore_verify,
    },
}


# --------------------------------------------------------------------------
# Router / Worker 编排 + 黑板 + 冲突仲裁
# --------------------------------------------------------------------------
class Orchestrator:
    def __init__(self):
        self._runs: "OrderedDict[str, Run]" = OrderedDict()
        self._lock = threading.Lock()

    # ---- 黑板：去重 + 冲突仲裁 ----
    @staticmethod
    def _dedupe(findings: List[Finding]) -> List[Finding]:
        best: Dict[str, Finding] = {}
        for f in findings:
            k = f.target_key or f.id
            cur = best.get(k)
            if cur is None or SEVERITY_RANK.get(f.severity, 0) > \
                    SEVERITY_RANK.get(cur.severity, 0):
                best[k] = f
        return sorted(best.values(),
                      key=lambda x: (-SEVERITY_RANK.get(x.severity, 0), x.id))

    @staticmethod
    def _plan(findings: List[Finding]) -> List[dict]:
        plan = []
        for f in findings:
            if not f.action:
                continue
            spec = ACTIONS.get(f.action)
            if not spec:                       # 白名单外动作一律拒绝
                continue
            plan.append({
                "finding_id": f.id, "title": f.title, "severity": f.severity,
                "action": f.action, "action_title": spec["title"],
                "args": f.action_args, "risk": spec["risk"],
                "requires_confirm": f.requires_confirm or spec["risk"] != "low",
                "async": spec["async"],
            })
        return plan

    # ---- 可选 LLM：仅做结论解释，绝不参与诊断决策 ----
    @staticmethod
    def _llm_explain(run: Run) -> tuple:
        try:
            from core.ai_alert import AIPredictor
            p = AIPredictor()
            cfg = p.get_config() or {}
            model = cfg.get("ai_model") or {}
            if not model.get("enabled") or not p._get_model_uri(cfg):
                return "", "local_rules", "未启用或未配置模型服务，使用本地规则结论"
            top = run.findings[:6]
            brief = "\n".join(
                "- [%s] %s：%s" % (f.severity, f.title, f.detail[:120])
                for f in top) or "- 未发现问题"
            prompt = (
                "你是 AIDBM 灾备运维助手。下面是平台 7 个自治 Agent 的**真实结构化诊断结果**。"
                "只允许依据这些结果用中文写 3-5 句结论与处置优先级，"
                "禁止补充未出现的事实、禁止编造数据、禁止建议删除备份或自动切换主备。\n\n"
                + brief)
            raw = p._call_model(prompt, cfg, max_tokens=400, timeout=8)
            text = ""
            if isinstance(raw, dict) and not raw.get("error"):
                # _call_model 成功只返回 ok/status_code/latency_ms/
                # response_body/content；历史上按 choices 直取永远为空，
                # 即使 HTTP 200 也会降级成「模型返回为空」的规则结论。
                text = str(raw.get("content") or "")
                if not text and raw.get("response_body"):
                    from core.ai_alert import extract_model_content
                    text = extract_model_content(raw["response_body"])
            if not text.strip():
                note = str((raw or {}).get("error") or "模型返回为空")
                return "", "llm_degraded", "模型不可用，已降级为规则结论：" + note[:120]
            return text.strip()[:1200], "llm", ""
        except Exception as e:
            return "", "llm_degraded", \
                "模型解释失败，已降级为规则结论：%s" % str(e)[:120]

    @staticmethod
    def _local_explain(run: Run) -> str:
        s = run.to_dict()["summary"]
        if not run.findings:
            return "7 个自治 Agent 本轮未发现需要处置的问题（%d 个 Agent 正常完成诊断）。" \
                % s["agents_ok"]
        lines = ["本轮共 %d 项发现：高危 %d、中危 %d，其中 %d 项可由平台安全动作处理。" %
                 (s["findings_total"], s["high"], s["medium"], s["auto_fixable"])]
        for f in run.findings[:5]:
            lines.append("- [%s] %s" % (f.severity, f.title))
        lines.append("未配置 LLM 或模型不可达时，结论完全来自本地规则与真实元数据，"
                     "不含模型推测内容。")
        return "\n".join(lines)

    # ---- 对外主入口 ----
    def sweep(self, domains: List[str] = None, use_llm: bool = False,
              parallel: bool = True) -> Run:
        keys = [k for k in (domains or []) if k in AGENTS] or list(AGENTS)
        agents = [AGENTS[k] for k in keys]
        if parallel and len(agents) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(agents))) as pool:
                reports = list(pool.map(lambda a: a.safe_diagnose(), agents))
        else:
            reports = [a.safe_diagnose() for a in agents]
        findings = self._dedupe(
            [f for r in reports for f in r.findings])
        run = Run(
            id=uuid.uuid4().hex[:12], created_at=datetime.now().isoformat(timespec="seconds"),
            domains=keys, reports=reports, findings=findings,
            plan=self._plan(findings))
        run.explanation = self._local_explain(run)
        run.explanation_source = "local_rules"
        if use_llm:
            text, source, note = self._llm_explain(run)
            if text:
                run.explanation, run.explanation_source = text, source
            else:
                run.llm_note = note
                run.explanation_source = source
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > MAX_RUNS:
                self._runs.popitem(last=False)
        try:
            db.add_log("INFO", "ai_agent",
                       "自治诊断完成 run=%s 发现=%d 高危=%d 来源=%s" %
                       (run.id, len(findings),
                        sum(1 for f in findings if f.severity == "high"),
                        run.explanation_source))
        except Exception:
            pass
        return run

    def get(self, run_id: str) -> Optional[Run]:
        with self._lock:
            return self._runs.get(run_id)

    def latest(self) -> Optional[Run]:
        """最近一次诊断批次（对话里省略 run_id 时兜底，仍须显式确认才执行动作）。"""
        with self._lock:
            return next(reversed(self._runs.values()), None) if self._runs else None

    def recent(self, limit: int = 10) -> List[dict]:
        with self._lock:
            return [r.to_dict()["summary"] | {"id": r.id,
                                              "created_at": r.created_at}
                    for r in list(self._runs.values())[-limit:]][::-1]

    # ---- 修复 + 回验闭环 ----
    def execute(self, run_id: str, finding_ids: List[str],
                approved: bool = False, dry_run: bool = True,
                verify_code: str = None) -> dict:
        run = self.get(run_id)
        if not run:
            return {"ok": False, "error": "诊断批次不存在或已被清理"}
        wanted = set(finding_ids or [])
        results = []
        for f in run.findings:
            if f.id not in wanted:
                continue
            spec = ACTIONS.get(f.action)
            if not spec:
                results.append({"finding_id": f.id, "ok": False,
                                "status": "rejected",
                                "message": "动作不在平台白名单内，拒绝执行"})
                continue
            need_confirm = f.requires_confirm or spec["risk"] != "low"
            if need_confirm and not approved:
                results.append({"finding_id": f.id, "ok": False,
                                "status": "needs_confirm",
                                "message": "该动作风险等级 %s，须显式确认" %
                                           spec["risk"]})
                continue
            # 高风险动作第二道闸：复用平台既有的高危操作验证码（开关默认关，
            # 开启后聊天通道与 API 通道都必须过闸，不能因「本地直执」绕过）。
            if spec["risk"] == "high":
                try:
                    import auth
                    if auth.op_verify_enabled():
                        ok, err = auth.check_verify_code(verify_code)
                        if not ok:
                            results.append({
                                "finding_id": f.id, "ok": False,
                                "status": "verify_code_required", "message": err})
                            continue
                except RuntimeError:
                    results.append({
                        "finding_id": f.id, "ok": False, "status": "no_session",
                        "message": "高危动作必须在 Web 会话内执行"})
                    continue
            if dry_run:
                results.append({"finding_id": f.id, "ok": True,
                                "status": "dry_run", "action": f.action,
                                "message": "预演：%s（未执行）" % spec["title"]})
                continue
            t0 = time.time()
            try:
                out = spec["executor"](dict(f.action_args or {}))
                executed = True
                err = ""
            except Exception as e:
                out, executed, err = {}, False, "%s: %s" % (
                    type(e).__name__, str(e)[:200])
            # 回验：重跑该 Agent 的只读诊断，看同 id 发现是否消失
            recheck = {"status": "unknown", "note": ""}
            try:
                agent = AGENTS.get(f.agent)
                if agent:
                    again = agent.safe_diagnose()
                    still = any(x.id == f.id for x in again.findings)
                    if spec["async"]:
                        recheck = {"status": "async_pending",
                                   "note": "动作已发起但结果异步产生，需稍后复查"}
                    else:
                        recheck = {"status": "persist" if still else "resolved",
                                   "note": "回验：该发现%s" %
                                           ("仍存在" if still else "已消失")}
            except Exception as e:
                recheck = {"status": "unknown",
                           "note": "回验失败：%s" % str(e)[:120]}
            item = {
                "finding_id": f.id, "ok": executed, "action": f.action,
                "status": "executed" if executed else "failed",
                "result": out, "error": err, "recheck": recheck,
                "duration_ms": int((time.time() - t0) * 1000),
            }
            results.append(item)
            try:
                db.add_log("WARNING" if not executed else "INFO", "ai_agent",
                           "自治修复 %s run=%s finding=%s ok=%s recheck=%s" %
                           (f.action, run_id, f.id, executed,
                            recheck["status"]),
                           task_id=(f.action_args or {}).get("task_id"))
            except Exception:
                pass
        run.executions.append({"at": datetime.now().isoformat(timespec="seconds"),
                               "approved": approved, "dry_run": dry_run,
                               "results": results})
        return {"ok": True, "run_id": run_id, "dry_run": dry_run,
                "results": results}


_ORCHESTRATOR = Orchestrator()


def get_orchestrator() -> Orchestrator:
    return _ORCHESTRATOR
