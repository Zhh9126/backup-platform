# -*- coding: utf-8 -*-
"""RAG 知识库备份单元（AI 大模型备份的能力补齐）。

问题背景
--------
向量库快照（Qdrant/Chroma）只保住「向量索引」这一层。一个能正确回答问题的
RAG 知识库还依赖三层快照**不包含**的配置：

    embedding 模型名 + 版本 + 向量维度（换了模型 = 向量空间全变，检索静默失效）
    分块规则（chunk_size / overlap / strategy）（换了 = 切片全部错位）
    原始文档资产（对象存储/文件任务）（索引可重建但文档丢了 RAG 无源可依）

本模块把这三件事做成一等公民：**RAG 知识库 = 向量任务 + 文档任务 +
embedding 配置指纹**，并提供：

1. 配置指纹 `fingerprint`：关键配置 sha256，恢复前比对，漂移即拦截/告警；
2. 备份清单 `build_baseline`：固化配置 + 当前 collection 元数据（points 数、
   维度、距离度量），作为检索质量基线随保护计划留存；
3. 恢复前校验 `precheck_restore`：目标侧配置与基线不一致时明确拒绝，
   不让"版本漂移的静默错误"发生；
4. 检索质量复核 `verify_retrieval`：恢复后探测 collection 的 points_count /
   维度 / 状态与基线对比，偏差如实报告（诚实边界：不做语义相关性打分，
   那需要 embedding 模型与查询集在场，见 docs/benchmark_ai_backup）。

零安装/离线：纯 Python 标准库 + core.db，向量库探测走各引擎既有 HTTP/SSH 通道。
不依赖收费 AI 助手模块。
"""
import hashlib
import json
import time
from typing import Optional

import core.db as db

KB_TABLE = """CREATE TABLE IF NOT EXISTS rag_knowledge_bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vector_task_id INTEGER,
    doc_task_id INTEGER DEFAULT 0,
    embed_model TEXT DEFAULT '',
    embed_version TEXT DEFAULT '',
    embed_dim INTEGER DEFAULT 0,
    distance_metric TEXT DEFAULT '',
    chunk_size INTEGER DEFAULT 0,
    chunk_overlap INTEGER DEFAULT 0,
    chunk_strategy TEXT DEFAULT '',
    top_k INTEGER DEFAULT 0,
    strict_version INTEGER DEFAULT 1,
    baseline TEXT DEFAULT '',
    last_verify TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
)"""

# 参与配置指纹的字段（顺序固定，变更任一即指纹变化）
FINGERPRINT_FIELDS = (
    "embed_model", "embed_version", "embed_dim", "distance_metric",
    "chunk_size", "chunk_overlap", "chunk_strategy",
)
# 顶层不参与指纹（只是恢复行为开关/展示）
NON_FINGERPRINT_FIELDS = ("name", "vector_task_id", "doc_task_id", "top_k",
                          "strict_version", "enabled", "remark", "baseline",
                          "last_verify")


class RAGError(ValueError):
    pass


def init_schema():
    db.execute(KB_TABLE)
    # 增量迁移：老库补列
    for col, ddl in (
        ("embed_version", "TEXT DEFAULT ''"),
        ("embed_dim", "INTEGER DEFAULT 0"),
        ("distance_metric", "TEXT DEFAULT ''"),
        ("chunk_size", "INTEGER DEFAULT 0"),
        ("chunk_overlap", "INTEGER DEFAULT 0"),
        ("chunk_strategy", "TEXT DEFAULT ''"),
        ("top_k", "INTEGER DEFAULT 0"),
        ("strict_version", "INTEGER DEFAULT 1"),
        ("baseline", "TEXT DEFAULT ''"),
        ("last_verify", "TEXT DEFAULT ''"),
        ("doc_task_id", "INTEGER DEFAULT 0"),
    ):
        try:
            db.execute("ALTER TABLE rag_knowledge_bases ADD COLUMN %s %s" % (col, ddl))
        except Exception:
            pass


def _now():
    return db.now_iso()


