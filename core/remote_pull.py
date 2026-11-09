# -*- coding: utf-8 -*-
"""异地容灾「云侧推对象存储 + 平台出站拉取」通道（离线定位，零公网 IP 依赖）。

要解决的现实约束
----------------
被保护的云主机/VPC 内主机**没有公网 IP**，平台也无法主动连进去；反过来，
云主机通常能出站访问同区域的对象存储。因此不建 P2P 反向隧道（那需要公网
打洞/中继，与完全离线定位冲突），改用**共享对象存储做邮箱**：

    云侧（源端）                     对象存储桶                    平台侧（本模块）
    备份产物 → 打成摆渡包    ──推──▶  <prefix>/xxx.inc.tar.gz    ──出站拉──▶ 落收件箱
              + manifest.json ──推──▶ <prefix>/xxx.manifest.json            → 校验 sha256
                                                                          → ferry_inbox.ingest_one
                                                                          → 登记 backup_records

平台只需能**出站访问**该对象存储（内网/专线/摆渡同步到内网 MinIO 均可），
自己的 8080 不需要暴露到公网；云侧也不需要被平台反向连入。

与既有能力的关系
----------------
* 复用 ``core/objectstore/client.py`` 的纯标准库 SigV4 S3 协议（离线自足）；
* 复用 ``core/ferry_inbox.ingest_one`` 的「校验 → 解包 → 登记记录 → 三级复制/告警」
  链路，包格式与摆渡盘完全一致，两种介质可互换；
* ``push_record()`` 提供**云侧推送的参考实现**（源端 AIDBM 或脚本可调用），
  让「推 + 拉」闭环可在同平台双站点场景自测；
* 与 ``pool_replication``（平台→多站点推副本）方向互补，不重复造轮子。

诚实边界
--------
1. 这不是 P2P 反向隧道，也不宣称「免公网 IP」的完全等价替代：前提是云侧与
   平台**都能访问同一个对象存储**（内网专线、专线同步或离线摆渡均可）。
2. 云侧必须按摆渡包格式推送（包 + manifest，manifest 带 task_id 与包 sha256），
   平台不解析任意第三方对象；任务定义需先在平台侧存在，否则入库如实失败并保留包。
3. 拉回登记的是**异地副本记录**，不会自动恢复业务；灾难时走既有数据恢复链路。
4. 默认拉取后**不删除**远端对象，异地副本继续保留；可选开启删除。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import time
from typing import Optional

from core import db

__all__ = [
    "RemotePullError", "init_table", "list_sources", "get_source",
    "create_source", "update_source", "delete_source", "test_source",
    "preview", "pull_source", "push_record", "run_due_sources",
    "list_logs", "scenario_note",
]

SOURCES = "remote_pull_sources"
LOGS = "remote_pull_logs"
PKG_SUFFIX = ".inc.tar.gz"
MANIFEST_SUFFIX = ".manifest.json"

FIELDS = [
    "name", "provider", "endpoint", "port", "secure", "verify_ssl",
    "region", "access_key", "secret_key", "bucket", "prefix",
    "enabled", "interval_minutes", "delete_after_pull", "remark",
]


class RemotePullError(Exception):
    """远端拉取配置/协议/校验失败。"""


def scenario_note() -> dict:
    """页面与 API 共用的能力边界说明（避免文档与代码口径漂移）。"""
    return {
        "route": "云侧推对象存储 → 平台出站拉取 → 摆渡包登记 backup_records",
        "is_p2p_tunnel": False,
        "requires": [
            "云侧可出站访问对象存储；平台可出站访问同一对象存储（内网/专线/摆渡同步均可）",
            "云侧按摆渡包格式推送：xxx.inc.tar.gz + xxx.manifest.json",
            "manifest 必带 task_id 与包文件 sha256；任务定义需先在平台侧存在",
            "平台 8080 不需要公网暴露，也不需要被云侧反向连入",
        ],
        "not_promised": [
            "不提供 P2P 打洞/反向隧道/公网中继",
            "不解析任意第三方对象，不自动恢复业务",
            "不替代平台→多站点的池复制（pool_replication），两者方向互补",
        ],
        "offline": "完全离线可用：S3 协议由平台标准库实现，无需公网 PyPI/DockerHub/NTP/AI API",
    }


# ---------------------------------------------------------------- 表
def init_table(conn=None) -> None:
    """幂等建表（独立 init_table，不改 SCHEMA 大字符串，规避元库方言风险）。"""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "provider TEXT DEFAULT 'minio', "
            "endpoint TEXT NOT NULL, "
            "port INTEGER, "
            "secure INTEGER DEFAULT 0, "
            "verify_ssl INTEGER DEFAULT 0, "
            "region TEXT DEFAULT 'us-east-1', "
            "access_key TEXT DEFAULT '', "
            "secret_key TEXT DEFAULT '', "
            "bucket TEXT NOT NULL, "
            "prefix TEXT DEFAULT '', "
            "enabled INTEGER DEFAULT 1, "
            "interval_minutes INTEGER DEFAULT 30, "
            "delete_after_pull INTEGER DEFAULT 0, "
            "last_run_at TEXT, "
            "last_status TEXT DEFAULT 'unknown', "
            "last_error TEXT DEFAULT '', "
            "remark TEXT DEFAULT '', "
            "created_at TEXT, "
            "updated_at TEXT)" % SOURCES)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source_id INTEGER NOT NULL, "
            "object_key TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "message TEXT DEFAULT '', "
            "record_id INTEGER, "
            "size_bytes INTEGER DEFAULT 0, "
            "sha256 TEXT DEFAULT '', "
            "created_at TEXT)" % LOGS)
        if own:
            conn.commit()
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _row(r: dict | None, secret: bool = False) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    raw = d.get("secret_key") or ""
    d["has_secret_key"] = bool(raw)
    d["secret_key"] = db.decrypt_secret(raw) if secret else ""
    for k in ("secure", "verify_ssl", "enabled", "delete_after_pull"):
        d[k] = int(d.get(k) or 0)
    return d


def list_sources(include_secret: bool = False) -> list:
    return [_row(r, include_secret)
            for r in db.query("SELECT * FROM %s ORDER BY id DESC" % SOURCES)]


def get_source(source_id: int, include_secret: bool = False) -> Optional[dict]:
    return _row(db.query_one("SELECT * FROM %s WHERE id=?" % SOURCES,
                             (int(source_id),)), include_secret)


def _normalize(data: dict, creating: bool = False) -> dict:
    d = {k: data.get(k) for k in FIELDS if k in data}
    if creating:
        if not str(d.get("name") or "").strip():
            raise RemotePullError("来源名称必填")
        if not str(d.get("endpoint") or "").strip() or not str(d.get("bucket") or "").strip():
            raise RemotePullError("对象存储端点与桶名为必填")
    if "endpoint" in d:
        d["endpoint"] = str(d["endpoint"]).strip().rstrip("/")
    for k in ("port", "interval_minutes"):
        if k in d:
            try:
                d[k] = int(d[k]) if d[k] not in (None, "") else None
            except (TypeError, ValueError):
                d[k] = None
    for k in ("secure", "verify_ssl", "enabled", "delete_after_pull"):
        if k in d:
            d[k] = 1 if d[k] in (1, True, "1", "true", "on", "yes") else 0
    if d.get("secret_key"):
        d["secret_key"] = db.encrypt_secret(d["secret_key"])
    else:
        d.pop("secret_key", None)     # 空值不覆盖已保存密钥
    return d


def create_source(data: dict) -> int:
    d = _normalize(data, creating=True)
    now = db.now_iso()
    d.update({"created_at": now, "updated_at": now,
              "last_status": "unknown", "last_error": ""})
    cols = list(d)
    return db.execute(
        "INSERT INTO %s (%s) VALUES (%s)" % (
            SOURCES, ",".join(cols), ",".join("?" * len(cols))),
        tuple(d.values()))


def update_source(source_id: int, data: dict) -> bool:
    if not get_source(source_id):
        return False
    d = _normalize(data)
    if not d:
        return False
    d["updated_at"] = db.now_iso()
    db.execute("UPDATE %s SET %s WHERE id=?" % (
        SOURCES, ",".join("%s=?" % k for k in d)),
        tuple(d.values()) + (int(source_id),))
    return True


def delete_source(source_id: int) -> bool:
    if not get_source(source_id):
        return False
    db.execute("DELETE FROM %s WHERE id=?" % SOURCES, (int(source_id),))
    return True


def _mark_run(source_id: int) -> None:
    """调度层记录本轮执行时间：即使下游 pull_source 被替换/短路，节流仍生效。"""
    db.execute("UPDATE %s SET last_run_at=? WHERE id=?" % SOURCES,
               (db.now_iso(), int(source_id)))


def _set_state(source_id: int, status: str, error: str = "") -> None:
    db.execute("UPDATE %s SET last_run_at=?, last_status=?, last_error=?, "
               "updated_at=? WHERE id=?" % SOURCES,
               (db.now_iso(), status, (error or "")[:500], db.now_iso(),
                int(source_id)))


def list_logs(source_id: int = None, limit: int = 100) -> list:
    where, params = "", []
    if source_id:
        where, params = "WHERE source_id=?", [int(source_id)]
    return db.query(
        "SELECT * FROM %s %s ORDER BY id DESC LIMIT %d" % (
            LOGS, where, max(1, min(1000, int(limit or 100)))),
        tuple(params))


# ---------------------------------------------------------------- 客户端
def _client_for(source: dict):
    from core.objectstore import providers as osp
    cfg = {
        "provider": source.get("provider") or "minio",
        "endpoint": source.get("endpoint") or "",
        "port": source.get("port"),
        "secure": bool(source.get("secure")),
        "verify_ssl": bool(source.get("verify_ssl")),
        "region": source.get("region") or "us-east-1",
        "access_key": source.get("access_key") or "",
        "secret_key": source.get("secret_key") or "",
    }
    return osp.build_client(cfg)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def test_source(source_or_id) -> dict:
    """真实出站探活（列桶），失败如实返回原因。"""
    src = get_source(int(source_or_id), include_secret=True) \
        if not isinstance(source_or_id, dict) else dict(source_or_id)
    if not src:
        raise RemotePullError("来源不存在")
    out = {"ok": False, "name": src.get("name"), "bucket": src.get("bucket"),
           "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        cli = _client_for(src)
        ok, msg = cli.test_connection()
        out["ok"] = bool(ok)
        out["message"] = msg
        if ok:
            out["bucket_exists"] = cli.bucket_exists(src["bucket"])
            if not out["bucket_exists"]:
                out["warning"] = "端点可达但桶不存在，请先在对象存储侧创建桶"
    except Exception as e:
        out["error"] = str(e).strip() or type(e).__name__
    if not isinstance(source_or_id, dict):
        _set_state(int(source_or_id), "online" if out["ok"] else "error",
                   out.get("error") or out.get("message") or "")
    return out


def _iter_manifest_keys(cli, bucket: str, prefix: str) -> list:
    keys = []
    for obj in cli.iter_objects(bucket, prefix=prefix or ""):
        key = obj.get("key") or ""
        if key.endswith(MANIFEST_SUFFIX):
            keys.append(key)
    return sorted(keys)


def _already_done(source_id: int, key: str) -> bool:
    return bool(db.query_one(
        "SELECT id FROM %s WHERE source_id=? AND object_key=? AND status='success' LIMIT 1"
        % LOGS, (int(source_id), key)))


def preview(source_or_id, limit: int = 100) -> dict:
    """列出待拉取的摆渡包（不下载、不改状态）。"""
    src = get_source(int(source_or_id), include_secret=True) \
        if not isinstance(source_or_id, dict) else dict(source_or_id)
    if not src:
        raise RemotePullError("来源不存在")
    cli = _client_for(src)
    pending, skipped = [], 0
    for mkey in _iter_manifest_keys(cli, src["bucket"], src.get("prefix") or ""):
        pkg_key = mkey[:-len(MANIFEST_SUFFIX)] + PKG_SUFFIX
        if not isinstance(source_or_id, dict) and _already_done(int(source_or_id), mkey):
            skipped += 1
            continue
        pending.append({"manifest_key": mkey, "package_key": pkg_key})
        if len(pending) >= max(1, int(limit or 100)):
            break
    return {"ok": True, "bucket": src["bucket"],
            "prefix": src.get("prefix") or "",
            "pending": pending, "count": len(pending), "skipped_done": skipped}


def _download(cli, bucket: str, key: str, dest: str, expected: int = None) -> int:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    return cli.download_to_file(bucket, key, dest, resume=True,
                                expected_size=expected)


def pull_source(source_id: int, dry_run: bool = False, limit: int = 50,
                client=None) -> dict:
    """出站拉取待处理摆渡包并登记。dry_run 只预览不下载。

    单包失败不影响其它包；失败如实记日志，成功才标记 key 已处理（幂等）。
    """
    src = get_source(source_id, include_secret=True)
    if not src:
        raise RemotePullError("来源不存在")
    cli = client or _client_for(src)
    bucket, prefix = src["bucket"], src.get("prefix") or ""
    keys = _iter_manifest_keys(cli, bucket, prefix)
    stat = {"source_id": int(source_id), "found": len(keys), "pulled": 0,
            "failed": 0, "skipped": 0, "dry_run": bool(dry_run),
            "details": [], "record_ids": []}
    if dry_run:
        stat["pending"] = [{"manifest_key": k,
                            "package_key": k[:-len(MANIFEST_SUFFIX)] + PKG_SUFFIX}
                           for k in keys[:max(1, int(limit or 50))]]
        return stat

    from core import ferry_inbox
    inbox = ferry_inbox.inbox_dir()
    for mkey in keys:
        if stat["pulled"] + stat["failed"] >= max(1, int(limit or 50)):
            break
        if _already_done(int(source_id), mkey):
            stat["skipped"] += 1
            continue
        pkg_key = mkey[:-len(MANIFEST_SUFFIX)] + PKG_SUFFIX
        base = os.path.basename(pkg_key)
        if not base.endswith(PKG_SUFFIX) or "/" in base or ".." in base:
            _log_pull(source_id, mkey, "failed", "非法的对象键: " + pkg_key)
            stat["failed"] += 1
            continue
        pkg_path = os.path.join(inbox, base)
        man_path = pkg_path[:-len(PKG_SUFFIX)] + MANIFEST_SUFFIX
        try:
            _download(cli, bucket, mkey, man_path)
            with open(man_path, "r", encoding="utf-8") as f:
                mani = json.load(f)
            expect_sha = str(mani.get("sha256") or "").lower()
            if not expect_sha or not mani.get("task_id"):
                raise RemotePullError("manifest 缺 task_id/sha256")
            _download(cli, bucket, pkg_key, pkg_path)
            real_sha = _sha256(pkg_path)
            if real_sha != expect_sha:
                raise RemotePullError("包 sha256 不一致（源 %s / 盘 %s）"
                                     % (expect_sha[:12], real_sha[:12]))
            res = ferry_inbox.ingest_one(pkg_path)
            if not res.get("ok"):
                raise RemotePullError(res.get("message") or "摆渡入库失败")
            rid = res.get("record_id")
            size = os.path.getsize(pkg_path)
            _log_pull(source_id, mkey, "success", "异地副本已登记",
                      record_id=rid, size=size, sha256=real_sha)
            stat["pulled"] += 1
            stat["record_ids"].append(rid)
            stat["details"].append({"key": base, "status": "success",
                                    "record_id": rid, "size": size})
            for p in (pkg_path, man_path):
                try:
                    shutil.move(p, os.path.join(inbox, "processed",
                                                os.path.basename(p)))
                except OSError:
                    pass
            if int(src.get("delete_after_pull") or 0):
                for k in (mkey, pkg_key):
                    try:
                        cli.delete_object(bucket, k)
                    except Exception:
                        pass
        except Exception as e:
            msg = str(e).strip() or type(e).__name__
            _log_pull(source_id, mkey, "failed", msg)
            stat["failed"] += 1
            stat["details"].append({"key": base, "status": "failed",
                                    "message": msg})
            for p in (pkg_path, man_path):
                if os.path.isfile(p):
                    try:
                        shutil.move(p, os.path.join(inbox, "failed",
                                                    os.path.basename(p)))
                    except OSError:
                        pass
    _set_state(int(source_id), "online" if not stat["failed"] else "error",
               "" if not stat["failed"] else "%d 个包拉取失败" % stat["failed"])
    return stat


def _log_pull(source_id: int, key: str, status: str, message: str,
              record_id=None, size: int = 0, sha256: str = "") -> None:
    db.execute(
        "INSERT INTO %s (source_id, object_key, status, message, record_id, "
        "size_bytes, sha256, created_at) VALUES (?,?,?,?,?,?,?,?)" % LOGS,
        (int(source_id), key, status, message[:500], record_id,
         int(size or 0), sha256, db.now_iso()))


def push_record(source_id: int, record_id: int, client=None) -> dict:
    """云侧推送参考实现：把本地一条备份记录打成摆渡包 + manifest 推到远端桶。

    源端 AIDBM（或等价脚本）在云侧调用本函数，平台侧再用 ``pull_source``
    出站拉回，形成完整的「推 + 拉」异地容灾闭环。
    """
    from core import models
    src = get_source(source_id, include_secret=True)
    if not src:
        raise RemotePullError("来源不存在")
    rec = models.get_record(record_id)
    if not rec or not rec.get("backup_path"):
        raise RemotePullError("备份记录不存在或产物路径为空: %s" % record_id)
    path = rec["backup_path"]
    if not os.path.isfile(path):
        raise RemotePullError("本地备份产物不存在: %s" % path)

    cli = client or _client_for(src)
    bucket = src["bucket"]
    prefix = (src.get("prefix") or "").strip().strip("/")
    ts = time.strftime("%Y%m%d%H%M%S")
    work = os.path.join(os.path.dirname(path) or ".", ".remote_push_%s_%s"
                        % (record_id, ts))
    os.makedirs(work, exist_ok=True)
    try:
        fname = os.path.basename(path)
        pkg = os.path.join(work, "pkg" + PKG_SUFFIX)
        with tarfile.open(pkg, "w:gz") as tf:
            tf.add(path, arcname=fname)
        sha = _sha256(pkg)
        stem = "%d_%s_%s" % (rec.get("task_id") or 0, ts, sha[:8])
        pkg_key = "%s/%s%s" % (prefix, stem, PKG_SUFFIX) if prefix \
            else stem + PKG_SUFFIX
        man_key = pkg_key[:-len(PKG_SUFFIX)] + MANIFEST_SUFFIX
        man = {
            "task_id": rec.get("task_id"), "sha256": sha,
            "finished_at": rec.get("finished_at") or db.now_iso(),
            "backup_type": rec.get("backup_type") or "full",
            "backup_mode": rec.get("backup_mode") or "logical",
            "file_name": fname,
            "size_bytes": os.path.getsize(pkg),
            "source": "aidbm-remote-pull",
        }
        man_path = os.path.join(work, "pkg" + MANIFEST_SUFFIX)
        with open(man_path, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False)
        cli.upload_file(bucket, pkg_key, pkg)
        cli.upload_file(bucket, man_key, man_path)
        return {"ok": True, "package_key": pkg_key, "manifest_key": man_key,
                "sha256": sha, "size_bytes": man["size_bytes"], "manifest": man}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_due_sources() -> dict:
    """调度入口：按各自 interval_minutes 节流拉取所有启用的来源。"""
    out = {"checked": 0, "pulled": 0, "failed": 0, "errors": []}
    now = time.time()
    for src in list_sources():
        if not int(src.get("enabled") or 0):
            continue
        last = str(src.get("last_run_at") or "")
        interval = max(1, int(src.get("interval_minutes") or 30))
        if last:
            try:
                import datetime as _dt
                t = _dt.datetime.fromisoformat(last).timestamp()
                if now - t < interval * 60:
                    continue
            except (ValueError, TypeError):
                pass
        out["checked"] += 1
        try:
            r = pull_source(int(src["id"]))
            _mark_run(int(src["id"]))
            out["pulled"] += int(r.get("pulled") or 0)
            out["failed"] += int(r.get("failed") or 0)
        except Exception as e:
            out["failed"] += 1
            out["errors"].append("#%s: %s" % (src["id"], str(e)[:200]))
            _set_state(int(src["id"]), "error", str(e)[:500])
    return out
