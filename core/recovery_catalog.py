"""Paged recovery choices for databases, files/NAS, object storage and VM recovery points."""
from core import db

CATEGORIES = ("db", "file", "obj", "vm")
_RECORD_CATEGORY = ("CASE WHEN COALESCE(NULLIF(r.db_type,''),t.db_type) IN ('file','synology','nas') THEN 'file' "
                    "WHEN COALESCE(NULLIF(r.db_type,''),t.db_type)='object_storage' THEN 'obj' ELSE 'db' END")
_RECORD_FILTER = ("r.status='success' AND COALESCE(r.is_simulated,0)=0 "
                  "AND COALESCE(NULLIF(r.db_type,''),t.db_type,'') NOT IN ('vm','netdev')")
_RECORD_FROM = "FROM backup_records r LEFT JOIN backup_tasks t ON t.id=r.task_id "
_VM_FROM = ("FROM vm_recovery_points r JOIN vm_protected p ON p.id=r.vm_id "
            "LEFT JOIN vm_hypervisors h ON h.id=p.hypervisor_id ")
_VM_FILTER = ("COALESCE(r.is_simulated,0)=0 AND COALESCE(r.artifact_path,'')<>'' "
              "AND (COALESCE(r.expires_at,'')='' OR r.expires_at>?)")


def catalog(category="db", keyword="", page=1, size=8, record_id=None, rp_id=None):
    if category not in CATEGORIES:
        raise ValueError("恢复类型无效")
    page, size = max(1, int(page)), max(1, min(50, int(size)))
    now = db.now_iso()
    counts = dict.fromkeys(CATEGORIES, 0)
    for row in db.query("SELECT " + _RECORD_CATEGORY + " AS category, COUNT(*) AS n " +
                        _RECORD_FROM + "WHERE " + _RECORD_FILTER + " GROUP BY " + _RECORD_CATEGORY):
        counts[row["category"]] = row["n"]
    counts["vm"] = db.query_one("SELECT COUNT(*) AS n " + _VM_FROM + "WHERE " + _VM_FILTER, (now,))["n"]
    if rp_id:
        category = "vm"
    if record_id and not rp_id:
        linked = db.query_one("SELECT id FROM vm_recovery_points WHERE record_id=? "
                              "AND COALESCE(is_simulated,0)=0 ORDER BY id DESC LIMIT 1", (int(record_id),))
        if linked:
            rp_id, category = linked["id"], "vm"
    if record_id and not rp_id:
        row = db.query_one("SELECT " + _RECORD_CATEGORY + " AS category " + _RECORD_FROM +
                           "WHERE r.id=? AND " + _RECORD_FILTER, (int(record_id),))
        if row:
            category = row["category"]
    where, args = [], []
    if category == "vm":
        select = ("SELECT r.id AS source_id,r.vm_id,r.record_id,r.task_id,r.pit_at AS created_at,"
                  "r.size_bytes,r.verified,r.checksum,r.rp_type AS mode,p.vm_name AS name,"
                  "p.node AS source,h.provider,p.vm_ref AS source_db ")
        source, base_filter = _VM_FROM, _VM_FILTER
        args.append(now)
        if rp_id:
            where.append("r.id=?")
            args.append(int(rp_id))
        if keyword:
            where.append("(LOWER(p.vm_name) LIKE ? OR LOWER(p.node) LIKE ? OR CAST(r.id AS TEXT)=?)")
            args.extend(["%" + keyword.lower() + "%"] * 2 + [keyword])
    else:
        select = ("SELECT r.id AS source_id,r.task_id,r.started_at AS created_at,r.size_bytes,r.verified,"
                  "r.checksum,COALESCE(NULLIF(r.backup_mode,''),t.backup_mode,'') AS mode,r.backup_mode AS recorded_mode,"
                  "r.backup_type,COALESCE(NULLIF(r.db_type,''),t.db_type) AS db_type,"
                  "COALESCE(NULLIF(t.biz_system,''),t.name,'已删除任务') AS name,t.name AS task_name,"
                  "t.host AS source,t.db_name AS source_db,t.extra_options ")
        source, base_filter = _RECORD_FROM, _RECORD_FILTER + " AND " + _RECORD_CATEGORY + "=?"
        args.append(category)
        if record_id:
            where.append("r.id=?")
            args.append(int(record_id))
        if keyword:
            where.append("(LOWER(COALESCE(NULLIF(t.biz_system,''),t.name,'')) LIKE ? OR LOWER(t.host) LIKE ? "
                         "OR LOWER(t.db_name) LIKE ? OR CAST(r.id AS TEXT)=?)")
            args.extend(["%" + keyword.lower() + "%"] * 3 + [keyword])
    filters = "WHERE " + base_filter + "".join(" AND " + item for item in where)
    total = db.query_one("SELECT COUNT(*) AS n " + source + filters, tuple(args))["n"]
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    rows = db.query(select + source + filters + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
                    tuple(args) + (size, (page - 1) * size))
    items = []
    for row in rows:
        row = dict(row)
        if category == "obj":
            import json
            try:
                options = json.loads(row.get("extra_options") or "{}")
                row["provider"] = options.get("os_provider") or options.get("provider") or "s3"
            except (TypeError, ValueError):
                row["provider"] = "s3"
        row.pop("extra_options", None)
        row["mode_inferred"] = category != "vm" and not row.pop("recorded_mode", None)
        row.update(category=category, key=category + ":" + str(row["source_id"]),
                   checksum_present=bool(row.pop("checksum", None)))
        items.append(row)
    return {"category": category, "items": items, "counts": counts, "total": total,
            "page": page, "pages": pages, "size": size}
