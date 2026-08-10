# -*- coding: utf-8 -*-
"""
MySQL / MariaDB 备份引擎实现。

继承 core.engines.base.BackupEngine，通过调用外部客户端
mysqldump 与 mysql 完成逻辑备份与恢复。

设计要点：
- 明文密码绝不进入命令行参数，统一写入临时选项文件
  (.cnf, 权限 0600)，命令中以 --defaults-extra-file 引用，
  结束（或异常）时务必删除该临时文件。
- 备份/恢复均优先走 _should_simulate() 仿真兜底（DEMO_MODE、
  客户端缺失、demo_only 等场景），保证平台在无客户端环境也可演示。
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
        if algo == "none":
            # 纯文本：直接重定向
            return f"{quoted} > {shlex.quote(out_path)}"
        if raw_path:
            # mysqldump | tee 原始副本 | 压缩 > 产物
            return (f"set -o pipefail; {quoted} | tee {shlex.quote(raw_path)} "
                    f"| {comp_str} > {shlex.quote(out_path)}")
        return f"set -o pipefail; {quoted} | {comp_str} > {shlex.quote(out_path)}"

    # ------------------------------------------------------------------ #
    # 备份
    # ------------------------------------------------------------------ #
    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        # 按备份模式分发：物理备份 vs 逻辑备份
        mode = self.backup_mode
        if mode == BackupMode.PHYSICAL:
            return self._try_or_fallback(lambda: self._backup_physical(backup_type),
                                         backup_type, "物理备份")
        # 默认：逻辑备份（mysqldump）
        return self._try_or_fallback(lambda: self._backup_logical(backup_type),
                                     backup_type, "逻辑备份")

    def _try_or_fallback(self, fn, backup_type, label):
        """尝试本机执行 → SSH远程 → 返回错误。"""
        try:
            result = fn()
            if result.success:
                return result
            reason = result.message or "未知错误"
        except Exception as e:
            reason = str(e)

        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host:
            self.logger.info("[%s] %s失败(%s)，改用SSH远程dump", self.task_name, label, reason)
            try:
                data = remote_dump.remote_db_dump(
                    self.task, ssh_host, "mysql",
                    int(self.task.get("compress") or 0))
                ext = ".sql.gz" if int(self.task.get("compress") or 0) else ".sql"
                return self._write_dump_file(data, backup_type, ssh_host, ext, "mysqldump")
            except Exception as e:
                reason = f"本机与SSH远程均失败: {e}"
        return BackupResult(success=False, status=BackupStatus.FAILED, message=reason)

    # ------------------ 物理备份 (XtraBackup) ------------------
    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        """物理备份：xtrabackup 全量/增量。参照 mysql_backup_webtool/物理备份.txt。"""
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
            cmd += ["--compress=zstd", "--compress-threads=4"]

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
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=target_dir, size_bytes=0, duration_sec=dur,
                            stdout=ret.stdout, simulated=False, checksum="",
                            message=f"MySQL 物理备份(XtraBackup)成功 {note}")

    # ------------------ 逻辑备份 (mysqldump) ------------------
    def _backup_logical(self, backup_type: BackupType) -> BackupResult:
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
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 0) 跨主机恢复：SFTP 推送到目标主机 → SSH 远程执行 mysql
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_db = kwargs.get("target_db") or self.task.get("db_name") or ""
            self.logger.info("[%s] 跨主机恢复 -> %s", self.task_name,
                             target_host_info.get("hostname"))
            return self._try_cross_host_restore(backup_path, target_host_info, target_db)

        # 1) 先尝试本机直接执行 mysql 恢复
        result = self._restore_local(backup_path, **kwargs)
        if result.success:
            return result

        # 2) 本机失败 -> 尝试通过 SSH 在数据库服务器执行恢复
        reason = (result.message or "未知错误")
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if ssh_host and os.path.exists(backup_path):
            self.logger.info(
                "[%s] 本机恢复失败(%s)，改用 SSH 在数据库服务器执行恢复 (host=%s)",
                self.task_name, reason, ssh_host.get("host_key"))
            try:
                with open(backup_path, "rb") as f:
                    dump_bytes = f.read()
                remote_dump.remote_db_restore(
                    self.task, ssh_host, "mysql", dump_bytes)
                target_db = kwargs.get("target_db") or self.task.get("db_name")
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path,
                    message="通过 SSH 在数据库服务器恢复成功"
                            + (f"（目标库: {target_db}）" if target_db else ""))
            except Exception as e:
                self.logger.error("[%s] 远程恢复也失败: %s", self.task_name, e)
                reason = f"本机与远程恢复均失败: {e}"

        # 3) 返回真实错误
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            backup_path=backup_path, message=reason)

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

            # mysql 客户端基础参数
            mysql_args = [
                "mysql",
                f"--defaults-extra-file={cnf}",
                "--host", str(host),
                "--port", str(port),
            ]
            if target_db:
                mysql_args.append(target_db)
            quoted = " ".join(shlex.quote(a) for a in mysql_args)

            # 依据压缩算法选择解压流式恢复或直接恢复
            # 统一使用 pipe_decompress：zstd(.zst) / gzip(.gz) 均可正确解压恢复
            # 加 set -o pipefail 防止解压失败时 mysql 仍执行成功
            if backup_path.endswith((".gz", ".zst")):
                dec = self.pipe_decompress("zstd" if backup_path.endswith(".zst") else "gzip")
                dec_str = " ".join(shlex.quote(c) for c in dec)
                inner = f"set -o pipefail; {dec_str} {shlex.quote(backup_path)} | {quoted}"
            else:
                inner = f"{quoted} < {shlex.quote(backup_path)}"

            res = self._run(["sh", "-c", inner])
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

        # 2) DEMO / 客户端缺失：生成仿真合成全量
        sim, _reason = self._should_simulate()
        if sim:
            return self._synthesize_full_simulated(base, incs, target_storage_tier)

        # 3) 物理模式：xtrabackup 合并
        if self.backup_mode == BackupMode.PHYSICAL:
            return self._synthesize_full_physical(base, incs, target_storage_tier)

        # 4) 逻辑模式：合成全量记录（SQL + 增量 binlog 重放占位）
        return self._synthesize_full_logical(base, incs, target_storage_tier)

    def _synthesize_full_simulated(self, base, incs, target_storage_tier):
        d = self._output_dir()
        ts = self._timestamp()
        fname = f"{ts}__{self.task_name}__synthetic_full.sim"
        fpath = os.path.join(d, fname)
        chain = []
        if base:
            chain.append(base.get("object_key") or f"set#{base.get('id')}")
        chain += [i.get("object_key") or f"set#{i.get('id')}" for i in incs]
        payload = {
            "simulated": True,
            "note": "合成全量(仿真)：将增量链合并为一份完整备份集的占位产物",
            "task_id": self.task_id, "task_name": self.task_name,
            "db_type": self.db_type,
            "chain": chain, "merged_incremental_count": len(incs),
            "generated_at": db.now_iso(),
        }
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        size = os.path.getsize(fpath)
        self.logger.info("[%s] 合成全量(仿真) 完成，合并 %d 个增量",
                         self.task_name, len(incs))
        return BackupResult(
            success=True, status=BackupStatus.SIMULATED,
            backup_path=fpath, size_bytes=size, simulated=True,
            checksum=db.sha256_file(fpath),
            message=f"合成全量(仿真)成功，合并 {len(incs)} 个增量")

    def _synthesize_full_physical(self, base, incs, target_storage_tier):
        xtrabackup = shutil.which("xtrabackup") or "/opt/xtrabackup/bin/xtrabackup"
        if not os.path.isfile(xtrabackup) or not base or not base.get("object_key"):
            # 缺少 xtrabackup 或全量基：退化为仿真，保证链路闭环
            self.logger.warning(
                "[%s] 物理合成全量缺少 xtrabackup 或全量基，退化仿真",
                self.task_name)
            return self._synthesize_full_simulated(base, incs, target_storage_tier)
        base_dir = base["object_key"]
        if not os.path.isdir(base_dir):
            return self._synthesize_full_simulated(base, incs, target_storage_tier)
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
        if self.task.get("demo_only") or config.DEMO_MODE == "on":
            return self._simulate_backup(
                BackupType.INCREMENTAL,
                "演示环境 binlog 抽取以仿真方式返回")

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
