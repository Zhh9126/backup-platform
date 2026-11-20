"""Unified recovery catalogue; execution uses the established restore and VM services."""
from flask import request, jsonify
from auth import permission_required
from . import api_bp


@api_bp.route("/recovery/catalog", methods=["GET"])
@permission_required("restore.run")
def recovery_catalog():
    from core.recovery_catalog import catalog
    try:
        result = catalog(request.args.get("category", "db"),
                         request.args.get("q", "").strip()[:200],
                         request.args.get("page", 1, type=int),
                         request.args.get("size", 8, type=int),
                         request.args.get("record_id", type=int),
                         request.args.get("rp_id", type=int))
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})
