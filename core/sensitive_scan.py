# -*- coding: utf-8 -*-
"""敏感数据发现与分级分类（Sensitive Data Discovery & Classification）。

对标业界数据资产管理平台的「敏感数据发现(Discovery) + 分级分类(Classification)」能力：

1) **基于内容的识别**（而非只看列名）：正则 + 校验算法双重判定，可识别混在自由文本、
   JSON、日志字段里的敏感值（业界同类产品普遍只做列名匹配，召回率低）。
2) **四级分级**：依据 GB/T 43697-2024《数据安全技术 数据分类分级规则》
   （数据安全法要求的区分：核心数据 / 重要数据 / 一般数据），对一般数据按敏感程度细化：
       L4 重要  >  L3 敏感  >  L2 内部  >  L1 公开
3) **合规标签**：PIPL（个人信息保护法）/ GB/T 35273（个人信息安全规范）/
   PCI-DSS（支付卡） / MLPS 等保 2.0，便于导出合规举证材料。
4) **原始值零留存**：扫描结果只输出「脱敏样例」（如 138****1234），
   原始敏感值既不写库、也不写日志——避免扫描本身造成二次泄漏。
5) **自动脱敏建议**：识别结果可直接转为脱敏规则，喂给「脱敏导出」。

本模块只依赖标准库，离线打包可用。
"""
import io
import json
import os
import re
import gzip
import bz2
import lzma
import json
from typing import List, Dict, Any, Optional, Iterable

# ----------------------------- 分级模型 -----------------------------

LEVELS = {
    4: {"code": "L4", "name": "重要", "desc": "泄露后可造成严重危害（身份凭证、鉴权密钥、金融账户主数据）",
        "badge": "danger", "must_protect": True},
    3: {"code": "L3", "name": "敏感", "desc": "个人信息与金融信息，受 PIPL / GB/T 35273 约束，需脱敏后才可外发",
        "badge": "warning", "must_protect": True},
    2: {"code": "L2", "name": "内部", "desc": "内部业务数据，不对外公开但危害有限",
        "badge": "info", "must_protect": False},
    1: {"code": "L1", "name": "公开", "desc": "可对外公开的数据",
        "badge": "secondary", "must_protect": False},
}


def level_meta(level: int) -> dict:
    return LEVELS.get(int(level or 1), LEVELS[1])


# ----------------------------- 内置校验算法 -----------------------------

_IDCARD_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDCARD_CK = "10X98765432"


def checksum_idcard(s: str) -> bool:
    """18 位居民身份证校验位校验（GB 11643-1999 ISO 7064:1983 MOD 11-2）。"""
    s = (s or "").strip().upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    try:
        total = sum(int(s[i]) * _IDCARD_W[i] for i in range(17))
    except ValueError:
        return False
    return _IDCARD_CK[total % 11] == s[17]


def luhn(s: str) -> bool:
    """银行卡/信用卡 Luhn(模10) 校验。"""
    s = re.sub(r"\D", "", s or "")
    if not 13 <= len(s) <= 19:
        return False
    total, alt = 0, False
    for ch in reversed(s):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# 统一社会信用代码：18 位，字符集去掉 I/O/S/V/Z，加权求和后 MOD 31
_USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_W = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def checksum_uscc(s: str) -> bool:
    s = (s or "").strip().upper()
    if len(s) != 18:
        return False
    total = 0
    for i in range(17):
        v = _USCC_CHARSET.find(s[i])
        if v < 0:
            return False
        total += v * _USCC_W[i]
    rem = 31 - total % 31
    if rem == 31:
        rem = 0
    return _USCC_CHARSET[rem] == s[17]


_PHONE_PREFIX = (
    "130 131 132 133 134 135 136 137 138 139 145 146 147 148 149 "
    "150 151 152 153 155 156 157 158 159 166 167 "
    "170 171 172 173 174 175 176 177 178 179 180 181 182 183 184 185 186 187 188 189 "
    "190 191 192 193 195 196 197 198 199"
).split()


def is_cn_mobile(s: str) -> bool:
    s = re.sub(r"\D", "", s or "")
    # 支持带 +86 / 0086 前缀
    if s.startswith("86") and len(s) == 13:
        s = s[2:]
    return len(s) == 11 and s[:3] in _PHONE_PREFIX


_IPV4_PART = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"


