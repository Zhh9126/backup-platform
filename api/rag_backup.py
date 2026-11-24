# -*- coding: utf-8 -*-
"""RAG 知识库备份 API（配置指纹 / 基线 / 恢复前漂移拦截 / 质量复核）。"""
from flask import request, jsonify
from auth import login_required
from core import rag_backup, db  # noqa: F401 - db 用于审计（新模块易漏 import）
from . import api_bp


@api_bp.route("/rag-kb", methods=["GET"])
@login_required
def list_rag_kbs():
    return jsonify({"kbs": rag_backup.list_kbs(),
                    "fingerprint_fields": list(rag_backup.FINGERPRINT_FIELDS)})


@api_bp.route("/rag-kb/<int:kb_id>", methods=["GET"])
@login_required
def get_rag_kb(kb_id):
    kb = rag_backup.get_kb(kb_id)
    if not kb:
        return jsonify({"error": "知识库不存在: %s" % kb_id}), 404
    return jsonify(kb)


@api_bp.route("/rag-kb", methods=["POST"])
@login_required
def create_rag_kb():
    data = request.get_json(force=True, silent=True) or {}
    try:
        kid = rag_backup.create_kb(data)
        return jsonify({"ok": True, "id": kid}), 201
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/rag-kb/<int:kb_id>", methods=["PUT"])
@login_required
def update_rag_kb(kb_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": rag_backup.update_kb(kb_id, data)})
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/rag-kb/<int:kb_id>", methods=["DELETE"])
@login_required
def delete_rag_kb(kb_id):
    return jsonify({"ok": rag_backup.delete_kb(kb_id)})


@api_bp.route("/rag-kb/<int:kb_id>/probe", methods=["POST"])
@login_required
def probe_rag_kb(kb_id):
    """探测向量库当前元数据（collection / points / 维度 / 距离）。"""
    kb = rag_backup.get_kb(kb_id)
    if not kb:
        return jsonify({"error": "知识库不存在"}), 404
    try:
        return jsonify(rag_backup.probe_vector_task(kb["vector_task_id"]))
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/rag-kb/<int:kb_id>/baseline", methods=["POST"])
@login_required
def build_rag_baseline(kb_id):
    """固化备份基线（配置指纹 + 当前元数据）。"""
    try:
        return jsonify({"ok": True, "baseline": rag_backup.build_baseline(kb_id)})
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/rag-kb/<int:kb_id>/precheck", methods=["POST"])
@login_required
def precheck_rag_restore(kb_id):
    """恢复前漂移校验（strict 模式失败返回 409，前端据此拦截）。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = rag_backup.precheck_restore(kb_id, data.get("target_config"))
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), (409 if (not result.get("ok") and result.get("strict")) else 200)


@api_bp.route("/rag-kb/<int:kb_id>/verify", methods=["POST"])
@login_required
def verify_rag_retrieval(kb_id):
    """恢复后结构/规模复核（非语义相关性评分，诚实边界在 note 内）。"""
    try:
        return jsonify(rag_backup.verify_retrieval(kb_id))
    except rag_backup.RAGError as e:
        return jsonify({"error": str(e)}), 400
