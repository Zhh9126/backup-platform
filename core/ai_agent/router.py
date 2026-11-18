# -*- coding: utf-8 -*-
"""本地意图路由器：不依赖大模型的自然语言决策层。

背景（为什么需要它）
-------------------
AI 助手原先只有「LLM 单通道 + 关键词 if/else 兜底」：平台一旦部署在**内网 /
离线环境**（endpoint 域名不可达，典型报错 `[Errno -2] Name or service not
known`），除少数硬编码关键词外的所有提问都会退化成
「AI 模型未返回有效内容」。而本平台的目标交付形态恰恰是完全离线环境。

设计（参考 GitHub 现有实现）
--------------------------
1. **aurelio-labs/semantic-router**：意图用「示例语句」建模 → 打分 → 取最高分
   → 低于阈值则拒识（返回 None），决策过程零 LLM 调用。本项目借鉴其结构：
   skill 的 utterances 即锚点，阈值以下不硬猜。
2. **eosphoros-ai/DB-GPT**：能力以 skill 为单位组织，便于扩展。
3. 差异点：离线环境**不能引入 numpy / jieba / sentence-transformers**，因此把
   稠密向量换成「字符 bigram TF-IDF + 加权覆盖率 + 序列相似度 + 强关键词」的
   混合词法打分。中文免分词、纯标准库、毫秒级、结果可解释（reason 字段）。

打分与拒识
---------
    score = 0.40*cos + 0.30*cover + 0.20*seq + 0.10*kw   (+0.18 若强关键词命中)
    cos   : 查询与示例语句的 bigram TF-IDF 余弦相似度（取该技能最大值）
    cover : 查询的加权 bigram 被示例语句覆盖的比例
    seq   : difflib 序列相似度最大值（抗错别字 / 语序差异）
    kw    : 强关键词是否命中
高于 fast_min   → 允许本地快速通道（跳过 LLM 直接执行 / 直答）
高于 accept_min → 可作为「模型不可用」时的兜底答案
低于 accept_min → 拒识，交由上层给出能力清单而不是报错
"""

import math
import re
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["Match", "SkillRouter", "normalize", "extract_task_id", "extract_keyword"]


_FULLWIDTH_BASE = 0xFEE0
_FULLWIDTH_MAX = 0xFF5E
_IDEOGRAPHIC_SPACE = 0x3000


def normalize(text: Any) -> str:
    """归一化：全角转半角、转小写、只保留字母数字（含中文）与 . _ -。

    去标点/空格是为了让「最近 10 条，备份记录？」与「最近10条备份记录」
    落在同一特征空间；中文标点与英文标点一并去除。
    """
    chars = []
    for ch in str(text or "").strip().lower():
        code = ord(ch)
        if _FULLWIDTH_BASE <= code <= _FULLWIDTH_MAX:
            ch = chr(code - _FULLWIDTH_BASE)
        elif code == _IDEOGRAPHIC_SPACE:
            ch = " "
        chars.append(ch)
    raw = "".join(chars)
    return "".join(ch for ch in raw if ch.isalnum() or ch in "._-")


def _bigrams(text: str) -> Dict[str, int]:
    """字符 bigram 词频。中文无需分词即可获得较稳健的局部特征。"""
    if not text:
        return {}
    if len(text) < 2:
        return {text: 1}
    grams: Dict[str, int] = {}
    for i in range(len(text) - 1):
        g = text[i:i + 2]
        grams[g] = grams.get(g, 0) + 1
    return grams


def _tfidf_vector(grams: Dict[str, int], idf: Dict[str, float]) -> Dict[str, float]:
    """TF-IDF 稀疏向量：tf 取 1+log(tf)，未登录 bigram 用最大 IDF 兜底。"""
    if not grams:
        return {}
    default_idf = max(idf.values()) if idf else 1.0
    vec: Dict[str, float] = {}
    for g, tf in grams.items():
        weight = 1.0 + math.log(tf)
        vec[g] = weight * idf.get(g, default_idf)
    return vec


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """稀疏余弦相似度。"""
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = 0.0
    for g, w in small.items():
        w2 = large.get(g)
        if w2:
            dot += w * w2
    if dot <= 0:
        return 0.0
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return min(1.0, dot / (norm_a * norm_b))