def is_ipv4(s: str) -> bool:
    if not re.fullmatch(rf"{_IPV4_PART}(?:\.{_IPV4_PART}){{3}}", s or ""):
        return False
    return True


# 常见弱口令/密钥态会出现的内**容**特征（不是列名）
_SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)"           # PEM 私钥
    r"|(?:AKID[0-9A-Za-z]{16,})"                        # 云厂商 AccessKeyId
    r"|(?:\$(?:2[aby]|[156]|argon2[id]?)\$[./$A-Za-z0-9]{20,})"  # bcrypt / crypt(3) 哈希串
)

# ----------------------------- 传感器定义 -----------------------------
# 每个传感器：
#   id/label       类别标识与中文名
#   level          默认分级（GB/T 43697-2024 四级）
#   category       分类：个人信息 / 金融支付 / 凭证密钥 / 组织 / 网络 / 位置 / 设备
#   compliance     合规标签
#   pattern        正则（需保证线性时间，避免 ReDoS）
#   check          二次校验函数（提升置信度、砍掉误报）
#   base_conf      未过校验时的置信度；过校验后按 confident 提升
#   hint           前向 k-v 上下文命中（如 "password": "xxx"）
#   mask           建议脱敏动作

# 注意：边界一律用 look-around，值本体**不写捕获组**——扫描时统一取 group(0)，
# 避免「首个捕获组只匹配到局部片段」导致的漏检（除 PAT_KV_SECRET 需要取值组）。
PAT_IDCARD = r"(?<![0-9])(?:[1-9]\d{5})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9])"
PAT_PHONE = r"(?<![0-9])(?:1[3-9]\d{9})(?![0-9])"
PAT_BANKCARD = r"(?<![0-9])(?:\d{16,19})(?![0-9])"
PAT_EMAIL = r"(?:[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
PAT_IPV4 = rf"(?<!\d)(?:{_IPV4_PART}(?:\.{_IPV4_PART}){{3}})(?!\d)"
PAT_IPV6 = r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
PAT_USCC = r"(?<![0-9A-Za-z])(?:[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})(?![0-9A-Za-z])"
PAT_PASSPORT = r"(?<![0-9A-Za-z])(?:[EGD]\d{8})(?![0-9A-Za-z])"
PAT_PLATE = r"(?<![一-龥A-Z0-9])(?:[一-龥][A-Z][A-Z0-9]{5,6})(?![一-龥A-Z0-9])"
PAT_DATE = r"(?:(?:19|20)\d{2}[-/年][01]?\d[-/月][0-3]?\d日?)"
PAT_IBAN = r"\b(?:[A-Z]{2}\d{2}[A-Z0-9]{11,30})\b"
# 键值型凭证：password/token/secret 等键后面的值（区分键的语义）
PAT_KV_SECRET = (
    r"(?:password|passwd|pwd|secret|token|api_?key|access_?key|private_?key|"
    r"authorization|credential|session_?id|cookie|otp|verification_?code)"
    r"[\"'\s]*[:=][\"'\s]*"
    r"([^,\s\"'}}\]]{4,64})"
)
PAT_MAC_ADDR = r"\b(?:[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b"
PAT_URL_TOKEN = r"(?:[a-zA-z]+://[^\s]*?(?:token|key|sig|signature|code)=[^\s&\"']{6,})"

