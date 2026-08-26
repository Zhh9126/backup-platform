# -*- coding: utf-8 -*-
"""数据价值挖掘 API：备份数据脱敏导出（Data Mining / Anonymized Export）。"""
import os

from flask import request, jsonify, send_file

from auth import login_required
from core import models, data_mining as mining_engine
from . import api_bp, safe_download_path


@api_bp.route("/datamining/exports", methods=["GET"])
@login_required
def list_exports():
    """列出脱敏导出历史。"""
    return jsonify(mining_engine.DataMiner().list_exports())


@api_bp.route("/datamining/export", methods=["POST"])
@login_required
def export_anonymized():
    """触发一次脱敏导出。body: {source_record_id, columns?, mask_rules?}。"""
    data = request.get_json(force=True, silent=True) or {}
    source_record_id = data.get("source_record_id")
    if not source_record_id:
        return jsonify({"error": "source_record_id 必填"}), 400
    try:
        result = mining_engine.DataMiner().export_anonymized(
            int(source_record_id),
            columns=data.get("columns"),
            mask_rules=data.get("mask_rules"),
            row_count=int(data.get("row_count") or 50),
        )
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"导出失败: {e}"}), 500


@api_bp.route("/datamining/exports/<int:export_id>/download", methods=["GET"])
@login_required
def download_export(export_id):
    """下载生成的脱敏文件（本地下载）。"""
    exp = models.get_anonymized_export(export_id)
    if not exp:
        return jsonify({"error": "导出记录不存在"}), 404
    # 安全整改：仅允许下载备份根目录内的导出文件
    fp = safe_download_path(exp.get("file_path") or "")
    if fp is None:
        return jsonify({"error": "文件不存在或路径不合法"}), 404
    return send_file(
        fp, as_attachment=True,
        download_name=os.path.basename(fp),
        mimetype="text/csv; charset=utf-8",
    )


@api_bp.route("/datamining/exports/<int:export_id>", methods=["DELETE"])
@login_required
def delete_export(export_id):
    """删除一条导出记录（同时删除物理文件）。"""
    if not models.get_anonymized_export(export_id):
        return jsonify({"error": "导出记录不存在"}), 404
    ok = mining_engine.DataMiner().delete_export(export_id)
    return jsonify({"ok": ok})


@api_bp.route("/datamining/rule-templates", methods=["GET"])
@login_required
def list_rule_templates():
    """返回脱敏规则模板（最小/标准/严格），前端一键套用。"""
    return jsonify(mining_engine.DataMiner().list_rule_templates())


@api_bp.route("/datamining/db-schemas", methods=["GET"])
@login_required
def list_db_schemas():
    """返回每个 db_type 对应的典型表/列集合（用于「按来源记录推荐列」）。"""
    return jsonify(mining_engine.DataMiner().list_db_schemas())


@api_bp.route("/datamining/records/<int:source_record_id>/suggest", methods=["GET"])
@login_required
def suggest_columns(source_record_id):
    """根据来源备份记录推荐可选列（解决"列固定"问题）。"""
    return jsonify(mining_engine.DataMiner().suggest_columns_for_record(source_record_id))


@api_bp.route("/datamining/preview-rules", methods=["POST"])
@login_required
def preview_mask_rules():
    """预览每列最终生效的脱敏规则 + 含义。
    body: {columns: [...], mask_rules?: {col: rule}}
    """
    data = request.get_json(force=True, silent=True) or {}
    columns = data.get("columns") or []
    mask_rules = data.get("mask_rules") or None
    return jsonify(mining_engine.DataMiner().preview_mask_rules(columns, mask_rules))
