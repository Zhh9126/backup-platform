# -*- coding: utf-8 -*-
"""
Oracle 备份/恢复引擎（基于 Oracle 官方客户端）。

Oracle 客户端前置条件（重要）：
- 必须在运行环境安装 Oracle 客户端工具，并正确配置环境变量
  ORACLE_HOME（指向 Oracle 客户端/数据库软件安装目录），且将
  $ORACLE_HOME/bin 加入 PATH，否则无法探测到 expdp / impdp / exp / imp。
- Data Pump（expdp / impdp）是“服务端”工具：导出的 DUMPFILE 与 LOGFILE
  实际生成在**数据库服务器**上，而非本机。因此需要：
    1) 在数据库服务端创建一个 DIRECTORY 对象（如 DATA_PUMP_DIR），例如：
         CREATE DIRECTORY DATA_PUMP_DIR AS '/u01/app/oracle/dpump';
    2) 对执行导出/导入的用户授予该目录的读写权限：
         GRANT READ, WRITE ON DIRECTORY DATA_PUMP_DIR TO <user>;
  expdp 命令中通过 DIRECTORY=DATA_PUMP_DIR 引用该服务端目录，
  本机只记录“server-side:DATA_PUMP_DIR/xxx.dmp”形式的逻辑路径，
  无法直接计算文件大小与校验和（size=0, checksum=""）。
- 传统 exp / imp 是“客户端”工具：导出/导入文件生成在**本机**，
  可正常计算 size_bytes 与 sha256 校验和。exp 支持增量导出
  （INCTYPE=INCREMENTAL / INCTYPE=CUMULATIVE）。

本引擎仅使用 Python 标准库 + 外部 Oracle 客户端，不依赖任何第三方包。
"""

import os
import json
import shlex
import shutil
import time

import config
import core.db as db
from core.engines.base import (
    BackupEngine,
    BackupType,
    BackupMode,
    BackupStatus,
    BackupResult,
)


