# -*- coding: utf-8 -*-
"""
日志仓库（LogRepository）：实时备份产物的本地 Tier1 落盘布局与生命周期。

目录布局（root 由 capture_kind 决定：db-log → config.RT_LOG_ROOT，file → config.RT_FILE_ROOT）：

    <root>/<task_id>/
        live/                    # 守护进程正在写入的段（未完成，不可入 journal）
        sealed/<YYYYMMDD>/       # 已封存的完整日志段（DB binlog/WAL）
        base/                    # 文件任务的基准全量归档
        inc/<YYYYMMDD>/          # 文件任务的增量归档
        bundles/                 # 聚合上云的 bundle（缓解对象存储写放大）
        state.json               # 守护续传状态（位点 / 最近封存段）

设计要点：
- 所有落盘走「临时文件 + os.replace」原子替换（Windows 防病毒/索引器会锁文件）；
- seal() 只搬运已完整的段，且校验 size > 0 才返回（R9：空包不入 journal）；
- disk_usage() 每 tick 被 Supervisor 调用，用于配额守护（R8）。
"""
import os
import json
import time
import shutil
import tarfile
import tempfile
from datetime import datetime
from typing import Optional

import config
import core.db as db

from .types import KIND_DB_LOG, KIND_FILE

# 单个目录扫描的最大条目数（防止极端情况下 disk_usage 卡死主循环）
_MAX_SCAN_ENTRIES = 200000


def _norm(path: str) -> str:
    """路径归一化：统一正斜杠，便于日志输出与跨平台字典序比较。"""
    return (path or "").replace("\\", "/")


def root_for(capture_kind: str) -> str:
    """按捕获类别返回仓库根目录。"""
    if capture_kind == KIND_DB_LOG:
        return config.RT_LOG_ROOT
    return config.RT_FILE_ROOT


