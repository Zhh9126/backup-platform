# -*- coding: utf-8 -*-
"""自然语言动作层（离线优先）。

把「用一句话操作平台」从 11 个工具扩展到全站主要功能域：声明式动作目录
+ 通用槽位抽取 + 多步编排 + 内部 API 真实执行（复用 RBAC / 验证码闸 / 审计）。

设计约束：
- 完全离线：不依赖 LLM，纯标准库词法/短语匹配；未命中时交回既有 SkillRouter/LLM。
- 真实执行：所有动作调用平台自身 HTTP API（透传调用方会话），不自行造状态、
  不仿真；只读直接执行，写操作二次确认，高危动作额外校验验证码闸。
- 不猜参数：缺必填槽位时追问，任务名多义时给候选，绝不随便挑一个执行。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 动作目录：短语优先匹配（长短语 > 短短语），未命中再用动词+域词打分
# risk: read=直接执行 / write=需确认 / high=需确认且受验证码闸约束
# ---------------------------------------------------------------------------
CATALOG: List[Dict] = [
    # ---------------- 只读：查询类 ----------------
    dict(name="list_tasks", desc="列出备份任务", risk="read", method="GET",
         path="/api/tasks", query=["db_type", "keyword", "enabled"],
         phrases=["任务列表", "所有任务", "列出任务", "查任务", "有哪些任务",
                  "任务清单", "备份任务清单", "看看任务", "查询任务"],
         verbs=["列出", "查询", "查看", "看看", "有哪些"], domains=["任务"]),
    dict(name="list_records", desc="查询备份记录", risk="read", method="GET",
         path="/api/records", query=["limit", "status", "task_id"],
         phrases=["备份记录", "备份历史", "最近备份", "失败记录", "成功记录",
                  "备份情况", "备份明细", "查记录", "备份记录列表"],
         verbs=["查询", "查看", "列出", "看看", "最近"], domains=["记录", "备份"]),
    dict(name="list_restores", desc="查询恢复记录", risk="read", method="GET",
         path="/api/restores", query=[],
         phrases=["恢复记录", "恢复历史", "还原记录", "恢复情况"],
         verbs=["查询", "查看", "列出"], domains=["恢复"]),
    dict(name="task_detail", desc="查看任务详情", risk="read", method="GET",
         path="/api/tasks/{task_id}", query=[], need=["task_id"],
         phrases=["任务详情", "任务信息", "看看任务"],
         verbs=["详情", "信息"], domains=["任务"]),
    dict(name="record_tables", desc="查看备份对象清单", risk="read", method="GET",
         path="/api/records/{record_id}/tables", query=[], need=["record_id"],
         phrases=["对象清单", "表清单", "备份对象", "有哪些表"],
         verbs=["查看", "列出"], domains=["记录", "对象", "表"]),
    dict(name="job_monitor", desc="查看任务监控概览", risk="read", method="GET",
         path="/api/job-monitor/summary", query=["limit"],
         phrases=["任务监控", "今日执行", "作业监控", "连续失败", "未按计划"],
         verbs=["查看", "看看"], domains=["监控", "执行"]),
    dict(name="report_summary", desc="查看报表中心汇总", risk="read", method="GET",
         path="/api/report-center/summary", query=["days"],
         phrases=["报表中心", "运行报表", "统计报表", "备份报表", "周报"],
         verbs=["查看", "看看", "生成"], domains=["报表", "统计"]),
    dict(name="storage_usage", desc="查看存储用量", risk="read", method="GET",
         path="/api/storage/usage", query=[],
         phrases=["存储用量", "磁盘用量", "空间使用", "存储统计", "磁盘空间"],
         verbs=["查看", "看看", "查询"], domains=["存储", "磁盘", "空间"]),
    dict(name="list_hosts", desc="列出纳管主机", risk="read", method="GET",
         path="/api/hosts", query=[],
         phrases=["纳管主机", "主机列表", "所有主机", "列出主机", "SSH主机"],
         verbs=["列出", "查看", "查询"], domains=["主机"]),
    dict(name="alert_predictions", desc="查看智能告警预测", risk="read", method="GET",
         path="/api/alerts/predictions", query=["days", "metric"],
         phrases=["告警预测", "智能告警", "预测告警", "风险预测", "告警列表"],
         verbs=["查看", "查询", "列出"], domains=["告警", "预测"]),
    dict(name="inspection_records", desc="查看巡检记录", risk="read", method="GET",
         path="/api/inspection/records", query=[],
         phrases=["巡检记录", "巡检结果", "巡检历史"],
         verbs=["查看", "查询", "列出"], domains=["巡检"]),
    dict(name="restore_verify_reports", desc="查看恢复校验报告", risk="read",
         method="GET", path="/api/restore-test-reports", query=["task_id"],
         phrases=["恢复校验报告", "校验报告", "校验结果", "恢复校验记录"],
         verbs=["查看", "查询", "列出"], domains=["校验", "恢复"]),
    dict(name="data_compare_reports", desc="查看数据对比报告", risk="read",
         method="GET", path="/api/data-compare-reports", query=["task_id", "limit"],
         phrases=["对比报告", "数据对比结果", "比对报告", "一致性报告"],
         verbs=["查看", "查询", "列出"], domains=["对比", "一致性"]),
    dict(name="migration_plans", desc="查看迁移计划", risk="read", method="GET",
         path="/api/db-migrate", query=[],
         phrases=["迁移计划", "迁移任务列表", "数据迁移计划"],
         verbs=["查看", "查询", "列出"], domains=["迁移"]),
    dict(name="sync_tasks", desc="查看同步任务", risk="read", method="GET",
         path="/api/sync-tasks", query=[],
         phrases=["同步任务", "同步列表", "数据同步任务"],
         verbs=["查看", "查询", "列出"], domains=["同步"]),
    dict(name="clone_requests", desc="查看克隆申请", risk="read", method="GET",
         path="/api/clone", query=[],
         phrases=["克隆申请", "克隆列表", "克隆记录", "VDB列表"],
         verbs=["查看", "查询", "列出"], domains=["克隆", "VDB"]),
    dict(name="drills", desc="查看演练记录", risk="read", method="GET",
         path="/api/drills", query=[],
         phrases=["演练记录", "演练列表", "演练历史", "容灾演练"],
         verbs=["查看", "查询", "列出"], domains=["演练"]),
    dict(name="rt_points", desc="查看实时恢复点", risk="read", method="GET",
         path="/api/rt/points", query=["task_id"],
         phrases=["恢复点", "时间点", "时间轴", "实时恢复点"],
         verbs=["查看", "查询", "列出"], domains=["恢复点", "实时"]),
    dict(name="locked_records", desc="查看锁定产物", risk="read", method="GET",
         path="/api/records/locked", query=[],
         phrases=["锁定记录", "不可变产物", "锁定产物", "WORM"],
         verbs=["查看", "查询", "列出"], domains=["锁定", "不可变"]),
    dict(name="pool_chains", desc="查看池复制链", risk="read", method="GET",
         path="/api/pool/chains", query=[],
         phrases=["复制链", "池复制", "两地三中心", "复制作业链"],
         verbs=["查看", "查询", "列出"], domains=["复制", "池"]),
    dict(name="protection_gaps", desc="查看保护缺口", risk="read", method="GET",
         path="/api/protection-gaps", query=[],
         phrases=["保护缺口", "RPO风险", "合规风险", "目标差距"],
         verbs=["查看", "查询"], domains=["风险", "缺口", "RPO"]),
    dict(name="license", desc="查看授权与配额", risk="read", method="GET",
         path="/api/system/license", query=[],
         phrases=["授权信息", "配额", "许可证", "license"],
         verbs=["查看", "查询"], domains=["授权", "配额"]),
    dict(name="verify_integrity", desc="校验记录完整性", risk="read", method="POST",
         path="/api/records/{record_id}/verify-integrity", body=[],
         need=["record_id"],
         phrases=["校验完整性", "完整性校验", "校验校验和", "检查校验和"],
         verbs=["校验", "检查"], domains=["记录", "完整性"]),
    dict(name="security_check", desc="恢复点安全检测", risk="read", method="POST",
         path="/api/records/{record_id}/security-check", body=[],
         need=["record_id"],
         phrases=["安全检测", "勒索检测", "异常检测", "安全检查"],
         verbs=["检测", "检查", "扫描"], domains=["记录", "安全"]),
    dict(name="backup_precheck", desc="备份预检", risk="read", method="POST",
         path="/api/tasks/{task_id}/precheck", body=[], need=["task_id"],
         phrases=["备份预检", "预检任务", "预检查", "检查备份条件"],
         verbs=["预检", "检查"], domains=["任务", "备份"]),
    dict(name="restore_precheck", desc="恢复预检", risk="read", method="POST",
         path="/api/records/{record_id}/restore-precheck", body=["target_db"],
         need=["record_id"],
         phrases=["恢复预检", "恢复前检查", "检查能不能恢复"],
         verbs=["预检", "检查"], domains=["恢复", "记录"]),
    dict(name="cleanup_preview", desc="备份清理预览", risk="read", method="POST",
         path="/api/backup-cleanup/preview", body=["keep_min", "task_id"],
         phrases=["清理预览", "扫描过期", "看看能清理", "过期备份"],
         verbs=["预览", "扫描", "查看"], domains=["清理", "过期"]),

    # ---------------- 写操作：需二次确认 ----------------
    dict(name="run_backup", desc="立即执行备份", risk="write", method="POST",
         path="/api/tasks/{task_id}/run", body=["backup_type"], need=["task_id"],
         slot_hint="请告诉我要备份的任务编号或任务名（例如「备份任务 77」）",
         confirm="将对任务 {task_id} 立即发起一次真实备份，会写入备份记录",
         phrases=["立即备份", "执行备份", "跑一次备份", "发起备份", "帮我备份",
                  "备份任务", "备份一下", "现在备份"],
         verbs=["备份", "执行", "发起"], domains=["任务", "备份"],
         followup=dict(path="/api/records", key="task_id", timeout=150)),
    dict(name="quick_backup", desc="一键备份全部启用任务", risk="write",
         method="POST", path="/api/tasks/quick-backup", body=["task_ids"],
         confirm="将对本平台全部启用任务各发起一次真实备份",
         phrases=["一键备份", "批量备份", "备份所有启用任务"],
         verbs=["备份"], domains=["全部", "一键"]),
    dict(name="run_inspection", desc="执行巡检", risk="write", method="POST",
         path="/api/inspection/run", body=["task_id", "triggered_by"],
         confirm="将真实连接各数据库执行一次平台巡检",
         phrases=["跑巡检", "执行巡检", "开始巡检", "做一次巡检"],
         verbs=["巡检", "检查"], domains=["巡检"]),
    dict(name="run_restore_verify", desc="执行恢复校验策略", risk="write",
         method="POST", path="/api/restore-verify-policies/{policy_id}/test",
         body=[], need=["policy_id"],
         confirm="将真实执行恢复校验策略 {policy_id} 并生成报告",
         phrases=["执行恢复校验", "跑恢复校验", "测试校验策略", "恢复校验策略"],
         verbs=["执行", "跑", "测试"], domains=["校验策略", "恢复校验"]),
    dict(name="run_sync", desc="执行同步任务", risk="write", method="POST",
         path="/api/sync-tasks/{sync_id}/run", body=[], need=["sync_id"],
         confirm="将真实执行同步任务 {sync_id}，会写入目标库",
         phrases=["执行同步", "跑同步", "同步任务", "执行数据同步"],
         verbs=["执行", "跑", "同步"], domains=["同步任务", "数据同步"]),
    dict(name="run_migration", desc="执行迁移计划", risk="write", method="POST",
         path="/api/migration-plans/{plan_id}/run", body=[], need=["plan_id"],
         confirm="将真实执行迁移计划 {plan_id}（结构+全量+校验）",
         phrases=["执行迁移", "跑迁移", "运行迁移计划", "开始迁移"],
         verbs=["执行", "跑", "迁移"], domains=["迁移计划", "数据迁移"]),
    dict(name="run_data_compare", desc="执行数据对比", risk="write", method="POST",
         path="/api/data-compare-tasks/{compare_id}/run", body=[],
         need=["compare_id"],
         confirm="将真实执行数据对比任务 {compare_id} 并生成报告",
         phrases=["执行对比", "跑数据对比", "运行对比任务", "开始比对"],
         verbs=["执行", "跑", "对比"], domains=["对比任务", "数据对比"]),
    dict(name="run_drill", desc="执行恢复演练", risk="write", method="POST",
         path="/api/drills/{drill_id}/run", body=[], need=["drill_id"],
         confirm="将真实执行演练 {drill_id}（真实恢复演练会临时建库并销毁）",
         phrases=["执行演练", "跑演练", "开始演练", "运行演练"],
         verbs=["执行", "跑", "演练"], domains=["演练"]),
    dict(name="rt_trigger", desc="实时备份封段", risk="write", method="POST",
         path="/api/rt/tasks/{task_id}/trigger", body=[], need=["task_id"],
         confirm="将对实时任务 {task_id} 强制封存一个日志段并生成恢复点",
         phrases=["封段", "强制封存", "生成恢复点", "触发实时封段"],
         verbs=["封存", "触发"], domains=["实时", "恢复点"]),
    dict(name="alerts_run", desc="执行一轮智能告警分析", risk="write",
         method="POST", path="/api/alerts/run", body=[],
         confirm="将真实执行一轮智能告警分析并可能写入预测/告警记录",
         phrases=["跑一轮告警", "执行告警分析", "生成告警预测", "运行告警"],
         verbs=["执行", "跑", "生成"], domains=["告警"]),
    dict(name="lock_record", desc="锁定备份记录", risk="write", method="POST",
         path="/api/records/{record_id}/lock", body=["days", "reason"],
         need=["record_id"], confirm="将锁定备份记录 {record_id}（产物去写位，防误删）",
         phrases=["锁定记录", "锁定产物", "设置不可变", "保留锁定"],
         verbs=["锁定", "保护"], domains=["记录", "不可变"]),
    dict(name="clone_request", desc="申请克隆服务", risk="high", method="POST",
         path="/api/clone", body=["source_record_id", "target_env", "requested_by",
                                  "clone_mode", "purpose"],
         need=["record_id"], confirm="将为备份记录 {record_id} 真实拉起一个克隆 VDB",
         phrases=["克隆记录", "申请克隆", "拉起克隆", "创建VDB"],
         verbs=["克隆", "申请"], domains=["克隆", "VDB"]),
    dict(name="restore", desc="发起数据恢复", risk="high", method="POST",
         path="/api/restores", body=["record_id", "target_db", "operator",
                                     "verify_code"],
         need=["record_id", "target_db"], verify_code=True,
         slot_hint="请说明备份记录编号和目标库（例如「把记录 102 恢复到 ft_nl_r」）",
         confirm="将把备份记录 {record_id} 真实恢复到目标库 {target_db}（覆盖同库数据）",
         phrases=["恢复记录", "把记录", "发起恢复", "还原到", "恢复到"],
         verbs=["恢复", "还原"], domains=["记录", "备份"]),
    dict(name="unlock_record", desc="解锁备份记录", risk="high", method="POST",
         path="/api/records/{record_id}/unlock", body=["reason", "verify_code"],
         need=["record_id"],
         verify_code=True, confirm="将解锁备份记录 {record_id}（高危，写审计日志）",
         phrases=["解锁记录", "解除锁定", "取消不可变"],
         verbs=["解锁", "解除"], domains=["记录", "锁定"]),
    dict(name="delete_task", desc="删除备份任务", risk="high", method="DELETE",
         path="/api/tasks/{task_id}", body=["verify_code"], need=["task_id"],
         verify_code=True,
         confirm="将删除备份任务 {task_id}（不可恢复）",
         phrases=["删除任务", "移除任务", "删掉任务"],
         verbs=["删除", "移除"], domains=["任务"]),
    dict(name="enable_task", desc="启用备份任务", risk="write", method="PUT",
         path="/api/tasks/{task_id}", body=["enabled"], need=["task_id"],
         confirm="将启用任务 {task_id}（可能被调度器自动触发）",
         phrases=["启用任务", "打开任务", "开启任务"],
         verbs=["启用", "开启", "打开"], domains=["任务"], fixed={"enabled": 1}),
    dict(name="disable_task", desc="停用备份任务", risk="write", method="PUT",
         path="/api/tasks/{task_id}", body=["enabled"], need=["task_id"],
         confirm="将停用任务 {task_id}（不再被调度触发）",
         phrases=["停用任务", "关闭任务", "禁用任务"],
         verbs=["停用", "关闭", "禁用"], domains=["任务"], fixed={"enabled": 0}),
]

_BY_NAME = {a["name"]: a for a in CATALOG}
# 长短语优先，避免「备份任务清单」被「备份任务」抢走
_ALL_PHRASES = sorted(
    [(p, a["name"]) for a in CATALOG for p in a.get("phrases", [])],
    key=lambda x: -len(x[0]))

_MULTISTEP_RE = re.compile(
    r"(?:然后|接着|之后|随后|并且|以及|；|;|，(?:再|然后|接着)|,\s*(?:再|然后))")
_ID_PATTERNS = {
    "task_id": [r"任务\s*#?\s*(\d+)", r"#\s*(\d+)\s*号?任务", r"(\d+)\s*号任务"],
    "record_id": [r"(?:备份)?记录\s*#?\s*(\d+)", r"#\s*(\d+)\s*号?记录",
                  r"记录号\s*(\d+)"],
    "plan_id": [r"(?:迁移)?计划\s*#?\s*(\d+)", r"第\s*(\d+)\s*个?计划"],
    "policy_id": [r"(?:校验)?策略\s*#?\s*(\d+)"],
    "sync_id": [r"同步任务\s*#?\s*(\d+)"],
    "compare_id": [r"(?:对比|比对)任务\s*#?\s*(\d+)"],
    "drill_id": [r"演练\s*#?\s*(\d+)"],
}
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_DB_TYPES = {
    "mysql": "mysql", "mariadb": "mariadb", "postgresql": "postgresql", "pg": "postgresql",
    "oracle": "oracle", "kingbase": "kingbase", "金仓": "kingbase",
    "dameng": "dameng", "达梦": "dameng", "sqlserver": "sqlserver",
    "redis": "redis", "mongodb": "mongodb",
}


def _num(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw)


def parse_slots(text: str) -> Dict[str, Any]:
    """从自然语言中抽取通用槽位（不依赖 LLM）。"""
    s: Dict[str, Any] = {}
    for key, pats in _ID_PATTERNS.items():
        for pat in pats:
            m = re.search(pat, text, re.I)
            if m:
                s[key] = int(m.group(1))
                break
    m = re.search(r"恢复(?:到|至|为|成)\s*[`'\"]?([A-Za-z_][\w$]*)", text)
    if not m:
        m = re.search(r"目标(?:库|数据库)\s*[`'\"]?([A-Za-z_][\w$]*)", text)
    if m:
        s["target_db"] = m.group(1)
    m = re.search(r"(?:最近|近|过去)\s*([一二三四五六七八九十两\d]+)\s*天", text)
    if m:
        v = _num(m.group(1))
        if v:
            s["days"] = v
    m = re.search(r"(?:最近|前|查(?:询)?|列出|看)\s*(\d+)\s*条", text)
    if m:
        s["limit"] = min(200, max(1, int(m.group(1))))
    if "失败" in text:
        s["status"] = "failed"
    elif "成功" in text:
        s["status"] = "success"
    if "增量" in text:
        s["backup_type"] = "incremental"
    elif "全量" in text:
        s["backup_type"] = "full"
    m = re.search(r"验证码\s*(\d{4})", text)
    if m:
        s["verify_code"] = m.group(1)
    for alias, val in _DB_TYPES.items():
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", text, re.I):
            s["db_type"] = val
            break
    m = re.search(r"[《\"'「]([^《\"'」]{1,40})[》\"'」]", text)
    if m:
        s["keyword"] = m.group(1).strip()
    m = re.search(r"(?:叫|名为|名称是|名字是)\s*([^\s，,。；;]{2,40})", text)
    if m:
        s["keyword"] = m.group(1).strip()
    # 无显式 ID 时的裸数字：只在明确语境里作为记录号/任务号，避免误判
    if not any(k in s for k in ("task_id", "record_id", "plan_id", "sync_id",
                                "compare_id", "drill_id", "policy_id")):
        m = re.search(r"(?<!\d)(\d{1,7})(?!\d)", text)
        if m:
            v = int(m.group(1))
            if "记录" in text or "备份" in text and "任务" not in text:
                s["record_id"] = v
            elif "任务" in text:
                s["task_id"] = v
    # 动作专用别名
    if "record_id" in s:
        s["source_record_id"] = s["record_id"]
    if "task_id" in s and "sync" in text and "同步" in text:
        s["sync_id"] = s["task_id"]
    if "task_id" in s and "对比" in text:
        s["compare_id"] = s["task_id"]
    s.setdefault("requested_by", "ai-agent")
    s.setdefault("target_env", "test")
    s.setdefault("clone_mode", "logical")
    s.setdefault("purpose", "test")
    s.setdefault("operator", "ai-agent")
    s.setdefault("triggered_by", "ai-agent")
    s.setdefault("reason", "AI 智能助手自然语言发起")
    s.setdefault("days", 1)
    return s


def resolve_task_name(text: str, slots: Dict) -> Tuple[Dict, Optional[List[Dict]], Optional[str]]:
    """把自然语言里的任务名解析成真实 task_id；多义时给候选，绝不猜。"""
    if slots.get("task_id"):
        return slots, None, None
    m = re.search(
        r"(?:备份|预检|启用|停用|删除|任务)\s*([\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]{1,39})"
        r"(?:\s*(?:的任务|这个任务|吧|呀|一下|一次))?", text)
    name = (m.group(1) if m else slots.get("keyword") or "").strip()
    name = re.sub(r"(?:一下|一次|吧|呀|的任务|这个任务|任务|列表|记录|情况|历史)$", "", name)
    if not name or name.isdigit():
        return slots, None, None
    try:
        from core import models
        tasks = models.list_tasks(include_secret=False) or []
    except Exception as e:
        return slots, None, "任务清单读取失败: %s" % e
    low = name.lower()
    hits = [t for t in tasks
            if low in str(t.get("name") or "").lower()
            or str(t.get("name") or "").lower() in low]
    if len(hits) == 1:
        slots["task_id"] = int(hits[0]["id"])
        slots["task_name"] = hits[0].get("name")
        return slots, None, None
    if len(hits) > 1:
        return slots, [{"id": t["id"], "name": t.get("name")} for t in hits[:8]], None
    return slots, None, "没有找到名称包含「%s」的任务" % name


def _score_action(action: Dict, text: str) -> int:
    score = 0
    for p in action.get("phrases", []):
        if p in text:
            score += 10 + len(p)
    for v in action.get("verbs", []):
        if v in text:
            score += 3
    for d in action.get("domains", []):
        if d in text:
            score += 2
    return score


# 查询语境词：出现时只允许匹配只读动作，避免「列出所有备份任务」被
# 执行类短语「备份任务」抢走（真机静态验证暴露的误匹配）。
_QUERY_WORDS = ("列出", "查询", "查看", "看看", "有哪些", "清单", "列表",
                "历史", "情况", "报告", "统计", "显示", "查", "多少")


def match_segment(seg: str) -> Optional[Dict]:
    """单句动作匹配：查询语境优先只读，长短语唯一命中优先，其次加权打分。"""
    if not seg.strip():
        return None
    query_ctx = any(w in seg for w in _QUERY_WORDS)
    pool = [a for a in CATALOG if a["risk"] == "read" or not query_ctx]
    allowed = {a["name"] for a in pool}
    hits = [(p, n) for p, n in _ALL_PHRASES if p in seg and n in allowed]
    if hits:
        best_len = len(hits[0][0])
        top = [n for p, n in hits if len(p) == best_len]
        # 同长度多命中时按打分消歧
        uniq = list(dict.fromkeys(top))
        if len(uniq) == 1:
            return _BY_NAME[uniq[0]]
        return max((_BY_NAME[n] for n in uniq),
                   key=lambda a: _score_action(a, seg))
    scored = [(_score_action(a, seg), a) for a in pool]
    scored = [(s, a) for s, a in scored if s >= 5]
    if not scored:
        return None
    return max(scored, key=lambda x: x[0])[1]


def plan(text: str, base_slots: Dict = None) -> Dict[str, Any]:
    """把一句/多句自然语言编译成可执行动作计划。

    base_slots 用于跨轮指代（「再备份一次」沿用上一轮的任务/记录编号）；
    只有当本轮没抽到该槽位时才沿用，绝不覆盖用户新给出的编号。
    """
    segs = [s.strip() for s in _MULTISTEP_RE.split(text) if s.strip()]
    slots = parse_slots(text)
    if base_slots:
        for k in ("task_id", "record_id", "plan_id", "sync_id", "compare_id",
                  "drill_id", "policy_id", "target_db", "db_type", "status",
                  "limit", "days", "verify_code"):
            if not slots.get(k) and base_slots.get(k):
                slots[k] = base_slots[k]
        if slots.get("record_id") and not slots.get("source_record_id"):
            slots["source_record_id"] = slots["record_id"]
    matched: List[Dict] = []
    for seg in segs or [text]:
        a = match_segment(seg)
        if a and all(x["name"] != a["name"] for x in matched):
            matched.append(a)
    if not matched:
        return {"actions": [], "slots": slots, "missing": [], "clarify": None,
                "question": text}

    # 任务名 → 真实 ID（仅对需要 task_id 的动作）
    clarify, err = None, None
    if any("task_id" in a.get("need", []) for a in matched):
        slots, clarify, err = resolve_task_name(text, slots)
        if clarify:
            return {"actions": matched, "slots": slots, "missing": [],
                    "clarify": clarify, "error": None,
                    "hint": "找到多个同名任务，请回复「执行任务 <ID>」指定其一"}

    missing: List[str] = []
    for a in matched:
        for k in a.get("need", []):
            if not slots.get(k):
                missing.append("%s:%s" % (a["name"], k))
    return {
        "actions": [{"name": a["name"], "desc": a["desc"], "risk": a["risk"],
                     "confirm": (a.get("confirm") or "").format(**{
                         k: slots.get(k, "?") for k in
                         re.findall(r"\{(\w+)\}", a.get("confirm") or "")})}
                    for a in matched],
        "slots": slots, "missing": missing, "clarify": None, "error": err,
        "multistep": len(matched) > 1, "question": text,
    }


# ---------------------------------------------------------------------------
# 执行：内部 HTTP（透传会话），复用平台 RBAC / 验证码闸 / 审计
# ---------------------------------------------------------------------------
def _base_url() -> str:
    try:
        from flask import request
        host = request.host or "127.0.0.1:8080"
        port = host.rsplit(":", 1)[-1] if ":" in host else "8080"
        return "http://127.0.0.1:%s" % port
    except Exception:
        return "http://127.0.0.1:8080"


def _http(method: str, path: str, headers: Dict, query: Dict = None,
          body: Dict = None, timeout: int = 90) -> Dict[str, Any]:
    url = _base_url() + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None and v != ""})
    data = (json.dumps(body or {}).encode()
            if method in ("POST", "PUT", "PATCH", "DELETE") and body is not None
            else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for h in ("Cookie", "Authorization", "X-Session-Token", "X-API-Token"):
        if headers.get(h):
            req.add_header(h, headers[h])
    # 不带 Origin/Referer：api_bp 的 CSRF 钩子仅校验「带了的必须同源」
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"ok": status < 400, "raw": raw[:300]}
    if isinstance(parsed, list):
        parsed = {"ok": True, "data": parsed, "count": len(parsed)}
    if not isinstance(parsed, dict):
        parsed = {"ok": status < 400, "data": parsed}
    parsed.setdefault("ok", status < 400 and not parsed.get("error"))
    parsed["_http_status"] = status
    return parsed


def _build_request(action: Dict, slots: Dict) -> Tuple[str, Dict, Dict]:
    path = action["path"]
    for key in re.findall(r"\{(\w+)\}", path):
        if slots.get(key) is None:
            raise KeyError(key)
        path = path.replace("{" + key + "}", str(slots[key]))
    query = {k: slots[k] for k in action.get("query", [])
             if slots.get(k) is not None and not isinstance(slots.get(k), (dict, list))}
    body = {k: slots[k] for k in action.get("body", [])
            if slots.get(k) is not None}
    body.update(action.get("fixed", {}))
    # DELETE 也允许携带 JSON 体：高危删除端点需要从中读取 verify_code，
    # 不带体会让验证码闸拿到空值并如实拒绝（真机 E2E 暴露）。
    if action["method"] == "DELETE":
        body = {k: v for k, v in body.items() if k == "verify_code"}
    return path, query, body


def _rows_of(resp: Dict) -> List:
    for k in ("data", "items", "records", "results", "targets", "messages",
              "policies", "plans", "tasks", "reports", "versions", "points"):
        v = resp.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("items", "data", "records", "targets", "tasks"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


# ---------------------------------------------------------------------------
# 结果明细渲染：把 API 真实返回值展示给用户，而不是只报「返回 N 条」
# 约束：不伪造数据、不展示敏感字段、长文本截断、列表默认只展示前若干条
# ---------------------------------------------------------------------------

FIELD_LABELS = {
    "id": "ID", "task_id": "任务 ID", "record_id": "记录 ID", "name": "名称",
    "task_name": "任务名", "db_type": "库型", "db_name": "库名",
    "host": "主机", "host_ip": "主机 IP", "port": "端口",
    "status": "状态", "last_status": "最近状态", "backup_type": "备份类型",
    "size_human": "大小", "size_bytes": "字节数", "duration_sec": "耗时(秒)",
    "started_at": "开始时间", "finished_at": "结束时间",
    "created_at": "创建时间", "updated_at": "更新时间", "backup_path": "产物路径",
    "message": "说明", "error": "错误", "total": "总数", "count": "条数",
    "path": "路径", "source": "来源", "source_label": "来源说明",
    "path_exists": "备份目录存在", "usage_path": "容量统计路径",
    "degraded": "容量统计降级", "hint": "提示", "warning": "警告",
    "total_bytes": "总容量", "used_bytes": "已用", "free_bytes": "可用",
    "total_human": "总容量", "used_human": "已用", "free_human": "可用",
    "total_gb": "总容量(GB)", "used_gb": "已用(GB)", "free_gb": "可用(GB)",
    "used_percent": "使用率(%)", "total_size": "总大小", "enabled": "已启用",
    "schedule_type": "调度类型", "interval_minutes": "间隔(分)",
    "target_db": "目标库", "target_env": "目标环境", "clone_mode": "克隆方式",
    "check_type": "检查类型", "level": "级别", "item": "检查项",
    "score": "评分", "rto_sec": "RTO(秒)", "rpo_sec": "RPO(秒)",
    "severity": "级别", "title": "发现", "suggestion": "建议",
    "record_count": "记录数", "task_count": "任务数", "success": "成功",
    "failed": "失败", "running": "运行中", "warning": "告警",
}

_PREFERRED_COLUMNS = (
    "id", "task_id", "record_id", "task_name", "name", "db_type", "db_name",
    "host", "host_ip", "status", "last_status", "backup_type", "size_human",
    "total_human", "used_human", "free_human", "used_percent",
    "score", "level", "severity", "title", "item", "check_type",
    "created_at", "started_at", "finished_at", "message",
)

_SKIP_COLUMNS = frozenset({
    "password", "password_hash", "secret", "secret_key", "token", "access_key",
    "private_key", "verify_code", "cookie", "authorization", "api_token",
    "raw", "log_output", "log_tail", "script", "custom_script", "sql",
    "extra_options", "config_json", "stdout", "stderr", "traceback",
})

MAX_DETAIL_ROWS = 8
MAX_DETAIL_COLS = 6
MAX_CELL_LEN = 48
_YES_NO_RE = re.compile(r"(有没有|是否有|是否|有无|是不是|多少|几次|几台|几个|吗|\?|？)")
_FILTER_KEYS = ("status", "db_type", "enabled")


def _cell(value) -> str:
    """单元格文本：转义竖线/换行、截断长文本，绝不把敏感结构展开。"""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    if len(text) > MAX_CELL_LEN:
        text = text[:MAX_CELL_LEN - 1] + "…"
    return text


def _format_bytes(value) -> str:
    """字节字段换算成 GB/TB 口径的可读值，并保留精确字节供追溯。"""
    if isinstance(value, bool):
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return ""
    raw = int(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            human = "%d B" % raw if unit == "B" else "%.2f %s" % (n, unit)
            return "%s（%s bytes）" % (human, format(raw, ","))
        n /= 1024.0
    return ""


def _display_value(key, value) -> str:
    key_l = str(key or "").lower()
    if key_l.endswith("_bytes") or key_l == "bytes":
        human = _format_bytes(value)
        if human:
            return _cell(human)
    return _cell(value)


def _columns_of(rows: List[Dict]) -> List[str]:
    """挑选可读列：业务字段优先，跳过敏感字段与嵌套结构。"""
    keys: List[str] = []
    for row in rows[:20]:
        for k, v in row.items():
            if (k not in keys and not k.startswith("_")
                    and k.lower() not in _SKIP_COLUMNS
                    and not isinstance(v, (dict, list))):
                keys.append(k)
    preferred = [k for k in _PREFERRED_COLUMNS if k in keys]
    rest = [k for k in keys if k not in preferred]
    return (preferred + rest)[:MAX_DETAIL_COLS]


def render_rows(rows: List[Dict], limit: int = MAX_DETAIL_ROWS) -> str:
    """把真实行渲染成 Markdown 表格（前端 AgentRender.md 支持表格）。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return "> 本次查询没有匹配的数据。"
    cols = _columns_of(rows)
    if not cols:
        return "> 返回数据中没有可直接展示的字段，详见步骤卡「原始结果」。"
    lines = ["| " + " | ".join(FIELD_LABELS.get(c, c) for c in cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(_display_value(c, r.get(c)) for c in cols) + " |")
    if len(rows) > limit:
        lines += ["", "> 共 %d 条，以上为前 %d 条；完整结果见步骤卡「原始结果」。" %
                  (len(rows), limit)]
    return "\n".join(lines)


def _render_kv(data: Dict, depth: int = 0) -> List[str]:
    """把 dict 型响应（存储用量 / 巡检结果 / 执行回执）渲染成键值明细。"""
    lines: List[str] = []
    if not isinstance(data, dict):
        return lines
    human_keys = {str(k).lower() for k in data
                  if str(k).lower().endswith("_human")}
    for k, v in data.items():
        if k.startswith("_") or k in ("ok", "raw") or k.lower() in _SKIP_COLUMNS:
            continue
        key_l = str(k).lower()
        if key_l.endswith("_bytes") and (key_l[:-6] + "_human") in human_keys:
            continue
        label = FIELD_LABELS.get(k, k)
        if isinstance(v, dict):
            if depth < 1:
                sub = _render_kv(v, depth + 1)
                if sub:
                    lines.append("- **%s**：" % label)
                    lines += ["  " + x for x in sub]
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                lines += ["", "**%s**（%d 条）" % (label, len(v)), "",
                          render_rows(v)]
            elif v:
                lines.append("- **%s**：%s" % (label, _display_value(k, v)))
        else:
            lines.append("- **%s**：%s" % (label, _display_value(k, v)))
    return lines


def _refilter(rows: List[Dict], query: Dict) -> Tuple[List[Dict], List[str]]:
    """接口未必支持 query 里的筛选条件；按真实返回字段在平台侧复核一次。

    例如 /api/records 不接受 status，问「最近备份有没有失败」时传了
    status=failed 也会被忽略并返回全部记录——这里按每行真实 status 重新筛。
    """
    out = list(rows or [])
    applied: List[str] = []
    for key in _FILTER_KEYS:
        want = (query or {}).get(key)
        if want in (None, ""):
            continue
        # 返回行里根本没有这个字段时不复核，避免把有效结果全删成「无匹配」
        field = key if key != "status" else ("status", "last_status")
        present = any(isinstance(r, dict) and any(
            f in r for f in (field if isinstance(field, tuple) else (field,)))
            for r in out)
        if not present:
            continue

        def _hit(row: Dict) -> bool:
            got = row.get(key)
            if got is None and key == "status":
                got = row.get("last_status")
            if key == "enabled":
                return str(got) in ("1", "True", "true", str(want))
            return str(got or "").lower() == str(want).lower()

        kept = [r for r in out if isinstance(r, dict) and _hit(r)]
        if len(kept) != len(out):
            out = kept
            applied.append("%s=%s" % (key, want))
    keyword = (query or {}).get("keyword")
    if keyword:
        low = str(keyword).lower()
        # 整行所有标量值都参与匹配，兼容接口按任意字段实现的关键字搜索
        kept = [r for r in out if isinstance(r, dict) and low in " ".join(
            str(v) for v in r.values() if isinstance(v, (str, int, float))
        ).lower()]
        if len(kept) != len(out):
            out = kept
            applied.append("keyword=%s" % keyword)
    return out, applied


def _conclusion(question: str, rows: List[Dict], filtered: List[Dict]) -> str:
    """对「有没有失败 / 有多少条」这类提问给出基于真实统计值的结论。"""
    q = question or ""
    if not rows or not _YES_NO_RE.search(q):
        return ""
    counts: Dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = str(r.get("status") or r.get("last_status") or "-").lower()
        counts[key] = counts.get(key, 0) + 1
    if any(w in q for w in ("失败", "异常", "问题", "报错", "告警")):
        n = counts.get("failed", 0)
        if n:
            return ("**结论：有，%d 条失败**（已核查接口返回的 %d 条记录，"
                    "成功 %d 条）。" % (n, len(rows), counts.get("success", 0)))
        return "**结论：没有失败记录**（已核查接口返回的 %d 条记录）。" % len(rows)
    if "成功" in q:
        return "**结论：成功 %d 条 / 共 %d 条**。" % (
            counts.get("success", 0), len(rows))
    return "**结论：共 %d 条**。" % len(filtered or rows)


def build_detail(name: str, resp: Dict, query: Dict = None,
                 question: str = "") -> str:
    """生成一个动作的完整结果明细（Markdown），供助手回答正文使用。"""
    if not isinstance(resp, dict):
        return "> 本次调用未返回结构化结果。"
    if not resp.get("ok"):
        bits = ["- **HTTP 状态**：%s" % _cell(resp.get("_http_status")),
                "- **错误**：%s" % _cell(resp.get("error") or resp.get("message"))]
        detail = resp.get("data")
        if isinstance(detail, dict):
            bits += _render_kv(detail, 1)
        return "**调用未成功，原因如下**：\n\n" + "\n".join(bits)
    rows = _rows_of(resp)
    filtered, applied = _refilter(rows, query)
    parts: List[str] = []
    concl = _conclusion(question, rows, filtered)
    if concl:
        parts += [concl, ""]
    if filtered:
        parts.append(render_rows(filtered))
    elif rows:
        parts.append("> 按条件复核后没有匹配的数据（接口共返回 %d 条）。" % len(rows))
    else:
        plain = resp.get("data")
        kv = _render_kv(plain) if isinstance(plain, dict) else []
        if not kv:
            kv = _render_kv(resp)
        parts += kv if kv else ["> 本次操作已执行，接口未返回明细数据。"]
    # 顶层标量（total / count / message 等）在列表结果里也有价值
    if rows:
        extra = {k: v for k, v in resp.items()
                 if k not in ("data", "items", "records", "results", "targets",
                              "messages", "policies", "plans", "tasks", "reports",
                              "versions", "points", "ok", "raw", "_http_status")
                 and not isinstance(v, (dict, list))}
        extra_lines = _render_kv(extra)
        if extra_lines:
            parts += ["", "**汇总**", ""] + extra_lines
    if applied:
        parts += ["", "> 注：接口未支持筛选条件 %s，已按真实返回字段在平台侧复核过滤。" %
                  "、".join(applied)]
    return "\n".join(parts)


def summarize(name: str, resp: Dict) -> str:
    """把任意 API 响应压成一句人话（不伪造数据）。"""
    if not resp.get("ok"):
        return "失败：%s" % (resp.get("error") or resp.get("message") or
                            resp.get("raw") or "未知原因")
    a = _BY_NAME.get(name, {})
    label = a.get("desc", name)
    rows = _rows_of(resp)
    msg = resp.get("message") or ""
    if resp.get("count") is not None:
        n = resp["count"]
    elif rows:
        n = len(rows)
    else:
        n = None
    parts = [label + "：执行成功"]
    if name == "storage_usage":
        d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        bits = []
        for human_key, cap_label, raw_key in (
                ("total_human", "总容量", "total_bytes"),
                ("used_human", "已用", "used_bytes"),
                ("free_human", "可用", "free_bytes")):
            val = d.get(human_key) or _format_bytes(d.get(raw_key))
            if val:
                bits.append("%s %s" % (cap_label, val))
        if d.get("used_percent") not in (None, ""):
            bits.append("使用率 %s%%" % d.get("used_percent"))
        if bits:
            parts.append("，".join(bits))
    if n is not None:
        parts.append("返回 %d 条" % n)
    if msg:
        parts.append(str(msg)[:110])
    elif not rows and isinstance(resp.get("data"), dict):
        d = resp["data"]
        keys = [k for k in ("status", "success", "record_id", "id", "total",
                            "pass", "warn", "fail") if k in d]
        if keys:
            parts.append(", ".join("%s=%s" % (k, str(d[k])[:30]) for k in keys))
    return "，".join(parts)


def execute(compiled: Dict, headers: Dict, extra_slots: Dict = None) -> Dict[str, Any]:
    """按计划顺序真实执行全部动作，返回逐步结果。"""
    slots = dict(compiled.get("slots") or {})
    if extra_slots:
        slots.update({k: v for k, v in extra_slots.items() if v is not None})
    steps: List[Dict] = []
    for item in compiled.get("actions", []):
        action = _BY_NAME.get(item["name"])
        if not action:
            steps.append({"name": item["name"], "ok": False, "error": "动作未注册"})
            continue
        # 高危动作：验证码闸开启且未提供验证码时如实拦截
        if action.get("verify_code"):
            try:
                from auth import op_verify_enabled
                enabled = op_verify_enabled()
            except Exception:
                enabled = False
            if enabled and not slots.get("verify_code"):
                steps.append({"name": action["name"], "ok": False,
                              "error": "系统已开启高危操作验证码，请在自然语言里附「验证码 1234」"
                                       "（验证码从 /api/verify-code 获取）"})
                continue
        try:
            path, query, body = _build_request(action, slots)
        except KeyError as e:
            steps.append({"name": action["name"], "ok": False,
                          "error": "缺少参数 %s" % e.args[0]})
            continue
        resp = _http(action["method"], path, headers, query, body)
        step = {"name": action["name"], "desc": action["desc"],
                "risk": action["risk"], "ok": bool(resp.get("ok")),
                "http_status": resp.get("_http_status"),
                "summary": summarize(action["name"], resp),
                "query": query,
                "detail": build_detail(action["name"], resp, query,
                                       compiled.get("question") or ""),
                "data": resp}
        # 备份类动作追加真实终态：轮询到 success/failed 才汇报
        fu = action.get("followup")
        if fu and step["ok"]:
            task_id = slots.get("task_id")
            deadline = time.time() + int(fu.get("timeout", 120))
            final = None
            while time.time() < deadline:
                r2 = _http("GET", fu["path"] + "?limit=30", headers)
                for row in _rows_of(r2):
                    if (str(row.get("task_id")) == str(task_id)
                            and row.get("status") in ("success", "failed")):
                        final = row
                        break
                if final:
                    break
                time.sleep(3)
            if final:
                step["followup"] = {
                    "record_id": final.get("id"), "status": final.get("status"),
                    "size_bytes": final.get("size_bytes"),
                    "message": str(final.get("message") or "")[:140]}
                step["summary"] += "；最新记录 %s %s" % (
                    final.get("id"), final.get("status"))
                step["detail"] += (
                    "\n\n**备份终态**：记录 %s `%s`，产物 %s，%s" % (
                        final.get("id"), _cell(final.get("status")),
                        _cell(final.get("size_human") or final.get("size_bytes")),
                        _cell(final.get("message"))))
        steps.append(step)
        try:
            import core.db as db
            db.add_log("INFO" if step["ok"] else "WARNING", "ai_agent",
                       "NL动作 %s ok=%s http=%s user=自然语言" %
                       (action["name"], step["ok"], step["http_status"]))
        except Exception:
            pass
    ok_all = bool(steps) and all(s.get("ok") for s in steps)
    blocks = []
    for i, s in enumerate(steps, 1):
        head = "%d. %s %s" % (i, "✓" if s.get("ok") else "✗",
                              s.get("summary") or s.get("error"))
        detail = s.get("detail") or ""
        blocks.append(head + ("\n\n" + detail if detail else ""))
    return {"ok": ok_all, "steps": steps,
            "content": "\n\n".join(blocks) or "没有可执行动作"}


def describe_capabilities() -> str:
    """离线能力引导：无大模型时告诉用户「你还能让我做什么」。"""
    reads = [a["phrases"][0] for a in CATALOG if a["risk"] == "read"]
    writes = [a["phrases"][0] for a in CATALOG if a["risk"] != "read"]
    return ("离线状态下我仍可直接执行以下操作（不需要大模型）：\n"
            "- 查询类：%s 等\n"
            "- 执行类（会先向你确认）：%s 等\n"
            "共支持 %d 类动作；也可以说「先查任务列表，然后备份任务 89」连续执行。"
            % ("、".join(reads[:10]), "、".join(writes[:10]), len(CATALOG)))


def format_plan_preview(compiled: Dict) -> str:
    """生成确认前的操作清单（人可读）。"""
    lines = ["我理解你要执行以下操作："]
    for i, a in enumerate(compiled.get("actions", []), 1):
        lines.append("%d. %s（%s）%s" %
                     (i, a["desc"], a["risk"],
                      ("— " + a["confirm"]) if a.get("confirm") else ""))
    lines.append("")
    lines.append("确认后我会真实执行；回复「确认」继续，回复「取消」放弃。")
    return "\n".join(lines)
