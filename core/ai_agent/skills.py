# -*- coding: utf-8 -*-
"""AI 助手技能库（离线意图引擎的数据层）。

设计参考（国内外开源实现）：
- **aurelio-labs/semantic-router**（3.9k stars，MIT）：每个意图用「示例语句
  (utterances)」建模，靠相似度打分 + 阈值拒识完成决策，**决策过程完全不调用
  LLM**；不相关时返回 None，由应用层决定兜底行为。
- **eosphoros-ai/DB-GPT**（20k stars，MIT）：把领域知识与执行流程打包成可复用
  skill / AWEL 工作流，而不是把判断散落在 if/else 里。
- **Rasa NLU / Snips**：intent 必须同时提供「置信度」和「Top-N 候选」，低置信时
  走澄清（clarification）而不是报错。

本文件只描述「平台能做什么」这一层**数据**（意图、示例语句、强关键词、槽位、
话术、风险等级）；打分与路由算法见 `router.py`。

与 `agent.py` 中 SYSTEM_PROMPT_TEMPLATE 的关系 —— 双通道同覆盖：
- SYSTEM_PROMPT_TEMPLATE 给「LLM 通道」看（在线时的智能增强）；
- 本文件给「本地通道」看（离线 / 模型不可达时的决策主干）。
两者必须覆盖同一批工具，否则会出现「在线能做、离线就报错」的能力落差。

约束：仅用标准库（离线交付环境无 numpy / jieba / sklearn）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class Skill:
    """一个可被自然语言触发的技能。

    Attributes:
        name: 技能唯一标识（工具类与工具名一致，纯问答类用 meta_*/kb_* 前缀）。
        kind: tool（执行工具）/ answer（固定话术）/ help（能力与用法说明）。
        utterances: 示例语句集合——本地引擎的「锚点」，决定该技能的词法覆盖。
            多写口语化变体（"你谁啊" / "你是谁"），这是识别率的关键。
        keywords: 强关键词。命中即给确定性加分，用于兜住示例没覆盖的说法。
        tool: kind=tool 时调用的工具名。
        args: kind=tool 的静态参数（与槽位抽取结果合并，槽位优先）。
        slot: 需要从用户文本抽取的参数名（task_id / task_name / keyword）。
        reply: kind=answer/help 时的回复正文（Markdown）。
        confirm_reason: 需要二次确认时展示给用户的理由。
        risk: low / medium / high，用于前端步骤卡着色与审计。
        fast: 是否允许走本地快速通道（跳过 LLM 直接执行）。
        slot_hint: 槽位抽取失败时的追问话术。
    """

    name: str
    kind: str = "tool"
    utterances: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    slot: str = ""
    reply: str = ""
    confirm_reason: str = ""
    risk: str = "low"
    fast: bool = True
    slot_hint: str = ""


# ---------------------------------------------------------------------------
# 灾备知识库（纯问答，不依赖任何工具与网络）
# ---------------------------------------------------------------------------

KNOWLEDGE: Dict[str, str] = {
    "rpo": "**RPO**（Recovery Point Objective，恢复点目标）：灾难发生后**允许丢失的数据量**对应的时间窗口。"
           "例如 RPO=5 分钟，表示最多容忍丢失最近 5 分钟的数据。",
    "rto": "**RTO**（Recovery Time Objective，恢复时间目标）：从灾难发生到**业务恢复可用**所允许的最大停机时间。"
           "例如 RTO=2 小时，表示必须在 2 小时内恢复服务。",
    "full_backup": "**全量备份**：对指定数据源做完整拷贝。恢复最快、不依赖备份链，但占用空间大、耗时长，"
                   "通常作为增量 / 差异备份的基线。",
    "incremental_backup": "**增量备份**：只备份自**上一次任意类型备份**以来变化的数据。占用空间最小，"
                          "但恢复必须依赖完整备份链，任何一段缺失都会导致链断。",
    "differential_backup": "**差异备份**：备份自**上一次全量备份**以来所有变化的数据。恢复只需「全量 + 最新差异」，"
                           "空间与耗时介于全量与增量之间。",
    "physical_backup": "**物理备份**：直接拷贝数据库的数据文件 / 目录（如 `pg_basebackup`、`xtrabackup`）。"
                       "恢复快、一致性好，但要求与源环境的数据库版本 / 平台兼容。",
    "logical_backup": "**逻辑备份**：把数据导出为 SQL 或逻辑格式（如 `mysqldump`、`pg_dump`、`expdp`）。"
                      "跨版本、跨平台兼容性好，但恢复较慢，大库导出对源库有压力。",
    "pitr": "**PITR**（Point-In-Time Recovery，时间点恢复）：基于「基础备份 + 连续归档日志」把数据库恢复到"
            "**任意历史时刻**。前提是已开启归档，且归档链完整。",
    "gtid": "**GTID**（Global Transaction ID，全局事务标识）：MySQL 用于唯一标记每个已提交事务，用于主从复制与故障切换。"
            "把含 GTID 的 dump 恢复到非空实例时，需要先 `RESET MASTER`（或加 `--set-gtid-purged=OFF`）。",
    "cdc": "**CDC**（Change Data Capture，变更数据捕获）：持续捕获数据库的增量变更（MySQL binlog、"
           "PostgreSQL WAL、Oracle redo），用于实时 / 准实时同步，是压低 RPO 的关键手段。",
    "restore_verify": "**恢复校验**：不覆盖生产环境，把备份产物**真实恢复**到隔离实例并逐表比对行数 / 校验值，"
                      "证明「备份是可恢复的」，而不是只看备份任务是否 success。",
    "clone": "**克隆（VDB）**：基于某次备份产物快速拉起一个独立数据库实例，用于验证备份可用性、"
             "或在测试环境复现生产故障，不影响生产库。",
}

# 知识条目的口语化提问模板
_KB_TEMPLATES: Tuple[str, ...] = (
    "什么是{a}", "{a}是什么", "{a}是什么意思", "{a} 的定义", "解释一下{a}",
    "{a}指什么", "介绍下{a}", "请解释{a}",
)


def _kb_skill(key: str, *alias: str) -> Skill:
    """由知识条目生成一个纯问答技能。"""
    utterances = [tpl.format(a=a) for a in alias for tpl in _KB_TEMPLATES]
    return Skill(
        name=f"kb_{key}", kind="answer",
        utterances=tuple(utterances), keywords=alias, reply=KNOWLEDGE[key],
    )


# ---------------------------------------------------------------------------
# 技能清单
# ---------------------------------------------------------------------------

SKILLS: Tuple[Skill, ...] = (
    # ===== 元问题 / 闲聊：离线也必须永远有答案（原实现在这里直接报错） =====
    Skill(
        name="meta_identity", kind="answer",
        utterances=(
            "你是谁", "你是谁啊", "你叫什么名字", "你叫什么", "介绍一下你自己",
            "自我介绍", "你是什么", "你是干嘛的", "你是做什么的", "你哪位",
            "你谁啊", "你是机器人吗", "你是什么助手", "你是 AI 吗",
            "你谁", "你是哪个", "你是哪位", "你是什么东西", "你是个啥",
        ),
        keywords=("你是谁", "你谁啊", "你叫什么", "你哪位", "你是干嘛", "自我介绍", "你是什么助手"),
        reply=(
            "我是 **AIDBM（AI 原生智能数据库灾备管理平台）的智能运维助手**。\n\n"
            "你可以直接用**自然语言**指挥我，不用一层层点菜单：查任务、看失败记录、"
            "执行备份、跑巡检、查存储用量、问灾备概念，都可以直接说。\n\n"
            "我在**完全离线**的环境下也能工作：模型服务不可达时，常用指令由本地意图引擎直接执行。"
        ),
    ),
    Skill(
        name="meta_capability", kind="help",
        utterances=(
            "你能做什么", "你能干什么", "你能帮我做什么", "你会什么", "你会干啥",
            "有什么功能", "你有哪些功能", "你能做哪些事", "你的能力", "有哪些能力", "能干啥",
            "说下你能干啥", "能干啥活", "有什么能力", "你都会什么",
        ),
        keywords=("你能做什么", "你能干什么", "你会什么", "有什么功能", "有哪些功能", "你能帮我做"),
        reply=(
            "我目前可以做这些事：\n\n"
            "| 类别 | 你可以这样说 |\n|---|---|\n"
            "| 查备份任务 | 列出所有备份任务 / bpm1 有哪些任务 |\n"
            "| 看执行结果 | 最近备份有没有失败 / 最近 10 条备份记录 |\n"
            "| 执行备份 | 帮我执行 bpm1 库的备份 / 执行任务 12 |\n"
            "| 巡检 | 帮我跑一次快速巡检 / 全量巡检 |\n"
            "| 存储用量 | 查询存储用量 / 磁盘还剩多少 |\n"
            "| 风险告警 | 有哪些告警 / 最近的风险预测 |\n"
            "| 概念答疑 | 什么是 RPO / 增量备份和差异备份的区别 |\n\n"
            "直接说人话就行，不必用固定命令。"
        ),
    ),
    Skill(
        name="meta_help", kind="help",
        utterances=(
            "怎么用", "怎么使用", "如何使用", "使用说明", "帮助", "我要怎么操作",
            "怎么跟你说", "怎么提问", "举个例子", "示例", "不知道怎么说", "怎么说",
        ),
        keywords=("怎么用", "使用说明", "帮助", "怎么操作", "怎么提问", "举个例子"),
        reply=(
            "直接用日常说法告诉我就行，例如：\n\n"
            "- “**最近备份有没有失败**” → 列出最近的备份执行记录并标出失败项\n"
            "- “**帮我执行 bpm1 库的备份**” → 定位到对应任务并立即执行\n"
            "- “**bpm1 有哪些备份任务**” → 按关键字过滤任务列表\n"
            "- “**查询存储用量**” → 查看本地 / 对象存储的剩余空间\n"
            "- “**帮我跑一次快速巡检**” → 做一轮轻量健康检查\n\n"
            "一句话里指代不清时，我会先把候选列出来让你点选，**不会猜着执行**。"
        ),
    ),
    Skill(
        name="meta_greeting", kind="answer",
        utterances=("你好", "您好", "hi", "hello", "在吗", "在么", "早上好", "下午好", "晚上好", "嗨"),
        keywords=("你好", "您好", "在吗", "早上好", "下午好", "晚上好"),
        reply="你好，我是 **AIDBM 智能运维助手**。可以让我查备份记录、执行备份、跑巡检，或问我灾备相关问题。",
    ),
    Skill(
        name="meta_thanks", kind="answer",
        utterances=("谢谢", "多谢", "感谢", "好的谢谢", "辛苦了", "明白了", "知道了", "收到", "ok", "好的"),
        keywords=("谢谢", "多谢", "感谢", "辛苦了"),
        reply="不客气。需要继续排查或执行其他任务，随时告诉我。",
    ),

    # ===== 查询类 =====
    Skill(
        name="list_tasks", kind="tool", tool="list_tasks", slot="keyword",
        utterances=(
            "列出所有备份任务", "所有备份任务", "有哪些备份任务", "备份任务列表",
            "任务列表", "查看任务列表", "显示所有任务", "我的备份任务",
            "有哪些任务", "列出任务", "查看所有任务", "平台上有多少任务",
            "bpm1 有哪些备份任务", "这个库有哪些备份任务", "有哪些备份", "多少备份任务", "全部任务",
            "平台上都有什么任务", "给我列一下任务", "列一下任务",
            "都有哪些任务", "平台上有什么任务", "列出全部任务",
        ),
        keywords=("备份任务", "任务列表", "所有任务", "有哪些任务", "列出任务", "查看任务"),
    ),
    Skill(
        name="list_recent_records", kind="tool", tool="list_recent_records",
        args={"limit": 10},
        utterances=(
            "最近备份有没有失败", "最近备份记录", "最近的备份记录", "最近 10 条备份记录",
            "备份历史", "最近的备份情况", "上次备份成功了吗", "备份有没有报错",
            "最近失败的任务", "有哪些失败的备份", "查看失败记录", "最近的执行记录",
            "备份成功了吗", "上一轮备份结果", "最近备份怎么样",
            "备份有没有出问题", "备份出问题了吗", "上次备份成了没", "备份成功没",
        ),
        keywords=("备份记录", "最近备份", "失败记录", "备份历史", "执行记录", "最近有没有失败"),
    ),
    Skill(
        name="get_storage_usage", kind="tool", tool="get_storage_usage",
        utterances=(
            "查询存储用量", "查询存储空间", "存储用量", "磁盘还剩多少", "剩余空间",
            "空间够不够", "存储空间还有多少", "备份盘还剩多少", "容量使用率",
            "存储使用情况", "还有多少空间",
        ),
        keywords=("存储用量", "存储空间", "剩余空间", "磁盘", "容量", "空间够不够", "还剩多少"),
    ),
    Skill(
        name="list_alert_predictions", kind="tool", tool="list_alert_predictions",
        args={"days": 7},
        utterances=(
            "有哪些告警", "最近的风险预测", "AI 预测告警", "有哪些风险", "查看告警",
            "预警信息", "有风险吗", "最近有什么预警", "风险提示", "告警列表",
        ),
        keywords=("告警", "预警", "风险预测", "风险提示", "有哪些风险"),
    ),
    Skill(
        name="get_inspection_report", kind="tool", tool="get_inspection_report",
        utterances=(
            "最新巡检报告", "巡检报告", "上次巡检结果", "查看巡检结果", "巡检结论",
            "最近一次巡检", "巡检情况怎么样", "看看巡检报告", "体检报告",
            "巡检做了吗", "巡检结果如何", "巡检了吗", "上次巡检怎么样",
        ),
        keywords=("巡检报告", "巡检结果", "巡检结论", "体检报告", "巡检情况"),
    ),

    # ===== 执行类 =====
    Skill(
        name="run_backup_task_by_id", kind="tool", tool="run_backup_task",
        slot="task_id", risk="low",
        utterances=(
            "执行任务 12", "执行第 12 个任务", "立即执行任务 12", "跑一下任务 12",
            "执行 12 号任务", "执行 #12",
        ),
        slot_hint="请告诉我要执行哪个任务，例如：**执行任务 12**。",
    ),
    Skill(
        name="run_backup_task_by_name", kind="tool", tool="run_backup_task",
        slot="task_name", risk="low",
        utterances=(
            "帮我执行 bpm1 库的备份", "帮我备份 bpm1", "备份一下 bpm1 库", "立即备份 bpm1",
            "跑一下 bpm1 的备份", "执行 bpm1 的备份任务", "帮我执行备份", "立即执行备份",
            "开始备份", "做一次备份", "手动备份一下", "执行一次备份", "帮我跑个备份",
            "把 bpm1 备份一下", "备份 bpm1 数据库", "帮我备份生产 MySQL", "备份一下对象存储附件桶",
        ),
        keywords=("执行备份", "备份一下", "立即备份", "开始备份", "做一次备份", "跑一次备份", "手动备份"),
        slot_hint="请告诉我要备份哪个库 / 业务系统 / 任务（任务名或 ID），例如：**帮我执行 bpm1 库的备份**。",
    ),
    Skill(
        name="run_inspection_quick", kind="tool", tool="run_inspection",
        args={"scope": "quick"}, risk="low",
        utterances=(
            "帮我跑一次快速巡检", "快速巡检", "跑一下快速巡检", "做一次快速巡检",
            "巡检一下", "帮我巡检", "检查一下平台状态", "体检一下", "快速体检",
            "跑一次巡检", "巡检一下数据库",
            "跑个巡检", "来个巡检", "巡检吧", "做个巡检", "跑下巡检", "巡检一下平台",
        ),
        keywords=("快速巡检", "巡检一下", "帮我巡检", "快速体检"),
    ),
    Skill(
        name="run_inspection_full", kind="tool", tool="run_inspection",
        args={"scope": "full"}, risk="medium",
        utterances=("全量巡检", "帮我跑一次全量巡检", "执行全量巡检", "做一次全量巡检", "全量体检"),
        keywords=("全量巡检", "全量体检"),
        confirm_reason="全量巡检会对数据库性能产生较大影响，请确认是否继续？",
    ),

    # ===== 7 个自治 Agent：自动诊断 → 修复 → 回验 =====
    # 措辞刻意避开既有「巡检一下 / 体检一下」技能，避免离线路由抢词
    Skill(
        name="autonomous_diagnose", kind="tool", tool="autonomous_diagnose",
        utterances=(
            "自动诊断一下", "启动自治诊断", "让自治 Agent 查一遍",
            "七个自治 Agent 检查一下", "7 个自治 Agent 诊断",
            "帮我自动排查平台问题", "自愈检查", "自动检查哪里有问题",
            "平台有什么隐患", "让 AI 自己查一遍", "全面自动诊断",
            "自动找找问题", "自动巡检有哪些异常",
        ),
        keywords=("自治 Agent", "自治诊断", "自动诊断", "自愈检查",
                  "自动排查", "自动找问题"),
    ),
    Skill(
        name="autonomous_diagnose_llm", kind="tool", tool="autonomous_diagnose",
        args={"use_llm": "true"},
        utterances=(
            "用大模型总结诊断结论", "让 DeepSeek 分析平台健康",
            "用模型解释一下体检结果", "AI 分析一下问题优先级",
        ),
        keywords=("DeepSeek", "大模型分析", "模型总结", "AI 分析"),
    ),
    Skill(
        name="autonomous_plan", kind="tool", tool="autonomous_plan",
        utterances=(
            "查看修复计划", "有哪些可以自动修复", "列出自治修复动作",
            "看看修复建议", "自动修复计划",
        ),
        keywords=("修复计划", "自动修复建议", "修复动作"),
    ),
    Skill(
        name="autonomous_execute", kind="tool", tool="autonomous_execute",
        args={"dry_run": "true"}, risk="low",
        utterances=(
            "执行自动修复", "帮我自动修复问题", "按修复计划执行",
            "自动修复这些发现", "让 Agent 自己修好", "预演修复计划",
        ),
        keywords=("执行自动修复", "自动修复", "让 Agent 修", "预演修复"),
    ),
    Skill(
        name="autonomous_execute_confirmed", kind="tool", tool="autonomous_execute",
        args={"dry_run": "false", "approved": "true"}, risk="high",
        utterances=(
            "确认执行修复", "确认执行自动修复", "我确认，执行修复",
            "同意执行修复计划", "确认按计划修复", "执行并回验修复",
        ),
        keywords=("确认执行修复", "确认按计划修复", "同意执行修复"),
    ),
    Skill(
        name="autonomous_verify", kind="tool", tool="autonomous_verify",
        utterances=(
            "回验修复结果", "修复生效了吗", "重新诊断确认一下",
            "再查一遍看问题还在不在", "验证自动修复",
        ),
        keywords=("回验", "修复生效", "验证自动修复", "重新诊断"),
    ),

    # ===== 灾备概念答疑 =====
    _kb_skill("rpo", "RPO", "rpo"),
    _kb_skill("rto", "RTO", "rto"),
    _kb_skill("full_backup", "全量备份"),
    _kb_skill("incremental_backup", "增量备份"),
    _kb_skill("differential_backup", "差异备份"),
    _kb_skill("physical_backup", "物理备份"),
    _kb_skill("logical_backup", "逻辑备份"),
    _kb_skill("pitr", "PITR", "pitr", "时间点恢复"),
    _kb_skill("gtid", "GTID", "gtid"),
    _kb_skill("cdc", "CDC", "cdc", "变更数据捕获"),
    _kb_skill("restore_verify", "恢复校验", "恢复验证"),
    _kb_skill("clone", "克隆", "VDB", "vdb"),
)


# 拒识话术：意图置信度不足且模型不可用时展示「我能做什么」，
# 而不是「AI 模型未返回有效内容」（后者对运维人员不可操作）。
FALLBACK_HELP = (
    "我暂时没完全理解你的意思。当前**模型服务不可达**，我先用本地能力响应。\n\n"
    "你可以试试这样说：\n\n"
    "- **列出所有备份任务**\n"
    "- **最近备份有没有失败**\n"
    "- **帮我执行 bpm1 库的备份**\n"
    "- **查询存储用量**\n"
    "- **帮我跑一次快速巡检**\n"
    "- **自动诊断一下（7 个自治 Agent 体检 + 修复计划）**\n"
    "- **什么是 RPO**\n\n"
    "如果都不是，补充一点细节（库名 / 任务名 / 想看什么数据）我再试一次。"
)


def skill_by_name(name: str) -> Optional[Skill]:
    """按名称取技能（找不到返回 None）。"""
    for s in SKILLS:
        if s.name == name:
            return s
    return None
