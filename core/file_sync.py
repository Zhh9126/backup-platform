# -*- coding: utf-8 -*-
"""文件推送式实时同步（P1-4，零 Agent，对标 DSG 文件级容灾）：

原理：源端**零安装零进程**——平台经 SSH 周期做只读指纹扫描
（core/dedup_source.remote_file_manifest：find+sha256sum+stat），
与上轮清单比对识别 新增/变更/删除，仅拉取变更文件到本地版本目录：

    BACKUP_ROOT/file_sync/<task_id>/<YYYYmmdd_HHMMSS>/<相对结构>

- 首轮 = 全量基线（version_1）
- 后续每轮只拉变更/新增文件；删除文件记入该版本 .deleted.txt
  （与文件备份引擎镜像语义对齐，恢复时可应用删除）
- 指纹 = sha256+size+mtime，固定块移位免疫
- 调度：统一 file_sync_tick 作业（30s）+ 每任务 interval 节流 + running 护栏

诚实边界：轮询间隔内源端同文件多次改写只留最后态（非 inotify 逐事件）；
文件级粒度（不拆文件内部 delta——rsync delta 留作后续加速口）。
"""
import json
import os
import shutil
import threading
import time
from typing import Optional

import core.db as db
import config

_RUNNING = set()          # 任务并发护栏（进程内）
_running_lock = threading.Lock()
_TICK_LOCK = threading.Lock()


class FileSyncError(ValueError):
    """文件同步错误（消息可直接透出）。"""


# ---------------------------------------------------------------- 任务 CRUD #

def _parse_json_list(v, default=None):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            out = json.loads(v or "[]")
            return out if isinstance(out, list) else default
        except Exception:
            return default
    return default


def create_task(data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise FileSyncError("任务名称必填")
    src_paths = [p.strip() for p in _parse_json_list(data.get("src_paths")) or []
                 if str(p).strip()]
    if not src_paths:
        raise FileSyncError("至少配置一个同步源路径")
    for p in src_paths:
        if not p.startswith("/"):
            raise FileSyncError(
                f"源路径必须为绝对路径（SFTP 语义需要）: {p}")
    host_id = data.get("ssh_host_id")
    if host_id:
        from core import ssh_hosts
        if not ssh_hosts.get_host(int(host_id)):
            raise FileSyncError(f"纳管主机不存在: #{host_id}")
    now = db.now_iso()
    return db.execute(
        "INSERT INTO file_sync_tasks(name, ssh_host_id, src_paths, excludes, "
        "interval_sec, keep_versions, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (name, int(host_id) if host_id else None,
         json.dumps(src_paths, ensure_ascii=False),
         json.dumps(_parse_json_list(data.get("excludes")) or [], ensure_ascii=False),
         max(30, int(data.get("interval_sec") or 60)),
         max(1, int(data.get("keep_versions") or 10)),
         1 if data.get("enabled", False) else 0, now, now))


def update_task(task_id: int, data: dict) -> bool:
    row = db.query_one("SELECT * FROM file_sync_tasks WHERE id=?", (task_id,))
    if not row:
        raise FileSyncError(f"同步任务不存在: #{task_id}")
    sets, params = [], []
    if (data.get("name") or "").strip():
        sets.append("name=?"); params.append(data["name"].strip())
    if "src_paths" in data:
        src_paths = [p.strip() for p in _parse_json_list(data.get("src_paths")) or []
                     if str(p).strip()]
        if not src_paths:
            raise FileSyncError("至少配置一个同步源路径")
        sets.append("src_paths=?"); params.append(json.dumps(src_paths, ensure_ascii=False))
    if "excludes" in data:
        sets.append("excludes=?")
        params.append(json.dumps(_parse_json_list(data.get("excludes")) or [],
                                 ensure_ascii=False))
    if "ssh_host_id" in data:
        sets.append("ssh_host_id=?")
        params.append(int(data["ssh_host_id"]) if data["ssh_host_id"] else None)
    if data.get("interval_sec"):
        sets.append("interval_sec=?")
        params.append(max(30, int(data["interval_sec"])))
    if data.get("keep_versions"):
        sets.append("keep_versions=?")
        params.append(max(1, int(data["keep_versions"])))
    if "enabled" in data:
        sets.append("enabled=?"); params.append(1 if data["enabled"] else 0)
    if not sets:
        return False
    sets.append("updated_at=?"); params.append(db.now_iso())
    params.append(task_id)
    db.execute("UPDATE file_sync_tasks SET {} WHERE id=?".format(",".join(sets)),
               tuple(params))
    return True


def delete_task(task_id: int) -> None:
    db.execute("DELETE FROM file_sync_tasks WHERE id=?", (task_id,))


def get_task(task_id: int) -> Optional:
    row = db.query_one("SELECT * FROM file_sync_tasks WHERE id=?", (task_id,))
    return _decorate(dict(row)) if row else None


