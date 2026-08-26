# -*- coding: utf-8 -*-
"""
Redis 备份/恢复引擎。

Redis 是内存数据库，其持久化以 RDB 快照为主。本引擎通过 redis-cli 的
`--rdb` 选项直接将远程（或本地）Redis 实例的 RDB 快照文件拉取到本地存储目录，
作为“全量/快照”备份。Redis 没有原生的物理增量备份（逻辑增量需 AOF 或主从复制），
因此 incremental / differential 类型统一回退为 RDB 全量快照，并在结果 message 中注明。

恢复说明：
Redis 不能通过 redis-cli 直接“灌入”一个 rdb 文件；常规做法是把 rdb 文件替换到
目标 Redis 的数据目录（默认 dump.rdb）并重启实例以加载。本引擎据此实现 restore：
将本地 rdb 经由 scp 复制到目标主机的目标目录，并提示在目标端重启 Redis 完成加载。

仅依赖 Python 标准库与 redis-cli 客户端；不使用任何第三方库。
"""

import os

import config
from core.engines.base import (
    BackupEngine, BackupType, BackupStatus, BackupResult
)
import core.db as db


class RedisEngine(BackupEngine):
    """Redis 数据库备份/恢复引擎（基于 redis-cli --rdb 快照）。"""

    db_type = "redis"
    display_name = "Redis"
    required_clients = ["redis-cli"]
    # 物理备份：外部插件 redis-tools
    physical_external_plugins = ["redis-tools"]

    def _redis_cli_args(self):
        """构造 redis-cli 连接基础参数（不含密码，密码走环境变量）。"""
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 6379
        return ["redis-cli", "-h", str(host), "-p", str(port)]

    def backup(self, backup_type: BackupType) -> BackupResult:
        """执行 Redis 备份（RDB 快照）。

        优先在 SSH 备份机/数据库服务器执行 redis-cli --rdb，失败再回退本机。
        """
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        # 统一处理增量/差异 -> 快照
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
            backup_type = BackupType.SNAPSHOT

        return self._try_remote_then_local(
            lambda ssh_host: self._backup_remote(ssh_host, backup_type),
            lambda: self._backup_local(backup_type),
            "Redis RDB 备份(redis-cli)",
        )

    def _backup_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机执行 redis-cli --rdb -，把 RDB 数据流拉回到本地落盘。"""
        from core import remote_dump
        import time
        t0 = time.time()
        data, _ = remote_dump.remote_db_dump(self.task, ssh_host, "redis")
        duration = round(time.time() - t0, 3)
        out_dir = self._output_dir()
        ts = self._timestamp()
        rdb_path = os.path.join(out_dir, "%s.rdb" % ts)
        with open(rdb_path, "wb") as f:
            f.write(data)
        size = os.path.getsize(rdb_path)
        checksum = db.sha256_file(rdb_path)
        self.logger.info("[%s] Redis 远程备份完成: %s (%d bytes)", self.task_name, rdb_path, size)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=rdb_path, size_bytes=size, duration_sec=duration,
            simulated=False, checksum=checksum,
            message="Redis RDB 远程快照备份成功")

    def _backup_local(self, backup_type: BackupType) -> BackupResult:
        """本机执行 Redis 备份（RDB 快照）。"""
        # 客户端可用性检查
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="备份失败: " + detail)

        out_dir = self._output_dir()
        ts = self._timestamp()
        note = ""
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
            note = ("Redis 逻辑增量需 AOF/主从复制，RDB 为全量快照；"
                    "已将 %s 回退为 RDB 快照备份。" % backup_type.value)
            backup_type = BackupType.SNAPSHOT
        rdb_path = os.path.join(out_dir, "%s.rdb" % ts)

        cmd = self._redis_cli_args() + ["--rdb", rdb_path]
        env_extra = {"REDISCLI_AUTH": self.task.get("password") or ""}

        t0 = __import__("time").time()
        res = self._run(cmd, env_extra=env_extra, timeout=3600)
        duration = __import__("time").time() - t0

        if res["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, size_bytes=0, duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="备份失败: redis-cli 返回非零码 %s" % res["returncode"])

        if not os.path.exists(rdb_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, size_bytes=0, duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="备份失败: 未生成 RDB 文件 %s" % rdb_path)

        size = os.path.getsize(rdb_path)
        checksum = db.sha256_file(rdb_path)
        msg = "Redis RDB 快照备份成功"
        if note:
            msg += "；" + note
        self.logger.info("[%s] Redis 备份完成: %s (%d bytes)", self.task_name, rdb_path, size)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=rdb_path, size_bytes=size, duration_sec=duration,
            stdout=res["stdout"], stderr=res["stderr"],
            simulated=False, checksum=checksum, message=msg)

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行 Redis 恢复。

        Redis 不能经 redis-cli 直接灌入 rdb 文件。本实现将本地 rdb 复制到
        目标 Redis 数据目录并重启加载。

        kwargs:
            target_host_info: 跨主机恢复时必填（SFTP+SSH 推送 .rdb）
            target_db:        目标库实例索引（默认 0）
            target_host: 目标主机（如 user@host）
            target_db:   目标 rdb 目录或完整路径（替换其中的 dump.rdb）
        """
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 跨主机恢复：SFTP 推 .rdb + redis 加载
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_db = kwargs.get("target_db") or "0"
            return self._try_cross_host_restore(backup_path, target_host_info, target_db)

        target_host = kwargs.get("target_host")
        target_dir = kwargs.get("target_db")

        # 2. 必须同时提供目标主机与目标目录
        if not (target_host and target_dir):
            return BackupResult(
                success=False,
                message="请提供目标主机与 rdb 目录(target_host, target_db)")

        # 3. 通过 scp 将 rdb 复制到目标主机的目标目录（重命名为 dump.rdb）
        remote_path = target_dir.rstrip("/") + "/dump.rdb"
        cmd = ["sh", "-c", "scp %s %s:%s" % (backup_path, target_host, remote_path)]
        t0 = __import__("time").time()
        res = self._run(cmd, timeout=3600)
        duration = __import__("time").time() - t0

        if res["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="恢复失败: scp 返回非零码 %s" % res["returncode"])

        self.logger.info("[%s] Redis rdb 已复制到 %s:%s", self.task_name, target_host, remote_path)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=backup_path, duration_sec=duration,
            stdout=res["stdout"], stderr=res["stderr"],
            message="恢复文件已推送至 %s:%s，请在目标端重启 Redis 以加载该 rdb" % (target_host, remote_path))

    def list_databases(self) -> list:
        """列出可备份的 Redis 库。

        简化实现：返回空列表（可按需通过 redis-cli --scan 列出 key 空间）。
        """
        return []