class LogRepository:
    """单个实时任务的本地日志/增量仓库。线程内使用，不共享可变状态。"""

    def __init__(self, task_id: int, capture_kind: str = KIND_FILE,
                 root: str = None, logger=None) -> None:
        self.task_id = int(task_id)
        self.capture_kind = capture_kind or KIND_FILE
        self.root = root or root_for(self.capture_kind)
        self.base = os.path.join(self.root, str(self.task_id))
        self.logger = logger or db.get_logger("rt.repo")
        os.makedirs(self.base, exist_ok=True)

    # ---------------- 目录布局 ----------------
    def _sub(self, *parts: str) -> str:
        path = os.path.join(self.base, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def live_dir(self) -> str:
        """守护进程正在写入的段目录。"""
        return self._sub("live")

    def sealed_dir(self, day: str = None) -> str:
        """已封存段目录。day 缺省为今天（YYYYMMDD）。"""
        return self._sub("sealed", day or time.strftime("%Y%m%d"))

    def base_dir(self) -> str:
        """文件任务基准全量目录。"""
        return self._sub("base")

    def inc_dir(self, day: str = None) -> str:
        """文件任务增量归档目录。"""
        return self._sub("inc", day or time.strftime("%Y%m%d"))

    def bundle_dir(self) -> str:
        """聚合上云 bundle 目录。"""
        return self._sub("bundles")

    def state_path(self) -> str:
        return os.path.join(self.base, "state.json")

    # ---------------- 原子写 ----------------
    @staticmethod
    def atomic_write(writer, dest_path: str, suffix: str = ".tmp") -> None:
        """原子写入：writer(tmp_path) 写临时文件 → os.replace 替换目标。

        与 core/engines/file.py:_atomic_write_archive 同款语义（共享知识 #2）。
        """
        parent = os.path.dirname(dest_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=suffix, dir=parent)
        try:
            os.close(fd)
            writer(tmp_path)
            if os.path.exists(dest_path):
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass  # Windows 上目标被占用时，交给 os.replace 再试一次
            os.replace(tmp_path, dest_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def atomic_move_in(self, src_path: str, dest_path: str) -> None:
        """把外部文件原子搬入仓库：同盘用 os.replace，跨盘先复制到同目录临时文件再替换。"""
        parent = os.path.dirname(dest_path) or "."
        os.makedirs(parent, exist_ok=True)
        try:
            os.replace(src_path, dest_path)
            return
        except OSError:
            pass  # 跨盘 / 目标被占用，走复制路径

        def _copy(tmp_path: str) -> None:
            shutil.copy2(src_path, tmp_path)

        self.atomic_write(_copy, dest_path)
        try:
            os.unlink(src_path)
        except OSError:
            self.logger.warning("[rt.repo] 搬入后删除源文件失败（已忽略）: %s", _norm(src_path))

    # ---------------- 封存 ----------------
    def seal(self, src_path: str, kind: str = "db-log", day: str = None,
             keep_source: bool = False) -> Optional[dict]:
        """把 live/ 下已完整的段封存进 sealed/<day>/。

        Args:
            src_path: 待封存文件的绝对路径。
            kind: db-log | file-inc | base-full，决定落到哪个子目录。
            day: 目标日期分区（YYYYMMDD），缺省今天。
            keep_source: True 时复制而非移动（用于源文件仍被子进程持有的场景）。

        Returns:
            {'path','name','size','checksum','sealed_at'}；
            源文件不存在 / size==0 时返回 None（R9：空包绝不入 journal）。
        """
        if not src_path or not os.path.exists(src_path):
            return None
        try:
            size = os.path.getsize(src_path)
        except OSError:
            return None
        if size <= 0:
            self.logger.warning("[rt.repo] 跳过空段（size=0）: %s", _norm(src_path))
            return None

        if kind == "base-full":
            target_dir = self.base_dir()
        elif kind == "file-inc":
            target_dir = self.inc_dir(day)
        else:
            target_dir = self.sealed_dir(day)

        name = os.path.basename(src_path)
        dest = os.path.join(target_dir, name)
        # 同名冲突时追加序号，避免覆盖已入 journal 的段
        if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(src_path):
            stem, ext = os.path.splitext(name)
            dest = os.path.join(target_dir, f"{stem}.{int(time.time())}{ext}")
            name = os.path.basename(dest)

        if os.path.abspath(dest) != os.path.abspath(src_path):
            if keep_source:
                self.atomic_write(lambda tmp: shutil.copy2(src_path, tmp), dest)
            else:
                self.atomic_move_in(src_path, dest)

        final_size = os.path.getsize(dest) if os.path.exists(dest) else 0
        if final_size <= 0:
            self.logger.error("[rt.repo] 封存后文件为空，已丢弃: %s", _norm(dest))
            try:
                os.unlink(dest)
            except OSError:
                pass
            return None

        return {
            "path": dest,
            "name": name,
            "size": final_size,
            "checksum": db.sha256_file(dest),
            "sealed_at": db.now_iso(),
            "kind": kind,
        }

    # ---------------- 续传状态 ----------------
    def save_state(self, state: dict) -> None:
        """保存守护续传状态（位点 / 最近封存段名）。原子写，损坏不影响下次启动。"""
        payload = dict(state or {})
        payload["saved_at"] = db.now_iso()

        def _write(tmp_path: str) -> None:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)

        try:
            self.atomic_write(_write, self.state_path(), suffix=".json")
        except Exception as exc:
            self.logger.warning("[rt.repo] 保存续传状态失败 task=%s: %s", self.task_id, exc)

    def load_state(self) -> dict:
        """读取续传状态；不存在或损坏时返回空字典（绝不抛异常阻塞守护启动）。"""
        path = self.state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.logger.warning("[rt.repo] 续传状态损坏，按空处理 task=%s: %s",
                                self.task_id, exc)
            return {}

    # ---------------- 聚合上云 ----------------
    def make_bundle(self, points: list, max_mb: int = None) -> Optional[dict]:
        """把多个恢复点的产物聚合成一个 tar.gz bundle（R7：缓解对象存储写放大）。

        Args:
            points: RecoveryPoint 列表或含 object_key/size_bytes 的 dict 列表。
            max_mb: 单个 bundle 的大小上限（MB），缺省 config.RT_UPLOAD_BATCH_MB。

        Returns:
            {'path','size','checksum','members':[rp_id...],'member_keys':[...]}；
            无有效成员时返回 None。
        """
        limit_bytes = int(max_mb or config.RT_UPLOAD_BATCH_MB) * 1024 * 1024
        members, member_keys, acc = [], [], 0
        for point in points or []:
            object_key = getattr(point, "object_key", None)
            rp_id = getattr(point, "id", None)
            size = getattr(point, "size_bytes", None)
            if object_key is None and isinstance(point, dict):
                object_key = point.get("object_key")
                rp_id = point.get("id")
                size = point.get("size_bytes")
            if not object_key or not os.path.exists(object_key):
                continue
            size = int(size or os.path.getsize(object_key))
            if acc and acc + size > limit_bytes:
                break
            members.append({"rp_id": rp_id, "path": object_key,
                            "name": os.path.basename(object_key), "size": size})
            member_keys.append(object_key)
            acc += size
        if not members:
            return None

        bundle_name = f"bundle_{self.task_id}_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
        bundle_path = os.path.join(self.bundle_dir(), bundle_name)

        manifest = {
            "task_id": self.task_id,
            "capture_kind": self.capture_kind,
            "created_at": db.now_iso(),
            "members": [{"rp_id": m["rp_id"], "name": m["name"], "size": m["size"]}
                        for m in members],
        }

        def _write(tmp_path: str) -> None:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for member in members:
                    tar.add(member["path"], arcname=member["name"])
                # 清单随包，恢复时无需查库即可知道成员构成
                manifest_bytes = json.dumps(manifest, ensure_ascii=False,
                                            indent=2).encode("utf-8")
                info = tarfile.TarInfo(name="_manifest.json")
                info.size = len(manifest_bytes)
                info.mtime = int(time.time())
                import io
                tar.addfile(info, io.BytesIO(manifest_bytes))

        self.atomic_write(_write, bundle_path)
        size = os.path.getsize(bundle_path) if os.path.exists(bundle_path) else 0
        if size <= 0:
            self.logger.error("[rt.repo] bundle 生成为空，已丢弃: %s", _norm(bundle_path))
            try:
                os.unlink(bundle_path)
            except OSError:
                pass
            return None
        return {
            "path": bundle_path,
            "name": bundle_name,
            "size": size,
            "checksum": db.sha256_file(bundle_path),
            "members": [m["rp_id"] for m in members if m["rp_id"]],
            "member_keys": member_keys,
            "manifest": manifest,
        }

    # ---------------- 容量守护 ----------------
    def disk_usage(self) -> dict:
        """统计本任务仓库占用。共享知识 #14：80% 告警、100% 暂停封存。"""
        total, files = 0, 0
        for dirpath, _dirs, names in os.walk(self.base):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
                files += 1
                if files >= _MAX_SCAN_ENTRIES:
                    break
            if files >= _MAX_SCAN_ENTRIES:
                break
        quota_bytes = int(config.RT_DISK_QUOTA_GB) * 1024 * 1024 * 1024
        pct = round(total * 100.0 / quota_bytes, 2) if quota_bytes > 0 else 0.0
        # 共享知识 #14：80% 以下 ok / 80%~100% warn / 100% 以上 full
        level = ("full" if pct >= 100.0
                 else ("warn" if pct >= 80.0 else "ok"))
        return {
            "bytes": total,
            "human": db.human_size(total),
            "files": files,
            "quota_gb": config.RT_DISK_QUOTA_GB,
            "used_percent": pct,
            "level": level,
            "over_soft": pct >= 80.0,     # 告警线
            "over_hard": pct >= 100.0,    # 暂停封存线
        }

    # ---------------- 清理 ----------------
    def prune(self, before_ts: float, protect_paths: set = None) -> int:
        """删除 mtime 早于 before_ts 的段/增量文件（base/ 与 bundles/ 不动）。

        Args:
            before_ts: float 时间戳或 ISO 字符串，早于它的文件被删除。
            protect_paths: 纸张对路径集合（仍被有效恢复链引用的产物）。

        Returns:
            实际删除的文件数。
        """
        # 兼容 ISO 字符串入参（测试直接传 _iso(...) 结果）
        if isinstance(before_ts, str):
            try:
                before_ts = datetime.fromisoformat(before_ts).timestamp()
            except (ValueError, TypeError):
                before_ts = 0.0
        before_ts = float(before_ts)
        protect = {os.path.abspath(p) for p in (protect_paths or set())}
        # prune 安全（共享知识 #15）：跳过仍被有效恢复链引用的产物，
        # 避免误删已在 recovery_journal 登记（storage_tier=1）的活跃段。
        try:
            registered = {os.path.abspath(r["object_key"])
                          for r in db.query(
                              "SELECT object_key FROM recovery_journal "
                              "WHERE task_id=? AND object_key IS NOT NULL "
                              "AND object_key <> ''", (self.task_id,))
                          if r.get("object_key")}
        except Exception:
            registered = set()
        removed = 0
        for sub in ("sealed", "inc"):
            root = os.path.join(self.base, sub)
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, names in os.walk(root, topdown=False):
                for name in names:
                    full = os.path.join(dirpath, name)
                    if os.path.abspath(full) in protect or os.path.abspath(full) in registered:
                        continue
                    try:
                        if os.path.getmtime(full) >= before_ts:
                            continue
                        os.unlink(full)
                        removed += 1
                    except OSError as exc:
                        self.logger.warning("[rt.repo] 删除过期文件失败 %s: %s",
                                            _norm(full), exc)
                    # 目录空了顺手删掉，避免留下大量空日期目录
                    try:
                        if dirpath != root and not os.listdir(dirpath):
                            os.rmdir(dirpath)
                    except OSError:
                        pass
        return removed

    def remove_object(self, object_key: str) -> bool:
        """安全删除单个产物：只允许删除本仓库根目录下的路径（防误删源数据）。

        Args:
            object_key: 产物绝对路径。

        Returns:
            True 表示确实删掉了文件；路径越界 / 文件不存在 / 删除失败均返回 False。
        """
        if not object_key:
            return False
        target = os.path.abspath(object_key)
        base = os.path.abspath(self.base)
        try:
            if os.path.commonpath([target, base]) != base:
                self.logger.warning("[rt.repo] 拒绝删除仓库外路径 task=%s: %s",
                                    self.task_id, _norm(object_key))
                return False
        except ValueError:
            # 不同盘符（Windows）→ 必然在仓库外
            return False
        if not os.path.isfile(target):
            return False
        try:
            os.unlink(target)
            return True
        except OSError as exc:
            self.logger.warning("[rt.repo] 删除产物失败 %s: %s", _norm(target), exc)
            return False

    def prune_empty_dirs(self) -> int:
        """清理 sealed/ 与 inc/ 下的空日期分区目录，返回删除数量。"""
        removed = 0
        for sub in ("sealed", "inc"):
            root = os.path.join(self.base, sub)
            if not os.path.isdir(root):
                continue
            for name in list(os.listdir(root)):
                day_dir = os.path.join(root, name)
                if not os.path.isdir(day_dir):
                    continue
                try:
                    if not os.listdir(day_dir):
                        os.rmdir(day_dir)
                        removed += 1
                except OSError:
                    continue
        return removed

    def destroy(self) -> None:
        """删除该任务的整个仓库目录（任务被删除时调用）。"""
        try:
            shutil.rmtree(self.base, ignore_errors=True)
        except Exception as exc:
            self.logger.warning("[rt.repo] 销毁仓库失败 task=%s: %s", self.task_id, exc)
