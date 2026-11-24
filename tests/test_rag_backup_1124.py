# -*- coding: utf-8 -*-
"""RAG 知识库备份 + 向量引擎修复的契约测试（隔离 META_DB，零生产写入）。"""
import os
import sys
import json
import tempfile
import shutil
import hashlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="aidbm_rag_test_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

import core.db as db  # noqa: E402
db.init_schema()

from core import rag_backup as rb  # noqa: E402
from core.engines.qdrant import QdrantEngine  # noqa: E402
from core.engines.base import BackupResult  # noqa: E402


def _mk_backup_task(name, db_type, db_name=""):
    return db.execute(
        "INSERT INTO backup_tasks(name, db_type, host, port, username, password, "
        "db_name, backup_mode, biz_system, enabled) VALUES (?,?,?,?,?,?,?,?,?,1)",
        (name, db_type, "127.0.0.1", 6333, "", "", db_name, "logical", "E2E-RAG"))


def test_schema_and_fingerprint():
    rb.init_schema()
    kb = {"embed_model": "text-embedding-3-large", "embed_version": "v1",
          "embed_dim": 1536, "distance_metric": "Cosine",
          "chunk_size": 512, "chunk_overlap": 64, "chunk_strategy": "recursive"}
    fp1 = rb.fingerprint(kb)
    assert fp1 == rb.fingerprint(dict(kb))
    kb2 = dict(kb); kb2["embed_version"] = "v2"
    assert rb.fingerprint(kb2) != fp1, "模型版本变化必须改变指纹"
    kb3 = dict(kb); kb3["chunk_size"] = 1024
    assert rb.fingerprint(kb3) != fp1, "分块规则变化必须改变指纹"
    kb4 = dict(kb); kb4["top_k"] = 10  # 非指纹字段
    assert rb.fingerprint(kb4) == fp1, "top_k 不应参与指纹"


def test_crud_and_baseline_precheck():
    vt = _mk_backup_task("rag-vec", "qdrant", "kb1")
    dt = _mk_backup_task("rag-doc", "file", "/data/docs")
    kid = rb.create_kb({"name": "客服知识库", "vector_task_id": vt,
                        "doc_task_id": dt, "embed_model": "BAAI/bge",
                        "embed_version": "v1.0", "embed_dim": 1024,
                        "distance_metric": "Cosine", "chunk_size": 512,
                        "chunk_overlap": 64, "strict_version": True})
    assert rb.get_kb(kid)["name"] == "客服知识库"
    # 无探测环境时 build_baseline 仍固化配置（探测失败如实记 note）
    base = rb.build_baseline(kid)
    assert base["fingerprint"] and base["config"]["embed_model"] == "BAAI/bge"
    ok = rb.precheck_restore(kid)
    assert ok["ok"] and ok["fingerprint"] == base["fingerprint"]
    # 漂移模拟：改版本 -> strict 拒绝
    rb.update_kb(kid, {"embed_version": "v2.0"})
    bad = rb.precheck_restore(kid)
    assert not bad["ok"] and "embed_version" in bad["diffs"]
    assert bad["strict"] is True
    # 非 strict 仅告警
    rb.update_kb(kid, {"strict_version": False})
    warn = rb.precheck_restore(kid)
    assert not warn["ok"] and warn["strict"] is False
    rb.delete_kb(kid)


def test_precheck_target_config_override():
    vt = _mk_backup_task("rag-vec2", "chroma", "/var/lib/chroma")
    kid = rb.create_kb({"name": "kb2", "vector_task_id": vt,
                        "embed_model": "m", "embed_version": "v1",
                        "embed_dim": 768})
    rb.build_baseline(kid)
    same = rb.precheck_restore(kid, {"embed_model": "m", "embed_version": "v1",
                                     "embed_dim": "768"})
    assert same["ok"], same
    drift = rb.precheck_restore(kid, {"embed_model": "different-model"})
    assert not drift["ok"] and "embed_model" in drift["diffs"]
    rb.delete_kb(kid)


def test_qdrant_restore_reads_manifest_and_checks_sha():
    """多集合恢复必须读清单全部恢复；sha 不符必须拒绝。"""
    src = open(os.path.join(ROOT, "core/engines/qdrant.py"), encoding="utf-8").read()
    assert "_manifest.json" in src and "priority=snapshot" in src
    assert "sha256 不匹配" in src, "恢复前必须校验清单 sha256"
    assert "pairs" in src and "for src, tgt, fp in pairs" in src, "必须遍历全部快照片"

    eng = QdrantEngine({"id": 99, "db_type": "qdrant", "host": "127.0.0.1",
                        "port": 6333, "db_name": "", "extra_options": ""}, _TMP)
    work = os.path.join(_TMP, "snaps"); os.makedirs(work, exist_ok=True)
    snap = os.path.join(work, "qdrant_kb1_20260101.snapshot")
    with open(snap, "wb") as f:
        f.write(b"fake snapshot content" * 100)
    mf = os.path.join(work, "qdrant_kb1_20260101.snapshot_manifest.json")
    with open(mf, "w", encoding="utf-8") as f:
        json.dump({"collections": [
            {"collection": "kb1", "file": os.path.basename(snap),
             "sha256": "0" * 64}]}, f)
    # HTTP 调用应因 sha 不符直接拒绝（不会发出必然错误的 multipart）
    with mock.patch.object(eng, "_base_url", return_value="http://127.0.0.1:6333"):
        r = eng.restore(snap, target_db="kb1")
    assert not r.success and "sha256 不匹配" in r.message, r.message


def test_qdrant_verify_checks_manifest_sha():
    eng = QdrantEngine({"id": 99, "db_type": "qdrant", "host": "127.0.0.1",
                        "port": 6333, "db_name": "", "extra_options": ""}, _TMP)
    work = os.path.join(_TMP, "verify"); os.makedirs(work, exist_ok=True)
    snap = os.path.join(work, "qdrant_kb_20260101.snapshot")
    # 造一个最小合法 tar（含 storage/ 成员）
    import tarfile
    with tarfile.open(snap, "w:gz") as tf:
        import io
        data = b"x" * 100
        info = tarfile.TarInfo("storage/content")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    mf = snap + "_manifest.json"
    with open(mf, "w", encoding="utf-8") as f:
        json.dump({"collections": [{"collection": "kb",
                                    "file": os.path.basename(snap),
                                    "sha256": "f" * 64}]}, f)
    r = eng.verify_record({"backup_path": snap})
    assert not r.success and "sha" in r.message.lower(), r.message
    # 正确 sha -> 通过
    real = hashlib.sha256(open(snap, "rb").read()).hexdigest()
    with open(mf, "w", encoding="utf-8") as f:
        json.dump({"collections": [{"collection": "kb",
                                    "file": os.path.basename(snap),
                                    "sha256": real}]}, f)
    r2 = eng.verify_record({"backup_path": snap})
    assert r2.success and "全部匹配" in r2.message, r2.message


def test_api_blueprint_registered():
    from api import api_bp
    rules = {r.rule for r in api_bp.app.url_map.iter_rules()} if getattr(api_bp, "app", None) else set()
    # 静态兜底：注册元组含 rag_backup + 端点函数存在
    init_src = open(os.path.join(ROOT, "api/__init__.py"), encoding="utf-8").read()
    assert "rag_backup" in init_src
    api_src = open(os.path.join(ROOT, "api/rag_backup.py"), encoding="utf-8").read()
    for ep in ("/rag-kb", "/baseline", "/precheck", "/verify", "/probe"):
        assert ep in api_src