def _coverage(query_vec: Dict[str, float], target_vec: Dict[str, float]) -> float:
    """查询特征的加权覆盖率：查询里有多少「信息量」在示例语句中出现过。"""
    if not query_vec:
        return 0.0
    total = sum(query_vec.values())
    if total <= 0:
        return 0.0
    hit = sum(w for g, w in query_vec.items() if g in target_vec)
    return min(1.0, hit / total)


# ---------------------------------------------------------------------------
# 槽位抽取
# ---------------------------------------------------------------------------

# 含这些词说明用户说的是「业务对象」而不是任务 ID，避免把端口/时长当 ID
_BIZ_HINTS = ("备份", "恢复", "巡检", "克隆", "库", "表", "实例", "主机", "端口", "任务")

_TASK_ID_PATTERNS = (
    r"任务\s*#?\s*(\d{1,})",
    r"#\s*(\d{1,})",
    r"(\d{1,})\s*号\s*任务",
)

# 裸数字直给：整句几乎只有一个数字（如「就执行 99995 吧」）
_BARE_ID_RE = re.compile(
    r"^(?:请|帮我|帮忙|就|要|选|用|执行|跑|来|走)*\s*#?\s*(\d{2,})\s*"
    r"(?:吧|呢|啊|哦|了|的|这个|那个)?\s*[!！。.，,]?\s*$"
)

_STOP_WORDS = (
    "立即执行", "帮我执行", "帮我跑", "帮我", "请帮我", "麻烦", "请", "执行", "跑一下", "跑一次",
    "立即", "马上", "开始", "做一次", "手动", "进行", "来一次", "一次", "备份一下", "备份",
    "巡检", "任务", "数据库", "数据", "库", "的", "把", "给", "我要", "我想", "需要",
    "现在", "这个", "那个", "一下", "一些", "吧", "了", "呢", "啊", "下",
    "跑个", "来个", "做个", "弄个", "搞好", "列一下", "看下", "看看",
)


def extract_task_id(text: str) -> str:
    """从文本抽取备份任务 ID（抽不到返回空串）。"""
    raw = str(text or "")
    for pat in _TASK_ID_PATTERNS:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    # 裸数字：仅在句中没有业务名词时启用，避免误吞「3307 端口」「2 小时」
    if not any(w in raw for w in _BIZ_HINTS):
        m = _BARE_ID_RE.match(raw.strip())
        if m:
            return m.group(1)
    return ""


def extract_keyword(text: str) -> str:
    """抽取任务名/库名关键字（无 LLM 环境下的口语化定位）。

    例："帮我执行 bpm1 库的备份" → "bpm1"；"备份一下对象存储附件桶" → "对象存储附件桶"。
    """
    cleaned = re.sub(r"[，。！？、,\.\!\?\s：:；;]+", " ", str(text or ""))
    for word in sorted(_STOP_WORDS, key=len, reverse=True):
        cleaned = cleaned.replace(word, " ")
    parts = [p.strip() for p in cleaned.split(" ") if p.strip()]
    if not parts:
        return ""
    # 含字母/数字的片段优先（库名、业务系统代号最能定位任务）
    for p in parts:
        if re.search(r"[A-Za-z0-9_\-]", p):
            return p
    # 否则取最长的中文片段（"对象存储附件桶" 优于 "对象存储"）
    return max(parts, key=len)


# ---------------------------------------------------------------------------
# 路由结果
# ---------------------------------------------------------------------------

@dataclass
class Match:
    """一次意图匹配结果。

    Attributes:
        skill: 命中的技能。
        score: 综合置信度 0~1。
        reason: 命中依据（keyword / lexical / id_pattern / exact）。
        args: 已抽取槽位的工具参数（未命中槽位时为其静态 args）。
        missing_slot: 需要但未抽取到的槽位名（非空时上层应追问）。
        alternatives: 备选匹配（分数次高，用于澄清）。
        ambiguous: top1 与 top2 过于接近，建议让用户确认。
    """

    skill: Any
    score: float
    reason: str = "lexical"
    args: Dict[str, Any] = dc_field(default_factory=dict)
    missing_slot: str = ""
    alternatives: List["Match"] = dc_field(default_factory=list)
    ambiguous: bool = False

    @property
    def name(self) -> str:
        return getattr(self.skill, "name", "")

    @property
    def kind(self) -> str:
        return getattr(self.skill, "kind", "tool")

    @property
    def fast(self) -> bool:
        return bool(getattr(self.skill, "fast", True))


