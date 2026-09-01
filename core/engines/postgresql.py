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
import shlex
import shutil
import subprocess
import json

from core.engines.base import BackupEngine, BackupType, BackupMode, BackupStatus, BackupResult
import config
import core.db as db


class PostgreSQLEngine(BackupEngine):
    """PostgreSQL 备份引擎，封装 pg_dump / pg_restore / psql。"""

    db_type = "postgresql"
    display_name = "PostgreSQL"
    # 该引擎依赖的客户端可执行文件名（用于 PATH 探测）
    required_clients = ["pg_dump", "pg_restore", "psql"]
    # 物理备份：PostgreSQL 自带 pg_basebackup 工具
    physical_bundled_tools = ["pg_basebackup"]

    # ------------------------- 备份 -------------------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        mode = self.backup_mode
        if mode == BackupMode.PHYSICAL:
            # 物理备份：优先 SSH 远端 pg_basebackup，失败再回退本机
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "PostgreSQL 物理备份(pg_basebackup)",
            )
        # 逻辑备份：优先 SSH 远程执行，失败再回退本机
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "PostgreSQL 逻辑备份(pg_dump)",
        )

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
        cmd.extend(self._pg_basebackup_extra_args())
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

    def _backup_physical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """物理备份：通过 SSH 在远端数据库服务器执行 pg_basebackup（-Ft -z 生成
        tar.gz），再经 SFTP 拉回本机落盘并计算 size/sha256。

        复用 core.remote_dump.remote_physical_backup，避免路径/端口写死；
        PostgreSQL 的额外参数 key 为 pg_basebackup_extra_args。
        """
        from core import remote_dump
        client = remote_dump._connect(ssh_host)
        res = remote_dump.remote_physical_backup(
            self.task, ssh_host,
            tool="pg_basebackup", default_port=5432, default_user="postgres",
            extra_args_key="pg_basebackup_extra_args", tool_label="pg_basebackup",
        )
        if not res["ok"]:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=res["message"],
                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""))

        out_dir = self._output_dir()
        pieces = remote_dump._pull_remote_tars(client, res["remote_dir"], out_dir)
        if not pieces:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                stdout=res.get("stdout", ""), stderr=res.get("stderr", ""),
                message=f"远端 pg_basebackup 执行成功但未在 {res['remote_dir']} 找到 *.tar[.gz] 产物。")

        total_size = sum(sz for _, sz in pieces)
        first_local = pieces[0][0]
        checksum = db.sha256_file(first_local)
        hk = ssh_host.get("host_key", "remote")
        msg = (f"通过 SSH 在 {hk} 执行 pg_basebackup 物理备份成功，"
               f"已拉回 {len(pieces)} 个 tar 包，共 {db.human_size(total_size)}"
               f"（主包: {os.path.basename(first_local)}）")
        self.logger.info("[%s] %s", self.task_name, msg)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=first_local, size_bytes=total_size,
            duration_sec=0, stdout=res.get("stdout", ""), stderr=res.get("stderr", ""),
            simulated=False, checksum=checksum, message=msg)

    # ------------------ 逻辑备份 (pg_dump) ------------------
    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机/数据库服务器上执行 pg_dump，把流拉回到本地落盘。"""
        from core import remote_dump
        comp = int(self.task.get("compress") or 0)
        data, compressed, fmt = remote_dump.remote_db_dump(self.task, ssh_host, "postgresql", comp)
        if fmt == "multi-db-tar":
            # 全实例：逐库 tar.gz（远端已 gzip，manifest.json 标注库清单）
            res = self._write_dump_file(data, backup_type, ssh_host, ".tar.gz", "pg_dump")
            res.compress_algo = "gzip"
            return res
        if fmt == "dumpall":
            res = self._write_dump_file(data, backup_type, ssh_host, ".sql", "pg_dumpall")
            res.compress_algo = "none"
            return res
        # 单库：远程 pg_dump 用 -Fc 自带压缩，落盘为 .dump（不再外挂 gzip）
        ext = ".dump" if compressed else ".sql"
        res = self._write_dump_file(data, backup_type, ssh_host, ext, "pg_dump")
        res.compress_algo = "zlib" if compressed else "none"
        return res

    def _backup_logical_local(self, backup_type: BackupType) -> BackupResult:
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

        # 3.5) 全实例（db_name 为空）：pg_dump 是单库工具，不存在
        #      --all-databases 参数，改为逐库 tar.gz + globals + manifest
        extra_eo = self._parse_extra_options()
        if (not db_name and not extra_eo.get("schemas")
                and not extra_eo.get("tables")):
            return self._backup_full_instance_local(backup_type)

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

    # ------------------------------------------------------------------
    # 全实例（逐库 tar）备份/恢复 —— db_name 为空时的路径
    # ------------------------------------------------------------------
    def _backup_full_instance_local(self, backup_type: BackupType) -> BackupResult:
        """全实例逻辑备份：枚举库 → 逐库 pg_dump + pg_dumpall 全局对象 → tar.gz。

        PG 系没有 --all-databases；逐库快照各自一致，globals 单独导出。
        """
        from core import logical_full
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{self._timestamp()}.tar.gz")
        dump_tool = shutil.which("pg_dump") or "pg_dump"
        query_tool = shutil.which("psql") or "psql"
        dumpall_tool = shutil.which("pg_dumpall") or ""
        try:
            manifest = logical_full.backup_full_instance(
                "postgresql",
                host=self.task.get("host"), port=self.task.get("port"),
                user=self.task.get("username"),
                password=db.decrypt_secret(self.task.get("password") or ""),
                dump_tool=dump_tool, out_path=out_path,
                query_tool=query_tool, dumpall_tool=dumpall_tool)
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, simulated=False,
                message=f"PostgreSQL 全实例备份失败: {e}")
        size, checksum = self._compute_size_checksum(out_path)
        dbs_txt = ", ".join(manifest.get("databases") or [])
        msg = (f"PostgreSQL 全实例备份成功: {len(manifest['databases'])} 个库"
               f"（{dbs_txt}）+ 全局对象({manifest.get('globals')})，"
               f"产物 {out_path}")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=out_path, size_bytes=size, duration_sec=0,
            simulated=False, checksum=checksum, message=msg)

    def _restore_full_instance_local(self, backup_path: str) -> BackupResult:
        """全实例恢复：解包 → globals → 缺失库自动建库 → 逐库 pg_restore。"""
        from core import logical_full
        restore_tool = shutil.which("pg_restore") or "pg_restore"
        query_tool = shutil.which("psql") or "psql"
        try:
            result = logical_full.restore_full_instance(
                "postgresql",
                host=self.task.get("host"), port=self.task.get("port"),
                user=self.task.get("username"),
                password=db.decrypt_secret(self.task.get("password") or ""),
                backup_path=backup_path,
                restore_tool=restore_tool, query_tool=query_tool)
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message=f"PostgreSQL 全实例恢复失败: {e}")
        dbs_txt = ", ".join(result.get("restored") or [])
        msg = (f"PostgreSQL 全实例恢复成功: {len(result['restored'])} 个库"
               f"（{dbs_txt}），全局对象{'已恢复' if result.get('globals') else '跳过/已存在'}")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=backup_path, duration_sec=0,
            simulated=False, checksum="", message=msg)

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
            return self._try_cross_host_restore(
                backup_path, target_host_info, target_db, kwargs.get("target_port"))

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

    def _pg_db_exists(self, host, port, user, db_name, env_extra) -> bool:
        """检查目标库是否已存在。"""
        chk = self._run(
            ["psql", "--host", str(host), "--port", str(port),
             "--username", str(user), "-d", "postgres", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname = '{db_name.replace(chr(39), chr(39)*2)}'"],
            env_extra=env_extra, timeout=120)
        return chk["returncode"] == 0 and chk["stdout"].strip() == "1"

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

        env_extra = {"PGPASSWORD": pw} if pw else None

        # 2.5) 全实例 tar 包（multi-db-tar）：逐库恢复，不适用单库流程
        if backup_path.endswith((".tar.gz", ".tgz")):
            return self._restore_full_instance_local(backup_path)

        # 3) 确保目标库存在（PostgreSQL 必须连接一个已存在的库再恢复）。
        #    先 DROP 再 CREATE：保证恢复是干净、可重复的（与 -C 相比更可靠，
        #    -C 在目标库同名已存在时会因 "cannot drop the currently open
        #    database" 而无法重建，导致旧对象残留、恢复失败）。
        safe_db = target_db.replace('"', '""')
        if not self._pg_db_exists(host, port, user, target_db, env_extra):
            chk = self._run(
                ["psql", "--host", str(host), "--port", str(port),
                 "--username", str(user), "-d", "postgres",
                 "-c", f'CREATE DATABASE "{safe_db}"'],
                env_extra=env_extra, timeout=180)
            if chk["returncode"] != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path, size_bytes=0,
                    message=f"创建目标库 {target_db} 失败: {chk['stderr']}",
                    stderr=chk["stderr"], simulated=False)
        else:
            # 目标库已存在：DROP 后重建，保证恢复结果与备份完全一致
            chk = self._run(
                ["psql", "--host", str(host), "--port", str(port),
                 "--username", str(user), "-d", "postgres",
                 "-c", f'DROP DATABASE IF EXISTS "{safe_db}" WITH (FORCE)'],
                env_extra=env_extra, timeout=180)
            if chk["returncode"] != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path, size_bytes=0,
                    message=f"清理目标库 {target_db} 失败: {chk['stderr']}",
                    stderr=chk["stderr"], simulated=False)
            chk = self._run(
                ["psql", "--host", str(host), "--port", str(port),
                 "--username", str(user), "-d", "postgres",
                 "-c", f'CREATE DATABASE "{safe_db}"'],
                env_extra=env_extra, timeout=180)
            if chk["returncode"] != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path, size_bytes=0,
                    message=f"重建目标库 {target_db} 失败: {chk['stderr']}",
                    stderr=chk["stderr"], simulated=False)

        # 4) 按文件后缀选择恢复方式
        if backup_path.endswith(".dump"):
            # 自定义格式用 pg_restore（目标库已创建，无需 -C；库已全新无需 -c）
            cmd = [
                "pg_restore",
                "--host", str(host),
                "--port", str(port),
                "--username", str(user),
                "--dbname", str(target_db),
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

    # ------------------------- 恢复校验 -------------------------
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """PostgreSQL 恢复校验：逻辑 dump 检查 PGDMP 头；物理 base backup 校验目录结构。"""
        options = options or {}
        backup_path = record.get("backup_path") or record.get("output_path") or ""
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"备份文件不存在: {backup_path}")
        if backup_path.endswith(".sim"):
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                message="postgresql: simulated backup verified", verified=True)

        mode = record.get("backup_mode") or self.backup_mode
        size = os.path.getsize(backup_path) if os.path.isfile(backup_path) else 0

        if mode == BackupMode.PHYSICAL:
            # 物理备份通常是一个目录，检查 PG_VERSION / base / global
            target = backup_path
            if os.path.isfile(backup_path):
                # 尝试解压到临时目录
                import tempfile
                import shutil
                recovery_pool = options.get("recovery_pool") or ""
                if recovery_pool and os.path.isdir(recovery_pool):
                    temp_dir = os.path.join(recovery_pool, f"pg_verify_{self.task_id}_{int(time.time())}")
                else:
                    temp_dir = tempfile.mkdtemp(prefix=f"pg_verify_{self.task_id}_")
                try:
                    os.makedirs(temp_dir, exist_ok=True)
                    if backup_path.endswith((".tar.gz", ".tgz")):
                        self._run(["tar", "-xzf", backup_path, "-C", temp_dir], timeout=3600)
                    elif backup_path.endswith((".tar", ".tar.bz2")):
                        algo = "j" if backup_path.endswith(".bz2") else ""
                        self._run(["tar", f"-x{algo}f", backup_path, "-C", temp_dir], timeout=3600)
                    target = temp_dir
                    # pg_basebackup tar 打包时目录在 base 下
                    entries = os.listdir(target)
                    if len(entries) == 1 and os.path.isdir(os.path.join(target, entries[0])):
                        target = os.path.join(target, entries[0])
                    # 若存在 backup_manifest 则用 pg_verifybackup
                    manifest = os.path.join(target, "backup_manifest")
                    if os.path.isfile(manifest) and shutil.which("pg_verifybackup"):
                        res = self._run(["pg_verifybackup", "-m", manifest, target], timeout=3600)
                        if res["returncode"] != 0:
                            return BackupResult(success=False, status=BackupStatus.FAILED,
                                                message=f"pg_verifybackup failed: {res['stderr']}")
                    if os.path.isfile(os.path.join(target, "PG_VERSION")):
                        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                            message="pg-family: base backup extracted OK",
                                            verified=True, size_bytes=size)
                    return BackupResult(success=False, status=BackupStatus.FAILED,
                                        message="pg-family: base backup structure invalid")
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
            else:
                if os.path.isfile(os.path.join(backup_path, "PG_VERSION")):
                    return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                        message="pg-family: base backup verified",
                                        verified=True, size_bytes=size)
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="pg-family: base backup structure invalid")

        # 逻辑备份
        try:
            opener = open
            if backup_path.endswith(".gz"):
                import gzip
                opener = gzip.open
            elif backup_path.endswith(".zst"):
                import zstandard as zstd
                opener = zstd.open
            elif backup_path.endswith(".dump"):
                with open(backup_path, "rb") as f:
                    header = f.read(8)
                if header[:5] == b"PGDMP":
                    return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                        message="pg-family: custom-format dump header OK",
                                        verified=True, size_bytes=size)
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="pg-family: invalid dump header")
            with opener(backup_path, "rb") as f:
                header = f.read(200)
            text = header.decode("utf-8", "ignore")
            if text.startswith("--") or "PostgreSQL database dump" in text:
                return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                    message="pg-family: plain-text dump header OK",
                                    verified=True, size_bytes=size)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="pg-family: dump header invalid")
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"postgresql: verify error: {e}")

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

    def _pg_basebackup_extra_args(self):
        """解析 extra_options 中 pg_basebackup 物理备份专属扩展参数。

        支持两种写法：
        - 字符串：{"pg_basebackup_extra_args": "--verbose --exclude *.bak"}
        - 列表：  {"pg_basebackup_extra_args": ["--verbose", "--exclude", "*.bak"]}
        """
        raw = self.task.get("extra_options")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception as e:
            self.logger.warning("[%s] pg_basebackup_extra_args 解析失败: %s", self.task_name, e)
            return []
        if not isinstance(data, dict):
            return []
        val = data.get("pg_basebackup_extra_args")
        if isinstance(val, list):
            return [str(x) for x in val]
        if isinstance(val, str):
            return shlex.split(val)
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
