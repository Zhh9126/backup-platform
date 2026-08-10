# -*- coding: utf-8 -*-
"""
数据价值挖掘：备份数据脱敏导出（Data Mining / Anonymized Export）。

把备份数据「脱敏后导出供分析」，提升数据资产价值（对应蓝图难点3解决方案）。

真实环境应从 backup_records / backup_sets 读出对应引擎的导出/解压结果；本期在
DEMO_MODE 或缺少真实导出能力时，生成一份仿真 CSV（含若干伪造行 + 指定列），然后
做脱敏，保证无真实环境也能跑通自测与演示。所有路径均在 backups/anonymized_exports/ 下。
"""
import os
import csv
import json
import random
import string
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import config
import core.db as db
import core.models as models


_logger = db.get_logger("data_mining")

# 默认导出的仿真列（PII 列用于演示自动脱敏）
DEFAULT_COLUMNS = [
    "id", "name", "phone", "email", "id_card",
    "address", "bank_card", "amount", "created_at",
]

# PII 列（按列名子串匹配）→ 默认脱敏规则
# 注意：键顺序按"特异性从高到低"，避免「email」被「mail」先误匹配。
PII_MASK_RULES = {
    "id_card": "mask",
    "idcard": "mask",
    "bank_card": "mask",
    "credit_card": "mask",
    "card_no": "mask",
    "phone": "mask",
    "mobile": "mask",
    "email": "mask",      # 必须在 "mail" 之前
    "mail": "mask",
    "id_number": "mask",
    "identity": "mask",
    "ssn": "mask",
    "name": "fake",
    "username": "fake",
    "real_name": "fake",
    "address": "fake",
    "location": "fake",
    "ip": "mask",
    "password": "hash",
    "pwd": "hash",
    "secret": "hash",
    "token": "hash",
}

# 规则模板（按合规严度从宽到严，一键套用）
RULE_TEMPLATES = {
    "minimal": {
        "label": "最小（仅强 PII 必脱敏）",
        "desc": "仅身份证/银行卡/密码做脱敏，姓名/手机/邮箱用途必要时可保留",
        "rules": {
            "id_card": "mask", "idcard": "mask",
            "bank_card": "mask", "credit_card": "mask",
            "password": "hash", "pwd": "hash", "secret": "hash",
        },
    },
    "standard": {
        "label": "标准（推荐）",
        "desc": "手机/邮箱/身份证/银行卡均打码，姓名/地址仿真替换",
        "rules": {
            "phone": "mask", "mobile": "mask",
            "email": "mask", "id_card": "mask", "idcard": "mask",
            "bank_card": "mask",
            "name": "fake", "username": "fake", "real_name": "fake",
            "address": "fake", "location": "fake",
        },
    },
    "strict": {
        "label": "严格（默认）",
        "desc": "所有可能含个人信息列均脱敏；IP/Token 哈希不可逆",
        "rules": {
            "phone": "mask", "mobile": "mask",
            "email": "mask", "mail": "mask",
            "id_card": "mask", "idcard": "mask", "id_number": "mask",
            "bank_card": "mask", "credit_card": "mask", "card_no": "mask",
            "name": "fake", "username": "fake", "real_name": "fake",
            "address": "fake", "location": "fake",
            "ip": "mask", "password": "hash", "pwd": "hash",
            "secret": "hash", "token": "hash",
        },
    },
}

# 规则说明（前端展示"这条规则做了什么"）
RULE_DESCRIPTIONS = {
    "mask": "部分打码（138****1234）",
    "hash": "不可逆哈希（sha256 前16位）",
    "drop": "删除该列",
    "fake": "仿真替换（保留可读性，去除真实信息）",
    "none": "不脱敏（保留原值）",
}