def list_tasks() -> list:
    return [_decorate(dict(r))
            for r in db.query("SELECT * FROM file_sync_tasks ORDER BY id DESC") or []]


def _decorate(row: dict) -> dict:
    row["src_paths"] = _parse_json_list(row.get("src_paths")) or []
    row["excludes"] = _parse_json_list(row.get("excludes")) or []
    try:
        row["last_summary"] = json.loads(row.get("last_summary") or "null")
    except Exception:
        row["last_summary"] = None
    try:
        row["manifest"] = json.loads(row.get("manifest") or "null")
    except Exception:
        row["manifest"] = None
    return row


# ---------------------------------------------------------------- 同步执行 #

def _ssh_client(task: dict):
    """SSH 通道：纳管主机；未配置 → 本机（SSH 127.0.0.1 密钥，与本机 E2E 语义一致）。"""
    import paramiko
    if task.get("ssh_host_id"):
        from core import ssh_hosts
        from core import remote_dump
        host = ssh_hosts.get_host(int(task["ssh_host_id"]), include_secret=True)
        if not host:
            raise FileSyncError(f"纳管主机不存在: #{task['ssh_host_id']}")
        client = remote_dump._connect(host)
        return client
    # 本机：SSH 127.0.0.1 密钥认证（root）
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("127.0.0.1", port=22, username="root",
                   key_filename=os.path.expanduser("~/.ssh/id_ed25519"),
                   timeout=10, allow_agent=False, look_for_keys=False)
    return client


def _excluded(rel_path: str, excludes: list) -> bool:
    """glob 排除（basename 或全路径匹配，与文件备份引擎语义一致）。"""
    import fnmatch
    base = os.path.basename(rel_path)
    for ex in excludes or []:
        ex = (ex or "").strip()
        if not ex:
            continue
        if fnmatch.fnmatch(base, ex) or fnmatch.fnmatch(rel_path, ex):
            return True
    return False


def _flatten_manifest(manifest: dict, excludes: list = None) -> dict:
    """多源清单摊平：{绝对路径: {sha,size,mtime,local_rel}}。

    local_rel = 「源 basename + 源内相对路径」（版本目录布局，如
    src_data/a.txt）；多源 basename 冲突时该源回退全路径展开。
    应用 glob 排除。"""
    out = {}
    # 先扫一遍各源 basename，检测冲突
    basenames = [(src.get("src_path") or "").rstrip("/") for src in
                 (manifest.get("sources") or [])]
    for idx, src in enumerate(manifest.get("sources") or []):
        prefix = (src.get("src_path") or "").rstrip("/")
        bn = os.path.basename(prefix) or "root"
        clash = bn in [os.path.basename(b) for i, b in enumerate(basenames)
                       if i != idx] or not bn
        root_rel = prefix.lstrip("/") if clash else bn
        for rel, meta in (src.get("files") or {}).items():
            path = f"{prefix}/{rel.lstrip('./')}"
            if _excluded(path, excludes):
                continue
            m = dict(meta)
            m["local_rel"] = f"{root_rel}/{rel.lstrip('./')}"
            out[path] = m
    return out


def _diff(old: dict, new: dict) -> tuple:
    """指纹 diff → (new_or_changed{path:meta}, deleted{path:meta})。"""
    changed = {}
    for path, meta in new.items():
        oldm = old.get(path)
        if not oldm or oldm.get("sha") != meta.get("sha") \
                or int(oldm.get("size") or 0) != int(meta.get("size") or 0):
            changed[path] = meta
    deleted = {p: m for p, m in old.items() if p not in new}
    return changed, deleted


