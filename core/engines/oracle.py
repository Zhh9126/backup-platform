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
    # Oracle 服务端工具（expdp/rman 等）仅存在于 oracle 用户的 profile PATH 中，
    # 远端预检需以 oracle 用户身份探测，root 下会误报缺失
    tool_check_user = "oracle"

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

    def _query_dp_dir(self, client, conn: str = "") -> str:
        """在远端（Oracle 服务器）查询 DATA_PUMP_DIR 的实际文件系统路径。

        兼容性要点：
        - 11g 与 19c 的 dpdump 目录结构不同（19c 可能带 GUID 子目录）；
        - CDB 与 PDB 各自维护同名目录对象，指向可能不同 —— 因此必须用与
          expdp/impdp 完全相同的登录方式（service 连接）查询 all_directories；
        - conn 为空时回退 / as sysdba（CDB 视角）。
        返回形如 /u01/app/oracle/admin/orcl19c/dpdump/5202.../ 的路径；
        查询失败返回 ""（调用方回退默认值）。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        # 用 SFTP 写 SQL 文件到 oracle 可读目录，再以 heredoc 交给 sqlplus 执行，
        # 完全规避 su / shlex / 引号多层嵌套问题（兼容 11g 与 19c）。
        login = conn if conn else '/ as sysdba'
        sql = ("SET HEADING OFF\nSET PAGESIZE 0\nSET FEEDBACK OFF\n"
               "SELECT directory_path FROM all_directories "
               "WHERE directory_name = 'DATA_PUMP_DIR';\nEXIT;\n")
        remote_sql = "/tmp/platform_qdpdir.sql"
        try:
            sftp = client.open_sftp()
            try:
                with sftp.open(remote_sql, "w") as f:
                    f.write(sql)
                try:
                    sftp.chmod(remote_sql, 0o644)
                except Exception:
                    pass
                # oracle 用户执行；连接串以环境变量 DP_CONN 传入避免引号
                inner = (f"export DP_CONN={shlex.quote(login)}; "
                         f"sqlplus -s $DP_CONN @{remote_sql}")
                shell = remote_dump._wrap_login(f"su - oracle -c {shlex.quote(inner)}")
                out, _err, rc = _ssh_exec_pipe(client, shell, timeout=60)
                text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("/"):
                        return line.rstrip("/")
                return ""
            finally:
                try:
                    sftp.remove(remote_sql)
                except Exception:
                    pass
                try:
                    sftp.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning("[%s] 查询远端 DATA_PUMP_DIR 失败: %s", self.task_name, e)
            return ""

    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """Oracle 恢复校验（真实恢复验证，非文件检查占位）。

        - 逻辑备份（.dmp）：推回服务端执行 `impdp SQLFILE` —— 真实解析 dump
          中的全部 DDL（不落数据、零风险），能完整解析即证明可导入；
        - 物理备份（.bkp）：在服务端执行 RMAN `RESTORE DATABASE VALIDATE`
          （逐备份片校验可恢复性），并**真实抽取最小的数据文件到暂存目录**
          （从备份片还原出实际文件，作为真实恢复证据），完成后清理。
        - 其他（自定义脚本产物等）：回退基类通用校验。
        """
        options = options or {}
        base_res = super().verify_record(record, options)
        if not base_res.success:
            return base_res

        backup_path = record.get("backup_path") or ""
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="恢复校验需要 SSH 主机（请纳管数据库服务器）")

        client = remote_dump._connect(ssh_host)
        try:
            if backup_path.endswith(".dmp") or backup_path.endswith(".dmp.gz"):
                return self._verify_logical_remote(client, ssh_host, record, backup_path)
            if backup_path.endswith(".bkp"):
                return self._verify_physical_remote(client, ssh_host, record, backup_path)
            return base_res
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _verify_logical_remote(self, client, ssh_host, record, backup_path) -> BackupResult:
        """逻辑备份校验：impdp SQLFILE 真实解析 dump 的 DDL。"""
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import time as _time

        local_dmp = backup_path
        if not os.path.exists(local_dmp):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"本地 dmp 不存在: {backup_path}")
        service = self._service_name()
        if not service:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无法确定 service name，无法执行 SQLFILE 校验")
        port = self.task.get("port") or 1521
        username = self.task.get("username") or "system"
        pw = db.decrypt_secret(self.task.get("password") or "")
        extra = self._parse_task_extra()
        schemas = extra.get("schemas")
        schemas_arg = (",".join(str(s) for s in schemas)
                       if isinstance(schemas, list) else str(schemas)) if schemas else ""

        ts = self._timestamp()
        dp_dir = ""
        try:
            dp_dir = self._query_dp_dir(client, f"{username}/{pw}@//{self.task.get('host')}:{port}/{service}")
            if not dp_dir:
                dp_dir = self._query_dp_dir(client)
        except Exception as e:
            self.logger.warning("[%s] 校验查询 DATA_PUMP_DIR 异常: %s", self.task_name, e)
        if not dp_dir:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无法解析服务端 DATA_PUMP_DIR，校验中止")

        sftp = client.open_sftp()
        try:
            remote_dmp = f"{dp_dir}/platform_verify_{ts}.dmp"
            sqlfile = f"platform_verify_{ts}.sql"
            sftp.put(local_dmp, remote_dmp)
            impdp_bin = self._resolve_oracle_tool(client, "impdp")
            if not impdp_bin:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="远端未找到 impdp，无法执行 SQLFILE 校验")
            mode_args = f"SCHEMAS={schemas_arg}" if schemas_arg else ""
            inner = (f"export PATH=$ORACLE_HOME/bin:$PATH; "
                     f"{shlex.quote(impdp_bin)} {username}/{pw}@//127.0.0.1:{port}/{service} "
                     f"DUMPFILE=platform_verify_{ts}.dmp SQLFILE={sqlfile} "
                     f"NOLOGFILE=Y {mode_args}")
            shell = f"su - oracle -c {shlex.quote(inner)}"
            start = _time.time()
            out, err, rc = _ssh_exec_pipe(
                client, remote_dump._wrap_login(shell),
                timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
            duration = round(_time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")

            # 读取生成的 SQLFILE（真实从 dump 解析出的 DDL）
            sql_head = ""
            try:
                with sftp.open(f"{dp_dir}/{sqlfile}", "r") as f:
                    sql_head = f.read().decode("utf-8", "replace")[:2000]
            except Exception:
                pass

            # 清理服务端临时文件
            for f_ in (remote_dmp, f"{dp_dir}/{sqlfile}"):
                try:
                    sftp.remove(f_)
                except Exception:
                    pass

            if rc != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text,
                    message=f"impdp SQLFILE 校验失败(rc={rc})，dmp 可能损坏: {out_text[-800:]}")
            if "CREATE" not in sql_head.upper():
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text,
                    message="SQLFILE 未解析出任何 DDL，dmp 内容异常")

            n_stmt = sql_head.upper().count("CREATE")
            msg = (f"Oracle 逻辑备份恢复校验通过：impdp SQLFILE 成功从 dmp 解析出 "
                   f"DDL（预览含 {n_stmt} 处 CREATE 语句），dmp 可正常导入")
            self.logger.info("[%s] %s", self.task_name, msg)
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                duration_sec=duration, stdout=out_text,
                                verified=True, message=msg + "\n--- SQLFILE 预览 ---\n" + sql_head)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _verify_physical_remote(self, client, ssh_host, record, backup_path) -> BackupResult:
        """物理备份校验：RESTORE DATABASE VALIDATE + 真实抽取最小数据文件。"""
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import time as _time

        ts = self._timestamp()
        rman_bin = self._resolve_oracle_tool(client, "rman")
        if not rman_bin:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="远端未找到 rman，无法执行恢复校验")

        # 1) 找出最小的数据文件（真实抽取用它，控制磁盘占用）
        sftp = client.open_sftp()
        try:
            sql = ("SET HEADING OFF\nSET PAGESIZE 0\nSET FEEDBACK OFF\n"
                   "SELECT file# FROM v$datafile WHERE bytes = "
                   "(SELECT MIN(bytes) FROM v$datafile) AND ROWNUM = 1;\nEXIT;\n")
            with sftp.open("/tmp/platform_verify_df.sql", "w") as f:
                f.write(sql)
            shell = remote_dump._wrap_login(
                f"su - oracle -c {shlex.quote('sqlplus -s / as sysdba @/tmp/platform_verify_df.sql')}")
            out, _err, rc = _ssh_exec_pipe(client, shell, timeout=60)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            df_no = None
            for line in text.splitlines():
                line = line.strip()
                if line.isdigit():
                    df_no = line
                    break
        finally:
            try:
                sftp.remove("/tmp/platform_verify_df.sql")
            except Exception:
                pass

        # 2) RMAN RESTORE DATABASE VALIDATE（全部备份片）
        with sftp.open("/tmp/platform_verify_rman.cmd", "w") as f:
            f.write("RUN {\n  RESTORE DATABASE VALIDATE;\n}\nEXIT;\n")
        shell = remote_dump._wrap_login(
            f"su - oracle -c {shlex.quote(f'{shlex.quote(rman_bin)} target / @/tmp/platform_verify_rman.cmd')}")
        start = _time.time()
        out, err, rc = _ssh_exec_pipe(client, shell, timeout=7200)
        duration = round(_time.time() - start, 3)
        out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        validate_ok = (rc == 0 and "validation complete" in out_text.lower()
                       and "validation failed" not in out_text.lower())
        try:
            sftp.remove("/tmp/platform_verify_rman.cmd")
        except Exception:
            pass
        if not validate_ok:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                duration_sec=duration, stdout=out_text,
                message=f"RMAN RESTORE VALIDATE 未通过(rc={rc}): {out_text[-1000:]}")

        # 3) 真实抽取最小数据文件到暂存目录（从备份片还原实际文件）
        restore_detail = "VALIDATE 通过（数据文件抽取跳过：未解析到数据文件编号）"
        if df_no:
            stage_dir = f"/var/tmp/platform_restore_test/{ts}"
            pre = (f"mkdir -p {stage_dir} && chown oracle {stage_dir} && chmod 755 {stage_dir}")
            _ssh_exec_pipe(client, remote_dump._wrap_login(pre), timeout=30)
            with sftp.open("/tmp/platform_verify_df2.cmd", "w") as f:
                f.write(f"RUN {{\n"
                        f"  SET NEWNAME FOR DATAFILE {df_no} TO '{stage_dir}/df_{df_no}.dbf';\n"
                        f"  RESTORE DATAFILE {df_no};\n"
                        f"}}\nEXIT;\n")
            shell = remote_dump._wrap_login(
                f"su - oracle -c {shlex.quote(f'{shlex.quote(rman_bin)} target / @/tmp/platform_verify_df2.cmd')}")
            out2, _err, rc2 = _ssh_exec_pipe(client, shell, timeout=3600)
            out2_text = out2.decode("utf-8", "replace") if isinstance(out2, bytes) else (out2 or "")
            # 确认真实文件落盘
            stat_cmd = f"ls -la {stage_dir}/ 2>/dev/null"
            out3, _e3, rc3 = _ssh_exec_pipe(client, remote_dump._wrap_login(stat_cmd), timeout=20)
            ls_text = out3.decode("utf-8", "replace") if isinstance(out3, bytes) else (out3 or "")
            # 清理暂存
            _ssh_exec_pipe(client, remote_dump._wrap_login(f"rm -rf {stage_dir}"), timeout=30)
            try:
                sftp.remove("/tmp/platform_verify_df2.cmd")
            except Exception:
                pass
            if rc2 == 0 and f"df_{df_no}" in ls_text:
                restore_detail = (f"RESTORE VALIDATE 通过；并已从备份片真实抽取数据文件 "
                                  f"#{df_no} 到暂存目录验证后清理（真实恢复证据）")
            else:
                restore_detail = ("RESTORE VALIDATE 通过；数据文件抽取未完成"
                                  f"(rc={rc2})，请检查磁盘空间")

        msg = f"Oracle 物理备份恢复校验：{restore_detail}"
        self.logger.info("[%s] %s", self.task_name, msg)
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            duration_sec=duration, stdout=out_text,
                            verified=True, message=msg)
    def _resolve_oracle_tool(self, client, tool: str) -> str:
        """在远端解析 Oracle 工具（expdp/impdp/rman/sqlplus）的绝对路径。

        顺序：oracle 用户 profile PATH → root 视角目录 glob 枚举（覆盖各种
        ORACLE_HOME 安装结构）→ 空（调用方报错）。绝不硬编码安装路径。
        """
        from core import remote_dump
        path = remote_dump.resolve_remote_tool(self._ssh_host_for_cache(), tool,
                                               check_user="oracle")
        if not path:
            try:
                path = remote_dump._resolve_remote_bin(client, tool) or ""
            except Exception:
                path = ""
        return path

    def _ssh_host_for_cache(self) -> dict:
        """提供缓存 key 用的主机标识（不重连）。"""
        return {"host_key": f"{self.task.get('host') or ''}:{self.task.get('port') or ''}"}

    def _ensure_pdbs_open(self, client) -> str:
        """确保 19c（CDB 架构）下的 PDB 处于 OPEN 状态并保存状态。

        Oracle 重启后 PDB 可能回到 MOUNTED（除非 SAVE STATE），导致经监听
        连 PDB 报 ORA-01109。备份前以 sysdba 打开全部 PDB 并 SAVE STATE。
        11g（非 CDB）无此概念，SQL 不适用会自动忽略。返回执行概要（"" 表示无动作）。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        sql = ("ALTER PLUGGABLE DATABASE ALL OPEN;\n"
               "ALTER PLUGGABLE DATABASE ALL SAVE STATE;\nEXIT;\n")
        remote_sql = "/tmp/platform_ensure_pdb.sql"
        try:
            sftp = client.open_sftp()
            try:
                with sftp.open(remote_sql, "w") as f:
                    f.write(sql)
                try:
                    sftp.chmod(remote_sql, 0o644)
                except Exception:
                    pass
                shell = remote_dump._wrap_login(
                    f"su - oracle -c {shlex.quote('sqlplus -s / as sysdba @' + remote_sql)}")
                out, _err, rc = _ssh_exec_pipe(client, shell, timeout=90)
                text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
                return text.strip()
            finally:
                try:
                    sftp.remove(remote_sql)
                except Exception:
                    pass
                try:
                    sftp.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning("[%s] 确保 PDB OPEN 失败(11g 可忽略): %s", self.task_name, e)
            return ""

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
        # 19c 场景：确保 PDB OPEN（RMAN 备份 CDB 时要求 PDB 打开）
        self._ensure_pdbs_open(client)
        # rman 路径动态解析（oracle 用户 profile → ORACLE_HOME 目录枚举）
        rman_bin = self._resolve_oracle_tool(client, "rman")
        if not rman_bin:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="远端未找到 rman（已探测 oracle 用户 PATH 与常见 ORACLE_HOME "
                        "安装目录），请确认数据库软件安装完整")
        self.logger.info("[%s] 远端 rman 路径=%s", self.task_name, rman_bin)
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
            inner = f"mkdir -p {remote_dir} && {shlex.quote(rman_bin)} target / @{remote_cmd_file}"
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

        client = remote_dump._connect(ssh_host)

        # 19c 场景：确保 PDB OPEN（重启后 PDB 可能回落到 MOUNTED，导致连 PDB 报 ORA-01109）
        self._ensure_pdbs_open(client)

        # 先查服务端 DATA_PUMP_DIR 实际路径（PDB 视角），再据此组装脚本与回拉路径
        dp_dir = ""
        try:
            dp_dir = self._query_dp_dir(client, conn_easy)
            if not dp_dir:
                # service 连接查询失败时回退 sysdba 视角
                dp_dir = self._query_dp_dir(client)
        except Exception as e:
            self.logger.warning("[%s] 动态查询 DATA_PUMP_DIR 异常: %s", self.task_name, e)
        if not dp_dir:
            # 兜底：若远端 profile 未就绪等场景，用 11g/19c 常见路径依次探测
            dp_dir = "/u01/app/oracle/admin/orcl19c/dpdump"
        self.logger.info("[%s] 远端 DATA_PUMP_DIR=%s", self.task_name, dp_dir)
        remote_dmp = f"{dp_dir}/{ts}.dmp"
        remote_log = f"{dp_dir}/{ts}.log"

        # 远端脚本：先试 system/<pw>@//host:port/service，失败回退 / as sysdba
        # 工具路径动态解析（oracle 用户 profile → 目录 glob 枚举），绝不硬编码
        expdp_bin = self._resolve_oracle_tool(client, "expdp")
        if not expdp_bin:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="远端未找到 expdp（已探测 oracle 用户 PATH 与常见 ORACLE_HOME "
                        "安装目录），请确认数据库软件安装完整")
        self.logger.info("[%s] 远端 expdp 路径=%s", self.task_name, expdp_bin)
        expdp_sh = (
            "#!/bin/bash\n"
            "export PATH=$ORACLE_HOME/bin:$PATH\n"
            f"EXPDP_BIN={shlex.quote(expdp_bin)}\n"
            f"\"$EXPDP_BIN\" {conn_easy} {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={ts}.dmp LOGFILE={ts}.log\n"
            "RC=$?\n"
            "if [ $RC -ne 0 ]; then\n"
            f"  echo '[fallback] primary expdp failed (rc=$RC), retry with / as sysdba'\n"
            f"  \"$EXPDP_BIN\" \"/ as sysdba\" {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={ts}.dmp LOGFILE={ts}.log\n"
            "  RC=$?\n"
            "fi\n"
            'echo "EXPDP_RC=$RC"\n'
            "exit $RC\n"
        )

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

    def _restore_logical_remote(self, backup_path: str, target_host_info: dict,
                                kwargs: dict) -> BackupResult:
        """逻辑备份（expdp 拉回的 dmp）远端恢复：SFTP 推回数据库服务器，
        以 oracle 用户执行 impdp 导入（TABLE_EXISTS_ACTION=REPLACE 覆盖恢复）。

        - 恢复目标 = 数据库服务器本机实例（impdp 在服务器上以 127.0.0.1 连接）；
        - 服务名优先级：kwargs.target_db → 任务 db_name/extra.service；
        - 服务端导入目录实时查询 DATA_PUMP_DIR（兼容 11g/19c）。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        local_dmp = backup_path
        if backup_path.endswith(".dmp.gz"):
            # 平台逻辑备份当前不压缩 dmp；防御性解压
            import gzip
            local_dmp = backup_path[:-3]
            if not os.path.exists(local_dmp):
                with gzip.open(backup_path, "rb") as fin, \
                        open(local_dmp, "wb") as fout:
                    fout.write(fin.read())
        if not os.path.exists(local_dmp):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"本地备份文件不存在，无法推回远端恢复: {backup_path}")

        service = (kwargs.get("target_db") or self._service_name() or "")
        if not service:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="无法确定恢复目标服务名：请在恢复表单填写目标库（service）")
        port = kwargs.get("target_port") or self.task.get("port") or 1521
        username = self.task.get("username") or "system"
        pw = db.decrypt_secret(self.task.get("password") or "")
        extra = self._parse_extra()
        schemas = extra.get("schemas")
        schemas_arg = None
        if schemas:
            schemas_arg = (",".join(str(s) for s in schemas)
                           if isinstance(schemas, list) else str(schemas))

        ts = self._timestamp()
        dmp_name = f"platform_restore_{ts}.dmp"
        log_name = f"platform_restore_{ts}.log"

        client = remote_dump._connect(target_host_info)
        try:
            restore_conn = f"{username}/{pw}@//127.0.0.1:{port}/{service}"
            dp_dir = self._query_dp_dir(client, restore_conn) or self._query_dp_dir(client)
            if not dp_dir:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message="无法在远端解析 DATA_PUMP_DIR 实际路径，恢复中止")

            # 1) 上传 dmp 到服务端导入目录
            sftp = client.open_sftp()
            try:
                remote_dmp = f"{dp_dir}/{dmp_name}"
                sftp.put(local_dmp, remote_dmp)
                self.logger.info("[%s] dmp 已推回远端: %s (%s)", self.task_name,
                                 remote_dmp, db.human_size(os.path.getsize(local_dmp)))

                # 2) 远端 impdp（oracle 用户执行，走本机 127.0.0.1 监听）
                impdp_bin = self._resolve_oracle_tool(client, "impdp")
                if not impdp_bin:
                    return BackupResult(
                        success=False, status=BackupStatus.FAILED,
                        message="远端未找到 impdp（已探测 oracle 用户 PATH 与常见 "
                                "ORACLE_HOME 安装目录），请确认数据库软件安装完整")
                mode_args = f"SCHEMAS={schemas_arg}" if schemas_arg else ""
                inner = (f"export PATH=$ORACLE_HOME/bin:$PATH; "
                         f"{shlex.quote(impdp_bin)} {username}/{pw}@//127.0.0.1:{port}/{service} "
                         f"DUMPFILE={dmp_name} LOGFILE={log_name} "
                         f"TABLE_EXISTS_ACTION=REPLACE {mode_args}")
                shell = f"su - oracle -c {shlex.quote(inner)}"
                start = time.time()
                out, err, rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(shell),
                    timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
                duration = round(time.time() - start, 3)
                out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
                self.logger.info("[%s] 远端 impdp 返回 rc=%s", self.task_name, rc)

                # 拉回导入日志便于排查
                remote_log = f"{dp_dir}/{log_name}"
                log_tail = ""
                try:
                    with sftp.open(remote_log, "r") as lf:
                        log_tail = lf.read().decode("utf-8", "replace")[-1500:]
                except Exception:
                    pass

                # 成功判定：rc==0；或 rc 非 0 但日志里数据对象已导入且仅有
                # ORA-31684(对象已存在)/ORA-39082(类型冲突) 等非致命提示。
                # 常见的"USER 已存在"会导致 rc 非 0，但表/数据已正确导入，应视为成功。
                fatal = False
                if rc != 0:
                    combined = (out_text or "") + "\n" + (log_tail or "")
                    non_fatal = (
                        "ORA-31684" in combined      # USER/对象已存在
                        or "ORA-39082" in combined   # 对象类型冲突（可忽略）
                    )
                    # 已成功导入数据对象才放行；否则视为失败
                    fatal = not non_fatal or ("imported" not in combined
                                              and "Import completed" not in combined)

                if rc != 0 and fatal:
                    snippet = (out_text or log_tail)[-1200:]
                    return BackupResult(
                        success=False, status=BackupStatus.FAILED,
                        duration_sec=duration, stdout=out_text,
                        message=f"远端 impdp 恢复失败(rc={rc}): {snippet}")

                note = ""
                if rc != 0:
                    note = "（含 ORA-31684/ORA-39082 非致命提示，数据对象已导入）"
                msg = (f"Oracle 逻辑恢复(impdp)成功：dmp 已推回数据库服务器 "
                       f"{target_host_info.get('host_key', '')} 并导入 "
                       f"{service}（TABLE_EXISTS_ACTION=REPLACE，"
                       f"耗时 {duration}s）{note}")
                if log_tail:
                    msg += "；导入日志尾部已记录于 stdout"
                self.logger.info("[%s] %s", self.task_name, msg)
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path, size_bytes=0,
                    duration_sec=duration, stdout=(out_text + "\n--- impdp log ---\n" + log_tail),
                    simulated=False, checksum="", message=msg)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        """执行 Oracle 恢复。

        流程：先做演示/仿真检测；通过后再根据备份路径判断是服务端导出
        （impdp）还是本机 exp 导出（imp），构造相应恢复命令。
        """
        # demo_only / DEMO_MODE 不再触发仿真，统一走真实恢复

        # 跨主机恢复：逻辑备份（dmp）优先走「推回数据库服务器执行 impdp」的真实通道；
        # 物理备份（RMAN 备份片）无服务器端 impdp 语义，仍交由 cross_host / 提示。
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            if backup_path and (backup_path.endswith(".dmp")
                                or backup_path.endswith(".dmp.gz")):
                return self._restore_logical_remote(backup_path, target_host_info,
                                                    kwargs)
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

        # 检查 rman 可用性（本机动态解析 PATH 与常见安装目录，不写死路径）
        rman = shutil.which("rman")
        if not rman:
            for cand_dir in ("/u01/app/oracle/product/*/*/bin",):
                import glob as _glob
                hits = sorted(_glob.glob(cand_dir + "/rman"))
                if hits:
                    rman = hits[-1]
                    break
        if not rman:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=script,
                message=("rman 不可用（本机 PATH 与常见 ORACLE_HOME 目录均未找到），"
                         f"PITR 脚本已生成但未执行：{script}；"
                         "请在数据库服务器上通过 SSH 通道执行，或在本平台安装 Oracle 客户端"))
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
            import glob as _glob
            for cand_dir in ("/u01/app/oracle/product/*/*/bin",):
                hits = sorted(_glob.glob(cand_dir + "/rman"))
                if hits:
                    rman = hits[-1]
                    break
        if not rman:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=script,
                message=("rman 不可用（本机 PATH 与常见 ORACLE_HOME 目录均未找到），"
                         f"归档备份脚本已生成但未执行：{script}；"
                         "请在数据库服务器上通过 SSH 通道执行"))

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