SENSORS: List[Dict[str, Any]] = [
    {
        "id": "id_card", "label": "居民身份证号码", "level": 4,
        "category": "个人信息", "compliance": ["PIPL", "GB/T 35273", "MLPS"],
        "pattern": PAT_IDCARD, "check": checksum_idcard, "confident": 0.99,
        "base_conf": 0.80, "mask": "mask",
        "why": "可直接定位到自然人，GB/T 43697 中列为重要的个人身份信息",
    },
    {
        "id": "bank_card", "label": "银行卡 / 信用卡号", "level": 4,
        "category": "金融支付", "compliance": ["PCI-DSS", "PIPL", "MLPS"],
        "pattern": PAT_BANKCARD, "check": luhn, "confident": 0.98,
        "base_conf": 0.30, "mask": "mask",
        "why": "支付账户主数据，PCI-DSS 要求不得明文存储",
    },
    {
        "id": "credential", "label": "口令 / 密钥 / Token", "level": 4,
        "category": "凭证密钥", "compliance": ["MLPS", "PIPL"],
        "pattern": PAT_KV_SECRET, "check": None, "confident": 0.90,
        "base_conf": 0.90, "mask": "hash",
        "why": "鉴权凭据泄露可直接导致越权访问",
    },
    {
        "id": "private_key", "label": "私钥 / 云 AK", "level": 4,
        "category": "凭证密钥", "compliance": ["MLPS"],
        "pattern": _SECRET_VALUE_RE.pattern, "check": None, "confident": 1.0,
        "base_conf": 1.0, "mask": "hash",
        "why": "非对称私钥 / 云凭证，最高敏感级",
    },
    {
        "id": "passport", "label": "护照 / 军官证", "level": 4,
        "category": "个人信息", "compliance": ["PIPL", "MLPS"],
        "pattern": PAT_PASSPORT, "check": None, "confident": 0.70,
        "base_conf": 0.45, "mask": "mask",
        "why": "跨境身份标识，结合姓名可定位自然人",
    },
    {
        "id": "mobile", "label": "手机号码", "level": 3,
        "category": "个人信息", "compliance": ["PIPL", "GB/T 35273"],
        "pattern": PAT_PHONE, "check": is_cn_mobile, "confident": 0.95,
        "base_conf": 0.60, "mask": "mask",
        "why": "个人联系方式，GB/T 35273 列为个人敏感信息",
    },
    {
        "id": "email", "label": "电子邮箱", "level": 3,
        "category": "个人信息", "compliance": ["PIPL", "GDPR", "GB/T 35273"],
        "pattern": PAT_EMAIL, "check": None, "confident": 0.90,
        "base_conf": 0.90, "mask": "mask",
        "why": "可直接联系到自然人 / 常作为账号标识",
    },
    {
        "id": "uscc", "label": "统一社会信用代码", "level": 3,
        "category": "组织", "compliance": ["MLPS"],
        "pattern": PAT_USCC, "check": checksum_uscc, "confident": 0.95,
        "base_conf": 0.30, "mask": "mask",
        "why": "法人与其他组织唯一标识，可定位到组织实体",
    },
    {
        "id": "iban", "label": "国际银行账号 IBAN", "level": 4,
        "category": "金融支付", "compliance": ["PCI-DSS", "GDPR"],
        "pattern": PAT_IBAN, "check": None, "confident": 0.75,
        "base_conf": 0.45, "mask": "mask",
        "why": "跨境金融账户标识",
    },
    {
        "id": "ip_addr", "label": "IP 地址", "level": 3,
        "category": "网络", "compliance": ["PIPL", "GB/T 35273"],
        "pattern": PAT_IPV4, "check": is_ipv4, "confident": 0.95,
        "base_conf": 0.70, "mask": "mask",
        "why": "GB/T 35273 明确 IP 属于个人信息范畴",
    },
    {
        "id": "mac_addr", "label": "MAC 地址", "level": 2,
        "category": "设备", "compliance": ["GB/T 35273"],
        "pattern": PAT_MAC_ADDR, "check": None, "confident": 0.85,
        "base_conf": 0.85, "mask": "mask",
        "why": "设备唯一标识，可关联到自然人使用习惯",
    },
    {
        "id": "url_secret", "label": "含凭证的 URL", "level": 4,
        "category": "凭证密钥", "compliance": ["MLPS"],
        "pattern": PAT_URL_TOKEN, "check": None, "confident": 0.85,
        "base_conf": 0.85, "mask": "drop",
        "why": "URL 中的签名/令牌，泄露即可重放请求",
    },
    {
        "id": "license_plate", "label": "车牌号", "level": 3,
        "category": "个人信息", "compliance": ["PIPL"],
        "pattern": PAT_PLATE, "check": None, "confident": 0.72,
        "base_conf": 0.45, "mask": "mask",
        "why": "可关联到车辆所有人，属可识别到自然人的信息",
    },
    {
        "id": "birthday", "label": "出生日期", "level": 2,
        "category": "个人信息", "compliance": ["GB/T 35273"],
        "pattern": PAT_DATE, "check": None, "confident": 0.5,
        "base_conf": 0.35, "mask": "generalize",
        "why": "单条日期敏感度低，但与姓名/手机号组合可定位自然人",
    },
]


