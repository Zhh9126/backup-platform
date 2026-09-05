# -*- coding: utf-8 -*-
"""
摆渡收件箱（M4，离线环境单向网闸/摆渡盘场景）：

跨网段离线环境无法直连时，源侧平台/脚本把备份产物打成「增量包」：
    <task_id>_<finished_at>_<sha256>.inc.tar.gz + 同名 .manifest.json
摆渡盘拷入目标侧平台的收件箱目录（默认 /data/ferry_inbox，可配），
本模块周期扫描：
  1. 校验 sha256（manifest 与实物一致）
  2. 解包产物落盘到该任务 L1 目录
  3. 登记 backup_records（status=success，note 标注「摆渡导入」）
  4. 进入统一三级复制/保留策略
  5. 处理完成的包移入 processed/（损坏的移入 failed/）

入口：ingest_all()（scheduler 周期调用，默认每 10 分钟）。
"""
import hashlib
import json
import os
import shutil
import tarfile
import threading

import core.db as db
import core.models as models

_logger = None


def _log():
    global _logger
    if _logger is None:
        _logger = db.get_logger("ferry")
    return _logger


def inbox_dir() -> str:
    import config
    d = os.environ.get("FERRY_INBOX_DIR") or db.get_system_config(
        "ferry_inbox_dir") or os.path.join(config.BACKUP_ROOT, "ferry_inbox")
    for sub in ("", "processed", "failed"):
        os.makedirs(os.path.join(d, sub) if sub else d, exist_ok=True)
    return d


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_one(pkg_path: str) -> dict:
    """处理单个摆渡增量包。返回 {ok, message, record_id}。"""
    d = inbox_dir()
    base = os.path.basename(pkg_path)
    mpath = pkg_path[:-9] + ".manifest.json"
    if not os.path.exists(mpath):
        return {"ok": False, "message": "缺少 manifest（" + base + "）"}
    try:
        with open(mpath, encoding="utf-8") as f:
            mani = json.load(f)
    except Exception as e:
        return {"ok": False, "message": "manifest 解析失败: " + str(e)}

    task_id = int(mani.get("task_id") or 0)
    sha = (mani.get("sha256") or "").lower()
    finished = mani.get("finished_at") or ""
    btype = mani.get("backup_type") or "full"
    if not task_id or not sha:
        return {"ok": False, "message": "manifest 缺 task_id/sha256"}
    if not models.get_task(task_id):
        return {"ok": False, "message": f"任务不存在: {task_id}（请先同步任务定义）"}
    if _sha256(pkg_path) != sha:
        return {"ok": False, "message": "sha256 校验失败（摆渡损坏）"}

    # 解包产物到任务 L1 目录
    t = models.get_task(task_id, include_secret=True)
    import config
    task_dir = os.path.join(config.BACKUP_ROOT,
                            f"{task_id}_{t.get('name', 'task')}")
    os.makedirs(task_dir, exist_ok=True)
    try:
        with tarfile.open(pkg_path, "r:gz") as tf:
            tf.extractall(task_dir)  # noqa: S202（离线受控来源）
    except Exception as e:
        return {"ok": False, "message": "解包失败: " + str(e)}

    # 找解包出的产物文件（取包内记录的 file_name）
    out_file = os.path.join(task_dir, mani.get("file_name") or "")
    if not os.path.isfile(out_file):
        # 兜底：取目录里最新文件
        cands = [os.path.join(task_dir, f) for f in os.listdir(task_dir)]
        out_file = max(cands, key=os.path.getmtime) if cands else ""
    size = os.path.getsize(out_file) if out_file and os.path.isfile(out_file) else 0
    now = db.now_iso()
    rid = models.create_record({
        "task_id": task_id, "backup_type": btype,
        "backup_mode": mani.get("backup_mode") or "logical",
        "status": "success", "backup_path": out_file,
        "size_bytes": size, "started_at": finished or now,
        "finished_at": finished or now,
        "note": "摆渡导入 " + base,
    })
    _log().info("[ferry] 摆渡包入库: %s -> record=%s (%s)",
                base, rid, db.human_size(size))
    try:
        from core import object_catalog
        object_catalog.scan_async(rid, out_file, t.get("db_type"), _log())
        from core import tier_replication
        tier_replication.replicate_async(out_file, t, rid, _log())
        from core import webhooks
        webhooks.emit_async("backup.ferry_imported",
                            {"record_id": rid, "task_id": task_id, "pkg": base})
    except Exception as e:
        _log().warning("[ferry] 后处理失败（不影响入库）: %s", e)
    return {"ok": True, "record_id": rid, "message": "入库成功"}


def ingest_all() -> dict:
    """扫描收件箱，处理全部待处理包（按文件名排序）。"""
    d = inbox_dir()
    pkgs = sorted(f for f in os.listdir(d) if f.endswith(".inc.tar.gz"))
    stat = {"ingested": 0, "failed": 0}
    for pkg in pkgs:
        path = os.path.join(d, pkg)
        try:
            res = ingest_one(path)
        except Exception as e:
            res = {"ok": False, "message": str(e)}
        if res.get("ok"):
            stat["ingested"] += 1
            shutil.move(path, os.path.join(d, "processed", pkg))
            mp = path[:-9] + ".manifest.json"
            if os.path.exists(mp):
                shutil.move(mp, os.path.join(d, "processed", os.path.basename(mp)))
        else:
            stat["failed"] += 1
            _log().warning("[ferry] 摆渡包处理失败: %s — %s",
                           pkg, res.get("message"))
            if res.get("message", "").startswith("任务不存在"):
                continue  # 任务未同步前保留在收件箱等待重试
            shutil.move(path, os.path.join(d, "failed", pkg))
            mp = path[:-9] + ".manifest.json"
            if os.path.exists(mp):
                shutil.move(mp, os.path.join(d, "failed", os.path.basename(mp)))
    if stat["ingested"] or stat["failed"]:
        _log().info("[ferry] 收件箱处理完成: %s", stat)
    return stat


def start_bg_worker() -> None:
    """后台周期扫描（10 分钟），由 app 启动时调用一次。"""

    def _loop():
        import time
        while True:
            try:
                ingest_all()
            except Exception as e:
                _log().warning("[ferry] 周期扫描异常: %s", e)
            time.sleep(600)

    threading.Thread(target=_loop, daemon=True, name="ferry-inbox").start()
