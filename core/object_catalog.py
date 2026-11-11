# -*- coding: utf-8 -*-
"""
对象目录（Object Catalog，M2 恢复标准化）：备份成功后异步扫描产物内
的对象清单（表/视图等），落库 backup_objects 表，供恢复向导勾选
表级恢复，避免"恢复完才知道里面有什么"。

支持的解析：
- MySQL .sql/.zst：流式按行解析 CREATE TABLE 标记（mysqldump 输出格式）
- PostgreSQL .dump：pg_restore -l 目录解析（TABLE/TABLE DATA 条目）
- PostgreSQL .sql：CREATE TABLE 正则
- 达梦 .dmp：二进制内 CREATE TABLE "name" 字符串扫描
- 其他类型：登记单条"整实例/整库"占位（不阻塞）
"""
import io
import os
import re
import threading

import core.db as db
import core.models as models

_logger = None


def _log():
    global _logger
    if _logger is None:
        _logger = db.get_logger("catalog")
    return _logger


def scan_async(record_id: int, backup_path: str, db_type: str,
               logger=None) -> None:
    """备份完成后异步扫描（不阻塞备份主流程）。"""
    th = threading.Thread(
        target=_scan_safe, daemon=True,
        args=(record_id, backup_path, db_type, logger),
        name=f"obj-catalog-{record_id}")
    th.start()


def _scan_safe(record_id, backup_path, db_type, logger):
    try:
        n = scan(record_id, backup_path, db_type)
        _log().info("[catalog] record=%s 扫描到 %d 个对象", record_id, n)
    except Exception as e:
        _log().warning("[catalog] record=%s 扫描失败（不影响备份）: %s",
                       record_id, e)


# ---------------------------------------------------------------- #
# 表结构（db.py SCHEMA 迁移在 init_schema 内追加）
# ---------------------------------------------------------------- #
def replace_objects(record_id: int, rows: list) -> int:
    """覆盖式写入某备份记录的对象清单。rows: [{obj_type,obj_name,schema}]"""
    db.execute("DELETE FROM backup_objects WHERE record_id=?", (record_id,))
    for r in rows[:2000]:  # 上限保护：超大清单截断
        db.execute(
            "INSERT INTO backup_objects(record_id, obj_type, obj_name, schema)"
            " VALUES (?,?,?,?)",
            (record_id, r.get("obj_type", "TABLE"),
             r.get("obj_name", ""), r.get("schema", "")))
    return len(rows[:2000])


def list_objects(record_id: int) -> list:
    return db.query(
        "SELECT obj_type, obj_name, schema FROM backup_objects "
        "WHERE record_id=? ORDER BY obj_type, obj_name", (record_id,))


# ---------------------------------------------------------------- #
# 解析器
# ---------------------------------------------------------------- #
def _read_decompressed(path: str, limit_mb: int = 512) -> str:
    """按扩展名解压读取文本（上限保护，超大文件只读前 limit_mb MB）。"""
    import gzip
    limit = limit_mb * 1024 * 1024
    lower = (path or "").lower()
    try:
        if lower.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                return f.read(limit).decode("utf-8", "ignore")
        if lower.endswith(".zst"):
            import subprocess
            r = subprocess.run(["zstd", "-dc", path], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=600)
            return r.stdout[:limit].decode("utf-8", "ignore")
        if lower.endswith(".dump"):
            # pg_restore -l 目录清单（需本机有 pg_restore；没有则退化为占位）
            import subprocess
            r = subprocess.run(["pg_restore", "-l", path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=300)
            return r.stdout.decode("utf-8", "ignore")[:limit]
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "ignore")
    except Exception as e:
        _log().warning("[catalog] 读取 %s 失败: %s", path, e)
        return ""


def _scan_mysql(text: str) -> list:
    rows = []
    seen = set()
    # mysqldump：CREATE TABLE `tbl` （USE 库后）或 CREATE TABLE `db`.`tbl`
    for m in re.finditer(
            r"CREATE TABLE\s+(?:`?(\w+)`?\.)?`?(\w+)`?\s*\(", text):
        schema, tbl = m.group(1) or "", m.group(2)
        key = (schema, tbl)
        if key not in seen:
            seen.add(key)
            rows.append({"obj_type": "TABLE", "obj_name": tbl,
                         "schema": schema})
    return rows


def _scan_pg_restore_list(text: str) -> list:
    rows, seen = [], set()
    # pg_restore -l 行示例：
    # 215; 1259 16456 TABLE t1 postgres
    for m in re.finditer(r"^\d+; \d+ \d+ (TABLE|VIEW) (\S+)(?: (\S+))?$",
                         text, re.M):
        obj_type, name, schema = m.group(1), m.group(2), m.group(3) or ""
        key = (schema, name)
        if key not in seen:
            seen.add(key)
            rows.append({"obj_type": obj_type, "obj_name": name,
                         "schema": schema})
    return rows


def _scan_pg_sql(text: str) -> list:
    rows, seen = [], set()
    for m in re.finditer(
            r"CREATE TABLE\s+(?:\"?(\w+)\"?\.)?\"?(\w+)\"?\s*\(", text):
        schema, tbl = m.group(1) or "public", m.group(2)
        key = (schema, tbl)
        if key not in seen:
            seen.add(key)
            rows.append({"obj_type": "TABLE", "obj_name": tbl,
                         "schema": schema})
    return rows