# 每种 db_type 的典型"表/字段"（用于按来源记录动态给出可选列）。
# 设计目标：不同的 db_type 看到不同的列集合（解决"无论选哪个备份列表都是固定的"问题）。
# 真实场景：会对接元数据库/快照索引的字段元数据；本期用领域典型示例。
DB_TYPE_SCHEMAS = {
    "mysql": {
        "label": "MySQL（订单/用户/日志）",
        "tables": {
            "t_orders": [
                "id", "order_no", "user_id", "user_name", "phone", "email",
                "address", "amount", "status", "created_at",
            ],
            "t_users": [
                "id", "username", "real_name", "phone", "email",
                "id_card", "address", "password", "created_at",
            ],
            "t_payments": [
                "id", "order_id", "bank_card", "amount", "status", "paid_at",
            ],
            "t_audit_logs": [
                "id", "user_id", "ip", "action", "detail", "created_at",
            ],
        },
    },
    "mariadb": {
        "label": "MariaDB（业务库）",
        "tables": {
            "customers": [
                "id", "customer_name", "phone", "email", "address",
                "id_card", "credit_card", "vip_level", "created_at",
            ],
            "sensors": [
                "id", "device_id", "location", "value", "recorded_at",
            ],
        },
    },
    "postgresql": {
        "label": "PostgreSQL（时序/JSON）",
        "tables": {
            "pg_users": [
                "id", "username", "phone", "email", "id_card",
                "password", "profile", "created_at",
            ],
            "pg_events": [
                "id", "user_id", "ip", "event_type", "payload", "ts",
            ],
        },
    },
    "oracle": {
        "label": "Oracle（财务/HR）",
        "tables": {
            "hr_employees": [
                "emp_id", "emp_name", "phone", "email", "id_card",
                "address", "salary", "hire_date",
            ],
            "fin_transactions": [
                "tx_id", "account_no", "amount", "memo", "posted_at",
            ],
        },
    },
    "kingbase": {
        "label": "KingbaseES（信创）",
        "tables": {
            "kb_customers": [
                "id", "name", "phone", "email", "id_card", "address",
            ],
            "kb_orders": [
                "id", "customer_id", "amount", "status", "created_at",
            ],
        },
    },
    "dameng": {
        "label": "达梦 DM（信创）",
        "tables": {
            "dm_personnel": [
                "id", "name", "phone", "id_card", "address",
                "bank_card", "salary", "joined_at",
            ],
            "dm_records": [
                "id", "category", "title", "content", "created_at",
            ],
        },
    },
    "redis": {
        "label": "Redis（会话/缓存）",
        "tables": {
            "user_session": [
                "session_id", "user_id", "username", "token", "ip",
                "login_at", "expire_at",
            ],
            "user_profile": [
                "user_id", "username", "phone", "email", "real_name",
            ],
        },
    },
    "mongodb": {
        "label": "MongoDB（文档/日志）",
        "tables": {
            "user_docs": [
                "id", "name", "phone", "email", "address",
                "id_card", "profile", "created_at",
            ],
            "app_logs": [
                "id", "user_id", "ip", "action", "payload", "ts",
            ],
        },
    },
    "file": {
        "label": "文件备份（文本/日志）",
        "tables": {
            "log_entries": [
                "ts", "user_id", "ip", "action", "detail", "user_agent",
            ],
            "config_files": [
                "id", "name", "value", "secret", "updated_at",
            ],
        },
    },
}


def _detect_db_type(record: dict) -> str:
    """从备份记录中嗅探 db_type（兼容 list 中字段顺序）。"""
    if not record:
        return ""
    return (record.get("db_type") or record.get("dbtype") or "").lower()


def _default_columns_for(db_type: str) -> List[str]:
    """取该 db_type 第一个表的所有列作为默认导出候选。"""
    schema = DB_TYPE_SCHEMAS.get(db_type)
    if not schema:
        return list(DEFAULT_COLUMNS)
    first_table = next(iter(schema["tables"].values()), [])
    return list(first_table)

VALID_RULES = ("mask", "hash", "drop", "fake", "none")

# 仿真用随机源（固定 seed，保证同一进程内可复现）
_rng = random.Random()