# ---------------------------------------------------------------------------
# 路由器
# ---------------------------------------------------------------------------

# 多步指令信号：命中则不走本地快速通道，交给 LLM（或提示拆开说），
# 避免把「查一下任务，然后执行 12」这类复合意图简化成单步误执行。
_MULTISTEP_RE = re.compile(
    r"(然后再|然后|接着|之后|顺便|同时|并且|以及|还有|再看|再做|再执行|再跑|再看下|并帮我)"
)

# 打分权重（调参集中在模块级，便于回归对比）
_W_COS = 0.40
_W_COVER = 0.30
_W_SEQ = 0.20
_W_KW = 0.10
_KEYWORD_BOOST = 0.18


class SkillRouter:
    """离线意图路由器（纯标准库，零网络零模型依赖）。

    Args:
        skills: Skill 序列（见 skills.py）。
        fast_min: 允许走「本地快速通道」的置信度门槛。
        accept_min: 可作为兜底答案的最低置信度；低于此值视为拒识。
        margin: top1 与 top2 的最小差值，低于该值标记 ambiguous。
    """

    def __init__(self, skills: Sequence[Any], *, fast_min: float = 0.72,
                 accept_min: float = 0.42, margin: float = 0.05) -> None:
        self.skills: Tuple[Any, ...] = tuple(skills)
        self.fast_min = float(fast_min)
        self.accept_min = float(accept_min)
        self.margin = float(margin)

        # ---- 预计算：utterance 归一化文本与 IDF ----
        self._utterances: Dict[str, List[str]] = {}
        df: Dict[str, int] = {}
        for skill in self.skills:
            norms: List[str] = []
            for u in getattr(skill, "utterances", ()) or ():
                n = normalize(u)
                if not n:
                    continue
                norms.append(n)
                for g in _bigrams(n):
                    df[g] = df.get(g, 0) + 1
            self._utterances[skill.name] = norms

        total = max(1, sum(len(v) for v in self._utterances.values()))
        self._idf: Dict[str, float] = {
            g: math.log((total + 1.0) / (c + 1.0)) + 1.0 for g, c in df.items()
        }

        # ---- 预计算：utterance 向量 ----
        self._vectors: Dict[str, List[Dict[str, float]]] = {}
        for name, norms in self._utterances.items():
            self._vectors[name] = [
                _tfidf_vector(_bigrams(n), self._idf) for n in norms
            ]

        self.session_mgr = None  # 兼容性占位：router 自身无状态，不存会话

    # ------------------------- 对外接口 -------------------------

    @staticmethod
    def is_multistep(text: str) -> bool:
        """是否包含多步/复合意图信号。"""
        return bool(_MULTISTEP_RE.search(str(text or "")))

    def match_all(self, text: str) -> List[Match]:
        """返回全部技能的匹配结果（按分数降序）。"""
        raw = str(text or "")
        q_norm = normalize(raw)
        if not q_norm:
            return []

        q_vec = _tfidf_vector(_bigrams(q_norm), self._idf)
        task_id = extract_task_id(raw)

        results: List[Match] = []
        for skill in self.skills:
            score, reason = self._score_skill(skill, q_norm, q_vec, task_id)
            if score <= 0:
                continue
            args, missing = self._build_args(skill, raw)
            results.append(Match(
                skill=skill, score=round(score, 4), reason=reason,
                args=args, missing_slot=missing,
            ))

        results.sort(key=lambda m: m.score, reverse=True)
        return results

    def route(self, text: str, *, min_score: Optional[float] = None,
              fast_only: bool = False) -> Optional[Match]:
        """路由到最可能的技能。

        Args:
            text: 用户原始输入。
            min_score: 覆盖默认接受阈值。
            fast_only: 仅返回可走本地快速通道的结果（更严，且排除多步与歧义）。

        Returns:
            Match 或 None（拒识）。
        """
        matches = self.match_all(text)
        if not matches:
            return None

        top = matches[0]
        floor = self.accept_min if min_score is None else float(min_score)

        if fast_only:
            if top.score < max(floor, self.fast_min):
                return None
            if not top.fast or top.missing_slot:
                return None
            if self.is_multistep(text):
                return None
            if len(matches) > 1 and (top.score - matches[1].score) < self.margin:
                return None

        if top.score < floor:
            return None

        # 歧义标记：分数接近时上层可让用户确认
        if len(matches) > 1 and (top.score - matches[1].score) < self.margin:
            top.ambiguous = True
            top.alternatives = matches[1:3]
        return top

    def explain(self, text: str, limit: int = 3) -> str:
        """调试用：打印 top-N 匹配与分数。"""
        ms = self.match_all(text)[:max(1, limit)]
        if not ms:
            return f"{text!r} → 无匹配"
        return "\n".join(
            f"{m.score:.3f}  {m.name:<24} {m.reason}  args={m.args}" for m in ms
        )

    # ------------------------- 内部：打分 -------------------------

    def _score_skill(self, skill: Any, q_norm: str,
                     q_vec: Dict[str, float], task_id: str) -> Tuple[float, str]:
        """计算查询与某技能的置信度。"""
        # 需要 task_id 槽位的技能（「执行任务 12」这类）必须真的抽到 ID：
        # 否则「给我列一下任务」会因「一下 / 任务」的 bigram 重合被判成执行任务，
        # 而备份是会真实触发的危险误判。
        if getattr(skill, "slot", "") == "task_id":
            return (1.0, "id_pattern") if task_id else (0.0, "")

        keywords = [normalize(k) for k in (getattr(skill, "keywords", ()) or ())]
        keywords = [k for k in keywords if k]
        kw_hit = any(k in q_norm for k in keywords)
        kw_score = 1.0 if kw_hit else 0.0

        norms = self._utterances.get(skill.name) or []
        vecs = self._vectors.get(skill.name) or []
        if not norms:
            # 没有示例语句的技能只能靠强关键词
            return (min(1.0, 0.55 + _KEYWORD_BOOST), "keyword") if kw_hit else (0.0, "")

        best = 0.0
        for n, v in zip(norms, vecs):
            if q_norm == n:
                return 1.0, "exact"
            cos = _cosine(q_vec, v)
            cover = _coverage(q_vec, v)
            # 明显不相关时跳过较贵的序列比对（性能优化，不改变高分路径结果）
            if cos < 0.03 and cover < 0.10 and not kw_hit:
                continue
            seq = SequenceMatcher(None, q_norm, n).ratio()
            s = _W_COS * cos + _W_COVER * cover + _W_SEQ * seq + _W_KW * kw_score
            if s > best:
                best = s

        if kw_hit:
            best = min(1.0, best + _KEYWORD_BOOST)
        return best, ("keyword" if kw_hit else "lexical")

    # ------------------------- 内部：槽位 -------------------------

    def _build_args(self, skill: Any, text: str) -> Tuple[Dict[str, Any], str]:
        """构造工具参数并判断是否缺槽位。"""
        args: Dict[str, Any] = dict(getattr(skill, "args", None) or {})
        slot = getattr(skill, "slot", "") or ""
        missing = ""

        if slot == "task_id":
            tid = extract_task_id(text)
            if tid:
                args["task_id"] = tid
            else:
                missing = "task_id"
        elif slot in ("task_name", "keyword"):
            kw = extract_keyword(text)
            if _looks_like_entity(kw):
                args["task_name" if slot == "task_name" else "keyword"] = kw
            elif slot == "task_name":
                missing = "task_name"
            # keyword 缺失不算错误：表示「列出全部」，符合用户预期
        return args, missing


# 泛化词：抽出来的「关键字」若只是这些，说明用户要的是全量列表而不是过滤条件
_GENERIC_TOKENS = (
    "所有", "全部", "列出", "查看", "显示", "列表", "多少", "哪些",
    "一些", "一个", "这个", "那个", "任务", "备份",
    # 疑问/状态短语：避免把「备份有没有出问题」抽成 task_name 去执行备份
    "什么", "都有", "给我", "有没有", "是不是", "是否", "怎么", "如何",
    "出问题", "成功没", "成了没", "吗", "呢", "啥",
)


def _looks_like_entity(keyword: str) -> bool:
    """判断抽取结果是否像一个真实的业务实体（库名/任务名/主机名）。

    用于避免把「列出所有备份任务」里的「列出所有」当成关键字去过滤，
    从而把本该全量返回的结果过滤成空列表。
    """
    kw = str(keyword or "").strip()
    if not kw or len(kw) < 2:
        return False
    return not any(t in kw for t in _GENERIC_TOKENS)
