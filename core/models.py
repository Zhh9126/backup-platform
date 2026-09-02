# -*- coding: utf-8 -*-
"""
数据访问层：备份任务、备份记录、恢复记录、系统日志的读写。

对外返回 dict（便于 JSON 序列化）。密码等敏感字段默认不回显，
仅当 include_secret=True（内部引擎/调度使用）才返回明文。
"""
from typing import Optional

import json

import config
import core.db as db

TASK_FIELDS = [
    # 注意：本列表是 create_task / update_task 的写入白名单，漏项会被静默丢弃
    # （无异常、无日志、HTTP 200）。新增可写字段必须同步登记于此。
    "name", "biz_system", "db_type", "host", "port", "username", "password", "db_name",
    "auth_mode", "backup_type", "backup_mode", "schedule_type", "cron_expr",
    "interval_minutes", "enabled", "retention_days", "retention_count",
    "storage_backend", "remote_host", "remote_port", "remote_user",
    "remote_password", "remote_path", "remote_key", "compress",
    "extra_options", "demo_only", "policy_id",
    "mixed_backup", "full_schedule_type", "full_schedule_expr",
    "full_schedule_days", "incremental_schedule_type", "incremental_schedule_expr",
    "incremental_schedule_days", "bandwidth_limit", "compress_level",
]