def _scan_dameng_dmp(path: str) -> list:
    """dmp 为二进制，扫描其中 CREATE TABLE "name" 文本片段。"""
    rows, seen = [], set()
    try:
        with open(path, "rb") as f:
            data = f.read(256 * 1024 * 1024)  # 上限 256MB
        text = data.decode("utf-8", "ignore")
        for m in re.finditer(
                r'CREATE TABLE\s+(?:"?(\w+)"?\s*\.\s*)?"?(\w+)"?\s*\(', text):
            schema, tbl = m.group(1) or "SYSDBA", m.group(2)
            key = (schema, tbl)
            if key not in seen:
                seen.add(key)
                rows.append({"obj_type": "TABLE", "obj_name": tbl,
                             "schema": schema})
    except Exception as e:
        _log().warning("[catalog] dmp 扫描失败: %s", e)
    return rows


def _scan_mysql_tar(path: str) -> list:
    """全实例逐库 tar（dbs/<db>.sql）：流式逐成员解析表清单（P0-1）。"""
    import tarfile
    rows, seen = [], set()
    with tarfile.open(path, "r:*") as tf:
        for m in tf.getmembers():
            name = m.name.lstrip("./")
            if not (m.isfile() and name.startswith("dbs/")
                    and name.endswith(".sql")):
                continue
            db_name = os.path.splitext(os.path.basename(name))[0]
            fh = tf.extractfile(m)
            if fh is None:
                continue
            for mm in re.finditer(
                    r"CREATE TABLE\s+(?:`?(\w+)`?\.)?`?(\w+)`?\s*\(",
                    fh.read(512 * 1024 * 1024).decode("utf-8", "ignore")):
                schema, tbl = mm.group(1) or db_name, mm.group(2)
                key = (schema, tbl)
                if key not in seen:
                    seen.add(key)
                    rows.append({"obj_type": "TABLE", "obj_name": tbl,
                                 "schema": schema})
    return rows


_MYSQL_FAMILY = ("mysql", "mariadb", "tidb", "oceanbase")
_PG_FAMILY = ("postgresql", "kingbase", "opengauss", "panwei", "highgo", "uxdb")


def _scan_pg_family_tar(path: str) -> list:
    """PG 系全实例逐库 tar：manifest 列真实库名，逐库 .sql 列真实表。

    .dump/-Fc 是二进制自定义格式，平台侧不猜测其中的表名（不伪造对象），
    只登记库级条目；需要表级恢复时由 pg_restore --table 通道处理。
    """
    import json as _json
    import tarfile
    rows, seen = [], set()

    def _add(obj_type, name, schema=""):
        key = (obj_type, schema, name)
        if name and key not in seen:
            seen.add(key)
            rows.append({"obj_type": obj_type, "obj_name": name,
                         "schema": schema})

    with tarfile.open(path, "r:*") as tf:
        names = tf.getnames()
        mf = next((n for n in names if n.endswith("manifest.json")), None)
        if mf:
            try:
                meta = _json.load(tf.extractfile(mf))
                for d in meta.get("databases") or []:
                    nm = d.get("name") if isinstance(d, dict) else d
                    _add("DATABASE", str(nm or ""))
            except Exception:
                pass
        for m in tf.getmembers():
            name = m.name.lstrip("./")
            if not (m.isfile() and
                    re.search(r"(^|/)dbs/[^/]+\.(sql|dump)$", name)):
                continue
            dbn = os.path.splitext(os.path.basename(name))[0]
            if name.endswith(".sql"):
                fh = tf.extractfile(m)
                if fh:
                    text = fh.read(64 * 1024 * 1024).decode("utf-8", "ignore")
                    for o in _scan_pg_sql(text):
                        _add("TABLE", o["obj_name"], o.get("schema") or dbn)
            else:
                _add("DATABASE", dbn)
    return rows


def scan(record_id: int, backup_path: str, db_type: str) -> int:
    """扫描备份产物对象清单并落库，返回对象数。"""
    rows = []
    p = (backup_path or "").lower()
    if db_type in _MYSQL_FAMILY and p.endswith((".tar.gz", ".tgz")):
        rows = _scan_mysql_tar(backup_path)
    elif db_type in _MYSQL_FAMILY and (p.endswith(".sql") or
                                       p.endswith(".zst") or
                                       p.endswith(".gz")):
        rows = _scan_mysql(_read_decompressed(backup_path))
    elif db_type in _PG_FAMILY:
        if p.endswith((".tar.gz", ".tgz")):
            rows = _scan_pg_family_tar(backup_path)
        elif p.endswith(".dump"):
            rows = _scan_pg_restore_list(_read_decompressed(backup_path))
        elif p.endswith((".sql", ".gz")):
            rows = _scan_pg_sql(_read_decompressed(backup_path))
    elif db_type == "dameng" and p.endswith(".dmp"):
        rows = _scan_dameng_dmp(backup_path)

    if not rows:
        # 占位：无法解析的类型登记整库条目，保证恢复向导有可用条目
        rows = [{"obj_type": "DATABASE", "obj_name": "(整库/整实例)",
                 "schema": ""}]
    return replace_objects(record_id, rows)