class OracleEngine(BackupEngine):
    """Oracle 数据库备份/恢复引擎。

    支持全量（expdp / 服务端 Data Pump）、增量与传统累计增量（exp / 客户端）、
    以及对应的恢复（impdp / imp）。
    """

    db_type = "oracle"
    display_name = "Oracle"
    # 引擎依赖的客户端可执行文件（用于 PATH 探测）
    required_clients = ["expdp", "impdp", "exp", "imp"]
    # 物理备份：Oracle 自带 RMAN 工具
    physical_bundled_tools = ["rman"]

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def _parse_extra(self) -> dict:
        """解析 task 中的 extra_options（JSON 字符串），返回 dict。"""
        raw = self.task.get("extra_options") or ""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.logger.warning("[%s] extra_options 不是合法 JSON，忽略: %r",
                                 self.task_name, raw)
            return {}

    def _conn_string(self, service: str) -> str:
        """构造 Oracle 连接串：user/password@//host:port/service_name。

        说明：口令已为明文（task 中已解密），Oracle 客户端接受将口令直接
        写入连接串。注意这会把明文口令暴露在进程参数列表中；如需更高安全性
        可改用 /@ 操作系统认证或钱包（wallet）认证，此处保持与平台规范一致。
        """
        user = self.task.get("username") or ""
        pw = db.decrypt_secret(self.task.get("password") or "")
        host = self.task.get("host") or "localhost"
        port = self.task.get("port") or 1521
        return f"{user}/{pw}@//{host}:{port}/{service}"

    def _service_name(self) -> str:
        """解析 Oracle service name，按优先级返回非空值。

        优先级：extra_options.service → task.db_name → task.host。
        三者都为空时返回 ""，调用方应据此返回明确错误（避免静默仿真）。
        """
        return (self._parse_extra().get("service")
                or self.task.get("db_name")
                or self.task.get("host") or "")

    def _client_available(self, name: str) -> bool:
        """判断某个具体客户端是否在 PATH 中可用。"""
        return bool(shutil.which(name))

    def _backup_full(self, conn: str, ts: str, extra: dict) -> BackupResult:
        """全量备份：使用 Data Pump（expdp）导出到数据库服务端 DIRECTORY。

        导出文件位于服务端，本机只记录逻辑路径，size=0, checksum=""。
        若 extra_options 指定了 schemas，则按指定模式导出，否则 FULL=Y 全库。
        """
        if not self._client_available("expdp"):
            self.logger.error("[%s] expdp 不可用，无法执行 Oracle 全量导出", self.task_name)
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="expdp 客户端不可用，无法执行 Oracle 全量导出")

        schemas = extra.get("schemas")
        if schemas:
            # schemas 可为列表或逗号分隔字符串
            if isinstance(schemas, list):
                schemas_arg = ",".join(str(s) for s in schemas)
            else:
                schemas_arg = str(schemas)
            mode_args = ["SCHEMAS=" + schemas_arg]
            mode_desc = f"指定模式(schemas={schemas_arg})"
        else:
            mode_args = ["FULL=Y"]
            mode_desc = "全库(FULL=Y)"

        dumpfile = f"{ts}.dmp"
        logfile = f"{ts}.log"
        cmd = [
            "expdp", conn,
            *mode_args,
            "DIRECTORY=DATA_PUMP_DIR",
            "DUMPFILE=" + dumpfile,
            "LOGFILE=" + logfile,
        ]

        start = time.time()
        res = self._run(cmd, timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
        duration = round(time.time() - start, 3)

        backup_path = f"server-side:DATA_PUMP_DIR/{dumpfile}"
        if res["returncode"] == 0:
            # 服务端文件：本机无法直接读取，size=0, checksum=""
            msg = (f"Oracle 全量备份(expdp)成功({mode_desc})；"
                   f"文件位于数据库服务端 DATA_PUMP_DIR({dumpfile})，"
                   f"需服务端已创建并授权该 DIRECTORY 对象。")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path, size_bytes=0,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum="", message=msg,
            )
        else:
            msg = f"Oracle 全量备份(expdp)失败，returncode={res['returncode']}"
            self.logger.error("[%s] %s | stderr: %s", self.task_name, msg, res["stderr"])
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, size_bytes=0,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum="", message=msg,
            )

    def _backup_exp(self, conn: str, ts: str, out_dir: str,
                    inc_type: str, label: str) -> BackupResult:
        """传统 exp 增量/累计增量导出（输出到本机，可计算 size/checksum）。

        inc_type: "INCREMENTAL" 或 "CUMULATIVE"。
        若 exp 不可用，则回退为 expdp 全量导出，并在 message 中注明回退原因。
        """
        if not self._client_available("exp"):
            # 传统 exp 不可用：回退到 expdp 全量导出
            self.logger.warning("[%s] exp 不可用，%s 备份回退为 expdp 全量导出",
                                 self.task_name, label)
            res = self._backup_full(conn, ts, self._parse_extra())
            res.message = (f"{label} 备份(exp 不可用)已回退为全量导出；" + res.message)
            return res

        dumpfile = os.path.join(out_dir, f"{ts}.dmp")
        logfile = os.path.join(out_dir, f"{ts}.log")
        cmd = [
            "exp", conn,
            "INCTYPE=" + inc_type,
            "FILE=" + dumpfile,
            "LOG=" + logfile,
        ]

        start = time.time()
        res = self._run(cmd, timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
        duration = round(time.time() - start, 3)

        if res["returncode"] == 0 and os.path.exists(dumpfile):
            size = os.path.getsize(dumpfile)
            checksum = db.sha256_file(dumpfile)
            msg = f"Oracle {label}备份(exp {inc_type})成功，本地文件: {dumpfile}"
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=dumpfile, size_bytes=size,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum=checksum, message=msg,
            )
        else:
            msg = (f"Oracle {label}备份(exp {inc_type})失败，"
                   f"returncode={res['returncode']}")
            self.logger.error("[%s] %s | stderr: %s", self.task_name, msg, res["stderr"])
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=None, size_bytes=0,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum="", message=msg,
            )

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def backup(self, backup_type: BackupType) -> BackupResult:
        """Oracle 备份：按 backup_mode 分发物理(RMAN)/逻辑(expdp/exp)。

        逻辑备份优先在 SSH 备份机/数据库服务器执行 exp，失败再回退本机 expdp/exp。
        """
        # demo_only / DEMO_MODE 不再触发仿真，统一走真实备份

        if self.backup_mode == BackupMode.PHYSICAL:
            # 物理备份优先走 SSH 远端真实 RMAN（Oracle 服务器自带 rman，
            # 且 db_recovery_file_dest 未配置需显式 FORMAT），失败再回退本机。
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "Oracle 物理备份(RMAN)",
            )
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "Oracle 逻辑备份(exp/expdp)",
        )

    def _backup_physical(self, backup_type: BackupType) -> BackupResult:
        """物理备份(本机兜底)：本地执行 RMAN。

        通常本机无 Oracle 客户端/实例，会被 _backup_physical_remote 的 SSH 远端
        路径优先替代。此处仅作为「无 SSH 主机」时的兜底：若本机确实装有 rman
        且配置好 ORACLE_HOME / 归档模式，则本地执行；否则如实返回 FAILED
        （不静默仿真）。显式 FORMAT 以规避 db_recovery_file_dest 未配置问题。
        """
        if not shutil.which("rman"):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="本机未安装 rman 客户端，无法执行本地物理备份；"
                        "请通过 SSH 纳管 Oracle 服务器以走远端 RMAN。")
        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        script = os.path.join(out_dir, f"rman_{ts}.cmd")
        fmt_db = os.path.join(out_dir, f"ora_bkp_{ts}_%U.bkp")
        fmt_arch = os.path.join(out_dir, f"ora_bkp_{ts}_arch_%U.bkp")
        lines = ["RUN {"]
        if backup_type == BackupType.INCREMENTAL:
            lines.append(
                f"  BACKUP AS COMPRESSED BACKUPSET INCREMENTAL LEVEL 0 DATABASE "
                f"FORMAT '{fmt_db}';")
        else:
            lines.append(f"  BACKUP AS COMPRESSED BACKUPSET DATABASE FORMAT '{fmt_db}';")
        lines.append(
            f"  BACKUP AS COMPRESSED BACKUPSET ARCHIVELOG ALL FORMAT '{fmt_arch}';")
        lines.append("}")
        lines.append("exit;")
        with open(script, "w") as f:
            f.write("\n".join(lines))
        start = time.time()
        ret = self._run(["rman", "target", "/", f"@{script}"], timeout=7200)
        dur = round(time.time() - start, 3)
        if ret["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"RMAN 物理备份失败: {ret.get('stderr','')[:500]}")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            backup_path=out_dir, duration_sec=dur,
                            stdout=ret.get("stdout", ""),
                            message="Oracle 物理备份(RMAN)成功(本机)")

    def _backup_physical_remote(self, ssh_host: dict, backup_type) -> BackupResult:
        """物理备份：通过 SSH 在远端 Oracle 服务器以 oracle 用户执行 RMAN，
        并用 SFTP 把生成的备份片拉回本机落盘。

        关键约束（基于对 192.168.220.129 的真实探测）：
        - Oracle 工具链仅在 oracle 用户 profile 中可用，必须用 `su - oracle -c`
          执行，不能以 root 直接跑 rman；
        - db_recovery_file_dest 未配置(size=0) → 必须显式 FORMAT 指定备份路径，
          否则 ORA-19801；
        - rman target / @script 已建立连接，脚本内不要再写 connect target /；
        - 备份片落在远端 /u01/app/oracle/backup/，再经 SFTP 拉回本机。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        bkp_prefix = f"ora_bkp_{ts}_"
        remote_dir = "/u01/app/oracle/backup"
        remote_cmd_file = f"{remote_dir}/rman_{ts}.cmd"

        # 1) 组装 RMAN 脚本（注意：rman target / @file 已建立连接，脚本内不要再 connect）
        if backup_type == BackupType.INCREMENTAL:
            db_line = (
                f"  BACKUP AS COMPRESSED BACKUPSET INCREMENTAL LEVEL 0 DATABASE "
                f"FORMAT '{remote_dir}/{bkp_prefix}%U.bkp';")
        else:
            db_line = (
                f"  BACKUP AS COMPRESSED BACKUPSET DATABASE "
                f"FORMAT '{remote_dir}/{bkp_prefix}%U.bkp';")
        rman_script = (
            "RUN {\n"
            f"{db_line}\n"
            f"  BACKUP AS COMPRESSED BACKUPSET ARCHIVELOG ALL "
            f"FORMAT '{remote_dir}/{bkp_prefix}arch_%U.bkp';\n"
            "}\n"
            "exit;\n"
        )

        client = remote_dump._connect(ssh_host)
        # 确保远端备份目录存在且 oracle 可写：
        # SSH 登录身份为 root，故以 root 建目录并把属主改为 oracle
        # （rman 以 oracle 用户运行，否则会 ORA-19504/ORA-27040 权限拒绝）。
        prep = (f"mkdir -p {remote_dir} && chown oracle {remote_dir} "
                f"&& chmod 755 {remote_dir}")
        _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)
        sftp = client.open_sftp()
        try:
            # 写 RMAN 脚本（oracle 可读）
            with sftp.open(remote_cmd_file, "w") as f:
                f.write(rman_script)
            try:
                sftp.chmod(remote_cmd_file, 0o644)
            except Exception:
                pass

            # 3) 远端执行：mkdir 备份目录 + rman target / @script（以 oracle 用户）
            inner = f"mkdir -p {remote_dir} && rman target / @{remote_cmd_file}"
            shell = f"su - oracle -c {shlex.quote(inner)}"
            wrapped = remote_dump._wrap_login(shell)
            start = time.time()
            out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
            duration = round(time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            err_text = err or ""
            self.logger.info("[%s] 远端 RMAN 返回 rc=%s, 输出尾部: %s",
                             self.task_name, rc, (out_text or err_text)[-600:])

            # 成功标志：rc==0 且输出出现 "Finished backup"
            if rc != 0 or ("Finished backup" not in out_text
                           and "Finished backup" not in err_text):
                snippet = (out_text or err_text)[-1200:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"远端 RMAN 物理备份失败(rc={rc}): {snippet}")

            # 4) SFTP 拉回所有备份片（匹配前缀 ora_bkp_{ts}_）
            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            pieces = []
            for attr in sftp.listdir_attr(remote_dir):
                fname = attr.filename
                if fname.startswith(bkp_prefix):
                    remote_path = f"{remote_dir}/{fname}"
                    local_path = os.path.join(out_dir, fname)
                    sftp.get(remote_path, local_path)
                    pieces.append((local_path, attr.st_size))

            if not pieces:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, duration_sec=duration,
                    stdout=out_text, stderr=err_text,
                    message=f"远端 RMAN 执行成功但未在 {remote_dir} 找到备份片"
                            f"(前缀 {bkp_prefix})，可能 FORMAT 路径不正确。")

            total_size = sum(sz for _, sz in pieces)
            # checksum 取首个备份片(主库数据备份)的 sha256；其余片写入清单
            first_local = pieces[0][0]
            checksum = db.sha256_file(first_local)
            manifest = os.path.join(out_dir, f"{ts}_rman_manifest.txt")
            with open(manifest, "w", encoding="utf-8") as mf:
                mf.write(f"Oracle RMAN physical backup via SSH\n")
                mf.write(f"ssh_host: {ssh_host.get('host_key','')}\n")
                mf.write(f"task: {self.task_name}\n")
                mf.write(f"backup_type: {backup_type.value}\n")
                mf.write(f"remote_dir: {remote_dir}\n")
                for p, sz in pieces:
                    mf.write(f"{os.path.basename(p)}\t{sz}\t{db.sha256_file(p)}\n")

            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 以 oracle 用户执行 RMAN 物理备份成功，"
                   f"已拉回 {len(pieces)} 个备份片，共 {db.human_size(total_size)}"
                   f"（主片: {os.path.basename(first_local)}）")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=first_local, size_bytes=total_size,
                duration_sec=duration, stdout=out_text, stderr=err_text,
                simulated=False, checksum=checksum, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _backup_logical_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """在 SSH 备份机(数据库服务器)以 oracle 用户执行 expdp(Data Pump)，
        再用 SFTP 把 dmp / log 拉回本机落盘，计算真实 size 与 sha256。

        - 连接串优先 system/<pw>@//host:port/service（已在 192.168.220.129 实测
          可用）；若失败回退 / as sysdba（仍由 oracle 用户执行）。
        - expdp 的服务端目录 DATA_PUMP_DIR 已存在(/u01/app/oracle/admin/orcl11g/
          dpdump)，dmp/log 生成在远端，拉回本机即可得真实文件与校验和。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        service = self._service_name()
        if not service:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="无法确定连接 service name：请填写 db_name 或服务名(service)")

        extra = self._parse_extra()
        schemas = extra.get("schemas")
        if schemas:
            if isinstance(schemas, list):
                schemas_arg = ",".join(str(s) for s in schemas)
            else:
                schemas_arg = str(schemas)
            mode_args = f"SCHEMAS={schemas_arg}"
            mode_desc = f"指定模式(schemas={schemas_arg})"
        else:
            mode_args = "FULL=Y"
            mode_desc = "全库(FULL=Y)"

        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 1521
        username = self.task.get("username") or "system"
        pw = db.decrypt_secret(self.task.get("password") or "")
        conn_easy = f"{username}/{pw}@//{host}:{port}/{service}"

        dp_dir = "/u01/app/oracle/admin/orcl11g/dpdump"
        remote_dmp = f"{dp_dir}/{ts}.dmp"
        remote_log = f"{dp_dir}/{ts}.log"

        # 远端脚本：先试 system/<pw>@//host:port/service，失败回退 / as sysdba
        expdp_sh = (
            "#!/bin/bash\n"
            "export ORACLE_HOME=/u01/app/oracle/product/19.0.0/dbhome_1\n"
            "export ORACLE_SID=orcl11g\n"
            "export PATH=$ORACLE_HOME/bin:$PATH\n"
            f"expdp {conn_easy} {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={ts}.dmp LOGFILE={ts}.log\n"
            "RC=$?\n"
            "if [ $RC -ne 0 ]; then\n"
            f"  echo '[fallback] primary expdp failed (rc=$RC), retry with / as sysdba'\n"
            f"  expdp \"/ as sysdba\" {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={ts}.dmp LOGFILE={ts}.log\n"
            "  RC=$?\n"
            "fi\n"
            'echo "EXPDP_RC=$RC"\n'
            "exit $RC\n"
        )

        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        try:
            try:
                sftp.mkdir("/u01/app/oracle/backup")
            except IOError:
                pass
            remote_sh = f"/u01/app/oracle/backup/expdp_{ts}.sh"
            with sftp.open(remote_sh, "w") as f:
                f.write(expdp_sh)
            try:
                sftp.chmod(remote_sh, 0o755)
            except Exception:
                pass

            inner = f"bash {remote_sh}"
            shell = f"su - oracle -c {shlex.quote(inner)}"
            wrapped = remote_dump._wrap_login(shell)
            start = time.time()
            out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
            duration = round(time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            err_text = err or ""
            self.logger.info("[%s] 远端 expdp 返回 rc=%s", self.task_name, rc)

            if rc != 0:
                snippet = (out_text or err_text)[-1500:]
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"远端 expdp 逻辑备份失败(rc={rc}): {snippet}")

            # 校验远端 dmp 确实存在
            try:
                dmp_attr = sftp.stat(remote_dmp)
            except IOError:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"远端 expdp 执行返回成功，但未找到 dmp 文件: {remote_dmp}")

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            local_dmp = os.path.join(out_dir, f"{ts}.dmp")
            local_log = os.path.join(out_dir, f"{ts}.log")
            sftp.get(remote_dmp, local_dmp)
            try:
                sftp.get(remote_log, local_log)
            except IOError:
                local_log = None  # log 缺失不致命

            size = os.path.getsize(local_dmp)
            checksum = db.sha256_file(local_dmp)
            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 以 oracle 用户执行 expdp({mode_desc})成功，"
                   f"已拉回 dmp: {local_dmp} ({db.human_size(size)})")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=local_dmp, size_bytes=size, duration_sec=duration,
                stdout=out_text, stderr=err_text, simulated=False,
                checksum=checksum, message=msg)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _backup_logical_local(self, backup_type: BackupType) -> BackupResult:
        """逻辑备份：expdp / exp（沿用原有实现）。"""

        ts = self._timestamp()
        extra = self._parse_extra()
        service = self._service_name()
        if not service:
            msg = "无法确定连接 service name（extra_options.service、db_name、host 均为空）"
            self.logger.error("[%s] %s", self.task_name, msg)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                 message=msg)
        conn = self._conn_string(service)
        out_dir = self._output_dir()

        # 2) 按备份类型分派
        if backup_type in (BackupType.FULL, BackupType.SNAPSHOT):
            # 全量 / 快照统一用 Data Pump 全量导出
            return self._backup_full(conn, ts, extra)
        elif backup_type == BackupType.INCREMENTAL:
            return self._backup_exp(conn, ts, out_dir, "INCREMENTAL", "incremental")
        elif backup_type == BackupType.DIFFERENTIAL:
            return self._backup_exp(conn, ts, out_dir, "CUMULATIVE", "differential")
        else:
            msg = f"不支持的备份类型: {backup_type}"
            self.logger.error("[%s] %s", self.task_name, msg)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                 message=msg)

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行 Oracle 恢复。

        流程：先做演示/仿真检测；通过后再根据备份路径判断是服务端导出
        （impdp）还是本机 exp 导出（imp），构造相应恢复命令。
        """
        # demo_only / DEMO_MODE 不再触发仿真，统一走真实恢复

        # 跨主机恢复（Oracle 用 impdp 模式由 cross_host 处理）
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            return self._try_cross_host_restore(backup_path, target_host_info,
                                                 kwargs.get("target_db") or "")

        if not backup_path:
            msg = "未提供备份路径 backup_path，无法恢复"
            self.logger.error("[%s] %s", self.task_name, msg)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                 message=msg)

        extra = self._parse_extra()
        service = self._service_name()
        if not service:
            msg = "无法确定连接 service name（extra_options.service、db_name、host 均为空）"
            self.logger.error("[%s] %s", self.task_name, msg)
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                 message=msg)
        conn = self._conn_string(service)

        # 2) 判断备份文件位置：server-side（expdp）还是本机（exp）
        if backup_path.startswith("server-side:"):
            # 服务端 Data Pump 导出：使用 impdp 恢复
            if not self._client_available("impdp"):
                self.logger.error("[%s] impdp 不可用，无法执行服务端恢复", self.task_name)
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                   message="impdp 客户端不可用，无法执行服务端恢复")
            # 形如 server-side:DATA_PUMP_DIR/xxx.dmp
            remainder = backup_path[len("server-side:"):]
            directory, _, dumpfile = remainder.rpartition("/")
            if not directory:
                directory = "DATA_PUMP_DIR"
            cmd = [
                "impdp", conn,
                "DIRECTORY=" + directory,
                "DUMPFILE=" + dumpfile,
            ]
            tool_name = "impdp"
        else:
            # 本机 exp 导出：使用传统 imp 恢复
            if not self._client_available("imp"):
                self.logger.error("[%s] imp 不可用，无法执行本地恢复", self.task_name)
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                   message="imp 客户端不可用，无法执行本地恢复")
            if not os.path.exists(backup_path):
                msg = f"本地备份文件不存在: {backup_path}"
                self.logger.error("[%s] %s", self.task_name, msg)
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                     message=msg)
            cmd = [
                "imp", conn,
                "FILE=" + backup_path,
                "FULL=Y",
            ]
            tool_name = "imp"

        start = time.time()
        res = self._run(cmd, timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
        duration = round(time.time() - start, 3)

        if res["returncode"] == 0:
            msg = f"Oracle 恢复({tool_name})成功，来源: {backup_path}"
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path, size_bytes=0,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum="", message=msg,
            )
        else:
            msg = f"Oracle 恢复({tool_name})失败，returncode={res['returncode']}"
            self.logger.error("[%s] %s | stderr: %s", self.task_name, msg, res["stderr"])
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, size_bytes=0,
                duration_sec=duration, stdout=res["stdout"],
                stderr=res["stderr"], simulated=False,
                checksum="", message=msg,
            )

    def list_databases(self) -> list:
        """列出可备份的数据库/实例。

        Oracle 列出用户/模式通常需要 sqlplus 等额外工具，此处按规范简化，
        返回空列表（由平台在 UI 上提示用户手动填写 db_name / service）。
        """
        return []

    # ------------------------------------------------------------------ #
    # RMAN PITR (基于时间点的恢复，参照 oracle_backup_web_tool)
    # ------------------------------------------------------------------ #
    def rman_pitr(self, target_time: str) -> BackupResult:
        """生成 RMAN 时间点恢复（PITR）脚本并可选立即执行。

        必要前提（参照 oracle_backup_web_tool/windows/oracle_physical_backup.bat）：
        - 数据库处于 ARCHIVELOG 模式；
        - 已存在有效 RMAN 全量/0级备份；
        - 已配置闪回区（db_recovery_file_dest）或归档日志目录。

        target_time: 'YYYY-MM-DD HH:MM:SS'，DBA 期望恢复到的时间点。

        生成 RMAN 命令样例：
          RUN {
            SET UNTIL TIME "TO_DATE('2024-08-01 12:00:00','YYYY-MM-DD HH24:MI:SS')";
            RESTORE DATABASE;
            RECOVER DATABASE;
            ALTER DATABASE OPEN RESETLOGS;
          }
        """
        if not target_time:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="必须提供 target_time")

        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        script = os.path.join(out_dir, f"rman_pitr_{ts}.rman")

        content = f"""-- RMAN PITR script generated by backup_platform
