# -*- coding: utf-8 -*-
"""容灾站点拓扑 / 候选备机池 / 目标驱动 API。

对标：航天云网 V5.0（分布式节点/任务重调度）、英方 i2Availability（多对多池化
多级接管）。路由（共享 api_bp）：

- GET/POST /api/dr/sites                     站点列表 / 新建（dr.link.manage）
- GET/PUT/DELETE /api/dr/sites/<id>          站点详情 / 更新 / 删除
- GET /api/dr/links/<id>/candidates          候选备机池（含探测健康）
- POST /api/dr/links/<id>/candidates         新增/更新候选（带 id 即更新）
- DELETE /api/dr/candidates/<id>             删除候选
- POST /api/dr/candidates/<id>/probe         单候选健康探测（只读）
- GET /api/dr/links/<id>/candidates/ranked   按优先级排序的可用/跳过列表
- GET/PUT /api/dr/links/<id>/targets         RTO/RPO 目标
- GET /api/dr/readiness                      容灾就绪度聚合（?light=1 跳过探测）
"""
from flask import jsonify, request

from auth import login_required, permission_required
from core import db, dr_topology as topo
from . import api_bp

PERM = "dr.link.manage"


@api_bp.route("/dr/sites", methods=["GET"])
@login_required
def dr_sites_list():
    return jsonify({"sites": topo.list_sites()})


@api_bp.route("/dr/sites", methods=["POST"])
@permission_required(PERM)
def dr_sites_create():
    data = request.get_json(silent=True) or {}
    try:
        sid = topo.create_site(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": sid, "ok": True}), 201


@api_bp.route("/dr/sites/<int:sid>", methods=["GET"])
@login_required
def dr_sites_get(sid):
    row = topo.get_site(sid)
    if not row:
        return jsonify({"error": "站点不存在"}), 404
    return jsonify(row)


@api_bp.route("/dr/sites/<int:sid>", methods=["PUT"])
@permission_required(PERM)
def dr_sites_update(sid):
    if not topo.update_site(sid, request.get_json(silent=True) or {}):
        return jsonify({"error": "站点不存在或无可更新字段"}), 404
    return jsonify({"ok": True, "id": sid})


@api_bp.route("/dr/sites/<int:sid>", methods=["DELETE"])
@permission_required(PERM)
def dr_sites_delete(sid):
    result = topo.delete_site(sid)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@api_bp.route("/dr/links/<int:link_id>/candidates", methods=["GET"])
@login_required
def dr_cands_list(link_id):
    return jsonify({"candidates": topo.list_candidates(link_id)})


@api_bp.route("/dr/links/<int:link_id>/candidates", methods=["POST"])
@permission_required(PERM)
def dr_cands_upsert(link_id):
    data = dict(request.get_json(silent=True) or {})
    data["link_id"] = link_id
    try:
        cid = topo.upsert_candidate(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": cid, "ok": True}), (201 if not data.get("id") else 200)


@api_bp.route("/dr/candidates/<int:cid>", methods=["DELETE"])
@permission_required(PERM)
def dr_cands_delete(cid):
    topo.delete_candidate(cid)
    return jsonify({"ok": True})


@api_bp.route("/dr/candidates/<int:cid>/probe", methods=["POST"])
@login_required
def dr_cands_probe(cid):
    # get_candidate 返回掩码密码（"***"）供页面展示，不能直接拿去探测——
    # decrypt_candidate 会把 "***" 解成空串导致必然认证失败（P1-8）。
    # 从库里取原始行（密码仍是密文）再解密。
    row = db.query_one("SELECT * FROM dr_failover_candidates WHERE id=?", (cid,))
    if not row:
        return jsonify({"error": "候选不存在"}), 404
    body = request.get_json(silent=True) or {}
    raw = topo.decrypt_candidate(dict(row))
    return jsonify(topo.probe_candidate(
        raw, check_replica=bool(body.get("check_replica", True))))


@api_bp.route("/dr/links/<int:link_id>/candidates/ranked", methods=["GET"])
@login_required
def dr_cands_ranked(link_id):
    check = request.args.get("check_replica", "1") not in ("0", "false", "False")
    return jsonify(topo.ranked_candidates(link_id, check_replica=check))


@api_bp.route("/dr/links/<int:link_id>/targets", methods=["GET"])
@login_required
def dr_targets_get(link_id):
    return jsonify(topo.get_targets(link_id))


@api_bp.route("/dr/links/<int:link_id>/targets", methods=["PUT"])
@permission_required(PERM)
def dr_targets_set(link_id):
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(topo.set_targets(
            link_id, data.get("rto_target_sec"), data.get("rpo_target_min")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/dr/readiness", methods=["GET"])
@login_required
def dr_readiness():
    light = request.args.get("light", "0") in ("1", "true", "True")
    link_id = request.args.get("link_id", type=int)
    return jsonify(topo.readiness(link_id=link_id, light=light))