def sync_once(task_id: int, force: bool = False) -> dict:
    """执行一轮同步（可被调度 tick 或手动触发调用）。"""
    from core.dedup_source import remote_file_manifest as _rfm
    row = db.query_one("SELECT * FROM file_sync_tasks WHERE id=?", (task_id,))
    if not row:
        raise FileSyncError(f"同步任务不存在: #{task_id}")
    task = _decorate(dict(row))
    started = time.time()
    client = _ssh_client(task)
    version_dir = None
    try:
        sftp = client.open_sftp()
        # 只读指纹扫描（零临时文件）
        def exec_fn(cmd):
            stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
            o = stdout.read().decode("utf-8", "replace")
            e = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
            return o, e, rc
        manifest = _rfm(exec_fn, task["src_paths"])
        flat = _flatten_manifest(manifest, task.get("excludes"))
        old_flat = _flatten_manifest(task.get("manifest") or {},
                                     task.get("excludes"))
        changed, deleted = _diff(old_flat, flat)

        if not old_flat:
            # 首轮（无基线清单）：全量基线
            changed = flat
            deleted = {}
        if not changed and not deleted:
            # 无变化：只更新基线（mtime 变化 sha 未变时刷新）
            _save_state(task_id, "no_change", {
                "new": 0, "changed": 0, "deleted": 0, "bytes": 0,
                "total_files": len(flat)},
                manifest, round(time.time() - started, 2))
            return {"status": "no_change", "total_files": len(flat)}

        # 版本目录
        root = os.path.join(config.BACKUP_ROOT, "file_sync", str(task_id))
        os.makedirs(root, exist_ok=True)
        version_dir = os.path.join(root, time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(version_dir, exist_ok=True)

        # 拉取变更文件（SFTP 逐文件，目录结构保持）
        pulled, failed, total_bytes = 0, 0, 0
        for path, meta in sorted(changed.items()):
            remote_path = path  # 摊平 key = 源路径原样（SFTP 可直接定位）
            local_path = os.path.join(
                version_dir, (meta.get("local_rel") or path.lstrip("/")))
            try:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                try:
                    sftp.get(remote_path, local_path)
                except FileNotFoundError:
                    # 扫描到拉取间隙被删：如实计入失败（下一轮 diff 会再判）
                    failed += 1
                    continue
                # 完整性对账
                local_sha = _sha256(local_path)
                if local_sha and meta.get("sha") and local_sha != meta["sha"]:
                    failed += 1
                    continue
                pulled += 1
                total_bytes += os.path.getsize(local_path)
            except Exception as e:
                failed += 1
                db.get_logger("file_sync").warning(
                    "[fsync] task=%s 拉取失败 %s: %s", task_id, path, e)
        if deleted:
            with open(os.path.join(version_dir, ".deleted.txt"), "w",
                      encoding="utf-8") as f:
                for p in sorted(deleted):
                    f.write(p + "\n")

        summary = {"new": pulled, "changed": pulled, "deleted": len(deleted),
                   "bytes": total_bytes, "failed": failed,
                   "total_files": len(flat)}
        _save_state(task_id, "failed" if failed and not pulled else "synced",
                    summary, manifest, round(time.time() - started, 2))
        # 版本保留策略
        _apply_retention(root, int(task.get("keep_versions") or 10))
        return {"status": "synced", "version_dir": version_dir, **summary}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _save_state(task_id: int, status: str, summary: dict,
                manifest: dict, duration: float) -> None:
    db.execute(
        "UPDATE file_sync_tasks SET last_run_at=?, last_status=?, "
        "last_summary=?, manifest=?, last_error='' WHERE id=?",
        (db.now_iso(), status, json.dumps(summary, ensure_ascii=False),
         json.dumps(manifest, ensure_ascii=False), task_id))


def _apply_retention(root: str, keep_versions: int) -> None:
    """版本保留：只留最近 N 个版本目录。"""
    try:
        versions = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and len(d) == 15)
        for d in versions[:-keep_versions] if keep_versions > 0 else []:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    except Exception as e:
        db.get_logger("file_sync").warning("[fsync] 版本保留清理失败: %s", e)


def versions_of(task_id: int) -> list:
    """版本目录列表（新→旧）。"""
    root = os.path.join(config.BACKUP_ROOT, "file_sync", str(task_id))
    if not os.path.isdir(root):
        return []
    out = []
    for d in sorted(os.listdir(root), reverse=True):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        n_files = sum(len(fs) for _, _, fs in os.walk(p))
        out.append({"version": d, "path": p, "files": n_files,
                    "size_bytes": sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fs in os.walk(p) for f in fs)})
    return out


# ---------------------------------------------------------------- 调度 tick #

def tick() -> int:
    """调度作业入口：对到期任务执行同步（每任务串行，护栏防重入）。

    返回本轮实际执行的任务数。
    """
    with _TICK_LOCK:
        now = time.time()
        ran = 0
        for row in db.query(
                "SELECT id, interval_sec, last_run_at FROM file_sync_tasks "
                "WHERE enabled=1 ORDER BY id") or []:
            tid = row["id"]
            key = str(tid)
            with _running_lock:
                if key in _RUNNING:
                    continue
                _RUNNING.add(key)
            try:
                # interval 节流
                if row.get("last_run_at"):
                    try:
                        last = time.mktime(time.strptime(
                            row["last_run_at"][:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:
                        last = 0
                    if now - last < int(row.get("interval_sec") or 60):
                        continue
                db.execute(
                    "UPDATE file_sync_tasks SET last_status='running' WHERE id=?",
                    (tid,))
                try:
                    sync_once(tid)
                    ran += 1
                except Exception as e:
                    db.execute(
                        "UPDATE file_sync_tasks SET last_status='failed', "
                        "last_error=? WHERE id=?", (str(e)[:300], tid))
                    db.get_logger("file_sync").warning(
                        "[fsync] task=%s 同步失败: %s", tid, e)
            finally:
                with _running_lock:
                    _RUNNING.discard(key)
        return ran
