# -*- coding: utf-8 -*-
"""
PostgreSQL 备份引擎。

基于 pg_dump / pg_restore / psql 客户端实现逻辑备份与恢复：
- backup(): 使用 pg_dump 导出数据库，-Fc 自定义格式自带压缩；compress==0 时
  使用 -Fp 纯文本格式。PostgreSQL 逻辑导出无真正增量能力，incremental /
  differential 会回退为 full 并在 message 中给出 WAL 归档/物理备份建议。
- restore(): 根据备份文件后缀选择 pg_restore（.dump）或 psql（.sql）恢复。
- list_databases(): 通过 psql 查询 pg_database 列出非模板库。

安全说明：密码通过 env_extra={"PGPASSWORD": pw} 注入进程环境变量，
绝不出现在命令行参数（argv）中，避免明文泄露。
仅依赖 Python 标准库与系统 PostgreSQL 客户端，不引入第三方包。
"""
import os
import time

from core.engines.base import BackupEngine, BackupType, BackupMode, BackupStatus, BackupResult
import config
import core.db as db


class PostgreSQLEngine(BackupEngine):
    """PostgreSQL 备份引擎，封装 pg_dump / pg_restore / psql。"""

    db_type = "postgresql"
    display_name = "PostgreSQL"
    # 该引擎依赖的客户端可执行文件名（用于 PATH 探测）
    required_clients = ["pg_dump", "pg_restore", "psql"]

    # ------------------------- 备份 -------------------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        mode = self.backup_mode
        if mode == BackupMode.PHYSICAL:
            return self._try_pg_fallback(lambda: self._backup_physical(backup_type), backup_type, "物理备份")
        return self._try_pg_fallback(lambda: self._backup_logical(backup_type), backup_type, "逻辑备份")

    def _try_pg_fallback(self, fn, backup_type, label):
        try:
            result = fn()
            if result.success:
                return result
            reason = result.message or "未知"
        except Exception as e:
            reason = str(e)
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host:
            self.logger.info("[%s] %s失败(%s)，SSH远程", self.task_name, label, reason)
            try:
                data = remote_dump.remote_db_dump(self.task, ssh_host, "postgresql", int(self.task.get("compress") or 0))
                ext = ".dump.gz" if int(self.task.get("compress") or 0) else ".sql"
                return self._write_dump_file(data, backup_type, ssh_host, ext, "pg_dump")
            except Exception as e:
                reason = f"本机与SSH均失败: {e}"
        return BackupResult(success=False, status=BackupStatus.FAILED, message=reason)

    # ------------------ 物理备份 (pg_basebackup) ------------------
    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 5432
        user = self.task.get("username") or "postgres"
        pw = db.decrypt_secret(self.task.get("password") or "")
        out_dir = self._output_dir()
        ts = self._timestamp()

        target = os.path.join(out_dir, f"pg_basebackup_{ts}")
        os.makedirs(target, exist_ok=True)
        cmd = ["pg_basebackup", "-h", host, "-p", str(port), "-U", user, "-D", target, "-Ft", "-z",
               "--checkpoint=fast", "--no-password"]
        env = {"PGPASSWORD": pw} if pw else None
        start = time.time()
        ret = subprocess.run(cmd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=7200)
        dur = round(time.time()-start, 3)
        if ret.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"pg_basebackup 失败: {ret.stderr[:500]}")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=target, duration_sec=dur, stdout=ret.stdout,
                            message=f"PostgreSQL 物理备份(pg_basebackup)成功")

    # ------------------ 逻辑备份 (pg_dump) ------------------
    def _backup_logical(self, backup_type: BackupType) -> BackupResult:
        return self._backup_local(backup_type)  # 已有逻辑备份，增强粒度由 extra_options 控制

    def _backup_local(self, backup_type: BackupType) -> BackupResult:
        """本机 pg_dump 真实备份。"""
        # 构造输出目录与文件名
        out_dir = self._output_dir()
        ts = self._timestamp()
        host = self.task.get("host")
        port = self.task.get("port")
        user = self.task.get("username")
        pw = db.decrypt_secret(self.task.get("password") or "")
        db_name = self.task.get("db_name")
        compress = int(self.task.get("compress") or 0)

        # 4) 增量/差异回退为全量（逻辑备份无真正增量），并在 message 注明
        real_type = backup_type
        fallback_note = ""
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
            real_type = BackupType.FULL
            fallback_note = (
                "（incremental/differential 逻辑备份无真正增量，已回退为 full；"
                "建议改用 WAL 归档 + 流式物理备份 pg_basebackup 实现增量）"
            )

        # 5) 根据 compress 决定格式与文件名后缀
        if compress == 0:
            # 纯文本格式（-Fp），不压缩
            out_path = os.path.join(out_dir, f"{ts}.sql")
            fmt_flag = "-Fp"
        else:
            # 自定义格式（-Fc），自带压缩
            out_path = os.path.join(out_dir, f"{ts}.dump")
            fmt_flag = "-Fc"

        # 6) 组装 pg_dump 命令（密码经 PGPASSWORD 环境变量注入）
        cmd = [
            "pg_dump",
            "--host", str(host),
            "--port", str(port),
            "--username", str(user),
            "-d", str(db_name),
            fmt_flag,
            "-f", out_path,
        ]
        # 追加用户自定义扩展选项（extra_options 为 JSON 字符串）
        extra = self._parse_extra_options()
        if extra:
            cmd.extend(extra)

        env_extra = {"PGPASSWORD": pw} if pw else None

        start = time.time()
        ret = self._run(cmd, env_extra=env_extra, timeout=3600)
        duration = time.time() - start

        if ret["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=None,
                size_bytes=0,
                duration_sec=duration,
                stdout=ret["stdout"],
                stderr=ret["stderr"],
                simulated=False,
                checksum="",
                message=f"pg_dump 执行失败(rc={ret['returncode']}): {ret['stderr']}",
            )

        # 7) 计算大小与校验和（文件用 sha256；目录则仅算总大小，checksum 留空）
        size, checksum = self._compute_size_checksum(out_path)

        message = f"PostgreSQL {real_type.value} 备份成功"
        if fallback_note:
            message += " " + fallback_note

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=out_path,
            size_bytes=size,
            duration_sec=duration,
            stdout=ret["stdout"],
            stderr=ret["stderr"],
            simulated=False,
            checksum=checksum,
            message=message,
        )

    # ------------------------- 恢复 -------------------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行 PostgreSQL 逻辑恢复。"""
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 0) 跨主机恢复
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_db = kwargs.get("target_db") or self.task.get("db_name") or ""
            return self._try_cross_host_restore(backup_path, target_host_info, target_db)

        # 1) 先尝试本机直接恢复
        result = self._restore_local(backup_path, **kwargs)
        if result.success:
            return result

        # 2) 本机失败 -> 尝试通过 SSH 在数据库服务器执行恢复
        reason = (result.message or "未知错误")
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host and os.path.exists(backup_path):
            try:
                with open(backup_path, "rb") as f:
                    dump_bytes = f.read()
                is_custom = backup_path.endswith(".dump")
                remote_dump.remote_db_restore(
                    self.task, ssh_host, "postgresql", dump_bytes, is_custom=is_custom)
                target_db = kwargs.get("target_db") or self.task.get("db_name")
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path,
                    message="通过 SSH 在数据库服务器恢复成功 -> " + str(target_db or ""))
            except Exception as e:
                self.logger.error("[%s] 远程恢复也失败: %s", self.task_name, e)
                reason = f"本机与远程恢复均失败: {e}"

        # 3) 返回真实错误
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            backup_path=backup_path, message=reason)

    def _restore_local(self, backup_path: str, **kwargs) -> BackupResult:
        """本机 psql/pg_restore 真实恢复。"""
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                message=f"备份文件不存在: {backup_path}",
            )

        host = self.task.get("host")
        port = self.task.get("port")
        user = self.task.get("username")
        pw = db.decrypt_secret(self.task.get("password") or "")
        # 目标库：优先使用调用方指定，否则回退到任务原始库名
        target_db = kwargs.get("target_db") or self.task.get("db_name")

        # 3) 按文件后缀选择恢复方式
        if backup_path.endswith(".dump"):
            # 自定义格式用 pg_restore，-c 先清理对象、-C 创建库
            cmd = [
                "pg_restore",
                "--host", str(host),
                "--port", str(port),
                "--username", str(user),
                "--dbname", str(target_db),
                "-c", "-C",
                backup_path,
            ]
        elif backup_path.endswith(".sql"):
            # 纯文本格式用 psql 执行 SQL 脚本
            cmd = [
                "psql",
                "--host", str(host),
                "--port", str(port),
                "--username", str(user),
                "-d", str(target_db),
                "-f", backup_path,
            ]
        else:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                message=f"无法识别的备份文件类型(需 .dump 或 .sql): {backup_path}",
            )

        # 追加用户自定义扩展选项
        extra = self._parse_extra_options()
        if extra:
            cmd.extend(extra)

        env_extra = {"PGPASSWORD": pw} if pw else None

        start = time.time()
        ret = self._run(cmd, env_extra=env_extra, timeout=3600)
        duration = time.time() - start

        if ret["returncode"] != 0:
            return BackupResult(
                success=False,
                status=BackupStatus.FAILED,
                backup_path=backup_path,
                size_bytes=0,
                duration_sec=duration,
                stdout=ret["stdout"],
                stderr=ret["stderr"],
                simulated=False,
                checksum="",
                message=f"恢复失败(rc={ret['returncode']}): {ret['stderr']}",
            )

        return BackupResult(
            success=True,
            status=BackupStatus.SUCCESS,
            backup_path=backup_path,
            size_bytes=os.path.getsize(backup_path) if os.path.isfile(backup_path) else 0,
            duration_sec=duration,
            stdout=ret["stdout"],
            stderr=ret["stderr"],
            simulated=False,
            checksum="",
            message=f"PostgreSQL 恢复成功 -> {target_db}",
        )

    # ------------------------- 列出数据库 -------------------------
    def list_databases(self) -> list:
        """列出可备份的数据库（无 Agent 模式：优先 SSH 远程 psql）。"""
        # 1) 尝试 SSH 远程
        try:
            from core import remote_dump
            dbs = remote_dump.remote_list_databases(self.task, "postgresql")
            if dbs:
                skip = {"template0", "template1"}
                return [d for d in dbs if d not in skip]
        except Exception as e:
            self.logger.info("[%s] SSH 远程 list_databases 失败，本地兜底: %s", self.task_name, e)
        # 2) 本地兜底
        ok, _ = self.check_client()
        if not ok:
            self.logger.warning("[%s] list_databases 跳过", self.task_name)
            return []

        host = self.task.get("host")
        port = self.task.get("port")
        user = self.task.get("username")
        pw = db.decrypt_secret(self.task.get("password") or "")

        cmd = [
            "psql",
            "--host", str(host),
            "--port", str(port),
            "--username", str(user),
            "-t",  # 仅输出元组（去掉表头/脚注）
            "-c",
            "SELECT datname FROM pg_database WHERE NOT datistemplate",
        ]
        env_extra = {"PGPASSWORD": pw} if pw else None
        ret = self._run(cmd, env_extra=env_extra, timeout=600)
        if ret["returncode"] != 0:
            self.logger.warning("[%s] 列举数据库失败: %s", self.task_name, ret["stderr"])
            return []

        # 解析 stdout：每行一个库名，去除空白与多余字符
        dbs = []
        for line in ret["stdout"].splitlines():
            name = line.strip()
            if name:
                dbs.append(name)
        return dbs

    # ------------------------- 内部辅助 -------------------------
    def _parse_extra_options(self):
        """解析 task 的 extra_options(JSON 字符串) 为命令参数列表。"""
        raw = self.task.get("extra_options")
        if not raw:
            return []
        try:
            data = __import__("json").loads(raw)
        except Exception as e:
            self.logger.warning("[%s] extra_options 解析失败: %s", self.task_name, e)
            return []
        if isinstance(data, dict):
            args = []
            for k, v in data.items():
                # 支持 {"key": "value"} 与 {"--flag": None} 形式
                if v is None or v == "":
                    args.append(str(k))
                else:
                    args.append(str(k))
                    args.append(str(v))
            return args
        if isinstance(data, list):
            return [str(x) for x in data]
        return []

    def _compute_size_checksum(self, path: str):
        """计算备份路径的大小与校验和：文件 -> sha256；目录 -> 总大小，checksum 留空。"""
        if os.path.isfile(path):
            size = os.path.getsize(path)
            checksum = db.sha256_file(path)
            return size, checksum
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
            return total, ""
        # 路径不存在（理论上不该发生）
        return 0, ""
