# -*- coding: utf-8 -*-
"""DNS 记录可视化与管理 API（容灾切换的流量牵引底座）。

  GET    /api/dr-dns/records           记录清单（含实时解析状态）
  POST   /api/dr-dns/records           新增/更新 A 记录 {domain, ip}
  DELETE /api/dr-dns/records/<domain>  删除记录
  GET    /api/dr-dns/resolve?domain=x  解析验证（确认流量牵引生效）
"""
from flask import jsonify, request

from api import api_bp
from core import dr_dns


@api_bp.route("/dr-dns/records", methods=["GET"])
def api_dns_records():
    try:
        records = dr_dns.list_records()
        # 附实时解析状态（DNS 是否真实生效）
        for r in records:
            resolved = dr_dns.resolve_domain(r["domain"])
            r["resolved_ip"] = resolved or ""
            r["in_sync"] = (resolved == r["ip"]) if resolved else None
        return jsonify({"ok": True, "records": records,
                        "conf_path": dr_dns._conf_path()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/dr-dns/records", methods=["POST"])
def api_dns_upsert():
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = dr_dns.upsert_record((data.get("domain") or "").strip(),
                                   (data.get("ip") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, **res})


@api_bp.route("/dr-dns/records/<path:domain>", methods=["DELETE"])
def api_dns_delete(domain):
    try:
        res = dr_dns.delete_record(domain)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, **res})


@api_bp.route("/dr-dns/resolve", methods=["GET"])
def api_dns_resolve():
    domain = (request.args.get("domain") or "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "domain 必填"}), 400
    ip = dr_dns.resolve_domain(domain)
    return jsonify({"ok": True, "domain": domain, "resolved": ip or None})
