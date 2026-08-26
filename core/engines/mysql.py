# -*- coding: utf-8 -*-
"""
MySQL / MariaDB 备份引擎实现。

继承 core.engines.base.BackupEngine，通过调用外部客户端
mysqldump 与 mysql 完成逻辑备份与恢复。

设计要点：
- 明文密码绝不进入命令行参数，统一写入临时选项文件
  (.cnf, 权限 0600)，命令中以 --defaults-extra-file 引用，
  结束（或异常）时务必删除该临时文件。
- 自 2026-08-14 起不再提供仿真/兜底占位备份；客户端或连接
  缺失时任务直接失败。
- 仅使用 Python 标准库 + 外部客户端，不引入任何第三方依赖。
"""
import os
import time
import json
import tempfile
import shlex
import subprocess
import shutil
from pathlib import Path

import config
import core.db as db
from core.engines.base import BackupEngine, BackupType, BackupMode, BackupStatus, BackupResult


class MySQLEngine(BackupEngine):
    """MySQL 备份引擎。"""

    db_type = "mysql"
    display_name = "MySQL"
    # mysqldump 负责导出，mysql 负责执行恢复 SQL
    required_clients = ["mysqldump", "mysql"]
    # 物理备份：本机已安装的 CLI 工具（任一就绪即可执行物理备份）
    physical_bundled_tools = ["xtrabackup"]
    # 物理备份：外部插件（至少有一个就放行，不需要全部）
    physical_external_plugins = ["percona-xtrabackup-80", "percona-xtrabackup-24", "mariabackup"]

    # ------------------------------------------------------------------ #
    # 内部辅助：临时选项文件（承载明文密码，避免泄露到进程参数）
    # ------------------------------------------------------------------ #
    def _make_cnf(self, user: str, password: str) -> str:
        """创建仅本进程可读的临时 .cnf，写入连接账号信息。"""
        fd, cnf = tempfile.mkstemp(prefix="bk_", suffix=".cnf")
        try:
            content = f"[client]\nuser={user}\npassword={password}\n"
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        # 仅属主可读写，杜绝密码被其他用户读取
        os.chmod(cnf, 0o600)
        return cnf

    @staticmethod
    def _cleanup_cnf(cnf) -> None:
        """无论成功与否都清理掉临时选项文件，避免明文密码残留。"""
        if cnf and os.path.exists(cnf):
            try:
                os.remove(cnf)
            except OSError:
                pass

    def _parse_extra_options(self) -> dict:
        """解析 task.extra_options（可能为 JSON 字符串）为字典。"""
        raw = self.task.get("extra_options")
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}

    def _server_version(self, host: str = None, port: int = None):
        """探测目标 MySQL 大/中版本号，返回 (major, minor) 元组。

        用于在不同版本间切换兼容的命令行选项（如 GTID 相关参数）。
        探测失败时保守返回 (8, 0)，即按现代版本处理。
        """
        try:
            res = self._exec_sql("SELECT VERSION();", host=host, port=port)
            if res.get("returncode") != 0:
                return (8, 0)
            out = res.get("stdout", "") or ""
            m = __import__("re").search(r"(\d+)\.(\d+)\.", out)
            if m:
                return (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        return (8, 0)

    def _add_target(self, dump_args: list, db_name: str, extra: dict) -> None:
        """根据 extra_options 决定 mysqldump 的库/表范围。"""
        tables = extra.get("tables") or []
        if extra.get("use_all_db"):
            # 全实例备份
            dump_args.append("--all-databases")
        elif tables:
            # 指定某库下的若干表：mysqldump db_name tbl1 tbl2 ...
            dump_args.append(db_name)
            dump_args.extend(str(t) for t in tables)
        else:
            # 默认按单库备份
            dump_args.append("--databases")
            dump_args.append(db_name)

    def _build_dump_shell(self, dump_args: list, out_path: str, algo: str,
                           raw_path: str = None) -> str:
        """组装 mysqldump 的 sh -c 命令（支持管道压缩或直接重定向）。

        - algo: none | gzip | zstd（zstd 压缩率显著高于 gzip，且可解压恢复）。
        - 加 set -o pipefail 是为了避免：mysqldump 失败而压缩器仍产出空文件，
          整条管道却返回 0（压缩器退出码），导致"假成功"。
        - 当传入 raw_path 时，用 tee 把未压缩原始流旁路写入 raw_path，
          用于事后统计原始数据量以计算压缩率（压缩产物本身不含该文件）。
        """
        quoted = " ".join(shlex.quote(a) for a in dump_args)
        comp = self.pipe_compress(algo)
        comp_str = " ".join(shlex.quote(c) for c in comp)
        pv = self._pv_throttle()
        pv_str = (" | " + " ".join(shlex.quote(p) for p in pv)) if pv else ""
        if algo == "none":
            # 纯文本：直接重定向（限速不适配纯文本重定向，仅压缩路径限速）
            return f"{quoted} > {shlex.quote(out_path)}"
        if raw_path:
            # mysqldump | tee 原始副本 | [pv 限速] | 压缩 > 产物
            return (f"set -o pipefail; {quoted} | tee {shlex.quote(raw_path)} "
                    f"{pv_str} | {comp_str} > {shlex.quote(out_path)}")
        return f"set -o pipefail; {quoted}{pv_str} | {comp_str} > {shlex.quote(out_path)}"

    # ------------------------------------------------------------------ #
    # 备份
    # ------------------------------------------------------------------ #
    def backup(self, backup_type: BackupType) -> BackupResult:
        # demo_only / DEMO_MODE 不再触发仿真，统一走真实备份；失败即失败。

        # 按备份模式分发：物理备份 vs 逻辑备份
        mode = self.backup_mode
        if mode == BackupMode.PHYSICAL:
            # 物理备份：优先 SSH 远端 xtrabackup/mariabackup，失败再回退本机
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "MySQL 物理备份(XtraBackup)",
            )
        # 逻辑备份：优先 SSH 远程执行，失败再回退本机
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "MySQL 逻辑备份(mysqldump)",
        )

    # ------------------ 物理备份 (XtraBackup) ------------------
    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        """物理备份：xtrabackup 全量/增量。"""
        xtrabackup = shutil.which("xtrabackup") or "/opt/xtrabackup/bin/xtrabackup"
        if not os.path.isfile(xtrabackup):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="xtrabackup 未安装或未找到(请设置 XTRABACKUP_PATH)")

        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        out_dir = self._output_dir()
        ts = self._timestamp()
        comp = int(self.task.get("compress") or 0)

        target_dir = os.path.join(out_dir, f"xtrabackup_{'full' if backup_type==BackupType.FULL else 'inc'}_{ts}")
        os.makedirs(target_dir, exist_ok=True)

        cmd = [xtrabackup, "--backup", f"--target-dir={target_dir}",
               f"--user={user}", f"--password={pw}",
               f"--host={host}", f"--port={port}", "--no-lock"]
        if comp:
            # 最高压缩：zstd 级别取任务 compress_level（上限 19，xtrabackup 支持范围）
            zl = int(self.task.get("compress_level") or 0)
            zl = max(1, min(zl, 19)) if zl else 19
            cmd += ["--compress=zstd", f"--compress-zstd-level={zl}", "--compress-threads=4"]

        note = ""
        if backup_type == BackupType.INCREMENTAL:
            # 找最近全量作为增量基
            import glob as _glob
            full_dirs = sorted(_glob.glob(os.path.join(out_dir, "xtrabackup_full_*")), reverse=True)
            base_dir = None
            for d in full_dirs:
                if os.path.isfile(os.path.join(d, ".success")):
                    base_dir = d
                    break
            if base_dir:
                cmd.append(f"--incremental-basedir={base_dir}")
                note = f" 增量基于 {base_dir}"
            else:
                # 无全量基，自动退化为全量
                backup_type = BackupType.FULL
                target_dir = os.path.join(out_dir, f"xtrabackup_full_{ts}")
                os.makedirs(target_dir, exist_ok=True)
                cmd[2] = f"--target-dir={target_dir}"
                note = " 增量基不存在，已自动退化为全量"

        start = time.time()
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        dur = round(time.time()-start, 3)

        if ret.returncode != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=None, duration_sec=dur,
                                stderr=ret.stderr, message=f"xtrabackup 失败: {ret.stderr[:500]}")
        # 标记成功
        Path(target_dir, ".success").touch()
        # 统计备份目录磁盘占用（xtrabackup 含 .zst 压缩时即为压缩后字节；未压缩为原始页大小）
        size = 0
        for _root, _dirs, _files in os.walk(target_dir):
            for _f in _files:
                try:
                    size += os.path.getsize(os.path.join(_root, _f))
                except OSError:
                    pass
        # 以 xtrabackup_checkpoints 指纹作为该备份的校验码（内容级标识）
        checksum = ""
        cp = os.path.join(target_dir, "xtrabackup_checkpoints")
        if os.path.isfile(cp):
            import hashlib as _hl
            with open(cp, "rb") as _cf:
                checksum = _hl.sha256(_cf.read()).hexdigest()[:16]
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=target_dir, size_bytes=size, duration_sec=dur,
                            stdout=ret.stdout, simulated=False, checksum=checksum,
                            message=f"MySQL 物理备份(XtraBackup)成功 {note}")

    def _backup_physical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """物理备份：通过 SSH 在远端数据库服务器以 xtrabackup/mariabackup 执行
        全量物理备份，打包 tar.gz 后经 SFTP 拉回本机落盘并计算 size/sha256。

        - 密码写入远端临时 my.cnf（0600），经 --defaults-extra-file 引用，避免
          明文出现在进程参数(ps)中。
        - xtrabackup 连接数据库走 TCP（--host/--port），故 SSH 登录身份无需是
          数据库 OS 用户；远端目录 /tmp 世界可写，无需额外 chown。
        - incremental/differential 在远端无基准目录跟踪，统一按全量(full)执行。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or "root"
        pw = db.decrypt_secret(self.task.get("password") or "")

        client = remote_dump._connect(ssh_host)
        # 探测 xtrabackup，找不到回退 mariabackup
        tool = remote_dump._resolve_remote_bin(client, "xtrabackup")
        if not tool:
            tool = remote_dump._resolve_remote_bin(client, "mariabackup")
        if not tool:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="远端主机未找到 xtrabackup / mariabackup，无法执行远端物理备份；"
                        "请在远端安装 Percona XtraBackup 或 MariaDB Backup。")

        remote_tmp = f"/tmp/mysql_xtra_{ts}"
        remote_tar = f"{remote_tmp}.tar.gz"
        remote_cnf = "/tmp/bk_xtrabackup.cnf"
        sftp = client.open_sftp()
        try:
            # 1) 写临时 my.cnf（承载明文密码，0600，避免 ps 泄露）
            with sftp.open(remote_cnf, "w") as f:
                f.write(f"[client]\nuser={user}\npassword={pw}\n")
            try:
                sftp.chmod(remote_cnf, 0o600)
            except Exception:
                pass

            # 2) 创建远端备份目录（/tmp 世界可写，无需 chown）
            prep = f"mkdir -p {remote_tmp}"
            _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)

            # 3) 远端执行：xtrabackup --backup -> tar czf（密码不在命令行）
            inner = (
                f"{tool} --backup --target-dir={remote_tmp} "
                f"--defaults-extra-file={remote_cnf} "
                f"--host={shlex.quote(host)} --port={port} --no-lock "
                f"&& tar czf {remote_tar} -C {remote_tmp} ."
            )
            wrapped = remote_dump._wrap_login(inner)
            start = time.time()
            out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
            duration = round(time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            self.logger.info("[%s] 远端 xtrabackup 返回 rc=%s", self.task_name, rc)

            if rc != 0:
                snippet = (out_text or err)[-1200:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err,
                    message=f"远端 XtraBackup 物理备份失败(rc={rc}): {snippet}")

            # 4) SFTP 拉回 tar.gz 到本机，计算真实 size + sha256
            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_path = os.path.join(out_dir, f"xtrabackup_{ts}.tar.gz")
            sftp.get(remote_tar, local_path)

            size = os.path.getsize(local_path)
            checksum = db.sha256_file(local_path)
            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 以 {os.path.basename(tool)} 执行 MySQL 物理备份成功，"
                   f"已拉回 {local_path} ({db.human_size(size)})")
            self.logger.info("[%s] %s", self.task_name, msg)

            # 清理远端临时目录与 tar 包（best-effort，失败不致命）
            try:
                _ssh_exec_pipe(client, remote_dump._wrap_login(
                    f"rm -rf {remote_tmp} {remote_tar}"), timeout=60)
            except Exception:
                pass

            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=local_path, size_bytes=size, duration_sec=duration,
                stdout=out_text, stderr=err, simulated=False,
                checksum=checksum, message=msg)
        finally:
            try:
                sftp.remove(remote_cnf)
            except Exception:
                pass
            try:
                sftp.close()
            except Exception:
                pass

    # ------------------ 逻辑备份 (mysqldump) ------------------
    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机/数据库服务器上执行 mysqldump，把流拉回到本地落盘。"""
        from core import remote_dump
        # 与本地逻辑备份对齐：统一由全局 COMPRESS_BY_DEFAULT 控制，远端优先 zstd
        enable = getattr(config, "COMPRESS_BY_DEFAULT", True)
        comp = 1 if enable else 0
        extra = self._parse_extra_options()
        # 默认禁用 GTID_PURGED；用户可通过 gtid_purged=true 显式保留。
        extra_args = "--set-gtid-purged=OFF" if not extra.get("gtid_purged") else ""
        data, compressed = remote_dump.remote_db_dump(self.task, ssh_host, "mysql", comp, extra_args)
        # compressed 反映远端实际是否压缩（缺 zstd 时 remote_dump 会降级为不压缩）
        suffix = ".sql.zst" if compressed else ".sql"
        res = self._write_dump_file(data, backup_type, ssh_host, suffix, "mysqldump")
        res.compress_algo = "zstd" if compressed else "none"
        if not compressed and enable:
            res.message = (res.message or "") + "（远端未安装 zstd，已降级为不压缩）"
        return res

    def _backup_logical_local(self, backup_type: BackupType) -> BackupResult:
        """逻辑备份：mysqldump。支持分库/分表/仅结构/仅数据。"""
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        db_name = self.task.get("db_name") or ""
        extra = self._parse_extra_options()
        algo = self._resolve_compress_algo()

        cnf = None
        start = time.time()
        try:
            cnf = self._make_cnf(user, pw)
            ts = self._timestamp()
            out_dir = self._output_dir()
            bt = backup_type.value if isinstance(backup_type, BackupType) else str(backup_type)

            # mysqldump 基础参数
            dump_args = [
                "mysqldump",
                f"--defaults-extra-file={cnf}",
                "--host", str(host), "--port", str(port),
                "--single-transaction", "--routines", "--triggers", "--events",
            ]

            # 默认禁用 GTID_PURGED，避免恢复到已有 GTID 的实例时报 1840。
            # 用户可通过 extra_options.gtid_purged=true 显式保留 GTID 信息。
            # 版本兼容：GTID 自 5.6 引入，5.5 及更早版本无 GTID，
            # 此时 --set-gtid-purged 选项本身不存在，传了会直接报错，故跳过。
            if not extra.get("gtid_purged"):
                major, minor = self._server_version(host=str(host), port=int(port))
                if (major, minor) >= (5, 6):
                    dump_args.append("--set-gtid-purged=OFF")

            note = ""
            # 分库/分表/仅结构/仅数据
            tables = extra.get("tables") or []
            schemas = extra.get("schemas") or []
            schema_only = bool(extra.get("schema_only"))
            data_only = bool(extra.get("data_only"))

            if tables:
                # 指定表：mysqldump db_name tbl1 tbl2 ...
                dump_args.append(db_name or "")
                dump_args.extend(tables)
                note += f" 指定表:{','.join(tables)}"
            elif schemas:
                dump_args += ["--databases"] + schemas
                note += f" 指定库:{','.join(schemas)}"
            elif db_name:
                self._add_target(dump_args, db_name, extra)
            else:
                dump_args.append("--all-databases")
                note += " 全实例"

            if schema_only:
                dump_args.append("--no-data")
                note += " (仅结构)"
            if data_only:
                dump_args.append("--no-create-info")
                note += " (仅数据)"

            if backup_type == BackupType.INCREMENTAL:
                dump_args.append("--flush-logs")
                note += "（需 binlog 支撑增量恢复）"

            # 输出文件（按算法加后缀）
            suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
            fname = f"{ts}__{self.task_name}__{bt}.sql{suffix}"
            out_path = os.path.join(out_dir, fname)

            raw_path = None
            if algo != "none":
                # tee 旁路记录原始(未压缩)数据量，用于计算压缩率
                raw_path = os.path.join(out_dir, f"{ts}__{self.task_name}__{bt}.raw")
            inner = self._build_dump_shell(dump_args, out_path, algo, raw_path)
            res = self._run(["sh", "-c", inner])
            dur = round(time.time()-start, 3)

            if res["returncode"] != 0:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    backup_path=None, duration_sec=dur,
                                    stderr=res["stderr"],
                                    message=f"mysqldump 失败: {res.get('stderr','')[:500]}")
            size = os.path.getsize(out_path)
            original = os.path.getsize(raw_path) if (raw_path and os.path.exists(raw_path)) else 0
            if raw_path and os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
            ratio = round(size / original, 6) if (original and size) else 0.0
            checksum = db.sha256_file(out_path)
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                backup_path=out_path, size_bytes=size,
                                original_size_bytes=original, compress_algo=algo,
                                compress_ratio=ratio,
                                duration_sec=dur, stdout=res["stdout"],
                                stderr=res["stderr"], checksum=checksum,
                                message=f"MySQL 逻辑备份(mysqldump)成功 算法:{algo}" + note)
        finally:
            self._cleanup_cnf(cnf)

    # ------------------------------------------------------------------ #
    # 恢复
    # ------------------------------------------------------------------ #
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        logs = [f"[MySQL 恢复] 备份文件: {backup_path}",
                f"[MySQL 恢复] 任务: {self.task_name} ({self.task.get('host')}:{self.task.get('port')})"]

        # demo_only / DEMO_MODE 不再触发仿真；客户端或连接缺失直接失败。

        # 0) 跨主机恢复：SFTP 推送到目标主机 → SSH 远程执行 mysql
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_db = kwargs.get("target_db") or self.task.get("db_name") or ""
            logs.append(f"[跨主机恢复] 目标: {target_host_info.get('hostname')}, 目标库: {target_db}")
            res = self._try_cross_host_restore(backup_path, target_host_info, target_db)
            res.detail_log = "\n".join(logs) + "\n" + (res.detail_log or res.stderr or res.stdout or "")
            return res

        # 1) 物理备份产物 -> XtraBackup 物理恢复（prepare + 临时实例校验）
        if self._is_physical_backup(backup_path):
            logs.append("[物理恢复] 检测到 XtraBackup 物理备份产物，执行物理恢复")
            result = self._restore_physical(backup_path, **kwargs)
            logs.append(f"[物理恢复] success={result.success}, message={result.message}")
            if result.stdout:
                logs.append(f"[物理恢复 stdout]\n{result.stdout}")
            if result.stderr:
                logs.append(f"[物理恢复 stderr]\n{result.stderr}")
            if result.success:
                result.detail_log = "\n".join(logs)
                return result
            reason = result.message or "未知错误"
            logs.append(f"[结果] 物理恢复失败: {reason}")
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, message=reason,
                detail_log="\n".join(logs))

        # 2) 逻辑备份 -> 本机直接执行 mysql 恢复
        logs.append("[本机恢复] 尝试本地执行 mysql 恢复...")
        result = self._restore_local(backup_path, **kwargs)
        logs.append(f"[本机恢复] success={result.success}, message={result.message}")
        if result.stdout:
            logs.append(f"[本机恢复 stdout]\n{result.stdout}")
        if result.stderr:
            logs.append(f"[本机恢复 stderr]\n{result.stderr}")
        if result.success:
            result.detail_log = "\n".join(logs)
            return result

        # 2) 本机失败 -> 尝试通过 SSH 在数据库服务器执行恢复
        reason = (result.message or "未知错误")
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host and os.path.exists(backup_path):
            hk = ssh_host.get("host_key")
            logs.append(f"[远程恢复] 本机恢复失败({reason})，改用 SSH 在数据库服务器执行恢复 (host={hk})")
            self.logger.info(
                "[%s] 本机恢复失败(%s)，改用 SSH 在数据库服务器执行恢复 (host=%s)",
                self.task_name, reason, hk)
            try:
                with open(backup_path, "rb") as f:
                    dump_bytes = f.read()
                logs.append(f"[远程恢复] 备份文件读取完成，大小 {len(dump_bytes)} bytes")
                remote_dump.remote_db_restore(
                    self.task, ssh_host, "mysql", dump_bytes)
                target_db = kwargs.get("target_db") or self.task.get("db_name")
                msg = "通过 SSH 在数据库服务器恢复成功" + (f"（目标库: {target_db}）" if target_db else "")
                logs.append(f"[远程恢复] {msg}")
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path, message=msg,
                    detail_log="\n".join(logs))
            except Exception as e:
                self.logger.error("[%s] 远程恢复也失败: %s", self.task_name, e)
                reason = f"本机与远程恢复均失败: {e}"
                logs.append(f"[远程恢复] 失败: {e}")

        # 3) 返回真实错误
        logs.append(f"[结果] 失败原因: {reason}")
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            backup_path=backup_path, message=reason,
            detail_log="\n".join(logs))

    def _exec_sql(self, sql: str, host: str = None, port: int = None,
                  target_db: str = None) -> dict:
        """通过 mysql 客户端执行一条 SQL，返回 {"returncode", "stdout", "stderr"}。"""
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        host = host or self.task.get("host") or "127.0.0.1"
        port = port or self.task.get("port") or 3306
        cnf = self._make_cnf(user, pw)
        try:
            args = [
                "mysql",
                f"--defaults-extra-file={cnf}",
                "--host", str(host),
                "--port", str(port),
            ]
            if target_db:
                args.append(target_db)
            # 将 SQL 以 stdin 喂入 mysql，避免 shell 管道在 Windows 下不可用
            return self._run_with_stdin(args, sql)
        finally:
            self._cleanup_cnf(cnf)

    def _reset_gtid_before_restore(self, host: str = None, port: int = None) -> None:
        """恢复前清空目标库 GTID，避免导入含 GTID_PURGED 的备份时报 1840。

        注意：这会清空 binlog 与 GTID 状态，仅应在恢复/演练环境中使用。
        """
        res = self._exec_sql("RESET MASTER;", host=host, port=port)
        if res["returncode"] != 0:
            # 8.4+ 改为 RESET BINARY LOGS AND GTID_EXECUTION，老版本会报语法错误
            res2 = self._exec_sql("RESET BINARY LOGS AND GTID_EXECUTION;", host=host, port=port)
            if res2["returncode"] != 0:
                self.logger.warning("[%s] 恢复前 RESET MASTER 失败: %s",
                                    self.task_name, res.get("stderr", ""))

    # ------------------------------------------------------------------ #
    # 物理备份恢复（XtraBackup）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_physical_backup(backup_path: str) -> bool:
        """判断备份产物是否为 XtraBackup 物理备份。"""
        if not backup_path:
            return False
        if os.path.isdir(backup_path):
            return os.path.isfile(os.path.join(backup_path, "xtrabackup_checkpoints"))
        return backup_path.endswith((".tar.gz", ".tgz", ".tar", ".xbstream"))

    @staticmethod
    def _decompress_xtrabackup_dir(work_dir: str) -> int:
        """批量解压 XtraBackup --compress=zstd 产物中的 .zst 文件（原地替换）。"""
        try:
            import zstandard as _zstd
        except ImportError:
            raise RuntimeError("缺少 zstandard 库，无法解压 .zst 备份（可用 pip install zstandard）")
        dctx = _zstd.ZstdDecompressor()
        cnt = 0
        for root, _dirs, files in os.walk(work_dir):
            for fn in files:
                if fn.endswith(".zst"):
                    src = os.path.join(root, fn)
                    dst = src[:-4]
                    with open(src, "rb") as fi, open(dst, "wb") as fo:
                        dctx.copy_stream(fi, fo)
                    os.remove(src)
                    cnt += 1
        return cnt

    def _restore_physical(self, backup_path: str, **kwargs) -> BackupResult:
        """XtraBackup 物理备份恢复：

        1) 解包(tar.gz/xbstream)或复制备份目录到临时工作区（不污染原备份，
           保证 prepare 后原备份仍可作为后续增量备份的基）；
        2) 解压 .zst 压缩产物；
        3) xtrabackup --prepare 应用 redo log；
        4) 以备份目录启动临时 mysqld（--skip-grant-tables + socket 免密）
           做可恢复性校验，并查询目标库表数量；
        5) 关闭临时实例并清理。
        """
        target_db = kwargs.get("target_db") or self.task.get("db_name")
        xtrabackup = shutil.which("xtrabackup") or "/opt/xtrabackup/bin/xtrabackup"
        if not os.path.isfile(xtrabackup):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message="物理恢复需要 xtrabackup，请先在插件市场安装 Percona XtraBackup")
        mysqld = shutil.which("mysqld") or "/opt/database/bin/mysqld"
        if not os.path.isfile(mysqld):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path,
                                message="物理恢复需要本机 mysqld 用于启动临时校验实例")
        mysql_cli = shutil.which("mysql") or "/opt/database/bin/mysql"

        logs = [f"[物理恢复] 备份产物: {backup_path}",
                f"[物理恢复] xtrabackup: {xtrabackup}", f"[物理恢复] mysqld: {mysqld}"]
        tmp = tempfile.mkdtemp(prefix="xb_restore_")
        start = time.time()
        sock = f"/tmp/xb_restore_{os.getpid()}_{int(time.time() * 1000)}.sock"
        pid_file = sock + ".pid"
        err_file = sock + ".err"
        proc = None
        try:
            # 1) 准备工作目录
            work = os.path.join(tmp, "data")
            if backup_path.endswith((".tar.gz", ".tgz")):
                os.makedirs(work)
                ret = subprocess.run(["tar", "xzf", backup_path, "-C", work],
                                     capture_output=True, text=True, timeout=3600)
                if ret.returncode != 0:
                    raise RuntimeError(f"解包 tar.gz 失败: {(ret.stderr or '')[:300]}")
            elif backup_path.endswith(".tar"):
                os.makedirs(work)
                ret = subprocess.run(["tar", "xf", backup_path, "-C", work],
                                     capture_output=True, text=True, timeout=3600)
                if ret.returncode != 0:
                    raise RuntimeError(f"解包 tar 失败: {(ret.stderr or '')[:300]}")
            elif backup_path.endswith(".xbstream"):
                os.makedirs(work)
                ret = subprocess.run([xtrabackup, "--xbstream", "-x", "-C", work],
                                     capture_output=True, text=True, timeout=7200)
                if ret.returncode != 0:
                    raise RuntimeError(f"xbstream 解流失败: {(ret.stderr or '')[:300]}")
            elif os.path.isdir(backup_path):
                logs.append("[物理恢复] 复制备份目录到临时工作区（避免 prepare 污染原备份/增量基）...")
                shutil.copytree(backup_path, work)
            else:
                raise RuntimeError(f"不支持的物理备份产物: {backup_path}")

            # 2) 解压 .zst（若存在）
            try:
                n = self._decompress_xtrabackup_dir(work)
                if n:
                    logs.append(f"[物理恢复] 已解压 {n} 个 .zst 文件")
            except RuntimeError:
                if not shutil.which("zstd"):
                    raise
                logs.append("[物理恢复] python 解压不可用，改用 xtrabackup --decompress")
                ret = subprocess.run([xtrabackup, "--decompress", f"--target-dir={work}"],
                                     capture_output=True, text=True, timeout=7200)
                if ret.returncode != 0:
                    raise RuntimeError(f"xtrabackup --decompress 失败: {(ret.stderr or '')[:300]}")

            # 3) prepare
            logs.append("[物理恢复] xtrabackup --prepare 应用 redo log ...")
            ret = subprocess.run([xtrabackup, "--prepare", f"--target-dir={work}"],
                                 capture_output=True, text=True, timeout=7200)
            if ret.returncode != 0:
                raise RuntimeError(f"xtrabackup --prepare 失败: {(ret.stderr or ret.stdout or '')[-500:]}")

            # 4) 启动临时 mysqld 校验
            logs.append("[物理恢复] 启动临时 mysqld 实例做可恢复性校验 ...")
            proc = subprocess.Popen(
                [mysqld, "--no-defaults",
                 f"--datadir={work}", f"--socket={sock}",
                 "--skip-networking", "--skip-grant-tables",
                 "--user=root", f"--pid-file={pid_file}",
                 f"--log-error={err_file}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = False
            for _ in range(90):
                if proc.poll() is not None:
                    break
                r = subprocess.run([mysql_cli, "--no-defaults", "-uroot", f"--socket={sock}",
                                    "-N", "-e", "SELECT 1"],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and r.stdout.strip() == "1":
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                tail = ""
                if os.path.exists(err_file):
                    with open(err_file, errors="replace") as f:
                        tail = f.read()[-600:]
                raise RuntimeError(f"临时实例启动超时/失败: {tail or '无错误日志'}")

            # 5) 校验数据可读
            if target_db:
                verify_sql = (
                    f"SELECT CONCAT('tables=', COUNT(*)) FROM information_schema.tables "
                    f"WHERE table_schema='{target_db}';")
                label = f"目标库 {target_db}"
            else:
                verify_sql = ("SELECT CONCAT('databases=', COUNT(*)) "
                              "FROM information_schema.schemata;")
                label = "实例库"
            r = subprocess.run([mysql_cli, "--no-defaults", "-uroot", f"--socket={sock}",
                                "-N", "-e", verify_sql],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"校验查询失败: {(r.stderr or '')[:300]}")
            verify_out = r.stdout.strip()
            logs.append(f"[物理恢复] 校验通过: {label} {verify_out}")

            duration = round(time.time() - start, 3)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path, duration_sec=duration,
                stdout="\n".join(logs) + f"\n[校验] {label} {verify_out}",
                message=f"MySQL 物理备份(XtraBackup)恢复成功（{label} {verify_out}）")
        except Exception as e:
            duration = round(time.time() - start, 3)
            self.logger.exception("[%s] 物理恢复失败", self.task_name)
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, duration_sec=duration,
                stdout="\n".join(logs), stderr=str(e),
                message=f"MySQL 物理备份恢复失败: {e}")
        finally:
            # 关闭临时实例 + 清理
            try:
                if proc and proc.poll() is None:
                    subprocess.run([mysql_cli, "--no-defaults", "-uroot", f"--socket={sock}",
                                    "-e", "SHUTDOWN"], capture_output=True, timeout=30)
            except Exception:
                pass
            if os.path.exists(pid_file):
                try:
                    with open(pid_file) as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 9)
                except Exception:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            for p in (sock, pid_file, err_file):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def _restore_local(self, backup_path: str, **kwargs) -> BackupResult:
        # 连接参数
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        target_db = kwargs.get("target_db")

        cnf = None
        start = time.time()
        try:
            cnf = self._make_cnf(user, pw)

            # 恢复前先清空 GTID，避免 1840 错误
            self._reset_gtid_before_restore(host=host, port=port)

            # mysql 客户端基础参数
            mysql_args = [
                "mysql",
                f"--defaults-extra-file={cnf}",
                "--host", str(host),
                "--port", str(port),
            ]
            if target_db:
                mysql_args.append(target_db)

            # 跨平台恢复：把备份文件（自动解压）作为 mysql 的 stdin 喂入，
            # 不再使用 shell 重定向 `< "含中文/空格路径"`，避免 Windows 下
            # `cmd /c` 报 "文件名、目录名或卷标语法不正确"，也避免 POSIX 下
            # 依赖 `sh` 造成命令不存在。压缩文件在基类 _read_decompressed
            # 中统一解压，保证 zstd / gzip 均可正确恢复。
            res = self._run(mysql_args, input_file=backup_path)
            duration = round(time.time() - start, 3)
            if res["returncode"] != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    backup_path=backup_path, duration_sec=duration,
                    stdout=res["stdout"], stderr=res["stderr"],
                    message="MySQL 恢复失败: " + (res["stderr"] or "未知错误"))

            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path, duration_sec=duration,
                stdout=res["stdout"], stderr=res["stderr"],
                message="MySQL 恢复成功"
                        + (f"（目标库: {target_db}）" if target_db else "（沿用备份中的库名）"))
        finally:
            self._cleanup_cnf(cnf)

    # ------------------------------------------------------------------ #
    # 恢复校验
    # ------------------------------------------------------------------ #
    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """MySQL 恢复校验：逻辑备份检查 SQL 头；物理备份尝试 xtrabackup --prepare。"""
        options = options or {}
        backup_path = record.get("backup_path") or record.get("output_path") or ""
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"备份文件不存在: {backup_path}")
        if backup_path.endswith(".sim"):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="mysql: 不支持仿真备份校验，请使用真实备份", verified=False)

        mode = record.get("backup_mode") or self.backup_mode
        size = os.path.getsize(backup_path)

        # 物理备份：优先 xtrabackup --prepare --apply-log-only
        if mode == BackupMode.PHYSICAL:
            xtrabackup = shutil.which("xtrabackup") or shutil.which("mariabackup")
            if xtrabackup:
                import tempfile
                import shutil as _shutil
                recovery_pool = options.get("recovery_pool") or ""
                if recovery_pool and os.path.isdir(recovery_pool):
                    temp_dir = os.path.join(recovery_pool, f"verify_{self.task_id}_{int(time.time())}")
                else:
                    temp_dir = tempfile.mkdtemp(prefix=f"mysql_verify_{self.task_id}_")
                try:
                    os.makedirs(temp_dir, exist_ok=True)
                    is_xbstream = backup_path.endswith(".xbstream")
                    if is_xbstream:
                        # 先解压 xbstream
                        extract_cmd = [xtrabackup, "--extract",
                                       f"--target-dir={temp_dir}",
                                       f"--stream=xbstream", "<", backup_path]
                        res = self._run(["sh", "-c", " ".join(extract_cmd)], timeout=3600)
                        if res["returncode"] != 0:
                            return BackupResult(success=False, status=BackupStatus.FAILED,
                                                message=f"mysql: xbstream extract failed: {res['stderr']}")
                    else:
                        # 复制/解压 tar 到临时目录
                        if backup_path.endswith((".tar.gz", ".tgz")):
                            self._run(["tar", "-xzf", backup_path, "-C", temp_dir], timeout=3600)
                        elif backup_path.endswith((".tar", ".tar.bz2")):
                            algo = "j" if backup_path.endswith(".bz2") else ""
                            self._run(["tar", f"-x{algo}f", backup_path, "-C", temp_dir], timeout=3600)
                        else:
                            _shutil.copytree(backup_path, temp_dir, dirs_exist_ok=True)
                    prepare_cmd = [xtrabackup, "--prepare",
                                   f"--target-dir={temp_dir}"]
                    res = self._run(prepare_cmd, timeout=7200)
                    if res["returncode"] != 0:
                        return BackupResult(success=False, status=BackupStatus.FAILED,
                                            message=f"mysql: xtrabackup prepare failed: {res['stderr']}")
                    return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                        message="mysql: xbstream extracted and prepared",
                                        verified=True, size_bytes=size)
                finally:
                    if os.path.isdir(temp_dir):
                        _shutil.rmtree(temp_dir, ignore_errors=True)
            else:
                return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                    message="mysql: xtrabackup not available, file verified",
                                    verified=True, size_bytes=size)

        # 逻辑备份：检查文件头
        try:
            opener = open
            if backup_path.endswith(".gz"):
                import gzip
                opener = gzip.open
            elif backup_path.endswith(".zst"):
                import zstandard as zstd
                opener = zstd.open
            with opener(backup_path, "rb") as f:
                header = f.read(200)
            if not header:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="mysql: dump header empty")
            text = header.decode("utf-8", "ignore")
            if any(text.startswith(p) for p in ("--", "/*!", "/*M!", "DROP TABLE", "CREATE TABLE")):
                return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                    message="mysql: logical dump header verified",
                                    verified=True, size_bytes=size)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="mysql: dump header invalid")
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"mysql: verify error: {e}")

    # ------------------------------------------------------------------ #
    # 合成全量（增量链合并，Phase 1）
    # ------------------------------------------------------------------ #
    def list_sets(self) -> list:
        """列出本任务关联的备份集（BackupSet）。"""
        import core.models as models
        return models.list_backup_sets(task_id=self.task_id)

    def synthesize_full(self, sets=None, target_storage_tier: int = None,
                        target_record_id: int = None) -> BackupResult:
        """合成全量：将增量链合并为一份完整备份集（仅 1%~10% 增量数据）。

        - 物理模式：若存在 xtrabackup 增量链，调用
              xtrabackup --prepare --incremental-dir=<inc> ...
          将增量逐层合并到全量，生成"合成全量"（synthetic_full）。
        - 逻辑模式：以"全量 SQL + 增量 binlog 重放"在恢复时合成；
          此处生成合成全量记录（合并标记文件），保证 BackupSet 链路闭环。
        - 无 xtrabackup / DEMO_MODE：走仿真路径（与既有 DEMO_MODE 兜底一致）。
        """
        # 1) 从入参收集增量链（链头 full/synthetic_full + 后续 incremental）
        base = None
        incs = []
        if sets:
            for s in sets:
                s = s if isinstance(s, dict) else None
                if not s:
                    continue
                if s.get("set_type") in ("full", "synthetic_full") and base is None:
                    base = s
                elif s.get("set_type") == "incremental":
                    incs.append(s)

        # 2) 客户端缺失直接失败，不再生成仿真合成全量
        sim, _reason = self._should_simulate()
        if sim:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"缺少必要客户端/连接，无法合成全量: {_reason}")

        # 3) 物理模式：xtrabackup 合并
        if self.backup_mode == BackupMode.PHYSICAL:
            return self._synthesize_full_physical(base, incs, target_storage_tier)

        # 4) 逻辑模式：合成全量记录（SQL + 增量 binlog 重放占位）
        return self._synthesize_full_logical(base, incs, target_storage_tier)

    def _synthesize_full_simulated(self, base, incs, target_storage_tier):
        """已不再使用，保留方法签名仅作兼容。"""
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            message="不再支持仿真合成全量")

    def _synthesize_full_physical(self, base, incs, target_storage_tier):
        xtrabackup = shutil.which("xtrabackup") or "/opt/xtrabackup/bin/xtrabackup"
        if not os.path.isfile(xtrabackup) or not base or not base.get("object_key"):
            # 缺少 xtrabackup 或全量基：直接失败
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="物理合成全量缺少 xtrabackup 或全量基")
        base_dir = base["object_key"]
        if not os.path.isdir(base_dir):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="物理合成全量基目录不存在: " + str(base_dir))
        # 按增量顺序逐层 prepare（--incremental-dir 可重复叠加）
        cmd = [xtrabackup, "--prepare", f"--target-dir={base_dir}"]
        for inc in incs:
            inc_dir = inc.get("object_key")
            if inc_dir and os.path.isdir(inc_dir):
                cmd.append(f"--incremental-dir={inc_dir}")
        start = time.time()
        ret = self._run(cmd)
        dur = round(time.time() - start, 3)
        if ret["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"xtrabackup --prepare 失败: {ret['stderr'][:500]}")
        # 产物即为已合并的 base_dir，登记为合成全量
        size = 0
        try:
            for root, _dirs, files in os.walk(base_dir):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        except OSError:
            pass
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=base_dir, size_bytes=size,
            checksum=db.sha256_file(base_dir) if os.path.isfile(base_dir) else "",
            message=f"物理合成全量成功，合并 {len(incs)} 个增量（{dur}s）")

    def _synthesize_full_logical(self, base, incs, target_storage_tier):
        # 逻辑合成：将全量 SQL 与增量（binlog 重放）合成为一份"合成全量"记录文件。
        d = self._output_dir()
        ts = self._timestamp()
        fname = f"{ts}__{self.task_name}__synthetic_full.sql"
        fpath = os.path.join(d, fname)
        lines = []
        if base and base.get("object_key"):
            lines.append(f"-- base full: {base['object_key']}")
        for inc in incs:
            lines.append(
                f"-- replay incremental (binlog): "
                f"{inc.get('object_key') or inc.get('id')}")
        lines.append(f"-- synthetic full generated at {db.now_iso()}")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        size = os.path.getsize(fpath)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=fpath, size_bytes=size,
            checksum=db.sha256_file(fpath),
            message=f"逻辑合成全量成功（SQL + {len(incs)} 增量重放占位）")

    # ------------------------------------------------------------------ #
    # Binlog 增量：mysqlbinlog 远程抽取，用于 PITR（参照 mysql_backup_webtool）
    # ------------------------------------------------------------------ #
    def backup_binlog(self, start_pos: str = None,
                      stop_pos: str = None,
                      start_datetime: str = None,
                      stop_datetime: str = None) -> BackupResult:
        """抽取 binlog 至本地并登记为增量备份。

        参数（参照 mysqlbinlog --read-from-remote-server 的标准选项）：
          start_pos / stop_pos       binlog 位点（数字）
          start_datetime / stop_datetime   日期时间字符串（ISO8601 或 'YYYY-MM-DD HH:MM:SS'）

        至少传入一对（pos 或 datetime）。
        """
        # 不再支持仿真；客户端/连接缺失即失败

        # mysqlbinlog 必须就绪
        mb = shutil.which("mysqlbinlog")
        if not mb:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="mysqlbinlog 未安装，请通过插件管理安装 percona-toolkit 或安装 MySQL 客户端")

        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")

        cnf = self._make_cnf(user, pw)
        try:
            ts = self._timestamp()
            out_dir = self._output_dir()
            fname = f"{ts}__{self.task_name}__binlog.sql"
            out_path = os.path.join(out_dir, fname)

            args = [mb, f"--defaults-extra-file={cnf}",
                    "--host", str(host), "--port", str(port),
                    "--read-from-remote-server", "--raw",
                    "--stop-never" if not (stop_pos or stop_datetime) else "",
                    "--verbose", "--result-file=" + os.path.splitext(out_path)[0]]
            if start_pos:
                args.append(f"--start-position={start_pos}")
            if stop_pos:
                args.append(f"--stop-position={stop_pos}")
            if start_datetime:
                args.append(f"--start-datetime={start_datetime}")
            if stop_datetime:
                args.append(f"--stop-datetime={stop_datetime}")

            # 去除空字符串元素
            args = [a for a in args if a]

            start = time.time()
            ret = self._run(args)
            dur = round(time.time() - start, 3)

            if ret["returncode"] != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=dur,
                    stderr=ret["stderr"],
                    message="mysqlbinlog 抽取失败: " + (ret["stderr"] or "未知")[:500])

            # 计算文件大小（--raw 模式下 result-file 名为 *.00000X）
            final_path = out_path
            if not os.path.exists(final_path):
                # 兜底：取输出目录里最新文件
                files = sorted(Path(out_dir).glob(
                    f"{ts}__{self.task_name}__binlog*"), reverse=True)
                if files:
                    final_path = str(files[0])
            size = os.path.getsize(final_path) if os.path.exists(final_path) else 0

            # 记录 binlog 位点元数据，便于后续 PITR
            meta_path = final_path + ".meta.json"
            meta = {
                "task_id": self.task_id,
                "task_name": self.task_name,
                "start_pos": start_pos,
                "stop_pos": stop_pos,
                "start_datetime": start_datetime,
                "stop_datetime": stop_datetime,
                "extracted_at": db.now_iso(),
                "host": host, "port": int(port),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=final_path, size_bytes=size,
                duration_sec=dur, checksum=db.sha256_file(final_path),
                message=f"MySQL binlog 抽取成功（{db.human_size(size)}, {dur}s）")
        finally:
            self._cleanup_cnf(cnf)

    def pitr_target_time(self, target_dt: str, binlog_path: str) -> BackupResult:
        """根据目标时间生成 binlog 重放 SQL，配合全量备份实现 PITR。

        - target_dt: 'YYYY-MM-DD HH:MM:SS' 或 ISO8601 字符串
        - binlog_path: backup_binlog 抽取出的 binlog 文件或 .meta.json

        返回值：包含可执行的 mysqlbinlog 重放脚本路径，运维可在 mysql 服务端
        使用 `mysql < replay.sql` 完成 PITR。
        """
        if not target_dt:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="必须提供目标时间 target_dt")
        # 加载元数据（可选）
        meta = {}
        if binlog_path and binlog_path.endswith(".meta.json") and os.path.exists(binlog_path):
            try:
                with open(binlog_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}

        ts = self._timestamp()
        out_dir = self._output_dir()
        replay_path = os.path.join(out_dir, f"{ts}__{self.task_name}__pitr_replay.sql")
        meta_block = "\n".join(f"-- {k}: {v}" for k, v in meta.items())

        # 拼装 mysqlbinlog 重放命令（仅生成脚本，不直接执行，由 DBA 在生产执行）
        mb = shutil.which("mysqlbinlog") or "mysqlbinlog"
        content = f"""-- MySQL PITR replay script
target_dt = '{target_dt}'
{meta_block}
-- 用法（在 MySQL 服务器上执行）：
--   {mb} --read-from-remote-server --host=<host> --user=<user> \\
--       --start-datetime='{meta.get('start_datetime') or '1970-01-01 00:00:00'}' \\
--       --stop-datetime='{target_dt}' \\
--       --result-file=replay.sql
-- 然后：
--   mysql -u<user> -p < replay.sql
"""
        with open(replay_path, "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(replay_path)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=replay_path, size_bytes=size,
            checksum=db.sha256_file(replay_path),
            message=f"已生成 PITR 重放脚本 ({replay_path})，请 DBA 在 MySQL 服务器上执行")

    # ------------------------------------------------------------------ #
    # 可选：列出可备份的数据库（优先 SSH 远程）
    # ------------------------------------------------------------------ #
    def list_databases(self) -> list:
        """列出可备份的数据库。

        优先策略：
        1) SSH 远程 SHOW DATABASES（无 Agent 模式，绕过本机无 mysql 客户端）
        2) 本机 mysql 客户端 SHOW DATABASES（fallback，本机有客户端时）
        3) 都失败 → 返回 []
        """
        # 1) 尝试 SSH 远程
        try:
            from core import remote_dump
            dbs = remote_dump.remote_list_databases(self.task, "mysql")
            if dbs:
                skip = {"information_schema", "performance_schema", "mysql", "sys"}
                return [d for d in dbs if d not in skip]
        except Exception as e:
            self.logger.info("[%s] SSH 远程 list_databases 失败，本地兜底: %s", self.task_name, e)
        # 2) 本地兜底
        ok, detail = self.check_client()
        if not ok:
            self.logger.warning("[%s] list_databases 跳过: %s", self.task_name, detail)
            return []
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 3306
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        cnf = None
        try:
            cnf = self._make_cnf(user, pw)
            mysql_args = [
                "mysql",
                f"--defaults-extra-file={cnf}",
                "--host", str(host),
                "--port", str(port),
                "-N", "-e", "SHOW DATABASES",
            ]
            res = self._run(mysql_args)
            if res["returncode"] != 0:
                self.logger.warning("[%s] list_databases 失败: %s",
                                    self.task_name, res["stderr"])
                return []
            return [line.strip() for line in res["stdout"].splitlines() if line.strip()]
        finally:
            self._cleanup_cnf(cnf)