class DataMiner:
    """脱敏导出引擎：从备份集读出数据 → 脱敏 → 写出 CSV → 记录元数据。"""

    def __init__(self, export_root: str = None):
        # 导出目录：backups/anonymized_exports/（确保存在）
        self.export_dir = Path(
            export_root or os.path.join(config.BACKUP_ROOT, "anonymized_exports"))
        self.export_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------- 对外 API -------------------------
    def export_anonymized(self, source_record_id: int,
                          columns: Optional[List[str]] = None,
                          mask_rules: Optional[Dict[str, str]] = None,
                          row_count: int = 50) -> Dict[str, Any]:
        """生成脱敏导出文件并记录元数据。

        Args:
            source_record_id: 来源备份记录 id（仅用于关联记录，导出内容在 DEMO 下仿真）。
            columns: 要导出的列名列表；为空则使用 DEFAULT_COLUMNS。
            mask_rules: {列名: 规则}；为空则根据 PII 列自动推断。
                - mask : 部分打码（如 138****1234）
                - hash : sha256 不可逆
                - drop : 删除该列
                - fake : 用仿真值替换
                - none : 不脱敏

        Returns:
            {"id", "file_path", "row_count", "columns"}
        """
        rec = models.get_record(source_record_id)
        if not rec:
            raise ValueError(f"备份记录不存在: id={source_record_id}")

        columns = list(columns) if columns else list(DEFAULT_COLUMNS)
        # 去重并去掉空列
        seen, uniq = set(), []
        for c in columns:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        columns = uniq
        if not columns:
            raise ValueError("导出列不能为空")

        # 规范化规则，并剔除被 drop 的列
        rules = self._normalize_rules(columns, mask_rules)
        kept_columns = [c for c in columns if rules.get(c) != "drop"]

        # 生成仿真数据 → 脱敏（行数 1~2000，取用户传的 row_count）
        row_count = max(1, min(2000, int(row_count or 50)))
        rows = self._generate_rows(kept_columns, count=row_count)
        masked_rows = self._apply_masks(rows, kept_columns, rules)

        # 写出 CSV（UTF-8 BOM，Excel 友好）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"anonymized_{source_record_id}_{ts}.csv"
        fpath = self.export_dir / fname
        self._write_csv(fpath, kept_columns, masked_rows)

        export_id = models.create_anonymized_export({
            "source_record_id": source_record_id,
            "columns": kept_columns,
            "mask_rules": {c: rules.get(c) for c in kept_columns},
            "file_path": str(fpath),
            "row_count": len(masked_rows),
            "note": "来源备份记录 #{}（{}）脱敏导出".format(
                source_record_id, rec.get("db_type") or ""),
        })
        _logger.info("[data_mining] 脱敏导出完成 id=%s 文件=%s 行数=%d",
                     export_id, fpath, len(masked_rows))
        return {
            "id": export_id,
            "file_path": str(fpath),
            "row_count": len(masked_rows),
            "columns": kept_columns,
        }

    def list_exports(self, limit: int = 200) -> List[dict]:
        return models.list_anonymized_exports(limit=limit)

    def list_rule_templates(self) -> dict:
        """返回规则模板（最小/标准/严格），前端一键套用。"""
        return {
            k: {"label": v["label"], "desc": v["desc"], "rules": dict(v["rules"])}
            for k, v in RULE_TEMPLATES.items()
        }

    def list_db_schemas(self) -> dict:
        """返回 db_type → {label, tables: {table: [columns]}}。

        不同 db_type 看到不同的列集合（解决"无论选哪个备份列表都是固定的"问题）。
       """
        return {
            k: {"label": v["label"], "tables": dict(v["tables"])}
            for k, v in DB_TYPE_SCHEMAS.items()
        }

    def suggest_columns_for_record(self, source_record_id: int) -> dict:
        """根据来源备份记录返回推荐的可选列清单。"""
        rec = models.get_record(source_record_id)
        if not rec:
            return {"db_type": "", "label": "通用（默认列）", "default": list(DEFAULT_COLUMNS), "tables": []}
        db_type = (rec.get("db_type") or "").lower()
        schema = DB_TYPE_SCHEMAS.get(db_type)
        if schema:
            default = list(next(iter(schema["tables"].values()), list(DEFAULT_COLUMNS)))
            tables = [{"name": t, "columns": list(cols)}
                      for t, cols in schema["tables"].items()]
        else:
            default = list(DEFAULT_COLUMNS)
            tables = []
        return {
            "db_type": db_type,
            "label": schema["label"] if schema else "通用（默认列）",
            "default": default,
            "tables": tables,
        }

    def preview_mask_rules(self, columns: List[str],
                            mask_rules: Optional[Dict[str, str]] = None) -> dict:
        """预测每列脱敏规则与含义（前端预览，所见即所得）。"""
        rules = self._normalize_rules(columns, mask_rules)
        return {
            "columns": [{"column": c, "rule": rules.get(c, "none"),
                          "desc": RULE_DESCRIPTIONS.get(rules.get(c, "none"), "")}
                        for c in columns],
        }

    def get_export(self, export_id: int) -> Optional[dict]:
        return models.get_anonymized_export(export_id)

    def delete_export(self, export_id: int) -> bool:
        exp = models.get_anonymized_export(export_id)
        if not exp:
            return False
        # 同时删除物理文件
        fp = exp.get("file_path")
        if fp and os.path.isfile(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
        models.delete_anonymized_export(export_id)
        return True

    # ------------------------- 内部：规则与生成 -------------------------
    def _normalize_rules(self, columns: List[str],
                         mask_rules: Optional[Dict[str, str]]) -> Dict[str, str]:
        """将用户规则与默认 PII 规则合并，返回 {列名: 规则}。"""
        rules: Dict[str, str] = {}
        for c in columns:
            cl = str(c).lower()
            r = None
            if mask_rules:
                r = mask_rules.get(c)
                if r is None:
                    r = mask_rules.get(cl)  # 兼容小写键
            if r is None:
                # 自动 PII 识别
                for key, rule in PII_MASK_RULES.items():
                    if key in cl:
                        r = rule
                        break
            if r not in VALID_RULES:
                r = "none"
            rules[c] = r
        return rules

    def _generate_rows(self, columns: List[str], count: int = 50) -> List[Dict[str, Any]]:
        rows = []
        for i in range(1, count + 1):
            row = {c: self._fake_value(c, i) for c in columns}
            rows.append(row)
        return rows

    def _apply_masks(self, rows: List[Dict[str, Any]], columns: List[str],
                     rules: Dict[str, str]) -> List[Dict[str, Any]]:
        out = []
        for row in rows:
            new_row = {}
            for c in columns:
                rule = rules.get(c, "none")
                val = row.get(c)
                if rule == "none":
                    new_row[c] = val
                elif rule == "hash":
                    new_row[c] = self._mask_hash(val)
                elif rule == "mask":
                    new_row[c] = self._mask_partial(c, val)
                elif rule == "fake":
                    new_row[c] = self._fake_value(c, _rng.randint(1, 999999))
                # drop 已在 kept_columns 中剔除，不会进入此处
                else:
                    new_row[c] = val
            out.append(new_row)
        return out

    # ------------------------- 内部：脱敏原语 -------------------------
    @staticmethod
    def _mask_hash(value: Any) -> str:
        s = "" if value is None else str(value)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _mask_partial(col: str, value: Any) -> str:
        """按列类型做部分打码。"""
        s = "" if value is None else str(value)
        cl = col.lower()
        # 重要：先匹配"含特定子串"的更具体规则（身份证/银行卡），避免「id_card」
        # 被「card」抢先命中走银行卡规则。
        if "id_card" in cl or "idcard" in cl or "id_number" in cl:
            return DataMiner._mask_middle(s, 6, 4)
        if "bank" in cl or "credit_card" in cl or "card_no" in cl:
            return DataMiner._mask_middle(s, 4, 4)
        if "phone" in cl or "mobile" in cl:
            return DataMiner._mask_middle(s, 3, 4)
        if "email" in cl or "mail" in cl:
            if "@" in s:
                local, domain = s.split("@", 1)
                head = local[0] if local else "*"
                return f"{head}***@{domain}"
            return DataMiner._mask_middle(s, 1, 1)
        if cl == "ip" or cl.startswith("ip_") or cl.endswith("_ip") or "ip_address" in cl:
            return DataMiner._mask_ip(s)
        return DataMiner._mask_middle(s, 1, 1)

    @staticmethod
    def _mask_ip(s: str) -> str:
        """IP 地址打码：1.2.3.4 → 1.***.3.4"""
        if not s:
            return s
        parts = s.split(".")
        if len(parts) == 4:
            parts[1] = "***"
            return ".".join(parts)
        return DataMiner._mask_middle(s, 1, 1)

    @staticmethod
    def _mask_middle(s: str, head: int, tail: int) -> str:
        if not s:
            return s
        if len(s) <= head + tail + 1:
            return "*" * len(s)
        return s[:head] + "****" + s[-tail:]

    # ------------------------- 内部：仿真数据生成 -------------------------
    _SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
    _GIVEN = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英"]
    _DOMAINS = ["example.com", "corp.cn", "mail.io", "bank.example", "test.org"]
    _CITIES = ["北京市朝阳区", "上海市浦东新区", "广州市天河区", "深圳市南山区", "杭州市西湖区", "成都市武侯区", "武汉市江汉区", "南京市鼓楼区"]
    _STREETS = ["南京路", "中山路", "人民大道", "科技园", "金融街", "滨江路", "高新路", "解放路"]

    def _fake_value(self, col: str, idx: int) -> Any:
        cl = col.lower()
        if cl == "id" or cl.endswith("_id"):
            return idx
        if "phone" in cl or "mobile" in cl:
            return "1" + str(_rng.randint(3, 9)) + "".join(
                _rng.choice(string.digits) for _ in range(9))
        if "email" in cl or cl == "mail":
            return f"user{idx}{_rng.randint(0, 999)}@{_rng.choice(self._DOMAINS)}"
        if "id_card" in cl or "idcard" in cl:
            region = _rng.choice(["110101", "310115", "440106", "440305", "330106", "510107"])
            y = _rng.randint(1970, 2000)
            m = _rng.randint(1, 12)
            d = _rng.randint(1, 28)
            seq = f"{_rng.randint(0, 9999):04d}"
            return f"{region}{y}{m:02d}{d:02d}{seq}{_rng.choice(string.digits)}"
        if "name" in cl or cl == "username":
            return _rng.choice(self._SURNAMES) + _rng.choice(self._GIVEN)
        if "address" in cl:
            return f"{_rng.choice(self._CITIES)}{_rng.choice(self._STREETS)}{_rng.randint(1, 999)}号"
        if "bank" in cl or "card" in cl:
            length = _rng.choice([16, 19])
            return "".join(_rng.choice(string.digits) for _ in range(length))
        if "amount" in cl or "price" in cl or "balance" in cl:
            return round(_rng.uniform(10, 99999), 2)
        if "created_at" == cl or "time" in cl or "date" in cl:
            y = _rng.randint(2023, 2026)
            mo = _rng.randint(1, 12)
            da = _rng.randint(1, 28)
            return f"{y}-{mo:02d}-{da:02d} {_rng.randint(0, 23):02d}:{_rng.randint(0, 59):02d}:00"
        if "age" in cl:
            return _rng.randint(18, 70)
        if "status" in cl:
            return _rng.choice(["active", "inactive", "pending"])
        # 默认：随机字母数字串
        return "val_" + "".join(_rng.choice(string.ascii_lowercase + string.digits)
                                for _ in range(6)) + f"_{idx}"

    # ------------------------- 内部：写出 -------------------------
    @staticmethod
    def _write_csv(fpath: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
        with open(fpath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})


# 便捷单例
data_miner = DataMiner()
