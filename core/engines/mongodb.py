# -*- coding: utf-8 -*-
"""
MongoDB 备份引擎实现。

继承 core.engines.base.BackupEngine，基于 mongodump / mongorestore 客户端
完成逻辑备份与恢复。备份以目录方式输出（--out {dir}/{ts}），恢复时直接将
目录还原到目标库（--drop）。

说明：
- MongoDB 官方工具采用在命令行参数中直接传递 --password 的惯例（而非通过
  环境变量读取），因此本引擎在构造命令时显式加入 --password {pw}。
- 增量/差异备份在 MongoDB 中没有等效的 mongodump 原生选项，统一回退为全量
  备份（full），并在 message 中提示建议改用 oplog / 时间点恢复方案。
"""
import os
import json

import config
import core.db as db
from core.engines.base import (
    BackupEngine, BackupType, BackupStatus, BackupResult
)


class MongoEngine(BackupEngine):
    """MongoDB 备份恢复引擎。"""

    db_type = "mongodb"
    display_name = "MongoDB"
    required_clients = ["mongodump", "mongorestore"]
    # 物理备份：外部插件 mongodb-database-tools
    physical_external_plugins = ["mongodb-database-tools"]

    def _parse_extra_options(self) -> dict:
        """解析 task 中的 extra_options（JSON 字符串），返回字典。"""
        raw = self.task.get("extra_options") or "{}"
        try:
            opts = json.loads(raw)
        except Exception:
            opts = {}
        return opts if isinstance(opts, dict) else {}

    def _build_auth_args(self, cmd: list) -> list:
        """根据 extra_options.authSource 追加 --authenticationDatabase 参数。"""
        opts = self._parse_extra_options()
        auth_source = opts.get("authSource")
        if auth_source:
            cmd += ["--authenticationDatabase", str(auth_source)]
        return cmd

    def backup(self, backup_type: BackupType) -> BackupResult:
        """执行 MongoDB 逻辑备份。

        优先在 SSH 备份机/数据库服务器执行 mongodump --archive，失败再回退本机。
        """
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        return self._try_remote_then_local(
            lambda ssh_host: self._backup_remote(ssh_host, backup_type),
            lambda: self._backup_local(backup_type),
            "MongoDB 逻辑备份(mongodump)",
        )

    def _backup_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机执行 mongodump --archive，把归档流拉回到本地落盘。"""
        from core import remote_dump
        import time
        # 与本地逻辑备份对齐：统一由全局 COMPRESS_BY_DEFAULT 控制，远端强制 zstd
        enable = getattr(config, "COMPRESS_BY_DEFAULT", True)
        compress = 1 if enable else 0
        t0 = time.time()
        data, compressed = remote_dump.remote_db_dump(self.task, ssh_host, "mongodb", compress)

        duration = round(time.time() - t0, 3)
        out_dir = self._output_dir()
        ts = self._timestamp()
        ext = ".archive.zst" if compressed else ".archive"
        archive_path = os.path.join(out_dir, "%s%s" % (ts, ext))
        with open(archive_path, "wb") as f:
            f.write(data)
        size = os.path.getsize(archive_path)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=archive_path, size_bytes=size, duration_sec=duration,
            compress_algo="zstd" if compress else "none",
            simulated=False, checksum="",
            message="MongoDB 远程归档备份成功(mongodump --archive)")

    def _backup_local(self, backup_type: BackupType) -> BackupResult:
        """本机执行 MongoDB 逻辑备份。"""
        # 1. 客户端检测
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="MongoDB 客户端检测失败: " + detail)

        # 2. 构造 mongodump 命令
        host = self.task.get("host")
        port = int(self.task.get("port") or 27017)
        username = self.task.get("username") or ""
        password = self.task.get("password") or ""
        db_name = self.task.get("db_name") or ""
        compress = int(self.task.get("compress") or 0)

        out_dir = self._output_dir()
        ts = self._timestamp()
        target_dir = os.path.join(out_dir, ts)

        cmd = [
            "mongodump",
            "--host", str(host),
            "--port", str(port),
            "--username", str(username),
            "--password", str(password),
            "--db", str(db_name),
        ]
        # Mongo 惯例：密码以 --password 参数明文传入命令行
        cmd = self._build_auth_args(cmd)
        cmd += ["--out", target_dir]
        if compress == 1:
            # 目录模式下 --gzip 会对每个集合文件单独压缩
            cmd += ["--gzip"]

        # 增量/差异备份在 mongodump 无原生支持，统一回退为全量
        note = ""
        if backup_type.value in ("incremental", "differential"):
            note = ("MongoDB 不支持增量/差异逻辑备份，已回退为全量备份；"
                    "建议改用 oplog 重放或时间点恢复(PITR)方案。")

        # 4. 执行命令
        start = __import__("time").time()
        res = self._run(cmd)
        duration = __import__("time").time() - start

        # 5. 按 returncode 判定结果
        if res["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=target_dir if os.path.isdir(target_dir) else None,
                duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="mongodump 执行失败(返回码=%s)" % res["returncode"])

        # 计算输出目录总大小（os.walk 求和）
        size_bytes = 0
        if os.path.isdir(target_dir):
            for root, _dirs, files in os.walk(target_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        size_bytes += os.path.getsize(fp)
                    except OSError:
                        pass

        msg = "mongodump 全量备份成功；密码以 --password 参数传入命令行(mongo 惯例)。"
        if note:
            msg = note + " " + msg

        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=target_dir, size_bytes=size_bytes,
            duration_sec=duration,
            stdout=res["stdout"], stderr=res["stderr"],
            simulated=False, checksum="",
            message=msg)

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行 MongoDB 逻辑恢复。

        仿真检测 → 客户端检测 → 构造 mongorestore 命令 → 执行 → 按 returncode 判定。
        """
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 跨主机恢复：SFTP 推归档 + 远端 mongorestore
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            return self._try_cross_host_restore(backup_path, target_host_info,
                                                 kwargs.get("target_db") or "")

        # 1. 客户端检测
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="MongoDB 客户端检测失败: " + detail)

        # 3. 构造 mongorestore 命令
        host = self.task.get("host")
        port = int(self.task.get("port") or 27017)
        username = self.task.get("username") or ""
        password = self.task.get("password") or ""
        db_name = self.task.get("db_name") or ""
        target_db = kwargs.get("target_db") or db_name
        compress = int(self.task.get("compress") or 0)

        is_archive = (
            str(backup_path).endswith(".archive")
            or str(backup_path).endswith(".archive.gz")
            or str(backup_path).endswith(".archive.zst")
        )
        # 外部压缩的归档需先解压再喂给 mongorestore --archive
        if str(backup_path).endswith(".archive.zst"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".archive", delete=False)
            tmp_path = tmp.name
            tmp.close()
            dec = self.pipe_decompress("zstd")
            rc = subprocess.run(dec + [backup_path, tmp_path])
            if rc.returncode != 0:
                os.unlink(tmp_path)
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path,
                    message="MongoDB 备份解压 zstd 失败(rc=%s)" % rc.returncode)
            restore_archive = tmp_path
        elif str(backup_path).endswith(".archive.gz"):
            # 兼容性：历史远程 gzip 压缩归档（升级前产物）
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".archive", delete=False)
            tmp_path = tmp.name
            tmp.close()
            dec = self.pipe_decompress("gzip")
            rc = subprocess.run(dec + [backup_path, tmp_path])
            if rc.returncode != 0:
                os.unlink(tmp_path)
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path,
                    message="MongoDB 备份解压 gzip 失败(rc=%s)" % rc.returncode)
            restore_archive = tmp_path
        else:
            restore_archive = backup_path

        cmd = [
            "mongorestore",
            "--host", str(host),
            "--port", str(port),
            "--username", str(username),
            "--password", str(password),
            "--db", str(target_db),
            "--drop",
        ]
        if is_archive:
            cmd += ["--archive=%s" % restore_archive]
        else:
            cmd += [restore_archive]
        cmd = self._build_auth_args(cmd)

        # 4. 执行命令
        start = __import__("time").time()
        res = self._run(cmd)
        duration = __import__("time").time() - start

        # 5. 按 returncode 判定结果
        if res["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="mongorestore 执行失败(返回码=%s)" % res["returncode"])

        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=backup_path, duration_sec=duration,
            stdout=res["stdout"], stderr=res["stderr"],
            simulated=False, checksum="",
            message=("mongorestore 恢复成功；目标库=%s；密码以 --password 参数传入"
                     "命令行(mongo 惯例)。" % target_db))

    def list_databases(self) -> list:
        """列出可备份的数据库名。

        mongosh/mongo 列举库较为复杂，此处简化返回空列表。
        """
        return []