-- target_time: {target_time}
connect target /;
RUN {{
    SET UNTIL TIME "TO_DATE('{target_time}','YYYY-MM-DD HH24:MI:SS')";
    RESTORE DATABASE;
    RECOVER DATABASE;
    ALTER DATABASE OPEN RESETLOGS;
}}
exit;
"""
        with open(script, "w", encoding="utf-8") as f:
            f.write(content)

        # 检查 rman 可用性
        rman = shutil.which("rman")
        if not rman:
            return BackupResult(
                success=True, status=BackupStatus.SIMULATED,
                backup_path=script, simulated=True,
                message=f"rman 不可用，仅生成 PITR 脚本：{script}")

        # 直接执行（需 OS 认证 / target /）
        start = time.time()
        ret = self._run([rman, "target", "/", f"@{script}"], timeout=14400)
        dur = round(time.time() - start, 3)
        if ret["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                duration_sec=dur, backup_path=script,
                stderr=ret["stderr"],
                message=f"RMAN PITR 执行失败: {(ret['stderr'] or '')[:500]}")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=script, duration_sec=dur,
            stdout=ret["stdout"], simulated=False,
            message=f"Oracle RMAN PITR 成功（恢复到 {target_time}，耗时 {dur}s）")

    def archivelog_backup(self) -> BackupResult:
        """RMAN 归档日志备份（保证 PITR 窗口）。

        必须先开启归档模式：
          SHUTDOWN IMMEDIATE;
          STARTUP MOUNT;
          ALTER DATABASE ARCHIVELOG;
          ALTER DATABASE OPEN;

        本方法生成如下 RMAN 脚本：
          BACKUP ARCHIVELOG ALL NOT BACKED UP DELETE INPUT;
        """
        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        script = os.path.join(out_dir, f"rman_arch_{ts}.rman")
        content = """-- RMAN archive log backup (保障 PITR 窗口)
connect target /;
BACKUP ARCHIVELOG ALL NOT BACKED UP DELETE INPUT;
exit;
"""
        with open(script, "w", encoding="utf-8") as f:
            f.write(content)

        rman = shutil.which("rman")
        if not rman:
            return BackupResult(
                success=True, status=BackupStatus.SIMULATED,
                backup_path=script, simulated=True,
                message=f"rman 不可用，仅生成归档备份脚本：{script}")

        start = time.time()
        ret = self._run([rman, "target", "/", f"@{script}"], timeout=7200)
        dur = round(time.time() - start, 3)
        if ret["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                duration_sec=dur, backup_path=script,
                stderr=ret["stderr"],
                message="RMAN 归档备份失败: " + (ret["stderr"] or "")[:500])
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=script, duration_sec=dur, stdout=ret["stdout"],
            message=f"Oracle 归档日志备份成功（{dur}s）")
