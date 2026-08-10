# -*- coding: utf-8 -*-
"""
日志仓库目录管理（LogRepository）—— 实时备份产物的本地 Tier1 落盘布局与生命周期。

与 core/rt_backup/repo.py 的 LogRepository 共享"日志仓库"概念，但本版本：
  1. 使用 log_repository DB 表持久化仓库元数据（根路径、子目录、配额、当前体积）；
  2. 提供 init_repo / get_repo / update_size / check_quota / cleanup_expired 等方法，
     与任务需求 T01 的验收标准对齐；
  3. 目录结构按任务描述：repo_root/db_logs/、repo_root/file_inc/、
     repo_root/file_snapshots/rt/<md5>/。

上层 supervisor / file_rt 可直接使用本类，也可沿用 rt_backup.repo.LogRepository
（后者多了 seal / bundle / state.json 等高级功能）。
"""
import hashlib
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional

import core.db as db
import core.models as models


class LogRepository:
    """单个实时任务的日志仓库目录管理器。线程内使用，不共享可变状态。"""

    def __init__(self, task_id: int, repo_root: str = None,
                 logger=None) -> None:
        self.task_id = int(task_id)
        self.logger = logger or db.get_logger("rt.log_repo")
        # repo_root 优先从参数获取，其次从 DB 读取，最后回落 config 默认
        if repo_root:
            self.repo_root = repo_root
        else:
            existing = self.get_repo()
            if existing:
                self.repo_root = existing.get("repo_root") or ""
            else:
                self.repo_root = ""

    # ---------------- 目录初始化 ----------------
    def init_repo(self, repo_root: str = None) -> dict:
        """创建日志仓库目录结构并写入 log_repository DB 记录。

        目录结构：
            repo_root/db_logs/            DB 日志子目录
            repo_root/file_inc/           文件增量子目录
            repo_root/file_snapshots/rt/<md5>/   文件快照基准目录

        Args:
            repo_root: 仓库根路径。若 None 则使用构造时的 repo_root。
                       必须提供（构造时未指定则此处必须指定）。

        Returns:
            log_repository DB 记录 dict。
        """
        root = repo_root or self.repo_root
        if not root:
            raise ValueError(f"task {self.task_id}: init_repo 需要指定 repo_root")
        self.repo_root = root
        root = os.path.normpath(root)

        # 创建目录结构
        db_log_dir = os.path.join(root, "db_logs")
        file_inc_dir = os.path.join(root, "file_inc")
        # 文件快照基准目录使用固定 md5 占位符（实际 md5 由上层 file engine 传入）
        file_snap_dir = os.path.join(root, "file_snapshots", "rt")
        for d in (root, db_log_dir, file_inc_dir, file_snap_dir):
            os.makedirs(d, exist_ok=True)

        # 写入/更新 log_repository DB 记录
        existing = models.get_log_repo(self.task_id)
        if existing:
            models.update_log_repo(self.task_id, {
                "repo_root": root,
                "db_log_dir": db_log_dir,
                "file_inc_dir": file_inc_dir,
            })
            return models.get_log_repo(self.task_id)
        else:
            rid = models.create_log_repo({
                "task_id": self.task_id,
                "repo_root": root,
                "db_log_dir": db_log_dir,
                "file_inc_dir": file_inc_dir,
            })
            row = db.query_one("SELECT * FROM log_repository WHERE id=?", (rid,))
            return dict(row) if row else {}

    # ---------------- DB 查询 ----------------
    def get_repo(self) -> Optional[dict]:
        """从 log_repository 表查本任务的仓库元数据。"""
        return models.get_log_repo(self.task_id)

    # ---------------- 体积统计 ----------------
    def update_size(self) -> int:
        """扫描目录统计当前体积，更新 log_repository.current_size_bytes。

        Returns:
            当前总字节数。
        """
        if not self.repo_root or not os.path.isdir(self.repo_root):
            return 0
        total = 0
        max_entries = 200000  # 防止极端情况卡死主循环
        entries = 0
        for dirpath, _dirs, names in os.walk(self.repo_root):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
                entries += 1
                if entries >= max_entries:
                    break
            if entries >= max_entries:
                break
        models.update_log_repo(self.task_id, {"current_size_bytes": total})
        return total

    # ---------------- 配额检查 ----------------
    def check_quota(self) -> dict:
        """检查是否超配额。

        Returns:
            {'over_quota': bool, 'current_bytes': int, 'quota_bytes': int,
             'used_percent': float, 'level': 'ok'|'warn'|'full'}
            level 判定：80%以下 ok / 80%-100% warn / 100%以上 full。
        """
        repo = self.get_repo()
        if not repo:
            return {"over_quota": False, "current_bytes": 0,
                    "quota_bytes": 214748364800, "used_percent": 0.0,
                    "level": "ok"}
        current = int(repo.get("current_size_bytes") or 0)
        quota = int(repo.get("quota_bytes") or 214748364800)
        # 如果 DB 记录的 current_size_bytes 过期，实时扫描一次
        if current == 0 and self.repo_root and os.path.isdir(self.repo_root):
            current = self.update_size()
        pct = round(current * 100.0 / quota, 2) if quota > 0 else 0.0
        level = ("full" if pct >= 100.0
                 else ("warn" if pct >= 80.0 else "ok"))
        return {
            "over_quota": pct >= 100.0,
            "current_bytes": current,
            "quota_bytes": quota,
            "used_percent": pct,
            "level": level,
        }

    # ---------------- 过期清理 ----------------
    def cleanup_expired(self, retention_days: int,
                        kind: str = "db_log") -> int:
        """按保留天数清理过期文件。

        从 recovery_journal 按 rp_timestamp 排序找最旧的恢复点，
        超过 retention_days 的按 kind 过滤后删除对应 DB 行与磁盘文件。

        Args:
            retention_days: 保留天数。
            kind: 'db_log' / 'file_inc' / 'snapshot'。
                  对应 recovery_journal 的 rp_kind 值：
                  db_log → 'db-log'，file_inc → 'file-inc'，snapshot → 'base-full'。

        Returns:
            删除的恢复点数量。
        """
        retention_days = max(1, int(retention_days or 1))
        cutoff_dt = datetime.now().astimezone() - timedelta(days=retention_days)
        cutoff = cutoff_dt.isoformat(timespec="seconds")

        # 映射 kind 参数到 recovery_journal 的 rp_kind
        kind_map = {
            "db_log": "db-log",
            "file_inc": "file-inc",
            "snapshot": "base-full",
        }
        rp_kind = kind_map.get(kind, kind)

        rows = models.list_recovery_points(
            task_id=self.task_id, kind=rp_kind,
            limit=100000, order="asc")

        removed = 0
        victim_ids = []
        for row in rows:
            pit_at = row.get("pit_at") or ""
            if pit_at >= cutoff:
                continue
            # 不删 base-full 链头（保护恢复链完整性）
            if row.get("rp_kind") in ("base-full", "db-full") and row.get("rp_type") == "full":
                continue
            victim_ids.append(int(row.get("id") or 0))
            # 删除磁盘文件
            object_key = row.get("object_key") or ""
            if object_key and os.path.isfile(object_key):
                # 安全：只删仓库内的文件
                try:
                    if os.path.commonpath([os.path.abspath(object_key),
                                           os.path.abspath(self.repo_root)]) == \
                       os.path.abspath(self.repo_root):
                        os.unlink(object_key)
                except (OSError, ValueError):
                    pass
            removed += 1

        if victim_ids:
            models.delete_recovery_points(victim_ids)

        self.logger.info("[rt.log_repo] task=%s cleanup_expired kind=%s 保留 %d 天, 删除 %d 个恢复点",
                         self.task_id, kind, retention_days, removed)
        # 删除后更新体积
        self.update_size()
        return removed

    # ---------------- 快照目录辅助 ----------------
    def snapshot_dir(self, source_key: str) -> str:
        """返回文件快照基准目录（file_snapshots/rt/<md5>/）。

        Args:
            source_key: 源配置指纹（与 core/engines/file.py 的 _source_config_key 一致）。

        Returns:
            快照目录绝对路径（已创建）。
        """
        md5 = hashlib.md5(source_key.encode("utf-8")).hexdigest() if source_key else "default"
        path = os.path.join(self.repo_root, "file_snapshots", "rt", md5)
        os.makedirs(path, exist_ok=True)
        return path

    # ---------------- 销毁 ----------------
    def destroy(self) -> None:
        """删除该任务的整个仓库目录与 DB 记录（任务被删除时调用）。"""
        if self.repo_root and os.path.isdir(self.repo_root):
            try:
                shutil.rmtree(self.repo_root, ignore_errors=True)
            except Exception as exc:
                self.logger.warning("[rt.log_repo] 销毁仓库目录失败 task=%s: %s",
                                    self.task_id, exc)
        models.delete_rt_task(self.task_id)
        db.execute("DELETE FROM log_repository WHERE task_id=?", (self.task_id,))