# ------------------------- 任务 -------------------------
def create_task(data: dict) -> int:
    data = {k: data.get(k) for k in TASK_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["last_status"] = "never"
    data["last_run_at"] = None

    if not data.get("port") and data.get("db_type") in config.DEFAULT_PORTS:
        data["port"] = config.DEFAULT_PORTS[data["db_type"]]
    if data.get("password"):
        data["password"] = db.encrypt_secret(data["password"])
    if data.get("remote_password"):
        data["remote_password"] = db.encrypt_secret(data["remote_password"])

    data["enabled"] = 1 if data.get("enabled") not in (0, False, "0", None) else 0
    data["compress"] = 1 if data.get("compress") not in (0, False, "0", None) else 0
    data["demo_only"] = 1 if data.get("demo_only") not in (0, False, "0", None) else 0
    data["mixed_backup"] = 1 if data.get("mixed_backup") not in (0, False, "0", None) else 0
    for num in ("port", "interval_minutes", "retention_days", "retention_count",
                "remote_port"):
        if data.get(num) in (None, ""):
            data[num] = None

    # 策略绑定：写入派生的保护列（等级 / 适配层 / RPO / RTO）
    if data.get("policy_id") in (0, "", None):
        data["policy_id"] = None
    if data.get("policy_id"):
        _fill_policy_columns(data, data.get("policy_id"), data.get("db_type"))

    cols = list(data.keys())
    sql = "INSERT INTO backup_tasks ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def update_task(task_id: int, data: dict) -> bool:
    data = {k: v for k, v in data.items() if k in TASK_FIELDS}
    if not data:
        return False
    data["updated_at"] = db.now_iso()
    sets = []
    params = []
    for k, v in data.items():
        if k == "password":
            if v in (None, ""):
                continue  # 不更新密码
            v = db.encrypt_secret(v)
        elif k == "remote_password":
            if v in (None, ""):
                continue
            v = db.encrypt_secret(v)
        elif k in ("enabled", "compress", "demo_only", "mixed_backup"):
            v = 1 if v not in (0, False, "0", None) else 0
        elif k == "policy_id":
            v = v if v not in (0, "", None) else None
        sets.append(f"{k}=?")
        params.append(v)
    # 策略绑定：若提交了 policy_id，联动写入派生的保护列
    if "policy_id" in data:
        _append_policy_binding(sets, params, task_id, data.get("policy_id"))
    params.append(task_id)
    sql = "UPDATE backup_tasks SET {} WHERE id=?".format(",".join(sets))
    db.execute(sql, tuple(params))
    return True


def delete_task(task_id: int) -> bool:
    with db._write_lock:
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM backup_records WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM restore_records WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM backup_tasks WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
    return True


def _decorate(row: dict, include_secret: bool) -> dict:
    row = dict(row)
    row["has_password"] = bool(row.get("password"))
    row["has_remote_password"] = bool(row.get("remote_password"))
    row["db_display_name"] = config.DB_DISPLAY_NAMES.get(row.get("db_type"), row.get("db_type"))
    # 展示派生字段（R2）：供任务列表列与编辑弹窗预填使用，前端只读它
    row["biz_label"] = compute_biz_label(row.get("biz_system"), row.get("name"))
    # 备份类型 / 备份模式中文派生（仪表盘、任务列表统一展示）
    row["backup_type_display"] = config.BACKUP_TYPE_DISPLAY_NAMES.get(
        row.get("backup_type"), row.get("backup_type") or "")
    row["backup_mode_display"] = config.BACKUP_MODE_DISPLAY_NAMES.get(
        row.get("backup_mode"), row.get("backup_mode") or "")
    if include_secret:
        row["password"] = db.decrypt_secret(row.get("password") or "")
        row["remote_password"] = db.decrypt_secret(row.get("remote_password") or "")
    else:
        row["password"] = "" if row.get("password") else ""
        row["remote_password"] = ""
    return row


def get_task(task_id: int, include_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM backup_tasks WHERE id=?", (task_id,))
    if not row:
        return None
    row = _decorate(row, include_secret)
    _attach_policy_to_task(row)
    return row


def list_tasks(include_secret: bool = False, db_type: str = None,
               db_type_exclude: str = None, enabled: bool = None) -> list:
    sql = "SELECT * FROM backup_tasks"
    where, params = [], []
    if db_type:
        where.append("db_type=?")
        params.append(db_type)
    if db_type_exclude:
        where.append("db_type<>?")
        params.append(db_type_exclude)
    if enabled is not None:
        where.append("enabled=?")
        params.append(1 if enabled else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    rows = db.query(sql, tuple(params))
    # 批量加载策略字典，避免 N+1 查询
    pol_map = {p["id"]: p for p in list_protection_policies()}
    out = []
    for r in rows:
        r = _decorate(r, include_secret)
        pid = r.get("policy_id")
        pol = pol_map.get(pid) if pid else None
        r["policy"] = pol
        r["policy_name"] = pol["name"] if pol else None
        out.append(r)
    return out


def set_task_status(task_id: int, last_run_at: str, last_status: str) -> None:
    db.execute(
        "UPDATE backup_tasks SET last_run_at=?, last_status=? WHERE id=?",
        (last_run_at, last_status, task_id))


# ------------------------- 保护策略（ProtectionPolicy） -------------------------
# 任务与策略的关联读写：任务可绑定一个保护策略，绑定后将其等级 / 适配层 /
# RPO / RTO 写入任务的派生列，便于调度（Phase1）与复制链路直接读取。

def _engine_adapter_tier(db_type: str):
    """返回指定 db_type 的适配层分级（core_self / peripheral_api）。"""
    if not db_type:
        return None
    try:
        from core.engines import get_adapter_tier
        return get_adapter_tier(db_type)
    except Exception:
        return None


def _policy_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将保护策略行转为 dict，并把 JSON 字段反序列化。"""
    if not row:
        return None
    d = dict(row)
    for f in ("backup_strategy", "link_strategy", "retention"):
        raw = d.get(f)
        if raw:
            try:
                d[f] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    d["enabled"] = bool(d.get("enabled"))
    return d


def _fill_policy_columns(data: dict, policy_id, db_type: str) -> None:
    """在 create_task 的数据 dict 中追加策略派生的保护列（就地修改）。"""
    pol = get_protection_policy(policy_id)
    if not pol:
        return
    data["protection_level"] = pol["level"]
    data["adapter_tier"] = _engine_adapter_tier(db_type)
    data["rpo_target_min"] = pol["rpo_target_min"]
    data["rto_target_min"] = pol["rto_target_min"]


def _append_policy_binding(sets: list, params: list, task_id: int,
                           policy_id) -> None:
    """在 update_task 的 SET 列表中追加策略派生列。"""
    now = db.now_iso()
    if not policy_id:
        for c in ("protection_level", "adapter_tier", "rpo_target_min", "rto_target_min"):
            sets.append(f"{c}=?")
            params.append(None)
        sets.append("updated_at=?")
        params.append(now)
        return
    pol = get_protection_policy(policy_id)
    if not pol:
        return
    task = get_task(task_id)
    tier = _engine_adapter_tier(task.get("db_type") if task else None)
    for c, v in (("protection_level", pol["level"]),
                 ("adapter_tier", tier),
                 ("rpo_target_min", pol["rpo_target_min"]),
                 ("rto_target_min", pol["rto_target_min"])):
        sets.append(f"{c}=?")
        params.append(v)
    sets.append("updated_at=?")
    params.append(now)


def _attach_policy_to_task(row: dict) -> None:
    """在任务 dict 上附加 policy / policy_name 字段。"""
    pid = row.get("policy_id")
    if pid:
        pol = get_protection_policy(pid)
        row["policy"] = _policy_to_dict(pol) if pol else None
        row["policy_name"] = pol["name"] if pol else None
    else:
        row["policy"] = None
        row["policy_name"] = None


POLICY_FIELDS = ["name", "level", "rpo_target_min", "rto_target_min",
                 "backup_strategy", "link_strategy", "retention", "enabled"]


def _policy_json(data: dict, key: str):
    """将 dict/list 形式的策略字段序列化为 JSON 字符串。"""
    v = data.get(key)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v if v is not None else None


def create_protection_policy(data: dict) -> int:
    data = {k: data.get(k) for k in POLICY_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["enabled"] = 1 if data.get("enabled") not in (0, False, "0", None) else 0
    if data.get("level") in (None, ""):
        data["level"] = "general"
    for num in ("rpo_target_min", "rto_target_min"):
        if data.get(num) in (None, ""):
            data[num] = 0
    data["backup_strategy"] = _policy_json(data, "backup_strategy")
    data["link_strategy"] = _policy_json(data, "link_strategy")
    data["retention"] = _policy_json(data, "retention")
    cols = list(data.keys())
    sql = "INSERT INTO protection_policies ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_protection_policy(policy_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM protection_policies WHERE id=?", (policy_id,))
    return _policy_to_dict(row) if row else None


def list_protection_policies() -> list:
    rows = db.query("SELECT * FROM protection_policies ORDER BY id DESC")
    return [_policy_to_dict(r) for r in rows]


def update_protection_policy(policy_id: int, data: dict) -> bool:
    data = {k: v for k, v in data.items() if k in POLICY_FIELDS}
    if not data:
        return False
    data["updated_at"] = db.now_iso()
    if "enabled" in data:
        data["enabled"] = 1 if data["enabled"] not in (0, False, "0", None) else 0
    if "backup_strategy" in data:
        data["backup_strategy"] = _policy_json(data, "backup_strategy")
    if "link_strategy" in data:
        data["link_strategy"] = _policy_json(data, "link_strategy")
    if "retention" in data:
        data["retention"] = _policy_json(data, "retention")
    sets, params = [], []
    for k, v in data.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(policy_id)
    db.execute("UPDATE protection_policies SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_protection_policy(policy_id: int) -> bool:
    db.execute("DELETE FROM protection_policies WHERE id=?", (policy_id,))
    return True


def count_tasks_by_policy(policy_id: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM backup_tasks WHERE policy_id=?", (policy_id,))
    return row["cnt"] if row else 0


def list_tasks_by_policy(policy_id: int) -> list:
    rows = db.query(
        "SELECT id, name, db_type, protection_level, adapter_tier, "
        "rpo_target_min, rto_target_min FROM backup_tasks WHERE policy_id=?",
        (policy_id,))
    return [dict(r) for r in rows]


def bind_policy_to_tasks(policy_id: int, task_ids: list) -> int:
    """将策略绑定到多个任务，返回成功绑定的数量。"""
    pol = get_protection_policy(policy_id)
    if not pol:
        return 0
    now = db.now_iso()
    bound = 0
    for tid in (task_ids or []):
        task = get_task(tid)
        if not task:
            continue
        tier = _engine_adapter_tier(task.get("db_type"))
        db.execute(
            "UPDATE backup_tasks SET policy_id=?, protection_level=?, adapter_tier=?, "
            "rpo_target_min=?, rto_target_min=?, updated_at=? WHERE id=?",
            (policy_id, pol["level"], tier, pol["rpo_target_min"],
             pol["rto_target_min"], now, tid))
        bound += 1
    return bound


def unbind_policy_from_tasks(task_ids: list) -> None:
    now = db.now_iso()
    for tid in (task_ids or []):
        db.execute(
            "UPDATE backup_tasks SET policy_id=NULL, protection_level=NULL, "
            "adapter_tier=NULL, rpo_target_min=NULL, rto_target_min=NULL, updated_at=? WHERE id=?",
            (now, tid))


def unbind_all_tasks_by_policy(policy_id: int) -> None:
    now = db.now_iso()
    db.execute(
        "UPDATE backup_tasks SET policy_id=NULL, protection_level=NULL, "
        "adapter_tier=NULL, rpo_target_min=NULL, rto_target_min=NULL, updated_at=? WHERE policy_id=?",
        (now, policy_id))


# ------------------------- 备份集（BackupSet，Phase 1） -------------------------
# 备份集是"备份文件"之上的语义实体，承载增量链 / 合成全量 / 去重 / 生命周期。

_BACKUP_SET_FIELDS = [
    "task_id", "record_id", "set_type", "storage_tier", "object_key",
    "parent_set_id", "verified", "size_bytes", "dedup_saved_bytes", "checksum",
    "chain_id", "chain_status",
]


def create_backup_set(data: dict) -> int:
    """创建一条备份集记录。返回新记录 id。"""
    data = {k: data.get(k) for k in _BACKUP_SET_FIELDS}
    data["created_at"] = db.now_iso()
    # 默认值（强类型，避免上游漏传导致脏数据）
    data["set_type"] = data.get("set_type") or "full"
    data["storage_tier"] = int(data.get("storage_tier") or 1)
    data["verified"] = 1 if data.get("verified") else 0
    data["size_bytes"] = int(data.get("size_bytes") or 0)
    data["dedup_saved_bytes"] = int(data.get("dedup_saved_bytes") or 0)
    data["chain_status"] = data.get("chain_status") or "active"
    if data.get("parent_set_id") in (0, "", None):
        data["parent_set_id"] = None
    cols = list(data.keys())
    sql = "INSERT INTO backup_sets ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_backup_set(set_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM backup_sets WHERE id=?", (set_id,))
    return dict(row) if row else None


def list_backup_sets(task_id: int = None, record_id: int = None,
                     set_type: str = None) -> list:
    """列出备份集。可按任务 / 记录 / 类型过滤。"""
    sql = "SELECT * FROM backup_sets"
    where, params = [], []
    if task_id is not None:
        where.append("task_id=?")
        params.append(task_id)
    if record_id is not None:
        where.append("record_id=?")
        params.append(record_id)
    if set_type is not None:
        where.append("set_type=?")
        params.append(set_type)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return db.query(sql, tuple(params))


def update_backup_set(set_id: int, data: dict) -> None:
    """更新备份集字段（白名单）。"""
    allow = {"set_type", "storage_tier", "object_key", "parent_set_id",
             "verified", "size_bytes", "dedup_saved_bytes", "checksum",
             "chain_id", "chain_status"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets, params = [], []
    for k, v in updates.items():
        if k == "verified":
            v = 1 if v else 0
        elif k in ("storage_tier", "size_bytes", "dedup_saved_bytes"):
            v = int(v or 0)
        elif k == "parent_set_id" and v in (0, "", None):
            v = None
        sets.append(f"{k}=?")
        params.append(v)
    params.append(set_id)
    db.execute("UPDATE backup_sets SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))


def delete_backup_set(set_id: int) -> None:
    db.execute("DELETE FROM backup_sets WHERE id=?", (set_id,))


def find_backup_set_by_checksum(checksum: str) -> Optional[dict]:
    """按去重哈希查找已存在备份集（对象级去重用）。"""
    if not checksum:
        return None
    row = db.query_one(
        "SELECT * FROM backup_sets WHERE checksum=? ORDER BY id ASC LIMIT 1",
        (checksum,))
    return dict(row) if row else None


def add_dedup_saved(set_id: int, extra_bytes: int) -> None:
    """累加某备份集的去重节省量（命中重复对象时调用）。"""
    if not set_id or not extra_bytes:
        return
    db.execute(
        "UPDATE backup_sets SET dedup_saved_bytes = "
        "COALESCE(dedup_saved_bytes,0) + ? WHERE id=?",
        (int(extra_bytes), int(set_id)))


# ------------------------- 备份记录 -------------------------
def create_record(data: dict) -> int:
    fields = ["task_id", "db_type", "backup_type", "started_at", "finished_at",
              "duration_sec", "status", "size_bytes", "backup_path", "checksum",
              "is_simulated", "message"]
    data = {k: data.get(k) for k in fields}
    cols = list(data.keys())
    sql = "INSERT INTO backup_records ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_record(record_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM backup_records WHERE id=?", (record_id,))


def normalize_host_ip(raw) -> str:
    """规则 R1：从 tasks.host 提取纯 IP 或保留「本地」文案，遇空返回占位符。

    现有脏数据 4 种形态 root@192.168.220.150:22 / 192.168.220.150 / 127.0.0.1 / 本地。
    必须"先剥离再兜底"，对「本地」不得置空（旧正则会吞掉整段 @IP）。
    """
    if not raw:
        return "-"
    s = str(raw).strip()
    if "@" in s:
        s = s.split("@", 1)[1]
    if ":" in s:
        s = s.rsplit(":", 1)[0]
    return s or "-"


def _db_type_display(dt) -> str:
    return config.DB_DISPLAY_NAMES.get(dt, dt or "-")


def _backup_type_display(bt) -> str:
    return config.BACKUP_TYPE_DISPLAY_NAMES.get(bt, bt or "-")


def compute_biz_label(biz_system, name) -> str:
    """规则 R2：业务系统展示标签。biz_system 优先，空则回退任务名。

    全站唯一实现。前端不得再做 ``biz_system || name`` 的判空回退
    （见 docs/record-display-v2-design.md §8.2 确立的约定）。

    Args:
        biz_system: 任务的业务系统原始值，可为 None / 空串 / 纯空白。
        name: 任务名称，作为回退来源。

    Returns:
        永不为空的展示字符串；两者皆空时返回占位符 ``-``。
    """
    s = (biz_system or "").strip()
    if s:
        return s
    n = (name or "").strip()
    return n or "-"


def list_records(task_id: int = None, keyword: str = None, policy_id: int = None, limit: int = 200) -> list:
    sql = ("SELECT br.*, bt.name AS task_name, bt.host AS host_raw, "
           "bt.biz_system AS biz_system "
           "FROM backup_records br "
           "LEFT JOIN backup_tasks bt ON br.task_id = bt.id")
    params = []
    where = []
    if task_id:
        where.append("br.task_id=?")
        params.append(task_id)
    if policy_id:
        where.append("bt.policy_id=?")
        params.append(policy_id)
    if keyword:
        # 搜索三字段并集（设计 §4.1.3）：业务系统新值、旧任务名、主机均可命中。
        # NULL LIKE '%kw%' 在 SQLite 中为假值，不会误命中，无需 COALESCE。
        kw = f"%{keyword}%"
        where.append("(bt.name LIKE ? OR bt.host LIKE ? OR bt.biz_system LIKE ?)")
        params.extend([kw, kw, kw])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY br.id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    for r in rows:
        r["host_ip"] = normalize_host_ip(r.get("host_raw"))
        r["biz_label"] = compute_biz_label(r.get("biz_system"), r.get("task_name"))
        r["task_name"] = r.get("task_name") or "-"
        r["db_type_display"] = _db_type_display(r.get("db_type"))
        r["backup_type_display"] = _backup_type_display(r.get("backup_type"))
    return rows


# ------------------------- 恢复记录 -------------------------
def create_restore(data: dict) -> int:
    fields = ["task_id", "record_id", "target_host", "target_db", "started_at",
              "finished_at", "status", "message", "operator"]
    data = {k: data.get(k) for k in fields}
    cols = list(data.keys())
    sql = "INSERT INTO restore_records ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def list_restores(limit: int = 100, keyword: str = None) -> list:
    sql = (
        "SELECT rr.*, "
        "br.db_type AS db_type, br.backup_type AS backup_type, "
        "br.started_at AS backup_started_at, "
        "bt.name AS task_name, bt.host AS host_raw, "
        "bt.biz_system AS biz_system "
        "FROM restore_records rr "
        "LEFT JOIN backup_records br ON rr.record_id = br.id "
        "LEFT JOIN backup_tasks bt ON rr.task_id = bt.id"
    )
    params = []
    where = []
    if keyword:
        # 与 list_records 保持同一搜索契约：三字段并集（设计 §4.1.3）
        kw = f"%{keyword}%"
        where.append("(bt.name LIKE ? OR bt.host LIKE ? OR bt.biz_system LIKE ?)")
        params.extend([kw, kw, kw])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rr.id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    for r in rows:
        r["host_ip"] = normalize_host_ip(r.get("host_raw"))
        r["biz_label"] = compute_biz_label(r.get("biz_system"), r.get("task_name"))
        r["task_name"] = r.get("task_name") or "-"
        r["db_type_display"] = _db_type_display(r.get("db_type"))
        r["backup_type_display"] = _backup_type_display(r.get("backup_type"))
    return rows


# ------------------------- 数据同步任务 -------------------------
SYNC_FIELDS = [
    "name", "source_type", "source_task_id", "src_db_type", "src_host",
    "src_port", "src_username", "src_password", "src_db_name", "src_schema",
    "tgt_db_type", "tgt_host", "tgt_port", "tgt_username", "tgt_password",
    "tgt_db_name", "tgt_schema", "source_table", "target_table",
    "source_tables_list", "sync_mode", "save_mode", "column_mapping",
    "field_ide", "incremental_column", "incremental_value", "batch_size",
    "source_where", "error_threshold", "realtime_enabled", "flink_config",
    "full_db_migrate", "validate_before_run", "verify_after_run",
    "schedule_type", "cron_expr", "interval_minutes", "enabled", "status",
    "message",
]


def _decorate_sync(row: dict) -> dict:
    row = dict(row)
    row["src_db_display"] = config.DB_DISPLAY_NAMES.get(
        row.get("src_db_type"), row.get("src_db_type"))
    row["tgt_db_display"] = config.DB_DISPLAY_NAMES.get(
        row.get("tgt_db_type"), row.get("tgt_db_type"))
    row["has_src_password"] = bool(row.get("src_password"))
    row["has_tgt_password"] = bool(row.get("tgt_password"))
    # 统一 last_status：未执行时回退到 status="never"，确保前端一致显示
    row["last_status"] = row.get("last_status") or row.get("status") or "never"
    # 若源是托管数据库任务，附带任务名供前端友好展示（去掉 ID 编号）
    if row.get("source_type") == "managed" and row.get("source_task_id"):
        name = db.query_one(
            "SELECT name FROM backup_tasks WHERE id=?",
            (row["source_task_id"],))
        row["source_task_name"] = name["name"] if name else None
    # JSON 字段反序列化，方便前端/执行器直接使用
    for k in ("column_mapping", "source_tables_list", "flink_config"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            try:
                row[k] = json.loads(v)
            except Exception:
                row[k] = [] if k in ("column_mapping", "source_tables_list") else {}
        elif v is None:
            row[k] = [] if k in ("column_mapping", "source_tables_list") else {}
    return row


def create_sync_task(data: dict) -> int:
    data = {k: data.get(k) for k in SYNC_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = "never"
    data["last_status"] = "never"
    data["last_run_at"] = None
    if data.get("src_password"):
        data["src_password"] = db.encrypt_secret(data["src_password"])
    if data.get("tgt_password"):
        data["tgt_password"] = db.encrypt_secret(data["tgt_password"])
    data["enabled"] = 1 if data.get("enabled") not in (0, False, "0", None) else 0
    for num in ("src_port", "tgt_port", "interval_minutes", "source_task_id"):
        if data.get(num) in (None, ""):
            data[num] = None
    cols = list(data.keys())
    sql = "INSERT INTO sync_tasks ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_sync_task(sync_id: int, include_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM sync_tasks WHERE id=?", (sync_id,))
    if not row:
        return None
    row = _decorate_sync(row)
    if include_secret:
        row["src_password"] = db.decrypt_secret(row.get("src_password") or "")
        row["tgt_password"] = db.decrypt_secret(row.get("tgt_password") or "")
    else:
        row["src_password"] = ""
        row["tgt_password"] = ""
    return row


def list_sync_tasks(include_secret: bool = False, enabled: bool = None) -> list:
    sql = "SELECT * FROM sync_tasks"
    params = ()
    if enabled is not None:
        sql += " WHERE enabled=?"
        params = (1 if enabled else 0,)
    sql += " ORDER BY id DESC"
    rows = db.query(sql, params)
    out = []
    for r in rows:
        r = _decorate_sync(r)
        if include_secret:
            r["src_password"] = db.decrypt_secret(r.get("src_password") or "")
            r["tgt_password"] = db.decrypt_secret(r.get("tgt_password") or "")
        else:
            r["src_password"] = ""
            r["tgt_password"] = ""
        out.append(r)
    return out


def update_sync_task(sync_id: int, data: dict) -> bool:
    data = {k: v for k, v in data.items() if k in SYNC_FIELDS}
    if not data:
        return False
    data["updated_at"] = db.now_iso()
    sets, params = [], []
    for k, v in data.items():
        if k == "src_password":
            if v in (None, ""):
                continue
            v = db.encrypt_secret(v)
        elif k == "tgt_password":
            if v in (None, ""):
                continue
            v = db.encrypt_secret(v)
        elif k == "enabled":
            v = 1 if v not in (0, False, "0", None) else 0
        sets.append(f"{k}=?")
        params.append(v)
    params.append(sync_id)
    db.execute("UPDATE sync_tasks SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_sync_task(sync_id: int) -> bool:
    with db._write_lock:
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM sync_records WHERE sync_task_id=?", (sync_id,))
            conn.execute("DELETE FROM sync_tasks WHERE id=?", (sync_id,))
            conn.commit()
        finally:
            conn.close()
    return True


def set_sync_status(sync_id: int, last_run_at: str, last_status: str,
                    message: str = "") -> None:
    db.execute(
        "UPDATE sync_tasks SET last_run_at=?, last_status=?, message=? WHERE id=?",
        (last_run_at, last_status, message, sync_id))


# ------------------------- 同步记录 -------------------------
def create_sync_record(data: dict) -> int:
    fields = ["sync_task_id", "started_at", "finished_at", "status",
              "rows_synced", "message"]
    data = {k: data.get(k) for k in fields}
    cols = list(data.keys())
    sql = "INSERT INTO sync_records ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_sync_record(record_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM sync_records WHERE id=?", (record_id,))


def list_sync_records(sync_task_id: int = None, limit: int = 200) -> list:
    if sync_task_id:
        return db.query(
            "SELECT sr.*, st.name AS sync_name FROM sync_records sr "
            "LEFT JOIN sync_tasks st ON st.id = sr.sync_task_id "
            "WHERE sr.sync_task_id = ? "
            "ORDER BY sr.id DESC LIMIT ?", (sync_task_id, limit))
    return db.query(
        "SELECT sr.*, st.name AS sync_name FROM sync_records sr "
        "LEFT JOIN sync_tasks st ON st.id = sr.sync_task_id "
        "ORDER BY sr.id DESC LIMIT ?", (limit,))


# ------------------------- 巡检记录 -------------------------
def create_inspection(data: dict) -> int:
    fields = ["task_id", "task_name", "db_type", "started_at", "finished_at",
              "status", "detail", "triggered_by"]
    data = {k: data.get(k) for k in fields}
    cols = list(data.keys())
    sql = "INSERT INTO inspection_records ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def list_inspections(limit: int = 200) -> list:
    return db.query(
        "SELECT * FROM inspection_records ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------- 日志 -------------------------
def list_logs(limit: int = 200, level: str = "", source: str = "") -> list:
    """读取系统日志，可按 level / source 过滤；按 id DESC 倒序返回。"""
    sql = "SELECT * FROM system_logs WHERE 1=1"
    params: list = []
    if level:
        sql += " AND level = ?"
        params.append(level)
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return db.query(sql, tuple(params))


def list_log_sources() -> list:
    """返回 system_logs 中出现过的来源（去重），供前端筛选用。"""
    rows = db.query(
        "SELECT DISTINCT source FROM system_logs WHERE source IS NOT NULL "
        "AND source != '' ORDER BY source")
    return [r["source"] for r in rows]


def clear_logs() -> int:
    """清空系统日志，返回删除条数。"""
    import sqlite3
    conn = db.get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) AS c FROM system_logs").fetchone()["c"]
        conn.execute("DELETE FROM system_logs")
        conn.commit()
        after = conn.execute("SELECT COUNT(*) AS c FROM system_logs").fetchone()["c"]
        return before - after
    finally:
        conn.close()


# ------------------------- 数据库部署 -------------------------
def create_deployment(data: dict) -> int:
    from core import db as _db
    fields = ["name","db_type","host_id",
              "direct_host","direct_port","direct_user","direct_password",
              "package_path","dependency_path","base_dir","data_dir",
              "port","password","config_json","status"]
    row = {k: data.get(k) for k in fields}
    row["created_at"] = _db.now_iso()
    row["status"] = row.get("status", "pending")
    cols = list(row.keys())
    sql = f"INSERT INTO deployments ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
    return _db.execute(sql, tuple(row.values()))


def list_deployments() -> list:
    rows = db.query("SELECT * FROM deployments ORDER BY id DESC")
    # 关联 ssh_hosts 展示 IP/主机名，避免前端只显示 host_id (#N)
    hosts = {h["id"]: h for h in db.query("SELECT id, name, host_key, hostname FROM ssh_hosts")}
    out = []
    for r in rows:
        row = dict(r)
        host = hosts.get(row.get("host_id"))
        if host:
            row["host_display"] = host.get("hostname") or host.get("host_key") or ""
            row["host_name"] = host.get("name") or ""
        elif row.get("direct_host"):
            row["host_display"] = row["direct_host"]
            row["host_name"] = ""
        else:
            row["host_display"] = "-"
            row["host_name"] = ""
        out.append(row)
    return out


def get_deployment(dep_id: int) -> dict:
    row = db.query_one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    if not row:
        return None
    row = dict(row)
    host = db.query_one("SELECT name, host_key, hostname FROM ssh_hosts WHERE id=?", (row.get("host_id"),))
    if host:
        row["host_display"] = host.get("hostname") or host.get("host_key") or ""
        row["host_name"] = host.get("name") or ""
    elif row.get("direct_host"):
        row["host_display"] = row["direct_host"]
        row["host_name"] = ""
    else:
        row["host_display"] = "-"
        row["host_name"] = ""
    return row


def update_deployment(dep_id: int, data: dict) -> None:
    allow = {"name","db_type","host_id","direct_host","direct_port",
             "direct_user","direct_password",
             "status","progress_pct","log_output","started_at","finished_at",
             "config_json","package_path","dependency_path","base_dir","data_dir","port","password"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE deployments SET {sets} WHERE id=?",
               tuple(updates.values()) + (dep_id,))


def delete_deployment(dep_id: int) -> None:
    db.execute("DELETE FROM deployments WHERE id=?", (dep_id,))


# ------------------------- CDC / 校验字段更新（备份记录） -------------------------
def update_record_cdc(record_id: int, binlog_file: str = None, binlog_pos: int = None,
                       wal_lsn: str = None) -> None:
    sets, params = [], []
    if binlog_file is not None:
        sets.append("binlog_file=?"); params.append(binlog_file)
    if binlog_pos is not None:
        sets.append("binlog_pos=?"); params.append(binlog_pos)
    if wal_lsn is not None:
        sets.append("wal_lsn=?"); params.append(wal_lsn)
    if not sets:
        return
    params.append(record_id)
    db.execute(f"UPDATE backup_records SET {','.join(sets)} WHERE id=?", tuple(params))


def mark_record_verified(record_id: int, ok: bool, msg: str) -> None:
    db.execute("UPDATE backup_records SET verified=?, verify_msg=? WHERE id=?",
               (1 if ok else 0, msg, record_id))


# ------------------------- 虚拟数据库（VDB / 测试库） -------------------------
def create_vdb(data: dict) -> int:
    fields = ["name", "source_record_id", "task_id", "db_type", "port", "host",
              "database_name", "username", "password", "status",
              "created_at", "expires_at", "note"]
    row = {k: data.get(k) for k in fields}
    if not row.get("created_at"):
        row["created_at"] = db.now_iso()
    cols = list(row.keys())
    sql = f"INSERT INTO vdb_instances ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
    return db.execute(sql, tuple(row.values()))


def list_vdbs() -> list:
    return db.query("SELECT * FROM vdb_instances ORDER BY id DESC")


def get_vdb(vdb_id: int) -> dict:
    rows = db.query("SELECT * FROM vdb_instances WHERE id=?", (vdb_id,))
    return rows[0] if rows else None


def update_vdb(vdb_id: int, data: dict) -> None:
    allow = {"status", "expires_at", "last_used_at", "note", "port", "host",
             "database_name", "username", "password"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE vdb_instances SET {sets} WHERE id=?",
               tuple(updates.values()) + (vdb_id,))


def delete_vdb(vdb_id: int) -> None:
    db.execute("DELETE FROM vdb_instances WHERE id=?", (vdb_id,))


# ------------------------- 容灾演练 -------------------------
def create_drill(data: dict) -> int:
    fields = ["name", "task_id", "drill_type", "scenario", "scheduled_at",
              "status", "triggered_by", "notes", "created_at"]
    row = {k: data.get(k) for k in fields}
    if not row.get("created_at"):
        row["created_at"] = db.now_iso()
    row["status"] = row.get("status", "pending")
    cols = list(row.keys())
    sql = f"INSERT INTO drills ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
    return db.execute(sql, tuple(row.values()))


def list_drills() -> list:
    return db.query("SELECT * FROM drills ORDER BY id DESC")


def get_drill(drill_id: int) -> dict:
    rows = db.query("SELECT * FROM drills WHERE id=?", (drill_id,))
    return rows[0] if rows else None


def update_drill(drill_id: int, data: dict) -> None:
    allow = {"status", "started_at", "finished_at", "rto_actual_sec",
             "rpo_actual_sec", "score", "issues_found", "notes", "report"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE drills SET {sets} WHERE id=?",
               tuple(updates.values()) + (drill_id,))


def delete_drill(drill_id: int) -> None:
    db.execute("DELETE FROM drills WHERE id=?", (drill_id,))


# ------------------------- 迁移计划（MigrationPlan，Phase 2） -------------------------
# 三阶段（pre 黄金点 / mid 高频增量 / post 重心切换 + 旧库保留）全流程保护。
_MIGRATION_FIELDS = [
    "task_id", "stage", "golden_backup_record_id", "verified",
    "old_retention_days", "note", "status",
]


def create_migration_plan(data: dict) -> int:
    """创建一条迁移计划。返回新计划 id。"""
    data = {k: data.get(k) for k in _MIGRATION_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["stage"] = data.get("stage") or "pre"
    data["status"] = data.get("status") or "created"
    data["verified"] = 1 if data.get("verified") else 0
    if data.get("golden_backup_record_id") in (0, "", None):
        data["golden_backup_record_id"] = None
    if data.get("old_retention_days") in (0, "", None):
        data["old_retention_days"] = None
    if data.get("task_id") in (0, "", None):
        data["task_id"] = None
    cols = list(data.keys())
    sql = "INSERT INTO migration_plans ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_migration_plan(plan_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM migration_plans WHERE id=?", (plan_id,))


def list_migration_plans() -> list:
    rows = db.query(
        "SELECT mp.*, t.name AS task_name, t.db_type AS task_db_type "
        "FROM migration_plans mp LEFT JOIN backup_tasks t ON t.id = mp.task_id "
        "ORDER BY mp.id DESC")
    return rows


def update_migration_plan(plan_id: int, data: dict) -> None:
    """更新迁移计划字段（白名单）。"""
    allow = {"stage", "golden_backup_record_id", "verified",
             "old_retention_days", "note", "status"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    updates["updated_at"] = db.now_iso()
    sets, params = [], []
    for k, v in updates.items():
        if k == "verified":
            v = 1 if v else 0
        elif k in ("golden_backup_record_id", "old_retention_days"):
            v = None if v in (0, "", None) else v
        sets.append(f"{k}=?")
        params.append(v)
    params.append(plan_id)
    db.execute("UPDATE migration_plans SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))


def delete_migration_plan(plan_id: int) -> None:
    db.execute("DELETE FROM migration_plans WHERE id=?", (plan_id,))


# ------------------------- 克隆请求（CloneRequest，Phase 2） -------------------------
_CLONE_FIELDS = [
    "source_record_id", "target_env", "status", "itsm_ticket_id",
    "requested_by", "approved_by", "expires_at", "vdb_instance_id", "note",
]


def create_clone_request(data: dict) -> int:
    """创建一条克隆请求。返回新请求 id。"""
    data = {k: data.get(k) for k in _CLONE_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = data.get("status") or "pending"
    for n in ("source_record_id", "itsm_ticket_id", "vdb_instance_id"):
        if data.get(n) in (0, "", None):
            data[n] = None
    cols = list(data.keys())
    sql = "INSERT INTO clone_requests ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_clone_request(request_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM clone_requests WHERE id=?", (request_id,))


def list_clone_requests() -> list:
    rows = db.query(
        "SELECT cr.*, br.db_type AS source_db_type, br.task_id AS source_task_id, "
        "t.name AS task_name, "
        "v.host AS vdb_host, v.port AS vdb_port, v.database_name AS vdb_dbname, "
        "v.status AS vdb_status, v.username AS vdb_username "
        "FROM clone_requests cr "
        "LEFT JOIN backup_records br ON br.id = cr.source_record_id "
        "LEFT JOIN backup_tasks t ON t.id = br.task_id "
        "LEFT JOIN vdb_instances v ON v.id = cr.vdb_instance_id "
        "ORDER BY cr.id DESC")
    return rows


def update_clone_request(request_id: int, data: dict) -> None:
    """更新克隆请求字段（白名单）。"""
    allow = {"status", "itsm_ticket_id", "approved_by", "expires_at",
             "vdb_instance_id", "note"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    updates["updated_at"] = db.now_iso()
    sets, params = [], []
    for k, v in updates.items():
        if k in ("itsm_ticket_id", "vdb_instance_id"):
            v = None if v in (0, "", None) else v
        sets.append(f"{k}=?")
        params.append(v)
    params.append(request_id)
    db.execute("UPDATE clone_requests SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))


def delete_clone_request(request_id: int) -> None:
    db.execute("DELETE FROM clone_requests WHERE id=?", (request_id,))


# ------------------------- 异构转换任务（HeteroJob，Phase 2） -------------------------
_HETERO_FIELDS = ["src_db_type", "dst_db_type", "src_record_id", "status",
                  "result_path", "note"]


def create_hetero_job(data: dict) -> int:
    data = {k: data.get(k) for k in _HETERO_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = data.get("status") or "pending"
    if data.get("src_record_id") in (0, "", None):
        data["src_record_id"] = None
    cols = list(data.keys())
    sql = "INSERT INTO hetero_jobs ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_hetero_job(job_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM hetero_jobs WHERE id=?", (job_id,))


def list_hetero_jobs() -> list:
    return db.query("SELECT * FROM hetero_jobs ORDER BY id DESC")


def update_hetero_job(job_id: int, data: dict) -> None:
    allow = {"status", "result_path", "note"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    updates["updated_at"] = db.now_iso()
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE hetero_jobs SET {sets} WHERE id=?",
               tuple(updates.values()) + (job_id,))


# ------------------------- ITSM 工单（Phase 2） -------------------------
_ITSM_FIELDS = ["system", "ticket_no", "ref_type", "ref_id", "status", "payload"]


def create_itsm_ticket(data: dict) -> int:
    data = {k: data.get(k) for k in _ITSM_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["system"] = data.get("system") or "internal"
    data["status"] = data.get("status") or "open"
    if data.get("ref_id") in (0, "", None):
        data["ref_id"] = None
    cols = list(data.keys())
    sql = "INSERT INTO itsm_tickets ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_itsm_ticket(ticket_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM itsm_tickets WHERE id=?", (ticket_id,))


def list_itsm_tickets(ref_type: str = None, ref_id: int = None) -> list:
    sql = "SELECT * FROM itsm_tickets"
    where, params = [], []
    if ref_type:
        where.append("ref_type=?")
        params.append(ref_type)
    if ref_id is not None:
        where.append("ref_id=?")
        params.append(ref_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return db.query(sql, tuple(params))


def update_itsm_ticket(ticket_id: int, data: dict) -> None:
    allow = {"ticket_no", "status", "payload"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    updates["updated_at"] = db.now_iso()
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE itsm_tickets SET {sets} WHERE id=?",
               tuple(updates.values()) + (ticket_id,))


# ------------------------- 容灾链路（DisasterLink，Phase 3） -------------------------
# 双运营商专线智能选路 / 日志间隙填补 / 备端只读一致性校验的元数据层。
_DISASTER_LINK_FIELDS = [
    "name", "primary_site", "dr_site", "status", "route_policy",
    "consistency_result", "note", "enabled",
    # UX-20260801 模块 D：数据源引用（sync_task | rt_task | manual）
    "source_kind", "source_id",
]

# 容灾链路允许的数据源类型
DISASTER_LINK_SOURCE_KINDS = ("sync_task", "rt_task", "manual")


def _default_route_policy() -> list:
    """默认路由策略：空列表（由用户按真实专线配置，不再内置演示路由）。"""
    return []


def _dl_json(data: dict, key: str):
    """将 dict/list 形式的路由策略字段序列化为 JSON 字符串。"""
    v = data.get(key)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v if v is not None else None


def _dl_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将容灾链路行转为 dict，并反序列化 JSON 字段。

    兼容存量：`source_kind` 为空/NULL 时归一化为 'manual'；
    `source_id` 统一为 int 或 None，避免前端拿到 '' 与 0 混用。
    """
    if not row:
        return None
    d = dict(row)
    if d.get("route_policy"):
        try:
            d["route_policy"] = json.loads(d["route_policy"])
        except (json.JSONDecodeError, TypeError):
            pass
    d["enabled"] = bool(d.get("enabled"))
    d["source_kind"] = d.get("source_kind") or "manual"
    try:
        d["source_id"] = int(d["source_id"]) if d.get("source_id") else None
    except (TypeError, ValueError):
        d["source_id"] = None
    return d


def create_disaster_link(data: dict) -> int:
    """创建一条容灾链路。返回新链路 id。"""
    data = {k: data.get(k) for k in _DISASTER_LINK_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = data.get("status") or "standby"
    data["enabled"] = 1 if data.get("enabled") not in (0, False, "0", None) else 0
    # 数据源引用归一化：缺省视为手工模式，manual 模式强制清空 source_id
    kind = str(data.get("source_kind") or "manual")
    if kind not in DISASTER_LINK_SOURCE_KINDS:
        kind = "manual"
    data["source_kind"] = kind
    if kind == "manual":
        data["source_id"] = None
    else:
        try:
            data["source_id"] = int(data.get("source_id")) if data.get("source_id") else None
        except (TypeError, ValueError):
            data["source_id"] = None
    if data.get("route_policy") in (None, ""):
        data["route_policy"] = json.dumps(_default_route_policy(), ensure_ascii=False)
    else:
        data["route_policy"] = _dl_json(data, "route_policy")
    cols = list(data.keys())
    sql = "INSERT INTO disaster_links ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_disaster_link(link_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
    return _dl_to_dict(row) if row else None


def list_disaster_links() -> list:
    rows = db.query("SELECT * FROM disaster_links ORDER BY id DESC")
    return [_dl_to_dict(r) for r in rows]


def update_disaster_link(link_id: int, data: dict) -> bool:
    """更新容灾链路字段（白名单）。"""
    allow = set(_DISASTER_LINK_FIELDS)
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return False
    updates["updated_at"] = db.now_iso()
    if "enabled" in updates:
        updates["enabled"] = 1 if updates.get("enabled") not in (0, False, "0", None) else 0
    if "route_policy" in updates:
        updates["route_policy"] = _dl_json(updates, "route_policy")
    if "source_kind" in updates:
        kind = str(updates.get("source_kind") or "manual")
        updates["source_kind"] = kind if kind in DISASTER_LINK_SOURCE_KINDS else "manual"
        if updates["source_kind"] == "manual":
            updates["source_id"] = None
    if "source_id" in updates and updates.get("source_id") is not None:
        try:
            updates["source_id"] = int(updates["source_id"]) if updates["source_id"] != "" else None
        except (TypeError, ValueError):
            updates["source_id"] = None
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(link_id)
    db.execute("UPDATE disaster_links SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_disaster_link(link_id: int) -> None:
    db.execute("DELETE FROM disaster_links WHERE id=?", (link_id,))


def set_disaster_link_status(link_id: int, status: str) -> None:
    """更新链路状态（选路/填补/校验过程联动）。"""
    db.execute("UPDATE disaster_links SET status=?, updated_at=? WHERE id=?",
               (status, db.now_iso(), link_id))


def update_disaster_link_check(link_id: int, consistency_result: str,
                               last_consistency_check: str = None) -> None:
    """记录一致性校验结果到链路元数据。"""
    now = db.now_iso()
    last = last_consistency_check or now
    db.execute(
        "UPDATE disaster_links SET consistency_result=?, last_consistency_check=?, "
        "updated_at=? WHERE id=?",
        (consistency_result, last, now, link_id))


# ------------------------- AI 预测告警（AlertPrediction，Phase 3） -------------------------
# 规则 + 轻量统计引擎产出的风险预测记录，供前端趋势展示与 critical 自动告警。
_ALERT_PREDICTION_FIELDS = [
    "metric", "risk_score", "risk_level", "predicted_at",
    "actual_at", "resolved_at", "details",
    "predicted_content", "basis",
]


def _ap_to_dict(row: Optional[dict]) -> Optional[dict]:
    """行 → dict：details 解析为 dict，basis 解析为 list[str]（供前端直接渲染）。"""
    if not row:
        return None
    d = dict(row)
    if d.get("details"):
        try:
            d["details"] = json.loads(d["details"])
        except (json.JSONDecodeError, TypeError):
            pass
    raw_basis = d.get("basis")
    if isinstance(raw_basis, str) and raw_basis:
        try:
            parsed = json.loads(raw_basis)
            d["basis"] = parsed if isinstance(parsed, list) else [str(parsed)]
        except (json.JSONDecodeError, TypeError):
            d["basis"] = [raw_basis]
    elif not isinstance(raw_basis, list):
        d["basis"] = []
    d["predicted_content"] = d.get("predicted_content") or ""
    return d


def create_alert_prediction(data: dict) -> int:
    """创建一条风险预测记录。返回新记录 id。"""
    data = {k: data.get(k) for k in _ALERT_PREDICTION_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["predicted_at"] = data.get("predicted_at") or now
    data["risk_score"] = float(data.get("risk_score") or 0)
    data["risk_level"] = data.get("risk_level") or "low"
    if isinstance(data.get("details"), (dict, list)):
        data["details"] = json.dumps(data["details"], ensure_ascii=False)
    elif data.get("details") is None:
        data["details"] = None
    # 人类可读预测内容 / 依据（basis 统一以 JSON 数组落库）
    data["predicted_content"] = data.get("predicted_content") or ""
    basis = data.get("basis")
    if basis is None:
        basis = []
    if isinstance(basis, str):
        basis = [basis] if basis else []
    if isinstance(basis, (list, tuple)):
        basis = [str(b) for b in basis]
    else:
        basis = [str(basis)]
    data["basis"] = json.dumps(basis, ensure_ascii=False)
    cols = list(data.keys())
    sql = "INSERT INTO alert_predictions ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def get_alert_prediction(pred_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM alert_predictions WHERE id=?", (pred_id,))
    return _ap_to_dict(row) if row else None


def list_alert_predictions(metric: str = None, limit: int = 200) -> list:
    """列出风险预测。可按 metric 过滤。"""
    sql = "SELECT * FROM alert_predictions"
    where, params = [], []
    if metric:
        where.append("metric=?")
        params.append(metric)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def update_alert_prediction(pred_id: int, data: dict) -> None:
    """更新预测记录（白名单，如实际发生/已处置时间）。"""
    allow = {"actual_at", "resolved_at", "risk_level", "risk_score"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets, params = [], []
    for k, v in updates.items():
        if k in ("risk_score",):
            v = float(v or 0)
        sets.append(f"{k}=?")
        params.append(v)
    params.append(pred_id)
    db.execute("UPDATE alert_predictions SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))


def delete_alert_prediction(pred_id: int) -> None:
    db.execute("DELETE FROM alert_predictions WHERE id=?", (pred_id,))


# ------------------------- 脱敏导出（Data Mining，Phase 4） -------------------------
def create_anonymized_export(data: dict) -> int:
    """创建一条脱敏导出元数据记录。返回新记录 id。"""
    now = db.now_iso()
    cols = data.get("columns")
    rules = data.get("mask_rules")
    row = {
        "source_record_id": int(data.get("source_record_id") or 0),
        "columns": json.dumps(cols, ensure_ascii=False) if isinstance(cols, (list, dict)) else (cols or None),
        "mask_rules": json.dumps(rules, ensure_ascii=False) if isinstance(rules, (list, dict)) else (rules or None),
        "file_path": data.get("file_path"),
        "row_count": int(data.get("row_count") or 0),
        "note": data.get("note") or "",
        "created_at": now,
    }
    cols_sql = list(row.keys())
    sql = "INSERT INTO anonymized_exports ({}) VALUES ({})".format(
        ",".join(cols_sql), ",".join("?" * len(cols_sql)))
    return db.execute(sql, tuple(row.values()))


def _anon_export_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for key in ("columns", "mask_rules"):
        raw = d.get(key)
        if isinstance(raw, str) and raw:
            try:
                d[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def get_anonymized_export(export_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM anonymized_exports WHERE id=?", (export_id,))
    return _anon_export_to_dict(row) if row else None


def list_anonymized_exports(limit: int = 200) -> list:
    """列出脱敏导出历史（按时间倒序）。"""
    rows = db.query("SELECT * FROM anonymized_exports ORDER BY id DESC LIMIT ?",
                    (limit,))
    return [_anon_export_to_dict(r) for r in rows]


def update_anonymized_export(export_id: int, data: dict) -> None:
    """更新脱敏导出记录（白名单，如备注）。"""
    allow = {"note", "row_count"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE anonymized_exports SET {sets} WHERE id=?",
               tuple(updates.values()) + (export_id,))


def delete_anonymized_export(export_id: int) -> None:
    db.execute("DELETE FROM anonymized_exports WHERE id=?", (export_id,))


# ------------------------- 准 CDP：PIT 恢复点日志 -------------------------
_RJ_FIELDS = [
    "task_id", "record_id", "set_id", "parent_rp_id", "rp_kind", "rp_type",
    "pit_at", "pit_seq", "consistency", "binlog_file", "binlog_pos",
    "binlog_end_file", "binlog_end_pos", "wal_lsn", "wal_end_lsn",
    "file_set_key", "changed_files", "deleted_files", "storage_tier",
    "object_key", "bundle_key", "size_bytes", "checksum", "verified",
    "verify_msg", "is_simulated", "message", "expires_at",
]

# 允许更新的列白名单（防止上游误写主键 / task_id）
_RJ_UPDATABLE = {
    "record_id", "set_id", "parent_rp_id", "rp_kind", "rp_type", "consistency",
    "binlog_file", "binlog_pos", "binlog_end_file", "binlog_end_pos",
    "wal_lsn", "wal_end_lsn", "file_set_key", "changed_files", "deleted_files",
    "storage_tier", "bundle_key", "size_bytes", "checksum", "verified",
    "verify_msg", "is_simulated", "message", "expires_at",
}


def create_recovery_point(data: dict) -> int:
    """写入一个 PIT 恢复点。返回新行 id。

    object_key 冲突（唯一索引 idx_rj_obj）时走幂等更新并返回已存在的 id，
    调用方无需处理 IntegrityError。
    """
    row = {k: data.get(k) for k in _RJ_FIELDS}
    row["created_at"] = db.now_iso()
    row["rp_kind"] = row.get("rp_kind") or "file-inc"
    row["rp_type"] = row.get("rp_type") or "incremental"
    row["pit_at"] = row.get("pit_at") or row["created_at"]
    row["pit_seq"] = int(row.get("pit_seq") or 0)
    row["consistency"] = row.get("consistency") or "crash"
    row["storage_tier"] = int(row.get("storage_tier") or 1)
    row["size_bytes"] = int(row.get("size_bytes") or 0)
    row["changed_files"] = int(row.get("changed_files") or 0)
    row["deleted_files"] = int(row.get("deleted_files") or 0)
    row["verified"] = 1 if row.get("verified") else 0
    row["is_simulated"] = 1 if row.get("is_simulated") else 0
    row["object_key"] = row.get("object_key") or ""
    if row.get("parent_rp_id") in (0, "", None):
        row["parent_rp_id"] = None

    existing = db.query_one(
        "SELECT id FROM recovery_journal WHERE task_id=? AND object_key=?",
        (row["task_id"], row["object_key"]))
    if existing:
        rp_id = int(existing["id"])
        update_recovery_point(rp_id, row)
        return rp_id

    cols = list(row.keys())
    sql = "INSERT INTO recovery_journal ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_recovery_point(rp_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM recovery_journal WHERE id=?", (rp_id,))
    return dict(row) if row else None


def list_recovery_points(task_id: int = None, start: str = None, end: str = None,
                         kind: str = None, limit: int = 500, offset: int = 0,
                         order: str = "asc") -> list:
    """按时间窗口列出恢复点。order='asc' 用于恢复链，'desc' 用于 UI 明细。"""
    sql = "SELECT * FROM recovery_journal"
    where, params = [], []
    if task_id is not None:
        where.append("task_id=?")
        params.append(task_id)
    if start:
        where.append("pit_at>=?")
        params.append(start)
    if end:
        where.append("pit_at<=?")
        params.append(end)
    if kind:
        where.append("rp_kind=?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    sql += f" ORDER BY pit_at {direction}, pit_seq {direction}, id {direction}"
    sql += " LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return [dict(r) for r in db.query(sql, tuple(params))]


def count_recovery_points(task_id: int = None, kind: str = None) -> int:
    sql = "SELECT COUNT(1) AS c FROM recovery_journal"
    where, params = [], []
    if task_id is not None:
        where.append("task_id=?")
        params.append(task_id)
    if kind:
        where.append("rp_kind=?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = db.query_one(sql, tuple(params))
    return int(row["c"]) if row else 0


def next_pit_seq(task_id: int, pit_at: str) -> int:
    """同 pit_at 秒内的下一个序号（0 起）。"""
    row = db.query_one(
        "SELECT MAX(pit_seq) AS m FROM recovery_journal WHERE task_id=? AND pit_at=?",
        (task_id, pit_at))
    if not row or row.get("m") is None:
        return 0
    return int(row["m"]) + 1


def update_recovery_point(rp_id: int, data: dict) -> None:
    """按白名单更新恢复点字段。"""
    updates = {k: v for k, v in data.items() if k in _RJ_UPDATABLE}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE recovery_journal SET {sets} WHERE id=?",
               tuple(updates.values()) + (rp_id,))


def delete_recovery_points(rp_ids: list) -> int:
    """批量删除恢复点（仅删 DB 行，磁盘文件由 RecoveryJournal.prune 负责）。"""
    ids = [int(i) for i in (rp_ids or [])]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM recovery_journal WHERE id IN ({placeholders})",
               tuple(ids))
    return len(ids)


# ------------------------- 准 CDP：实时捕获运行态 -------------------------
_RT_STATE_FIELDS = [
    "capture_kind", "engine", "daemon_status", "degrade_reason", "pid",
    "watcher_impl", "last_heartbeat_at", "last_capture_at", "last_rp_at",
    "last_binlog_file", "last_binlog_pos", "last_wal_lsn", "source_pos_at",
    "lag_sec", "rpo_actual_sec", "health", "consecutive_fail", "restart_count",
    "bytes_today", "rp_count_today", "last_error",
]


def upsert_rt_state(task_id: int, data: dict) -> None:
    """高频更新实时捕获运行态。使用 UPSERT 单行更新，避免读-改-写竞态。

    只有 data 中显式出现的键会被更新，其余列保持原值。
    """
    payload = {k: v for k, v in (data or {}).items() if k in _RT_STATE_FIELDS}
    payload["updated_at"] = db.now_iso()
    cols = ["task_id"] + list(payload.keys())
    values = [int(task_id)] + list(payload.values())
    placeholders = ",".join("?" * len(cols))
    sets = ", ".join(f"{k}=excluded.{k}" for k in payload.keys())
    sql = (f"INSERT INTO rt_capture_state ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(task_id) DO UPDATE SET {sets}")
    db.execute(sql, tuple(values))


def get_rt_state(task_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM rt_capture_state WHERE task_id=?", (int(task_id),))
    return dict(row) if row else None


def list_rt_states(task_ids: list = None) -> list:
    """列出全部（或指定任务集）的实时捕获运行态。"""
    if task_ids:
        ids = [int(i) for i in task_ids]
        placeholders = ",".join("?" * len(ids))
        rows = db.query(
            f"SELECT * FROM rt_capture_state WHERE task_id IN ({placeholders})",
            tuple(ids))
    else:
        rows = db.query("SELECT * FROM rt_capture_state", ())
    return [dict(r) for r in rows]


def delete_rt_state(task_id: int) -> None:
    db.execute("DELETE FROM rt_capture_state WHERE task_id=?", (int(task_id),))


def list_rt_tasks(only_enabled: bool = True) -> list:
    """列出开启了实时保护的备份任务（返回含明文密码，供守护进程使用）。"""
    sql = "SELECT * FROM backup_tasks WHERE COALESCE(rt_enabled,0)=1"
    if only_enabled:
        sql += " AND COALESCE(enabled,1)=1"
    sql += " ORDER BY id ASC"
    rows = db.query(sql, ())
    return [_decorate(r, include_secret=True) for r in rows]


# 实时配置可写列（API PUT /rt_backup/tasks/<id>/config 的白名单）
_RT_CONFIG_FIELDS = {
    "rt_enabled": int,
    "rt_mode": str,
    "rt_interval_sec": int,
    "rt_consistency": str,
    "rt_log_retention_days": int,
    "rt_rpo_target_sec": int,
}


def update_rt_config(task_id: int, data: dict) -> dict:
    """更新某任务的实时保护配置（白名单 + 强类型），返回更新后的任务行。

    仅 data 中出现的键被写入；rt_rpo_target_sec 允许显式置 NULL（传 None/空串）。
    """
    updates, params = [], []
    for key, caster in _RT_CONFIG_FIELDS.items():
        if key not in (data or {}):
            continue
        raw = data.get(key)
        if key == "rt_rpo_target_sec" and raw in (None, "", "null"):
            value = None
        elif caster is int:
            if key == "rt_enabled":
                value = 1 if raw in (1, "1", True, "true", "True", "on") else 0
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    continue
        else:
            value = str(raw or "")
            if not value:
                continue
        updates.append(f"{key}=?")
        params.append(value)
    if updates:
        updates.append("updated_at=?")
        params.append(db.now_iso())
        params.append(int(task_id))
        db.execute("UPDATE backup_tasks SET {} WHERE id=?".format(",".join(updates)),
                   tuple(params))
    return get_task(task_id, include_secret=False) or {}


# ------------------------- 实时备份任务扩展（rt_tasks） -------------------------
_RT_TASK_FIELDS = [
    "task_id", "rt_mode", "capture_interval", "db_log_retention_days",
    "file_inc_retention_days", "db_flush_interval", "is_running",
    "last_tick_at", "health_status", "rpo_current_seconds", "disk_quota_gb",
]


def _rt_task_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将 rt_tasks 行转为 dict，布尔字段还原为 bool。"""
    if not row:
        return None
    d = dict(row)
    d["is_running"] = bool(d.get("is_running"))
    d["rpo_current_seconds"] = int(d.get("rpo_current_seconds") or -1)
    d["disk_quota_gb"] = int(d.get("disk_quota_gb") or 200)
    d["capture_interval"] = int(d.get("capture_interval") or 180)
    d["db_log_retention_days"] = int(d.get("db_log_retention_days") or 7)
    d["file_inc_retention_days"] = int(d.get("file_inc_retention_days") or 30)
    d["db_flush_interval"] = int(d.get("db_flush_interval") or 300)
    return d


def create_rt_task(data: dict) -> int:
    """为某备份任务创建实时扩展行。返回新行 id。"""
    row = {k: data.get(k) for k in _RT_TASK_FIELDS}
    now = db.now_iso()
    row["created_at"] = now
    row["updated_at"] = now
    row["rt_mode"] = row.get("rt_mode") or "file_polling"
    row["task_id"] = int(row.get("task_id") or 0)
    row["is_running"] = 1 if row.get("is_running") not in (0, False, "0", None) else 0
    row["health_status"] = row.get("health_status") or "unknown"
    row["rpo_current_seconds"] = int(row.get("rpo_current_seconds") or -1)
    for num in ("capture_interval", "db_log_retention_days",
                "file_inc_retention_days", "db_flush_interval", "disk_quota_gb"):
        if row.get(num) in (None, ""):
            row[num] = None
    cols = list(row.keys())
    sql = "INSERT INTO rt_tasks ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_rt_task(task_id: int) -> Optional[dict]:
    """按 backup_tasks.id 查关联的 rt_tasks 扩展行。"""
    row = db.query_one("SELECT * FROM rt_tasks WHERE task_id=?", (int(task_id),))
    return _rt_task_to_dict(row) if row else None


def update_rt_task(task_id: int, data: dict) -> bool:
    """更新 rt_tasks 扩展行（白名单）。task_id 是 backup_tasks.id。"""
    data = {k: v for k, v in data.items() if k in _RT_TASK_FIELDS}
    if not data:
        return False
    data["updated_at"] = db.now_iso()
    sets, params = [], []
    for k, v in data.items():
        if k == "is_running":
            v = 1 if v not in (0, False, "0", None) else 0
        elif k in ("capture_interval", "db_log_retention_days",
                   "file_inc_retention_days", "db_flush_interval",
                   "rpo_current_seconds", "disk_quota_gb"):
            v = int(v) if v not in (None, "") else None
        elif k == "task_id":
            continue  # 不允许改关联主键
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return False
    params.append(int(task_id))
    db.execute("UPDATE rt_tasks SET {} WHERE task_id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_rt_task(task_id: int) -> None:
    """删除 rt_tasks 扩展行。task_id 是 backup_tasks.id。"""
    db.execute("DELETE FROM rt_tasks WHERE task_id=?", (int(task_id),))


# ------------------------- 日志仓库目录管理（log_repository） -------------------------
_LOG_REPO_FIELDS = [
    "task_id", "repo_root", "db_log_dir", "file_inc_dir",
    "current_size_bytes", "quota_bytes",
]


def _log_repo_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将 log_repository 行转为 dict，数值字段还原类型。"""
    if not row:
        return None
    d = dict(row)
    d["current_size_bytes"] = int(d.get("current_size_bytes") or 0)
    d["quota_bytes"] = int(d.get("quota_bytes") or 214748364800)
    return d


def create_log_repo(data: dict) -> int:
    """为某任务创建日志仓库记录。返回新行 id。"""
    row = {k: data.get(k) for k in _LOG_REPO_FIELDS}
    row["created_at"] = db.now_iso()
    row["task_id"] = int(row.get("task_id") or 0)
    row["repo_root"] = row.get("repo_root") or ""
    row["current_size_bytes"] = int(row.get("current_size_bytes") or 0)
    row["quota_bytes"] = int(row.get("quota_bytes") or 214748364800)
    cols = list(row.keys())
    sql = "INSERT INTO log_repository ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_log_repo(task_id: int) -> Optional[dict]:
    """按 task_id 查日志仓库记录。"""
    row = db.query_one("SELECT * FROM log_repository WHERE task_id=?", (int(task_id),))
    return _log_repo_to_dict(row) if row else None


def update_log_repo(task_id: int, data: dict) -> bool:
    """更新日志仓库记录（白名单）。"""
    allow = {"repo_root", "db_log_dir", "file_inc_dir",
             "current_size_bytes", "quota_bytes"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return False
    for num in ("current_size_bytes", "quota_bytes"):
        if num in updates:
            updates[num] = int(updates[num] or 0)
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(int(task_id))
    db.execute("UPDATE log_repository SET {} WHERE task_id=?".format(",".join(sets)),
               tuple(params))
    return True


# ========================== AI 智能助手会话与消息 ==========================

_AI_SESSION_FIELDS = ["id", "title", "created_at", "updated_at", "message_count"]
_AI_MESSAGE_FIELDS = ["session_id", "role", "content", "tool_calls", "tool_name", "tool_result", "created_at"]


# ---- 会话 CRUD ----

import uuid as _uuid_mod  # noqa: E402


def create_ai_session(data: dict) -> str:
    """创建 AI 会话。返回 session_id。"""
    row = {k: data.get(k) for k in _AI_SESSION_FIELDS}
    row["id"] = row.get("id") or str(_uuid_mod.uuid4())
    row["title"] = row.get("title") or "新对话"
    row["message_count"] = int(row.get("message_count") or 0)
    cols = list(row.keys())
    sql = "INSERT INTO ai_sessions ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    db.execute(sql, tuple(row.values()))
    return row["id"]


def get_ai_session(session_id: str) -> Optional[dict]:
    """获取 AI 会话。"""
    row = db.query_one("SELECT * FROM ai_sessions WHERE id=?", (session_id,))
    return dict(row) if row else None


def list_ai_sessions() -> list:
    """列出所有 AI 会话（按更新时间倒序）。"""
    rows = db.query("SELECT * FROM ai_sessions ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


def update_ai_session(session_id: str, **kwargs) -> bool:
    """更新 AI 会话属性（白名单）。"""
    allow = {"title", "updated_at", "message_count"}
    updates = {k: v for k, v in kwargs.items() if k in allow}
    if not updates:
        return False
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(session_id)
    db.execute("UPDATE ai_sessions SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_ai_session(session_id: str) -> bool:
    """删除 AI 会话。"""
    db.execute("DELETE FROM ai_sessions WHERE id=?", (session_id,))
    return True


# ---- 消息 CRUD ----

def add_ai_message(data: dict) -> int:
    """添加 AI 消息。返回消息 id。"""
    row = {k: data.get(k) for k in _AI_MESSAGE_FIELDS}
    row["session_id"] = row.get("session_id") or ""
    row["role"] = row.get("role") or "user"
    row["content"] = row.get("content") or ""
    row["created_at"] = row.get("created_at") or db.now_iso()
    # tool_calls / tool_name / tool_result 可为 None
    cols = list(row.keys())
    sql = "INSERT INTO ai_messages ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def list_ai_messages(session_id: str, limit: int = 50) -> list:
    """列出 AI 消息（按时间升序，最旧的在前）。"""
    rows = db.query(
        "SELECT * FROM ai_messages WHERE session_id=? ORDER BY created_at ASC LIMIT ?",
        (session_id, limit))
    return [dict(r) for r in rows]


def delete_ai_messages(session_id: str) -> bool:
    """删除某会话的所有消息。"""
    db.execute("DELETE FROM ai_messages WHERE session_id=?", (session_id,))
    return True


# ========================== 恢复校验策略 & 恢复测试报告 ==========================
_RESTORE_VERIFY_POLICY_FIELDS = [
    "task_id", "name", "recovery_pool", "schedule_type", "cron_expr",
    "interval_minutes", "clone_retention_min", "enabled",
]


def _rvp_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将 restore_verify_policies 行转为 dict，数值/布尔字段还原。"""
    if not row:
        return None
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["clone_retention_min"] = int(d.get("clone_retention_min") or 0)
    d["interval_minutes"] = int(d.get("interval_minutes") or 0) if d.get("interval_minutes") is not None else None
    d["task_id"] = int(d.get("task_id") or 0)
    d["last_report_id"] = int(d.get("last_report_id") or 0) if d.get("last_report_id") is not None else None
    return d


def create_restore_verify_policy(data: dict) -> int:
    """创建恢复校验策略。返回新策略 id。"""
    row = {k: data.get(k) for k in _RESTORE_VERIFY_POLICY_FIELDS}
    now = db.now_iso()
    row["created_at"] = now
    row["updated_at"] = now
    row["enabled"] = 1 if row.get("enabled") not in (0, False, "0", None) else 0
    row["clone_retention_min"] = int(row.get("clone_retention_min") or 30)
    if row.get("interval_minutes") in ("", None):
        row["interval_minutes"] = None
    else:
        row["interval_minutes"] = int(row["interval_minutes"])
    if not row.get("name"):
        row["name"] = f"校验策略-{now[:10]}"
    cols = list(row.keys())
    sql = "INSERT INTO restore_verify_policies ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_restore_verify_policy(policy_id: int) -> Optional[dict]:
    """按 id 获取恢复校验策略，并关联任务名与 db_type。"""
    sql = ("SELECT rvp.*, t.name AS task_name, t.db_type AS db_type, "
           "t.name AS instance_name "
           "FROM restore_verify_policies rvp "
           "LEFT JOIN backup_tasks t ON t.id = rvp.task_id "
           "WHERE rvp.id=?")
    row = db.query_one(sql, (policy_id,))
    return _rvp_to_dict(row) if row else None


def list_restore_verify_policies(enabled_only: bool = False, task_id: int = None) -> list:
    """列出恢复校验策略。"""
    sql = ("SELECT rvp.*, t.name AS task_name, t.db_type AS db_type, "
           "t.name AS instance_name "
           "FROM restore_verify_policies rvp "
           "LEFT JOIN backup_tasks t ON t.id = rvp.task_id WHERE 1=1")
    params: list = []
    if enabled_only:
        sql += " AND rvp.enabled=1"
    if task_id is not None:
        sql += " AND rvp.task_id=?"
        params.append(task_id)
    sql += " ORDER BY rvp.id DESC"
    rows = db.query(sql, tuple(params))
    return [_rvp_to_dict(r) for r in rows]


def update_restore_verify_policy(policy_id: int, data: dict) -> bool:
    """更新恢复校验策略（白名单）。"""
    allow = set(_RESTORE_VERIFY_POLICY_FIELDS)
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return False
    updates["updated_at"] = db.now_iso()
    if "enabled" in updates:
        updates["enabled"] = 1 if updates.get("enabled") not in (0, False, "0", None) else 0
    if "clone_retention_min" in updates:
        updates["clone_retention_min"] = int(updates["clone_retention_min"] or 30)
    if "interval_minutes" in updates:
        if updates.get("interval_minutes") in ("", None):
            updates["interval_minutes"] = None
        else:
            updates["interval_minutes"] = int(updates["interval_minutes"])
    if "task_id" in updates:
        updates["task_id"] = int(updates["task_id"] or 0)
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(policy_id)
    db.execute("UPDATE restore_verify_policies SET {} WHERE id=?".format(",".join(sets)), tuple(params))
    return True


def delete_restore_verify_policy(policy_id: int) -> None:
    """删除恢复校验策略及其测试报告。"""
    with db._write_lock:
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM restore_test_reports WHERE policy_id=?", (policy_id,))
            conn.execute("DELETE FROM restore_verify_policies WHERE id=?", (policy_id,))
            conn.commit()
        finally:
            conn.close()


def set_restore_verify_status(policy_id: int, last_run_at: str,
                              last_status: str, last_report_id: int = None) -> None:
    """更新策略最近一次运行状态。"""
    sql = ("UPDATE restore_verify_policies SET last_run_at=?, last_status=?, "
           "last_report_id=?, updated_at=? WHERE id=?")
    db.execute(sql, (last_run_at, last_status, last_report_id, db.now_iso(), policy_id))


# ------------------------- 恢复测试报告 -------------------------
_RESTORE_TEST_REPORT_FIELDS = [
    "policy_id", "task_id", "record_id", "db_type", "status",
    "duration_sec", "message", "cleaned", "created_at", "finished_at",
]


def _rtr_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将 restore_test_reports 行转为 dict。"""
    if not row:
        return None
    d = dict(row)
    d["cleaned"] = bool(d.get("cleaned"))
    d["duration_sec"] = float(d.get("duration_sec") or 0)
    return d


def create_restore_test_report(data: dict) -> int:
    """创建恢复测试报告（运行中）。返回报告 id。"""
    row = {k: data.get(k) for k in _RESTORE_TEST_REPORT_FIELDS}
    now = db.now_iso()
    row["created_at"] = now
    row["finished_at"] = None
    row["cleaned"] = 1 if row.get("cleaned") else 0
    row["duration_sec"] = float(row.get("duration_sec") or 0)
    cols = list(row.keys())
    sql = "INSERT INTO restore_test_reports ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_restore_test_report(report_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM restore_test_reports WHERE id=?", (report_id,))
    return _rtr_to_dict(row) if row else None


def update_restore_test_report(report_id: int, data: dict) -> None:
    """更新测试报告（白名单）。"""
    allow = {"status", "duration_sec", "message", "cleaned", "finished_at"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    if "duration_sec" in updates:
        updates["duration_sec"] = float(updates["duration_sec"] or 0)
    if "cleaned" in updates:
        updates["cleaned"] = 1 if updates.get("cleaned") else 0
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(report_id)
    db.execute("UPDATE restore_test_reports SET {} WHERE id=?".format(",".join(sets)), tuple(params))


def list_restore_test_reports(policy_id: int = None, task_id: int = None,
                              limit: int = 200) -> list:
    """列出恢复测试报告，可选按策略/任务过滤。"""
    sql = ("SELECT rtr.*, rvp.name AS policy_name, t.name AS task_name, "
           "t.name AS instance_name "
           "FROM restore_test_reports rtr "
           "LEFT JOIN restore_verify_policies rvp ON rvp.id = rtr.policy_id "
           "LEFT JOIN backup_tasks t ON t.id = rtr.task_id WHERE 1=1")
    params: list = []
    if policy_id is not None:
        sql += " AND rtr.policy_id=?"
        params.append(policy_id)
    if task_id is not None:
        sql += " AND rtr.task_id=?"
        params.append(task_id)
    sql += " ORDER BY rtr.id DESC LIMIT ?"
    params.append(int(limit))
    rows = db.query(sql, tuple(params))
    return [_rtr_to_dict(r) for r in rows]


def get_restore_verify_stats() -> dict:
    """恢复校验仪表盘 KPI。"""
    total = db.query_one("SELECT COUNT(*) AS c FROM restore_verify_policies")
    success = db.query_one(
        "SELECT COUNT(*) AS c FROM restore_test_reports WHERE status=?", ("success",))
    failed = db.query_one(
        "SELECT COUNT(*) AS c FROM restore_test_reports WHERE status=?", ("failed",))
    last = db.query_one(
        "SELECT created_at FROM restore_test_reports ORDER BY id DESC LIMIT 1")
    return {
        "policy_count": int(total["c"] if total else 0),
        "success_count": int(success["c"] if success else 0),
        "failed_count": int(failed["c"] if failed else 0),
        "last_test_at": last["created_at"] if last else None,
    }


# ========================= 数据对比（恢复数据 vs 生产库） =========================
import json as _json

_DATA_COMPARE_TASK_FIELDS = [
    "name",
    "source_db_type", "source_host", "source_port", "source_username",
    "source_password", "source_database", "source_schema",
    "target_db_type", "target_host", "target_port", "target_username",
    "target_password", "target_database", "target_schema",
    "tables", "enable_checksum", "sample_rows",
    "schedule_type", "cron_expr", "interval_minutes", "enabled",
]

_DATA_COMPARE_ENDPOINT_PREFIX = ("source", "target")


def _dct_to_dict(row: Optional[dict]) -> Optional[dict]:
    """将 data_compare_tasks 行转为 dict，敏感字段脱敏、数值字段还原。"""
    if not row:
        return None
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["enable_checksum"] = bool(d.get("enable_checksum"))
    d["sample_rows"] = int(d.get("sample_rows") or 100)
    d["interval_minutes"] = (int(d["interval_minutes"])
                             if d.get("interval_minutes") is not None else None)
    d["last_report_id"] = (int(d["last_report_id"])
                           if d.get("last_report_id") is not None else None)
    for side in _DATA_COMPARE_ENDPOINT_PREFIX:
        has_pwd = bool(d.get(side + "_password"))
        d["has_" + side + "_password"] = has_pwd
        d[side + "_password"] = ""  # 接口不回显明文
    try:
        d["tables"] = _json.loads(d.get("tables") or "[]")
    except Exception:
        d["tables"] = []
    return d


def _dct_prepare_row(data: dict) -> dict:
    """创建/更新前整理字段：加密密码、tables 序列化、数值规范化。"""
    import core.db as _db
    row = {k: data.get(k) for k in _DATA_COMPARE_TASK_FIELDS}
    # 密码：留空不覆盖原值由调用方处理；新值加密存储
    for side in _DATA_COMPARE_ENDPOINT_PREFIX:
        pwd = row.get(side + "_password")
        if pwd in (None, ""):
            row[side + "_password"] = None
        else:
            row[side + "_password"] = _db.encrypt_secret(str(pwd))
    if row.get("tables") in (None, ""):
        row["tables"] = "[]"
    elif not isinstance(row["tables"], str):
        row["tables"] = _json.dumps(row["tables"], ensure_ascii=False)
    row["enable_checksum"] = 1 if row.get("enable_checksum") not in (0, False, "0", None) else 0
    row["enabled"] = 1 if row.get("enabled") not in (0, False, "0", None) else 1
    row["sample_rows"] = int(row.get("sample_rows") or 100)
    row["source_port"] = int(row["source_port"]) if row.get("source_port") not in (None, "") else None
    row["target_port"] = int(row["target_port"]) if row.get("target_port") not in (None, "") else None
    if row.get("interval_minutes") in ("", None):
        row["interval_minutes"] = None
    else:
        row["interval_minutes"] = int(row["interval_minutes"])
    return row


def create_data_compare_task(data: dict) -> int:
    """创建数据对比任务。返回新任务 id。"""
    row = _dct_prepare_row(data)
    now = db.now_iso()
    row["created_at"] = now
    row["updated_at"] = now
    if not row.get("name"):
        row["name"] = f"数据对比-{now[:10]}"
    cols = list(row.keys())
    sql = "INSERT INTO data_compare_tasks ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_data_compare_task(task_id: int, include_secret: bool = False) -> Optional[dict]:
    """按 id 获取数据对比任务。默认脱敏；include_secret=True 时返回解密密码。"""
    row = db.query_one("SELECT * FROM data_compare_tasks WHERE id=?", (task_id,))
    if not row:
        return None
    d = _dct_to_dict(row)
    if include_secret:
        import core.db as _db
        for side in _DATA_COMPARE_ENDPOINT_PREFIX:
            raw = row.get(side + "_password") or ""
            d[side + "_password"] = _db.decrypt_secret(raw) if raw else ""
    return d


def list_data_compare_tasks(enabled_only: bool = False) -> list:
    """列出数据对比任务（脱敏）。"""
    sql = "SELECT * FROM data_compare_tasks WHERE 1=1"
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY id DESC"
    return [_dct_to_dict(r) for r in db.query(sql)]


def update_data_compare_task(task_id: int, data: dict) -> bool:
    """更新数据对比任务（白名单）。密码留空表示不修改。"""
    existing = db.query_one("SELECT * FROM data_compare_tasks WHERE id=?", (task_id,))
    if not existing:
        return False
    updates = {k: v for k, v in data.items() if k in _DATA_COMPARE_TASK_FIELDS}
    if not updates:
        return False
    prepared = _dct_prepare_row(updates)
    # 密码留空 → 不覆盖原密码
    for side in _DATA_COMPARE_ENDPOINT_PREFIX:
        if prepared.get(side + "_password") in (None, ""):
            prepared.pop(side + "_password", None)
    prepared = {k: v for k, v in prepared.items() if k in updates}
    if not prepared:
        return False
    prepared["updated_at"] = db.now_iso()
    sets, params = [], []
    for k, v in prepared.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(task_id)
    db.execute("UPDATE data_compare_tasks SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_data_compare_task(task_id: int) -> None:
    """删除数据对比任务及其报告。"""
    with db._write_lock:
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM data_compare_reports WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM data_compare_tasks WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()


def set_data_compare_status(task_id: int, last_run_at: str, last_status: str,
                            last_report_id: int = None) -> None:
    """更新对比任务最近一次运行状态。"""
    db.execute(
        "UPDATE data_compare_tasks SET last_run_at=?, last_status=?, "
        "last_report_id=?, updated_at=? WHERE id=?",
        (last_run_at, last_status, last_report_id, db.now_iso(), task_id))


def create_data_compare_report(data: dict) -> int:
    """创建数据对比报告（运行中）。返回报告 id。"""
    now = db.now_iso()
    row = {
        "task_id": data.get("task_id"),
        "status": data.get("status") or "running",
        "duration_sec": float(data.get("duration_sec") or 0),
        "summary_json": data.get("summary_json"),
        "tables_json": data.get("tables_json"),
        "message": data.get("message"),
        "created_at": now,
        "finished_at": None,
    }
    cols = list(row.keys())
    sql = "INSERT INTO data_compare_reports ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(row.values()))


def get_data_compare_report(report_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM data_compare_reports WHERE id=?", (report_id,))
    if not row:
        return None
    d = dict(row)
    d["duration_sec"] = float(d.get("duration_sec") or 0)
    for f in ("summary_json", "tables_json"):
        try:
            d[f] = _json.loads(d.get(f)) if d.get(f) else None
        except Exception:
            pass
    return d


def update_data_compare_report(report_id: int, data: dict) -> None:
    """更新对比报告。"""
    allow = {"status", "duration_sec", "summary_json", "tables_json",
             "message", "finished_at"}
    updates = {k: v for k, v in data.items() if k in allow}
    if not updates:
        return
    if "duration_sec" in updates:
        updates["duration_sec"] = float(updates["duration_sec"] or 0)
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(report_id)
    db.execute("UPDATE data_compare_reports SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))


def list_data_compare_reports(task_id: int = None, limit: int = 200) -> list:
    """列出对比报告（明细字段不展开，供列表页）。"""
    sql = ("SELECT r.*, t.name AS task_name FROM data_compare_reports r "
           "LEFT JOIN data_compare_tasks t ON t.id = r.task_id WHERE 1=1")
    params: list = []
    if task_id is not None:
        sql += " AND r.task_id=?"
        params.append(task_id)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(int(limit))
    rows = []
    for r in db.query(sql, tuple(params)):
        d = dict(r)
        d["duration_sec"] = float(d.get("duration_sec") or 0)
        rows.append(d)
    return rows


def get_data_compare_stats() -> dict:
    """数据对比仪表盘 KPI。"""
    total = db.query_one("SELECT COUNT(*) AS c FROM data_compare_tasks")
    success = db.query_one(
        "SELECT COUNT(*) AS c FROM data_compare_reports WHERE status=?", ("success",))
    failed = db.query_one(
        "SELECT COUNT(*) AS c FROM data_compare_reports WHERE status=?", ("failed",))
    last = db.query_one(
        "SELECT created_at FROM data_compare_reports ORDER BY id DESC LIMIT 1")
    return {
        "task_count": int(total["c"] if total else 0),
        "success_count": int(success["c"] if success else 0),
        "failed_count": int(failed["c"] if failed else 0),
        "last_compare_at": last["created_at"] if last else None,
    }


# ======================================================================
# 外部 API 调用令牌（Bearer Token，仅哈希落库，明文只在创建时展示一次）
# ======================================================================
def create_api_token(name: str, created_by: str = "system") -> str:
    """创建外部调用令牌，返回明文（仅此一次，落库为 sha256 哈希）。"""
    import hashlib
    import secrets
    name = (name or "").strip() or "unnamed"
    plain = "bk_" + secrets.token_hex(24)
    token_hash = hashlib.sha256(plain.encode()).hexdigest()
    db.execute(
        "INSERT INTO api_tokens (name, token_hash, created_by, created_at, revoked)"
        " VALUES (?,?,?,?,0)",
        (name, token_hash, created_by, db.now_iso()))
    return plain


def list_api_tokens() -> list:
    """列出令牌（脱敏：不含哈希）。"""
    rows = db.query(
        "SELECT id, name, created_by, created_at, last_used_at, revoked"
        " FROM api_tokens ORDER BY id DESC")
    return rows


def revoke_api_token(token_id: int) -> bool:
    cur = db.query_one("SELECT id FROM api_tokens WHERE id=?", (int(token_id),))
    if not cur:
        return False
    db.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (int(token_id),))
    return True


def verify_api_token(token: str) -> Optional[dict]:
    """校验外部令牌；有效返回令牌行并刷新 last_used_at，无效返回 None。"""
    if not token:
        return None
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.query_one(
        "SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (token_hash,))
    if not row:
        return None
    try:
        db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                   (db.now_iso(), row["id"]))
    except Exception:
        pass
    return row