def _compile(sensor: dict) -> Any:
    if sensor.get("_re") is None:
        sensor["_re"] = re.compile(sensor["pattern"], re.IGNORECASE)
    return sensor["_re"]


def list_sensors() -> List[dict]:
    """供前端展示「识别能力清单」（业界同类产品的核心卖点页面）。"""
    out = []
    for s in SENSORS:
        lv = level_meta(s["level"])
        out.append({
            "id": s["id"], "label": s["label"], "level": s["level"],
            "level_code": lv["code"], "level_name": lv["name"],
            "level_desc": lv["desc"], "category": s["category"],
            "compliance": s["compliance"], "mask": s["mask"],
            "why": s["why"],
            "validator": bool(s.get("check")),
        })
    out.sort(key=lambda x: (-x["level"], x["category"]))
    return out


# ----------------------------- 脱敏样例 -----------------------------

def redact(value: str, keep_head: int = 3, keep_tail: int = 4) -> str:
    """生成脱敏样例（**扫描结果中绝不出现原始值**）。

    邮箱特殊处理：保留域名（用于判断业务归属），隐去账号主体。
    """
    v = str(value or "")
    if not v:
        return ""
    if "@" in v and re.fullmatch(r"[^@\s]+@[^@\s]+", v):
        head, domain = v.split("@", 1)
        return (head[:2] + "*" * max(3, len(head) - 2)) + "@" + domain
    n = len(v)
    if n <= keep_head + keep_tail:
        return v[0] + "*" * max(1, n - 1)
    return v[:keep_head] + "*" * (n - keep_head - keep_tail) + v[-keep_tail:]