def config_of(kb: dict) -> dict:
    """取参与指纹的配置子集（归一为字符串）。"""
    return {f: str(kb.get(f) or "") for f in FINGERPRINT_FIELDS}


def fingerprint(kb: dict) -> str:
    """配置指纹：模型/版本/维度/距离/分块任一变化即变化。"""
    canon = json.dumps(config_of(kb), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _parse(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def list_kbs() -> list:
    rows = db.query("SELECT * FROM rag_knowledge_bases ORDER BY id DESC") or []
    out = []
    for r in rows:
        d = dict(r)
        d["config"] = config_of(d)
        d["fingerprint"] = fingerprint(d)
        d["baseline"] = _parse(d.get("baseline"))
        d["last_verify"] = _parse(d.get("last_verify"))
        out.append(d)
    return out


def get_kb(kb_id: int) -> Optional[dict]:
    for k in list_kbs():
        if k["id"] == kb_id:
            return k
    return None


def _validate_task(task_id, field_name, allow_empty=True):
    if not task_id:
        return 0
    tid = int(task_id)
    row = db.query_one("SELECT id, name, db_type FROM backup_tasks WHERE id=?", (tid,))
    if not row:
        raise RAGError("%s 对应的备份任务不存在: %s" % (field_name, tid))
    return tid


def create_kb(data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise RAGError("知识库名称必填")
    vtid = _validate_task(data.get("vector_task_id"), "向量索引任务", False)
    if not vtid:
        raise RAGError("必须指定向量索引备份任务")
    dtid = _validate_task(data.get("doc_task_id"), "原始文档任务")
    now = _now()
    return db.execute(
        "INSERT INTO rag_knowledge_bases(name, vector_task_id, doc_task_id, "
        "embed_model, embed_version, embed_dim, distance_metric, chunk_size, "
        "chunk_overlap, chunk_strategy, top_k, strict_version, enabled, remark, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, vtid, dtid,
         (data.get("embed_model") or "").strip(),
         (data.get("embed_version") or "").strip(),
         int(data.get("embed_dim") or 0),
         (data.get("distance_metric") or "").strip(),
         int(data.get("chunk_size") or 0),
         int(data.get("chunk_overlap") or 0),
         (data.get("chunk_strategy") or "").strip(),
         int(data.get("top_k") or 0),
         1 if data.get("strict_version", True) else 0,
         1 if data.get("enabled", True) else 0,
         (data.get("remark") or "").strip(), now, now))


def update_kb(kb_id: int, data: dict) -> bool:
    if not get_kb(kb_id):
        raise RAGError("知识库不存在: %s" % kb_id)
    sets, params = [], []
    for f in FINGERPRINT_FIELDS + ("top_k", "name", "remark"):
        if f in data:
            v = data[f]
            if f in ("embed_dim", "chunk_size", "chunk_overlap", "top_k"):
                v = int(v or 0)
            elif f != "name":
                v = (v or "").strip() if isinstance(v, str) else v
            sets.append("%s=?" % f)
            params.append(v)
    if "vector_task_id" in data:
        sets.append("vector_task_id=?")
        params.append(_validate_task(data["vector_task_id"], "向量索引任务", False))
    if "doc_task_id" in data:
        sets.append("doc_task_id=?")
        params.append(_validate_task(data["doc_task_id"], "原始文档任务"))
    for f in ("enabled", "strict_version"):
        if f in data:
            sets.append("%s=?" % f)
            params.append(1 if data[f] else 0)
    if not sets:
        return False
    sets.append("updated_at=?"); params.append(_now())
    params.append(kb_id)
    db.execute("UPDATE rag_knowledge_bases SET " + ",".join(sets) + " WHERE id=?",
               tuple(params))
    return True


def delete_kb(kb_id: int) -> bool:
    if not get_kb(kb_id):
        return False
    db.execute("DELETE FROM rag_knowledge_bases WHERE id=?", (kb_id,))
    return True


# ---------------- 向量库元数据探测（复用引擎通道） ----------------
def probe_vector_task(task_id: int) -> dict:
    """从向量库备份任务读当前元数据（collection 列表 / 维度 / 距离）。

    走引擎既有通道：Qdrant HTTP / Chroma SSH；无引擎时只返回任务配置摘要，
    不伪造元数据（诚实边界）。
    """
    row = db.query_one("SELECT * FROM backup_tasks WHERE id=?", (task_id,))
    if not row:
        raise RAGError("向量备份任务不存在: %s" % task_id)
    d = dict(row)
    out = {"task_id": task_id, "db_type": d.get("db_type"),
           "db_name": d.get("db_name") or "", "collections": [],
           "note": ""}
    if d.get("db_type") == "qdrant":
        try:
            from core.engines.qdrant import QdrantEngine
            import config as _cfg
            eng = QdrantEngine(d, str(_cfg.get_backup_root()))
            cols = eng._collections()
            out["collections"] = cols
            base = eng._base_url()
            details = []
            for c in cols:
                try:
                    from core.engines.qdrant import _http
                    _, body = _http("GET", "%s/collections/%s" % (base, c),
                                    headers=eng._headers(), timeout=15)
                    res = (json.loads(body).get("result") or {})
                    params = res.get("config", {}).get("params", {}) or {}
                    vec = params.get("vectors") or {}
                    details.append({
                        "name": c,
                        "status": res.get("status") or "",
                        "points_count": res.get("points_count"),
                        "dim": (vec.get("size") if isinstance(vec, dict)
                                else None),
                        "distance": (vec.get("distance") if isinstance(vec, dict)
                                     else None),
                    })
                except Exception as e:
                    details.append({"name": c, "error": str(e)[:120]})
            out["details"] = details
        except Exception as e:
            out["note"] = "Qdrant 元数据探测失败: %s" % str(e)[:150]
    elif d.get("db_type") == "chroma":
        out["note"] = ("Chroma 为目录级快照，无 collection 元数据 API；"
                       "质量基线以 sqlite 完整性 + 目录清单为准")
    else:
        out["note"] = "非向量库任务，仅记录任务配置"
    return out


# ---------------- 备份基线（固化配置 + 当前元数据） ----------------
def build_baseline(kb_id: int) -> dict:
    """固化知识库配置指纹 + 向量库当前元数据，作为可核对的检索质量基线。"""
    kb = get_kb(kb_id)
    if not kb:
        raise RAGError("知识库不存在: %s" % kb_id)
    cur = probe_vector_task(kb["vector_task_id"])
    baseline = {
        "kb_id": kb_id,
        "fingerprint": fingerprint(kb),
        "config": config_of(kb),
        "vector_probe": cur,
        "doc_task_id": kb.get("doc_task_id") or 0,
        "built_at": _now(),
        "note": ("基线=配置指纹 + 探测时点元数据；检索质量语义对比需 embedding "
                 "模型与查询集在场，见 docs/benchmark_ai_backup_20261103.md"),
    }
    db.execute("UPDATE rag_knowledge_bases SET baseline=?, updated_at=? WHERE id=?",
               (json.dumps(baseline, ensure_ascii=False), _now(), kb_id))
    db.add_log("info", "rag_backup",
               "RAG 知识库「%s」备份基线已固化（fp=%s，collections=%s）"
               % (kb["name"], baseline["fingerprint"][:12],
                  ",".join(cur.get("collections") or []) or "-"))
    return baseline


# ---------------- 恢复前校验（核心：拦截配置漂移） ----------------
def precheck_restore(kb_id: int, target_config: dict = None) -> dict:
    """恢复前校验：目标配置指纹必须与备份基线一致，否则拒绝。

    这是"怎么备份 RAG"的核心结论：只恢复向量快照而不锁定 embedding 配置，
    恢复后的系统会静默给出错误检索结果——比不恢复更危险。

    target_config 不传则用知识库当前配置（即"恢复到原系统"场景）。
    """
    kb = get_kb(kb_id)
    if not kb:
        raise RAGError("知识库不存在: %s" % kb_id)
    baseline = kb.get("baseline")
    if not baseline:
        return {"ok": False, "reason": "尚未固化备份基线（先执行 build_baseline）",
                "hint": "POST /api/rag-kb/<id>/baseline"}
    if not isinstance(baseline, dict):
        return {"ok": False, "reason": "基线格式异常"}
    target = dict(kb)
    if target_config:
        for f in FINGERPRINT_FIELDS:
            if f in target_config:
                target[f] = target_config[f]
    want = baseline.get("fingerprint")
    got = fingerprint(target)
    strict = bool(kb.get("strict_version"))
    if want != got:
        diffs = {f: {"baseline": baseline.get("config", {}).get(f, ""),
                     "target": config_of(target).get(f, "")}
                 for f in FINGERPRINT_FIELDS
                 if baseline.get("config", {}).get(f, "") != config_of(target).get(f, "")}
        msg = ("RAG 配置漂移：备份基线 fp=%s，目标 fp=%s；差异=%s"
               % (want[:12], got[:12], json.dumps(diffs, ensure_ascii=False)))
        db.add_log("warning" if not strict else "error", "rag_backup",
                   "知识库「%s」恢复前校验：%s（strict=%s）"
                   % (kb["name"], msg, strict))
        return {"ok": False, "strict": strict, "reason": msg, "diffs": diffs,
                "hint": ("strict 模式下拒绝恢复；确认要跨版本恢复请先调整配置或"
                         "关闭 strict_version（风险自担）") if strict else
                        "非 strict 模式仅告警，可继续但检索质量不保证"}
    return {"ok": True, "strict": strict, "fingerprint": got,
            "note": "配置指纹一致，可继续恢复（仍需按引擎通道完成快照回放）"}


# ---------------- 恢复后检索质量复核 ----------------
def verify_retrieval(kb_id: int) -> dict:
    """恢复后对比 collection 元数据与基线：points 数/维度/距离偏差如实报告。

    诚实边界：这是"结构完整性 + 规模"复核，不是语义相关性评分。语义质量
    需 embedding 模型 + 查询集在场做 top-k 对比（列为 P2，不虚报）。
    """
    kb = get_kb(kb_id)
    if not kb:
        raise RAGError("知识库不存在: %s" % kb_id)
    baseline = kb.get("baseline") or {}
    cur = probe_vector_task(kb["vector_task_id"])
    base_probe = baseline.get("vector_probe") or {}
    b_details = {d["name"]: d for d in base_probe.get("details", [])}
    c_details = {d["name"]: d for d in cur.get("details", [])}
    diffs = []
    for name in sorted(set(b_details) | set(c_details)):
        b, c = b_details.get(name, {}), c_details.get(name, {})
        if not b:
            diffs.append({"collection": name, "issue": "基线无此 collection，恢复后新增"})
            continue
        if not c:
            diffs.append({"collection": name, "issue": "基线有此 collection，恢复后缺失"})
            continue
        if b.get("points_count") != c.get("points_count"):
            diffs.append({"collection": name, "issue": "points 数变化",
                          "baseline": b.get("points_count"),
                          "current": c.get("points_count")})
        if b.get("dim") and c.get("dim") and b["dim"] != c["dim"]:
            diffs.append({"collection": name, "issue": "向量维度变化",
                          "baseline": b["dim"], "current": c["dim"]})
        if b.get("distance") and c.get("distance") and b["distance"] != c["distance"]:
            diffs.append({"collection": name, "issue": "距离度量变化",
                          "baseline": b["distance"], "current": c["distance"]})
    result = {"ok": not diffs, "kb_id": kb_id, "diffs": diffs,
              "current": cur, "baseline_at": baseline.get("built_at"),
              "verified_at": _now(),
              "note": "结构/规模复核；语义检索质量对比需 embedding 模型+查询集（未做，不虚报）"}
    db.execute("UPDATE rag_knowledge_bases SET last_verify=?, updated_at=? WHERE id=?",
               (json.dumps(result, ensure_ascii=False), _now(), kb_id))
    return result