def mask_value(value: str, rule: str) -> str:
    """按规则对单个值做脱敏（导出/预览用）。"""
    v = str(value or "")
    if rule in (None, "", "none"):
        return v
    if rule == "drop":
        return ""
    if rule == "mask":
        return redact(v)
    if rule == "hash":
        import hashlib
        return hashlib.sha256(v.encode("utf-8")).hexdigest()[:16]
    if rule == "fake":
        import hashlib
        h = hashlib.md5(v.encode("utf-8")).hexdigest()
        return "SIM_" + h[:8]
    if rule == "generalize":
        # 数值/日期泛化：保留结构、去掉精度
        if re.fullmatch(r"(?:19|20)\d{2}[-/]?\d{2}[-/]?\d{2}", v):
            return v[:4] + "-**-**"
        try:
            f = float(v)
        except ValueError:
            return redact(v)
        step = 100 if abs(f) >= 1000 else 10
        return str(int(f // step * step))
    return v


# ----------------------------- 扫描核心 -----------------------------

_MAX_SAMPLE_PER_TYPE = 5          # 每类最多留多少条脱敏样例
_MAX_CHARS_DEFAULT = 2 * 1024 * 1024   # 默认最多扫 2MB 文本（防止大文件拖死）


def scan_text(text: str, *, sensor_ids: List[str] = None,
              max_chars: int = _MAX_CHARS_DEFAULT) -> Dict[str, Any]:
    """扫描一段文本，返回按类型聚合的发现结果。

    返回结构：
      {"scanned_chars": int, "findings": [ {type,label,level,level_code,...,hits,confidence,samples:[脱敏样例]} ],
       "max_level": 1..4, "risk_score": 0..100}
    """
    if not text:
        return {"scanned_chars": 0, "findings": [], "max_level": 0, "risk_score": 0}
    blob = text[:max_chars]
    want = set(sensor_ids) if sensor_ids else None
    findings: Dict[str, dict] = {}

    for sensor in SENSORS:
        sid = sensor["id"]
        if want and sid not in want:
            continue
        regex = _compile(sensor)
        hits = 0
        passed = 0
        samples: List[str] = []
        for m in regex.finditer(blob):
            val = m.group(1) if m.groups() else m.group(0)
            if not val:
                continue
            if len(val) < 4 and sid != "birthday":
                continue
            ok = None
            if sensor.get("check"):
                try:
                    ok = bool(sensor["check"](val))
                except Exception:  # noqa: BLE001
                    ok = False
            # 有校验器的类型必须通过校验才算命中（身份证/卡号/统一社会信用代码/手机号）
            if sensor.get("check") and not ok:
                continue
            hits += 1
            if ok:
                passed += 1
            if len(samples) < _MAX_SAMPLE_PER_TYPE:
                s = redact(val) if sid != "credential" else "***" + str(len(val))
                if s not in samples:
                    samples.append(s)
        if hits:
            # 带校验器的类型：能通过校验（身份证校验位 / Luhn / 号段）才是确证命中 → 高置信；
            # 无校验器的类型：本身没有验证手段，保守取其基准置信度。
            conf = sensor["confident"] if sensor.get("check") else sensor["base_conf"]
            lv = level_meta(sensor["level"])
            findings[sid] = {
                "type": sid,
                "label": sensor["label"],
                "level": sensor["level"],
                "level_code": lv["code"],
                "level_name": lv["name"],
                "level_desc": lv["desc"],
                "category": sensor["category"],
                "compliance": sensor["compliance"],
                "suggested_mask": sensor["mask"],
                "why": sensor["why"],
                "hits": hits,
                "confidence": round(float(conf), 3),
                "samples": samples,
            }

    out = sorted(findings.values(), key=lambda x: (-x["level"], -x["hits"]))
    max_level = max([f["level"] for f in out], default=0)
    # 风险分：以最高等级为主项，命中密度与类型数为修正项
    density = min(1.0, sum(f["hits"] for f in out) / max(1, len(blob) / 4096))
    risk_score = int(min(
        100,
        (max_level / 4) * 70 + density * 20 + min(len(out), 8) * 1.25
    ))
    return {
        "scanned_chars": len(blob),
        "findings": out,
        "max_level": max_level,
        "risk_score": risk_score,
    }


def classify_columns(columns: List[str]) -> List[dict]:
    """仅按字段名做轻量分级（无内容时的兜底，置信度标记 -1 表示「未取样，仅列名推断」）。"""
    name_map = {
        "id_card": ["id_card", "idcard", "identity", "id_number", "sfz"],
        "bank_card": ["bank_card", "credit_card", "card_no", "account_no", "bankcard"],
        "credential": ["password", "passwd", "pwd", "secret", "token", "api_key",
                       "private_key", "credential"],
        "mobile": ["phone", "mobile", "tel", "telephone", "手机号码", "联系电话"],
        "email": ["email", "mail", "邮箱"],
        "ip_addr": ["ip", "ip_addr", "client_ip", "remote_addr"],
        "uscc": ["uscc", "credit_code", "tax_no", "organization_code"],
        "mac_addr": ["mac", "mac_addr"],
        "license_plate": ["plate", "car_no", "vehicle"],
        "birthday": ["birthday", "birth", "date_of_birth"],
    }
    sensors = {s["id"]: s for s in SENSORS}
    out = []
    for col in columns or []:
        low = str(col).lower()
        sid = None
        for k, keys in name_map.items():
            if any(x in low for x in keys):
                sid = k
                break
        if not sid:
            out.append({
                "column": col, "type": None, "label": "未识别",
                "level": 1, "level_code": "L1", "level_name": "公开",
                "category": "—", "compliance": [], "suggested_mask": "none",
                "confidence": 0.0, "source": "name",
            })
            continue
        s = sensors[sid]
        lv = level_meta(s["level"])
        out.append({
            "column": col, "type": s["id"], "label": s["label"],
            "level": s["level"], "level_code": lv["code"], "level_name": lv["name"],
            "category": s["category"], "compliance": s["compliance"],
            "suggested_mask": s["mask"], "why": s["why"],
            "confidence": 0.6, "source": "name",
        })
    # 排序键：column 可能是 dict（API 层传 {name, samples} 结构），直接
    # 比较会 TypeError → 多列分类整体返回空（实测 4 列即崩，单列不触发）
    def _ckey(x):
        col = x.get("column")
        if isinstance(col, str):
            return col
        try:
            return json.dumps(col, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(col)

    out.sort(key=lambda x: (-x["level"], _ckey(x)))
    return out


def to_mask_rules(findings: List[dict]) -> Dict[str, str]:
    """把识别结果直接转成脱敏规则 {类别: 动作}，供「脱敏导出」一键套用。"""
    return {f["type"]: f.get("suggested_mask") or "mask" for f in findings if f.get("type")}


# ----------------------------- 文件扫描 -----------------------------

TEXT_EXT = (".sql", ".csv", ".txt", ".json", ".log", ".ndjson", ".xml", ".yaml", ".yml")
COMPRESSED_EXT = (".gz", ".bz2", ".xz", ".lzma")
_SQL_INSERT_RE = re.compile(r"insert\s+into\s+[`\"\[]?([\w.$]+)", re.IGNORECASE)


def _open_maybe_compressed(path: str):
    """按后缀透明解压（zstd 若不可用则跳过该文件）。"""
    low = path.lower()
    if low.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    if low.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="ignore")
    if low.endswith((".xz", ".lzma")):
        return lzma.open(path, "rt", encoding="utf-8", errors="ignore")
    if low.endswith(".zst"):
        try:
            import zstandard as zstd  # type: ignore
            return io.TextIOWrapper(zstd.open(path, "rb"), encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return None
    return open(path, "rt", encoding="utf-8", errors="ignore")


def scan_file(path: str, *, max_chars: int = _MAX_CHARS_DEFAULT) -> Dict[str, Any]:
    """扫描一个备份产物文件（支持透明解压 + SQL/CSV 表级归属）。

    返回在 scan_text 基础上补充 file / size_bytes / tables 字段。
    """
    res: Dict[str, Any] = {
        "file": os.path.basename(path), "path": path, "scanned_chars": 0,
        "findings": [], "max_level": 0, "risk_score": 0, "tables": [],
        "scannable": True, "reason": "",
    }
    try:
        if not os.path.isfile(path):
            res.update(scannable=False, reason="文件不存在或无权限")
            return res
        size = os.path.getsize(path)
        res["size_bytes"] = size
        ext = os.path.splitext(path)[1].lower()
        if ext not in TEXT_EXT and ext not in COMPRESSED_EXT and ext != ".zst":
            res.update(scannable=False, reason="非文本类型（跳过内容扫描，仅按资产元信息盘点）")
            return res
        fh = _open_maybe_compressed(path)
        if fh is None:
            res.update(scannable=False, reason="缺少 zstandard 依赖，无法解压 .zst")
            return res
        with fh:
            chunks = []
            total = 0
            for line in fh:
                chunks.append(line)
                total += len(line)
                if total >= max_chars:
                    break
        text = "".join(chunks)
        result = scan_text(text, max_chars=max_chars)
        res.update(result)
        res["tables"] = sorted(set(_SQL_INSERT_RE.findall(text)))[:20]
    except Exception as e:  # noqa: BLE001
        res.update(scannable=False, reason=f"扫描失败: {e}")
    return res


def scan_files(paths: Iterable[str], *, max_chars: int = _MAX_CHARS_DEFAULT) -> Dict[str, Any]:
    """批量扫描多个文件并汇总（资产级视图）。"""
    agg: Dict[str, dict] = {}
    files: List[dict] = []
    max_level = 0
    scanned = 0
    for p in paths:
        r = scan_file(p, max_chars=max_chars)
        item = {k: r.get(k) for k in
                ("file", "path", "size_bytes", "max_level", "risk_score",
                 "scanned_chars", "scannable", "reason", "tables")}
        item["findings"] = r.get("findings", [])
        files.append(item)
        if not r.get("scannable"):
            continue
        scanned += 1
        max_level = max(max_level, r.get("max_level", 0))
        for f in r.get("findings", []):
            a = agg.setdefault(f["type"], dict(f, files=0, hits=0, samples=[]))
            a["hits"] += f["hits"]
            a["files"] += 1
            for s in f["samples"]:
                if len(a["samples"]) < 5 and s not in a["samples"]:
                    a["samples"].append(s)
    findings = sorted(agg.values(), key=lambda x: (-x["level"], -x["hits"]))
    return {
        "files": files,
        "scanned_files": scanned,
        "findings": findings,
        "max_level": max_level,
        "total_hits": sum(f["hits"] for f in findings),
    }


if __name__ == "__main__":  # 自测
    demo = """
    INSERT INTO t_users(id,name,phone,id_card,email,bank_card,ip) VALUES
    (1,'张三','13812341234','11010119900307487X','z***@163.com','6222021234567890123','192.168.1.23');
    {"password": "P@ssw0rd-2024", "token": "eyJhbGciOiJIUzI1NiJ9xxxx"}
    """
    print(json.dumps(scan_text(demo), ensure_ascii=False, indent=2))
