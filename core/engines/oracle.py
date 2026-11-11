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
import re
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
        """解析 task 中的 extra_options（dict 或 JSON 字符串），返回 dict。"""
        raw = self.task.get("extra_options") or ""
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.logger.warning("[%s] extra_options 不是合法 JSON，忽略: %r",
                                 self.task_name, raw)
            return {}

    def _dp_mode_args(self, extra: dict = None, for_import: bool = False) -> tuple:
        """expdp/impdp 模式参数：tables 优先（支持 `表:分区` 分区级导出/导入，
        分区表按表名整体导出亦由此路径生效），其次 schemas，最后 FULL=Y。

        tables 未带模式前缀时自动补 schemas 首个模式（expdp TABLES 要求
        schema-qualified 名称）。返回 (mode_args, mode_desc)；
        for_import=True 且无 tables/schemas 时返回空串（= 整包导入，
        与历史行为一致，避免 FULL=Y 改变既有恢复语义）。
        """
        extra = extra if extra is not None else self._parse_extra()
        schemas = extra.get("schemas")
        schemas_arg = (",".join(str(s) for s in schemas)
                       if isinstance(schemas, list) else
                       (str(schemas) if schemas else ""))
        tables = extra.get("tables")
        if tables:
            tlist = [t.strip() for t in
                     (tables if isinstance(tables, list)
                      else str(tables).split(",")) if t.strip()]
            if schemas_arg:
                s0 = schemas_arg.split(",")[0].strip().upper()
                tlist = [t if "." in t else f"{s0}.{t}" for t in tlist]
            arg = ",".join(tlist)
            return f"TABLES={arg}", f"指定表(tables={arg})"
        if schemas_arg:
            return f"SCHEMAS={schemas_arg}", f"指定模式(schemas={schemas_arg})"
        return ("" if for_import else "FULL=Y"), "全库"

    def _restore_extra_with_tables(self, extra: dict, kwargs: dict) -> dict:
        """恢复时指定表（P0-1）：kwargs.tables 覆盖任务 extra.tables。

        表级恢复（impdp TABLES=）只在本次恢复生效，不改任务配置；
        未传 tables 时原样返回，行为与既有完全一致。
        """
        rt_tables = [str(t).strip() for t in (kwargs.get("tables") or [])
                     if str(t).strip()]
        if not rt_tables:
            return extra
        merged = dict(extra or {})
        merged["tables"] = rt_tables
        merged.pop("schemas", None)  # TABLES 与 SCHEMAS 互斥，TABLES 优先
        return merged

    @staticmethod
    def _quote_password(pw: str) -> str:
        """给连接串中的口令加引号。

        连接串 user/pw@//host:port/service 会以「第一个 @」作为连接描述符的
        分隔符，口令自身含 @（如 Ceshi@133）时会被提前截断，导致
        ORA-12154 "TNS: 无法解析指定的连接标识符"。官方推荐做法是用双引号
        包裹口令，Oracle 客户端会将其整体视为口令。
        """
        pw = pw or ""
        if len(pw) >= 2 and pw.startswith('"') and pw.endswith('"'):
            return pw
        return f'"{pw}"'

    def _db_host_for_remote(self, ssh_host: dict = None) -> str:
        """远端执行 expdp/impdp 时连接数据库所用主机名。

        默认 127.0.0.1（监听绑回环最常见，129 实测最稳）；但部分环境监听
        只绑定内网 IP（167 openEuler 11g 实测：127.0.0.1:1521 拒连 →
        TNS-12541），此时改用任务配置的数据库地址（与远端同机，内网可达）。
        """
        host = (self.task.get("host") or "").strip()
        if host in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return "127.0.0.1"
        return host

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
        return f"{user}/{self._quote_password(pw)}@//{host}:{port}/{service}"

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
                # 数据库 OS 用户执行（标准变量，可按任务/主机覆盖）；
                # 连接串以环境变量 DP_CONN 传入避免引号
                inner = (f"export DP_CONN={shlex.quote(login)}; "
                         f"sqlplus -s $DP_CONN @{remote_sql}")
                shell = remote_dump._wrap_login(
                    f"su - {self._resolve_check_user()} -c {shlex.quote(inner)}")
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

    # ---- 非归档应急热备（hot_copy，对标鼎甲 A6「Oracle 非归档在线热备」） ----

    def _hot_copy_tablespaces(self) -> list:
        """extra_options.hot_copy_tablespaces：指定表空间清单（空=全库）。"""
        raw = (self.extra or {}).get("hot_copy_tablespaces") or ""
        if isinstance(raw, str):
            return [x.strip().upper() for x in raw.split(",") if x.strip()]
        return [str(x).strip().upper() for x in raw if str(x).strip()]

    def _hot_copy_dirs(self) -> tuple:
        """返回 (备份目录, 时间戳)。备份目录取 extra.backup_dir，缺省 /tmp/hotcopy。"""
        ts = self._timestamp()
        bdir = ((self.extra or {}).get("backup_dir") or "/tmp/hotcopy").rstrip("/")
        return bdir, ts

    def _backup_hot_copy_remote(self, ssh_host, backup_type: BackupType) -> BackupResult:
        """SSH 通道：sqlplus BEGIN/END BACKUP + cp 数据文件 + tar + 拉回。"""
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe
        import shlex as _shlex
        bdir, ts = self._hot_copy_dirs()
        tss = self._hot_copy_tablespaces()
        work = f"{bdir}/hotcopy_{ts}"
        client = remote_dump._connect(ssh_host)
        try:
            sftp = client.open_sftp()
            sql = ["SET PAGESIZE 0 FEEDBACK OFF HEADING OFF VERIFY OFF LINESIZE 400"]
            if tss:
                lst = ",".join("'" + t + "'" for t in tss)
                sql.append("".join("ALTER TABLESPACE " + t + " BEGIN BACKUP;\n" for t in tss))
                sql.append("SELECT f.name FROM v$datafile f JOIN v$tablespace t "
                           "ON t.ts#=f.ts# WHERE t.name IN (" + lst + ");")
            else:
                sql.append("ALTER DATABASE BEGIN BACKUP;")
                sql.append("SELECT name FROM v$datafile;")
            sql += ["SELECT name FROM v$controlfile;",
                    "SELECT value FROM v$parameter WHERE name='spfile';", "EXIT;"]
            endsql = "SET PAGESIZE 0 FEEDBACK OFF\n" + (
                "".join("ALTER TABLESPACE " + t + " END BACKUP;\n" for t in tss)
                if tss else "ALTER DATABASE END BACKUP;\n") + "EXIT;\n"
            sqlfile, endfile, shfile = ("/tmp/hc_%s.sql" % ts, "/tmp/hc_%s_end.sql" % ts,
                                        "/tmp/hc_%s.sh" % ts)
            try:
                with sftp.open(sqlfile, "w") as f:
                    f.write("\n".join(sql))
                with sftp.open(endfile, "w") as f:
                    f.write(endsql)
                script = (
                    "#!/bin/bash\n"
                    "set -u\n"
                    "W=" + _shlex.quote(work) + "\n"
                    "mkdir -p \"$W\" || exit 4\n"
                    "sqlplus -s '/ as sysdba' @" + _shlex.quote(sqlfile) +
                    " > /tmp/hc_" + ts + ".list 2>&1\n"
                    "rc1=$?\n"
                    "if [ $rc1 -ne 0 ]; then echo SQLSTEP1_RC=$rc1; cat /tmp/hc_" + ts +
                    ".list; exit 3; fi\n"
                    "n=0\n"
                    "while IFS= read -r f; do\n"
                    "  f=$(echo \"$f\" | xargs)\n"
                    "  case \"$f\" in /*) [ -f \"$f\" ] && cp -f \"$f\" \"$W/\" && n=$((n+1));; esac\n"
                    "done < /tmp/hc_" + ts + ".list\n"
                    "if [ \"$n\" -eq 0 ]; then echo NO_FILES_COPIED; cat /tmp/hc_" + ts +
                    ".list; exit 5; fi\n"
                    "ls -la \"$W\" > \"$W/manifest.txt\" 2>&1 || true\n"
                    "sqlplus -s '/ as sysdba' @" + _shlex.quote(endfile) +
                    " > /tmp/hc_" + ts + "_end.log 2>&1\n"
                    "tar czf " + _shlex.quote(work + ".tar.gz") + " -C " +
                    _shlex.quote(bdir) + " hotcopy_" + ts + " 2>/dev/null\n"
                    "echo HOTCOPY_OK files=$n size=$(du -b " +
                    _shlex.quote(work + ".tar.gz") + " | cut -f1)\n")
                with sftp.open(shfile, "w") as f:
                    f.write(script)
                try:
                    sftp.chmod(shfile, 0o755)
                except Exception:
                    pass
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            shell = remote_dump._wrap_login(
                "su - " + self._resolve_check_user() + " -c " +
                _shlex.quote("bash " + shfile))
            out, err, rc = _ssh_exec_pipe(client, shell, timeout=7200)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            if "HOTCOPY_OK" not in text:
                # 兜底 END BACKUP（防悬挂在备份模式拖垮 redo）
                try:
                    _ssh_exec_pipe(client, remote_dump._wrap_login(
                        "su - " + self._resolve_check_user() + " -c " +
                        _shlex.quote("sqlplus -s '/ as sysdba' @" + endfile)),
                        timeout=60)
                except Exception:
                    pass
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=("在线热备失败（远端 rc=%s）：%s" % (rc, (text or err)[-400:])))
            n_files, size = 0, 0
            for line in text.splitlines():
                if line.startswith("HOTCOPY_OK"):
                    for tok in line.split():
                        if tok.startswith("files="):
                            n_files = int(tok.split("=")[1])
                        if tok.startswith("size="):
                            size = int(tok.split("=")[1])
            if size <= 0:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="在线热备产物为空（远端打包异常）")
            local_path = os.path.join(self.storage_root,
                                      "hotcopy_%s_%s.tar.gz" % ((self.task.get("db_name") or "orcl"), ts))
            pulled = remote_dump.sftp_pull_resumable(
                client, work + ".tar.gz", local_path, task=self.task,
                db_type="oracle", label="oracle_hot_copy")
            final = (pulled or {}).get("path") or local_path
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=os.path.abspath(final),
                size_bytes=os.path.getsize(os.path.abspath(final)),
                message=("在线热备完成：%d 个文件（表空间 %s），产物 %d KB。"
                         "崩溃一致备份：恢复需整包替换文件后 startup"
                         % (n_files, ",".join(tss) if tss else "全库",
                            os.path.getsize(os.path.abspath(final)) // 1024)))
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _backup_hot_copy_local(self, backup_type: BackupType) -> BackupResult:
        """本机通道：平台与 Oracle 同机，subprocess 直跑同一流程。"""
        import subprocess
        import tarfile
        bdir, ts = self._hot_copy_dirs()
        tss = self._hot_copy_tablespaces()
        work = "%s/hotcopy_%s" % (bdir, ts)
        os.makedirs(work, exist_ok=True)
        sql = ["SET PAGESIZE 0 FEEDBACK OFF HEADING OFF VERIFY OFF LINESIZE 400"]
        if tss:
            lst = ",".join("'" + t + "'" for t in tss)
            sql.append("".join("ALTER TABLESPACE " + t + " BEGIN BACKUP;\n" for t in tss))
            sql.append("SELECT f.name FROM v$datafile f JOIN v$tablespace t "
                       "ON t.ts#=f.ts# WHERE t.name IN (" + lst + ");")
        else:
            sql.append("ALTER DATABASE BEGIN BACKUP;")
            sql.append("SELECT name FROM v$datafile;")
        sql += ["SELECT name FROM v$controlfile;",
                "SELECT value FROM v$parameter WHERE name='spfile';", "EXIT;"]
        sqlfile = "/tmp/hc_%s.sql" % ts
        with open(sqlfile, "w") as f:
            f.write("\n".join(sql))
        try:
            r1 = subprocess.run(["sqlplus", "-s", "/ as sysdba", "@" + sqlfile],
                                capture_output=True, timeout=300)
            files = []
            for line in (r1.stdout or b"").decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("/") and os.path.isfile(line):
                    files.append(line)
            if not files:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=("在线热备失败：未获取到数据文件清单（检查 sysdba 权限） %s"
                             % (r1.stdout or b"")[-200:]))
            import shutil as _sh
            for f_ in files:
                _sh.copy2(f_, work)
            endsql = "SET PAGESIZE 0 FEEDBACK OFF\n" + (
                "".join("ALTER TABLESPACE " + t + " END BACKUP;\n" for t in tss)
                if tss else "ALTER DATABASE END BACKUP;\n") + "EXIT;\n"
            with open("/tmp/hc_%s_end.sql" % ts, "w") as f:
                f.write(endsql)
            subprocess.run(["sqlplus", "-s", "/ as sysdba", "@/tmp/hc_%s_end.sql" % ts],
                           capture_output=True, timeout=120)
            local_path = os.path.join(self.storage_root,
                                      "hotcopy_%s_%s.tar.gz" % ((self.task.get("db_name") or "orcl"), ts))
            with tarfile.open(local_path, "w:gz") as tf:
                for fn in sorted(os.listdir(work)):
                    tf.add(os.path.join(work, fn), arcname="hotcopy_" + ts + "/" + fn)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=os.path.abspath(local_path),
                size_bytes=os.path.getsize(local_path),
                message=("在线热备完成：%d 个文件（表空间 %s）。崩溃一致备份，"
                         "恢复需整包替换文件后 startup"
                         % (len(files), ",".join(tss) if tss else "全库")))
        except FileNotFoundError:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="本机通道未找到 sqlplus（本机无 Oracle 客户端，"
                                        "请纳管 SSH 主机走远端通道）")

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
            dp_dir = self._query_dp_dir(
                client,
                f"{username}/{self._quote_password(pw)}"
                f"@//{self.task.get('host')}:{port}/{service}")
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
            remote_dump.sftp_put_resumable(
                client, local_dmp, remote_dmp,
                rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                label="恢复校验 dmp 推送")
            impdp_bin = self._resolve_oracle_tool(client, "impdp")
            if not impdp_bin:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="远端未找到 impdp，无法执行 SQLFILE 校验")
            mode_args = self._dp_mode_args(for_import=True)[0]
            verify_conn = f"{username}/{self._quote_password(pw)}@//{self._db_host_for_remote(ssh_host)}:{port}/{service}"
            def _sqlfile_shell(conn_str):
                _inner = (f"export PATH=$ORACLE_HOME/bin:$PATH; "
                          f"{shlex.quote(impdp_bin)} {shlex.quote(conn_str)} "
                          f"DUMPFILE=platform_verify_{ts}.dmp SQLFILE={sqlfile} "
                          # DIRECTORY 为必填：缺省时报 ORA-39145（目录对象不能为空）
                          f"DIRECTORY=DATA_PUMP_DIR NOLOGFILE=Y {mode_args} </dev/null")
                return (f"su - {self._resolve_check_user(ssh_host)} -c "
                        f"{shlex.quote(_inner)}")

            start = _time.time()
            out, err, rc = _ssh_exec_pipe(
                client, remote_dump._wrap_login(_sqlfile_shell(verify_conn)),
                timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
            _fb_note = ""
            if rc != 0:
                _probe = ((out.decode("utf-8", "replace") if isinstance(out, bytes)
                           else (out or "")) + (err or ""))
                if any(p in _probe for p in ("01017", "12154", "12514", "12541", "12170")):
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(_sqlfile_shell("'/ as sysdba'")),
                        timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
                    _fb_note = "（主连接认证失败，已以 / as sysdba 兜底校验）"
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
                   f"DDL（预览含 {n_stmt} 处 CREATE 语句），dmp 可正常导入{_fb_note}")
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
                f"su - {self._resolve_check_user(ssh_host)} -c "
                f"{shlex.quote('sqlplus -s / as sysdba @/tmp/platform_verify_df.sql')}")
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

        # 2) 备份片推回 + 编目 + RMAN RESTORE DATABASE VALIDATE
        #    物理备份完成后平台会清理远端残留（数据库服务器零残留设计），而
        #    RESTORE DATABASE VALIDATE 只能读**远端真实存在且已编目**的备份片；
        #    直接 VALIDATE 会因 ORA-19505「无法识别文件」必然失败（168 环境
        #    Oracle 11g 实测暴露）。因此校验前把平台本地保存的备份片推回原目录
        #    并 CATALOG 进 RMAN 仓库，校验结束后再清理，既完成校验又保住零残留。
        pushed = []
        remote_dir = ""
        try:
            import glob as _glob
            local_dir = os.path.dirname(os.path.abspath(backup_path or ""))
            # 优先采用备份时落盘 manifest 记录的远端目录，确保与 RMAN FORMAT 一致
            for _mf in _glob.glob(os.path.join(local_dir, "*_rman_manifest.txt")):
                try:
                    with open(_mf, "r", encoding="utf-8", errors="replace") as _fh:
                        for _line in _fh:
                            if _line.strip().startswith("remote_dir:"):
                                remote_dir = _line.split(":", 1)[1].strip()
                except Exception:
                    pass
                if remote_dir:
                    break
            if not remote_dir:
                # 无 manifest 时回退：任务级 rman_temp_dir 优先，再默认值
                remote_dir = (str((self._parse_extra() or {})
                                  .get("rman_temp_dir") or "").strip().rstrip("/")
                              or "/u01/app/oracle/backup")
            for _p in sorted(_glob.glob(os.path.join(local_dir, "*.bkp"))):
                _rp = f"{remote_dir}/{os.path.basename(_p)}"
                remote_dump.sftp_put_resumable(
                    client, _p, _rp,
                    rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                    label="RMAN 备份片回推")
                pushed.append(_rp)
            if pushed:
                _ssh_exec_pipe(
                    client,
                    "chown oracle:oinstall " + " ".join(shlex.quote(x) for x in pushed)
                    + " && chmod 640 " + " ".join(shlex.quote(x) for x in pushed),
                    timeout=120)
                self.logger.info("[%s] 已推回 %d 个备份片到 %s 供 VALIDATE 使用",
                                 self.task_name, len(pushed), remote_dir)
        except Exception as e:
            self.logger.warning("[%s] 推回备份片失败，VALIDATE 可能不可用: %s",
                                self.task_name, e)

        validate_ok = False
        out_text = ""
        rc = -1
        duration = 0.0
        restore_detail = "VALIDATE 通过（数据文件抽取跳过：未解析到数据文件编号）"
        try:
            catalog_line = ""
            if pushed:
                catalog_line = f"  CATALOG START WITH '{remote_dir}' NOPROMPT;\n"
            # P1-5：任务配置了多通道时 VALIDATE 同样受益（逐片并行读备份片）
            _vch = self._rman_channels()
            _vch_lines = "".join(
                f"  ALLOCATE CHANNEL ch{i:02d} DEVICE TYPE DISK;\n"
                for i in range(_vch)) if _vch > 1 else ""
            _vch_rel = "".join(
                f"  RELEASE CHANNEL ch{i:02d};\n"
                for i in range(_vch)) if _vch > 1 else ""
            with sftp.open("/tmp/platform_verify_rman.cmd", "w") as f:
                f.write(f"RUN {{\n{_vch_lines}{catalog_line}  RESTORE DATABASE VALIDATE;\n{_vch_rel}}}\nEXIT;\n")
            shell = remote_dump._wrap_login(
                f"su - {self._resolve_check_user(ssh_host)} -c "
                f"{shlex.quote(f'{shlex.quote(rman_bin)} target / @/tmp/platform_verify_rman.cmd')}")
            start = _time.time()
            out, err, rc = _ssh_exec_pipe(client, shell, timeout=7200)
            duration = round(_time.time() - start, 3)
            out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            validate_ok = (rc == 0 and "validation complete" in out_text.lower()
                           and "validation failed" not in out_text.lower())

            # 3) 真实抽取最小数据文件到暂存目录（从备份片还原实际文件）。
            #    必须在清理推回的备份片**之前**执行：备份片一旦删除，RMAN 读取时
            #    只会报 ORA-19505「无法识别文件」，抽取必然失败（168 环境实测）。
            if validate_ok and df_no:
                stage_dir = f"/var/tmp/platform_restore_test/{ts}"
                pre = (f"mkdir -p {stage_dir} && chown oracle {stage_dir} && chmod 755 {stage_dir}")
                _ssh_exec_pipe(client, remote_dump._wrap_login(pre), timeout=30)
                with sftp.open("/tmp/platform_verify_df2.cmd", "w") as f:
                    f.write(f"RUN {{\n"
                            f"  SET NEWNAME FOR DATAFILE {df_no} TO '{stage_dir}/df_{df_no}.dbf';\n"
                            f"  RESTORE DATAFILE {df_no};\n"
                            f"}}\nEXIT;\n")
                shell = remote_dump._wrap_login(
                    f"su - {self._resolve_check_user(ssh_host)} -c "
                    f"{shlex.quote(f'{shlex.quote(rman_bin)} target / @/tmp/platform_verify_df2.cmd')}")
                out2, _err, rc2 = _ssh_exec_pipe(client, shell, timeout=3600)
                out2_text = out2.decode("utf-8", "replace") if isinstance(out2, bytes) else (out2 or "")
                # 确认真实文件落盘
                stat_cmd = f"ls -la {stage_dir}/ 2>/dev/null"
                out3, _e3, rc3 = _ssh_exec_pipe(client, remote_dump._wrap_login(stat_cmd), timeout=20)
                ls_text = out3.decode("utf-8", "replace") if isinstance(out3, bytes) else (out3 or "")
                # 清理暂存
                _ssh_exec_pipe(client, remote_dump._wrap_login(f"rm -rf {stage_dir}"), timeout=30)
                if rc2 == 0 and f"df_{df_no}" in ls_text:
                    restore_detail = (f"RESTORE VALIDATE 通过；并已从备份片真实抽取数据文件 "
                                      f"{df_no} 到暂存目录验证后清理（真实恢复证据）")
                else:
                    restore_detail = ("RESTORE VALIDATE 通过；数据文件抽取未完成"
                                      f"(rc={rc2})：" + out2_text[-300:].replace("\n", " "))
        finally:
            # 无论成败都清理推回的备份片与临时脚本，保住「数据库服务器零残留」
            for _rp in pushed:
                try:
                    sftp.remove(_rp)
                except Exception:
                    pass
            for _tmp in ("/tmp/platform_verify_rman.cmd", "/tmp/platform_verify_df2.cmd"):
                try:
                    sftp.remove(_tmp)
                except Exception:
                    pass

        if not validate_ok:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                duration_sec=duration, stdout=out_text,
                message=f"RMAN RESTORE VALIDATE 未通过(rc={rc}): {out_text[-1000:]}")

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

    def _check_archive_mode(self) -> dict:
        """预检专用：查 v$database.log_mode（oracledb 直连，native-first）。

        返回 {"mode": "ARCHIVELOG"|"NOARCHIVELOG"|""}，失败带 error。
        """
        import oracledb as _odb
        try:
            conn = _odb.connect(
                user=self.task.get("username") or "system",
                password=db.decrypt_secret(self.task.get("password") or ""),
                host=self.task.get("host") or "127.0.0.1",
                port=int(self.task.get("port") or 1521),
                service_name=self.task.get("db_name") or "orcl",
                login_timeout=8)
            cur = conn.cursor()
            cur.execute("select log_mode from v$database")
            row = cur.fetchone()
            cur.close()
            conn.close()
            return {"mode": (row[0] or "").strip() if row else ""}
        except Exception as e:
            return {"mode": "", "error": str(e)[:160]}

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
                    f"su - {self._resolve_check_user()} -c "
                    f"{shlex.quote('sqlplus -s / as sysdba @' + remote_sql)}")
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

        mode_args_str, mode_desc = self._dp_mode_args(extra)
        mode_args = [mode_args_str] if mode_args_str else []

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
    def _archive_trigger_met(self) -> tuple:
        """归档日志备份触发条件检查（对标鼎甲"自定义归档日志备份触发条件"）。

        extra.archive_trigger = {"min_unbacked_logs": N, "fra_free_floor_gb": X}
        - 未备份归档日志数（v$archived_log backup_count=0 且未删除）≥ N → 触发；
        - FRA 剩余空间 ≤ X GB → 触发（快满优先备份，防止 FRA 打爆挂库）。
        配置了多个条件按 **任一满足即触发**；全部未满足 → 不触发。
        未配置 archive_trigger → 恒触发（兼容既有行为）。

        Returns:
            (met: bool, detail: str)
        """
        extra = self._parse_extra()
        trig = extra.get("archive_trigger")
        if not isinstance(trig, dict) or not trig:
            return True, "未配置触发条件（恒触发）"
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ssh = remote_dump.resolve_ssh_host(self.task)
        if not ssh:
            return True, "未纳管 SSH 主机，无法查询触发条件（按触发处理）"
        try:
            client = remote_dump._connect(ssh)
            ts = self._timestamp()
            sqlfile = f"/tmp/atrigger_{ts}.sql"
            sftp = client.open_sftp()
            try:
                with sftp.open(sqlfile, "w") as f:
                    f.write(
                        "connect / as sysdba\nSET PAGESIZE 0 FEEDBACK OFF "
                        "HEADING OFF\n"
                        "SELECT COUNT(*) FROM v$archived_log "
                        "WHERE backup_count = 0 AND deleted = 'NO';\n"
                        "SELECT ROUND((SPACE_LIMIT - SPACE_USED)/1024/1024/1024, 1) "
                        "FROM v$recovery_file_dest;\nEXIT;\n")
            finally:
                sftp.close()
            shell = (f"su - {self._resolve_check_user(ssh)} -c "
                     f"{shlex.quote('sqlplus -s /nolog @' + sqlfile)}")
            out, err, rc = _ssh_exec_pipe(
                client, remote_dump._wrap_login(shell), timeout=60)
            txt = out.decode("utf-8", "replace") if isinstance(out, bytes) \
                else str(out or "")
            nums = [l.strip() for l in txt.splitlines() if l.strip().isdigit()]
            unbacked = int(nums[0]) if len(nums) > 0 else 0
            fra_free_gb = float(nums[1]) if len(nums) > 1 else -1.0
        except Exception as e:  # 探测失败按触发处理（宁多备不漏备）
            return True, f"触发条件探测失败（按触发处理）: {e}"

        reasons = []
        met = False
        n_min = int(trig.get("min_unbacked_logs") or 0)
        if n_min > 0:
            if unbacked >= n_min:
                met = True
                reasons.append(f"未备份归档日志 {unbacked} 个 ≥ 阈值 {n_min}（满足）")
            else:
                reasons.append(f"未备份归档日志 {unbacked} 个 < 阈值 {n_min}")
        floor = float(trig.get("fra_free_floor_gb") or 0)
        if floor > 0 and fra_free_gb >= 0:
            if fra_free_gb <= floor:
                met = True
                reasons.append(f"FRA 剩余 {fra_free_gb}GB ≤ 阈值 {floor}GB（满足）")
            else:
                reasons.append(f"FRA 剩余 {fra_free_gb}GB > 阈值 {floor}GB")
        return met, "；".join(reasons) or "条件字段为空（恒触发）"

    def backup(self, backup_type: BackupType) -> BackupResult:
        """Oracle 备份：按 backup_mode 分发物理(RMAN)/逻辑(expdp/exp)。

        逻辑备份优先在 SSH 备份机/数据库服务器执行 exp，失败再回退本机 expdp/exp。
        配置 extra.archive_trigger 时先做触发条件检查（未满足则如实跳过）。
        """
        # demo_only / DEMO_MODE 不再触发仿真，统一走真实备份

        trig = self._parse_extra().get("archive_trigger")
        if isinstance(trig, dict) and trig:
            met, detail = self._archive_trigger_met()
            if not met:
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    message=(f"归档备份触发条件未满足，已跳过（{detail}）。"
                             "如需强制执行请调整 archive_trigger 配置或临时移除该选项"),
                )
            self.logger.info("[%s] 归档触发条件满足: %s", self.task_name, detail)

        if self.backup_mode == BackupMode.PHYSICAL:
            # 物理备份优先走 SSH 远端真实 RMAN（Oracle 服务器自带 rman，
            # 且 db_recovery_file_dest 未配置需显式 FORMAT），失败再回退本机。
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_physical_remote(ssh_host, backup_type),
                lambda: self._backup_physical(backup_type),
                "Oracle 物理备份(RMAN)",
            )
        if self.backup_mode == BackupMode.HOT_COPY:
            # 非归档应急热备（对标鼎甲 A6）：begin/end backup 文件拷贝，
            # 免开归档免 RMAN。诚实边界：崩溃一致备份（恢复=整包替换文件后
            # startup，崩溃恢复自动应用联机日志）；推荐尽快转储并做恢复校验。
            return self._try_remote_then_local(
                lambda ssh_host: self._backup_hot_copy_remote(ssh_host, backup_type),
                lambda: self._backup_hot_copy_local(backup_type),
                "Oracle 在线热备(begin backup 文件拷贝)",
            )
        return self._try_remote_then_local(
            lambda ssh_host: self._backup_logical_remote(ssh_host, backup_type),
            lambda: self._backup_logical_local(backup_type),
            "Oracle 逻辑备份(exp/expdp)",
        )

    # ---- P1-5 单任务多通道并行 ----
    _RMAN_MAX_CHANNELS = 8

    def _rman_channels(self) -> int:
        """任务级 RMAN 通道数（extra_options.rman_channels，1-8；<=1 返回 0=单连接）。"""
        try:
            n = int((self._parse_extra() or {}).get("rman_channels") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 1:
            return 0
        return min(n, self._RMAN_MAX_CHANNELS)

    @staticmethod
    def _rman_run_script(db_backup_line: str, arch_backup_line: str,
                         channels: int = 0) -> str:
        """组装 RMAN RUN 块。channels>1 时显式 ALLOCATE/RELEASE 多通道，
        单条 BACKUP 语句由 RMAN 自动分片到各通道（FORMAT %U 保证片名唯一）。"""
        nch = max(0, min(int(channels or 0), 8))
        alloc = rel = ""
        if nch > 1:
            alloc = "".join(
                f"  ALLOCATE CHANNEL ch{i:02d} DEVICE TYPE DISK;\n"
                for i in range(nch))
            rel = "".join(
                f"  RELEASE CHANNEL ch{i:02d};\n" for i in range(nch))
        return ("RUN {\n" + alloc + db_backup_line + "\n"
                + arch_backup_line + "\n" + rel + "}\nexit;\n")

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
        if backup_type == BackupType.INCREMENTAL:
            db_line = (
                f"  BACKUP AS COMPRESSED BACKUPSET INCREMENTAL LEVEL 0 DATABASE "
                f"FORMAT '{fmt_db}';")
        else:
            db_line = f"  BACKUP AS COMPRESSED BACKUPSET DATABASE FORMAT '{fmt_db}';"
        arch_line = (
            f"  BACKUP AS COMPRESSED BACKUPSET ARCHIVELOG ALL FORMAT '{fmt_arch}';")
        nch = self._rman_channels()
        with open(script, "w") as f:
            f.write(self._rman_run_script(db_line, arch_line, channels=nch))
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
        - Oracle 工具链仅在数据库 OS 用户（默认 oracle，可用 db_os_user
          标准变量覆盖）profile 中可用，必须以 `su - <用户> -c`
          执行，不能以 root 直接跑 rman；
        - db_recovery_file_dest 未配置(size=0) → 必须显式 FORMAT 指定备份路径，
          否则 ORA-19801；
        - rman target / @script 已建立连接，脚本内不要再写 connect target /；
        - 备份片落在远端 /u01/app/oracle/backup/，再经 SFTP 拉回本机。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        ts = self._timestamp()
        # 固定产物形态：备份片前缀**确定性**（按任务固定，不含随机时间戳），
        # 备份成功后写完成标记；拉回中断时重试直接续传，**不重跑 RMAN**。
        tid = int(self.task.get("id") or 0)
        bkp_prefix = f"ora_bkp_t{tid}_"
        # 临时备份目录：任务级 rman_temp_dir 可覆盖（FIX-2026-001 P1）。
        # /u01/app/oracle/backup 非 Oracle 强制路径，只是平台默认值；数据文件/
        # 归档在独立挂载分区时必须改配，否则备份片会写满根分区。
        _rman_dir = str((self._parse_extra() or {}).get("rman_temp_dir") or "").strip()
        remote_dir = (_rman_dir or "/u01/app/oracle/backup").rstrip("/")
        remote_marker = f"{remote_dir}/.bkdone_t{tid}"
        remote_cmd_file = f"{remote_dir}/rman_t{tid}.cmd"

        # 1) 组装 RMAN 脚本（注意：rman target / @file 已建立连接，脚本内不要再 connect）
        if backup_type == BackupType.INCREMENTAL:
            db_line = (
                f"  BACKUP AS COMPRESSED BACKUPSET INCREMENTAL LEVEL 0 DATABASE "
                f"FORMAT '{remote_dir}/{bkp_prefix}%U.bkp';")
        else:
            db_line = (
                f"  BACKUP AS COMPRESSED BACKUPSET DATABASE "
                f"FORMAT '{remote_dir}/{bkp_prefix}%U.bkp';")
        arch_line = (
            f"  BACKUP AS COMPRESSED BACKUPSET ARCHIVELOG ALL "
            f"FORMAT '{remote_dir}/{bkp_prefix}arch_%U.bkp';")
        nch = self._rman_channels()
        rman_script = self._rman_run_script(db_line, arch_line, channels=nch)

        client = remote_dump._connect(ssh_host)
        # 19c 场景：确保 PDB OPEN（RMAN 备份 CDB 时要求 PDB 打开）
        self._ensure_pdbs_open(client)
        # 非归档模式策略（对标 DBackup：abort / auto_enable）：
        # NOARCHIVELOG 下 RMAN 热备必败（ORA-19602）
        _arch = self._check_archive_mode()
        if _arch.get("mode") == "NOARCHIVELOG":
            _policy = "abort"
            try:
                _eo = self.task.get("extra_options")
                _extra = json.loads(_eo) if isinstance(_eo, str) else (_eo or {})
                _policy = str(_extra.get("noarchivelog_policy") or "abort").lower()
            except Exception:
                pass
            if _policy != "auto_enable":
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=(
                        "Oracle 处于非归档模式，RMAN 热备将失败（ORA-19602）。"
                        "处置三选一：① 开启归档模式（shutdown immediate → "
                        "startup mount → alter database archivelog → alter "
                        "database open，需 DBA 评估重启窗口）；② 任务高级选项配置 "
                        "noarchivelog_policy=auto_enable（平台自动开归档后备份，"
                        "会短暂重启实例）；③ 改用逻辑备份(expdp)"))
            # auto_enable：自动启用归档（会重启实例，审计日志高亮）
            self.logger.warning(
                "[%s] noarchivelog_policy=auto_enable：将自动启用归档模式"
                "（实例会重启一次，请确保已评估影响）", self.task_name)
            _ae_sql = (
                "shutdown immediate;\n"
                "startup mount;\n"
                "alter database archivelog;\n"
                "alter database open;\n"
                "select log_mode from v$database;\n")
            _ae_file = "/tmp/platform_enable_arch.sql"
            from core.engines.file import _ssh_exec_pipe as _sep_ae
            _asftp = None
            try:
                _asftp = client.open_sftp()
                with _asftp.open(_ae_file, "w") as f:
                    f.write("SET PAGESIZE 0 FEEDBACK OFF\n" + _ae_sql + "\nEXIT;\n")
            finally:
                try:
                    _asftp.close()
                except Exception:
                    pass
            _chk = self._resolve_check_user(ssh_host)
            _ae_sh = ("su - %s -c %s" % (
                _chk, shlex.quote("sqlplus -s / as sysdba @" + _ae_file)))
            _ao, _ae2, _arc = _sep_ae(
                client, remote_dump._wrap_login(_ae_sh), timeout=600)
            _aot = _ao.decode("utf-8", "replace") if isinstance(_ao, bytes) else str(_ao)
            if "ARCHIVELOG" not in _aot or _arc != 0:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message="自动启用归档失败（exit=%s）：%s；请 DBA 手工执行开启归档"
                            % (_arc, _aot[-400:]))
            self.logger.warning("[%s] 归档模式已自动启用（ARCHIVELOG），继续 RMAN 备份",
                                self.task_name)
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
        q_dir = shlex.quote(remote_dir)
        prep = (f"mkdir -p {remote_dir} && chown oracle {remote_dir} "
                f"&& chmod 755 {remote_dir}")
        _ssh_exec_pipe(client, remote_dump._wrap_login(prep), timeout=60)
        # 前置校验（FIX-2026-001 P1）：① oracle 用户实际可写（提前失败，
        # 避免备份跑到中途才 ORA-19504）；② 目录所在文件系统与 / 同分区 →
        # 警告（RMAN 产物增长不可控，写根分区是高风险操作）。
        chk = (f"su - {self._resolve_check_user(ssh_host)} -c "
               f"{shlex.quote(f'touch {q_dir}/.aidbm_wtest && rm -f {q_dir}/.aidbm_wtest')} "
               f">/dev/null 2>&1; echo W=$?; stat -c %d /; stat -c %d {q_dir}")
        _wout, _werr, _wrc = _ssh_exec_pipe(
            client, remote_dump._wrap_login(chk), timeout=30)
        _wtxt = _wout.decode("utf-8", "replace") if isinstance(_wout, bytes) else (_wout or "")
        _rootfs_note = ""
        if "W=1" in _wtxt:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=(f"RMAN 备份目录 {remote_dir} 对 oracle 用户不可写——"
                         f"请确认目录存在/属主为 oracle，或在任务高级选项修改 "
                         f"rman_temp_dir 到实际挂载分区"))
        _devs = [l.strip() for l in _wtxt.splitlines() if l.strip().isdigit()]
        if len(_devs) >= 2 and _devs[0] == _devs[1]:
            _rootfs_note = "（警告：备份目录与根分区同一文件系统，建议改配到独立挂载点）"
            self.logger.warning("[%s] %s", self.task_name, _rootfs_note)
        sftp = client.open_sftp()
        try:
            # 0) 复用探测：上次 RMAN 已跑完（写了完成标记）但备份片拉回中断/失败 →
            #    跳过重跑备份（RMAN 对大库可能跑数小时），直接从断点续传拉回。
            ready = remote_dump.remote_artifact_ready(client, remote_marker, min_size=1)
            reuse = bool(ready["reusable"])
            if reuse:
                self.logger.info("[%s] 复用远端已完成备份片（前缀 %s），跳过 RMAN 执行",
                                 self.task_name, bkp_prefix)
            else:
                # 清掉上次残留备份片，避免与本次产物混淆
                try:
                    for _a in sftp.listdir_attr(remote_dir):
                        if _a.filename.startswith(bkp_prefix):
                            try:
                                sftp.remove(f"{remote_dir}/{_a.filename}")
                            except Exception:
                                pass
                except Exception:
                    pass

            # 写 RMAN 脚本（oracle 可读）
            with sftp.open(remote_cmd_file, "w") as f:
                f.write(rman_script)
            try:
                sftp.chmod(remote_cmd_file, 0o644)
            except Exception:
                pass

            if reuse:
                out_text = f"复用远端已完成备份片（前缀 {bkp_prefix}），跳过 RMAN 执行"
                err_text, rc, duration = "", 0, 0.0
            else:
                # 3) 远端执行：mkdir 备份目录 + rman target / @script（以 oracle 用户）
                inner = (f"mkdir -p {remote_dir} && "
                         f"{shlex.quote(rman_bin)} target / @{remote_cmd_file}")
                shell = (f"su - {self._resolve_check_user(ssh_host)} -c "
                         f"{shlex.quote(inner)}")
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
                hint = ""
                # 常见失败专项指引（错误原文透出 + 可执行处置）
                if "ORA-19602" in snippet or "NOARCHIVELOG" in snippet:
                    hint = (
                        "；处置指引：数据库处于非归档模式，无法对活动数据文件做"
                        " RMAN 热备。开启归档模式（需短暂重启实例，请 DBA 评估窗口）："
                        "① sqlplus / as sysdba → "
                        "② shutdown immediate → "
                        "③ startup mount → "
                        "④ alter database archivelog → "
                        "⑤ alter database open → "
                        "⑥ 验证：select log_mode from v$database（应为 ARCHIVELOG）；"
                        "或改用本任务【逻辑备份(expdp)】规避")
                elif "ORA-01031" in snippet or "insufficient privileges" in snippet:
                    hint = ("；处置指引：RMAN 需 sysdba 权限，请在任务高级选项配置"
                            " sysdba 账户或授予该用户 sysdba")
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    duration_sec=duration, stdout=out_text, stderr=err_text,
                    message=f"远端 RMAN 物理备份失败(rc={rc}): {snippet}{hint}")

            # 3.5) 写远端完成标记：下次重试据此复用备份片，**不重跑 RMAN**
            if not reuse:
                remote_dump.mark_remote_artifact_done(
                    client, remote_marker, note=f"rman {ssh_host.get('host_key','')}")

            # 4) 拉回所有备份片（匹配确定性前缀）；中断后从断点继续。
            #    P1-5：extra.parallel_pull 开启时多片并行拉回——每 worker 一条
            #    独立 SSH 连接，单片断点续传语义不变；worker 数 = min(片数, 通道数或 3)。
            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            try:
                names = sorted(a.filename for a in sftp.listdir_attr(remote_dir)
                               if a.filename.startswith(bkp_prefix))
            except Exception:
                names = []
            _parallel = str((self._parse_extra() or {}).get("parallel_pull")
                            or "").lower() in ("1", "true", "yes", "on")
            pull_workers = 1
            if _parallel and len(names) > 1:
                pull_workers = min(len(names), max(nch or 3, 3))
                items = [(fn, f"{remote_dir}/{fn}", os.path.join(out_dir, fn))
                         for fn in names]
                pieces = remote_dump.sftp_pull_files_parallel(
                    ssh_host, items, task=self.task,
                    db_type=f"{self.db_type}_rman", max_workers=pull_workers,
                    stable_secs=3, min_size=1024)
            else:
                pieces = []
                for fname in names:
                    remote_path = f"{remote_dir}/{fname}"
                    local_path = os.path.join(out_dir, fname)
                    res = remote_dump.sftp_pull_resumable(
                        client, remote_path, local_path, task=self.task, key=fname,
                        db_type=f"{self.db_type}_rman",
                        host_key=ssh_host.get("host_key", ""),
                        has_rc=False, stable_secs=3,
                        label=f"rman {fname}", min_size=1024)
                    pieces.append((res["path"], res["size"]))

            if not pieces:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED, duration_sec=duration,
                    stdout=out_text, stderr=err_text,
                    message=f"远端 RMAN 执行成功但未在 {remote_dir} 找到备份片"
                            f"(前缀 {bkp_prefix})，可能 FORMAT 路径不正确。")

            # 已完整拉回 → 清理远端备份片与完成标记（数据库服务器不留产物）
            remote_dump.cleanup_remote_artifacts(
                client, [f"{remote_dir}/{fn}" for fn in names] + [remote_marker])

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
                mf.write(f"rman_channels: {nch or 1}\n")
                mf.write(f"pull_workers: {pull_workers}\n")
                for p, sz in pieces:
                    mf.write(f"{os.path.basename(p)}\t{sz}\t{db.sha256_file(p)}\n")

            hk = ssh_host.get("host_key", "remote")
            # 已完整拉回 → 清理远端 RMAN 命令脚本（服务器不留产物）。
            # 失败分支在此之前已 return，脚本保留作为排障证据（message 中会给出路径）。
            try:
                sftp.remove(remote_cmd_file)
            except Exception:
                pass
            msg = (f"通过 SSH 在 {hk} 以 oracle 用户执行 RMAN 物理备份成功，"
                   f"已拉回 {len(pieces)} 个备份片，共 {db.human_size(total_size)}"
                   + (f"，RMAN 通道 {nch}" if nch > 1 else "")
                   + (f"，并行拉回 {pull_workers} 线程" if pull_workers > 1 else "")
                   + f"（主片: {os.path.basename(first_local)}）{_rootfs_note}")
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
        mode_args, mode_desc = self._dp_mode_args(extra)
        host = self.task.get("host") or "127.0.0.1"
        port = self.task.get("port") or 1521
        username = self.task.get("username") or "system"
        pw = db.decrypt_secret(self.task.get("password") or "")
        # 远端脚本在数据库服务器本机执行：listener 对外部 IP 的注册可能
        # 不稳定（实测 129 出现 ORA-12514），本机回环最可靠 → 优先 127.0.0.1
        conn_easy = f"{username}/{self._quote_password(pw)}@//127.0.0.1:{port}/{service}"

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
            # 兜底：动态查询失败时按实例名与版本常见路径依次远端探测
            # （回退值曾写死 orcl19c，168 11g 实测解析到不存在的目录导致
            # impdp 日志拉不到、恢复收尾判定失效）
            sid = (self.task.get("db_name") or "orcl").strip()
            for cand in (
                f"/u01/app/oracle/admin/{sid}/dpdump",
                f"/u01/app/oracle/admin/{sid.lower()}/dpdump",
                "/u01/app/oracle/admin/orcl11g/dpdump",
                "/u01/app/oracle/admin/orcl/dpdump",
                "/u01/app/oracle/admin/orcl19c/dpdump",
            ):
                from core.engines.file import _ssh_exec_pipe as _sep_probe
                _po, _pe, _prc = _sep_probe(
                    client, remote_dump._wrap_login(
                        f"test -d {shlex.quote(cand)} && echo Y"), timeout=10)
                _pt = _po.decode("utf-8", "replace") if isinstance(_po, bytes) else str(_po)
                if "Y" in _pt:
                    dp_dir = cand
                    break
            dp_dir = dp_dir or f"/u01/app/oracle/admin/{sid or 'orcl'}/dpdump"
        self.logger.info("[%s] 远端 DATA_PUMP_DIR=%s", self.task_name, dp_dir)
        # 目录属主健康：DATA_PUMP_DIR 被 root 接管（root mkdir 过）时 oracle
        # 用户写转储/日志必报 ORA-29283（171 实测）——统一修正属主
        try:
            _chk = self._resolve_check_user(ssh_host)
            _ssh_exec_pipe(client, remote_dump._wrap_login(
                f"mkdir -p {shlex.quote(dp_dir)} && chown -R "
                f"{_chk}:oinstall {shlex.quote(dp_dir)} 2>/dev/null; "
                f"chmod 775 {shlex.quote(dp_dir)}"), timeout=30)
        except Exception as _e:
            self.logger.warning("[%s] DATA_PUMP_DIR 属主修正失败: %s",
                                self.task_name, _e)
        # 固定产物形态：dmp/log 文件名**确定性**（按任务固定，不含随机时间戳），
        # 成功后写完成标记 → 拉回中断时重试直接续传，**不重跑 expdp**。
        tid = int(self.task.get("id") or 0)
        dmp_name = f"bk_t{tid}.dmp"
        log_name = f"bk_t{tid}.log"
        remote_dmp = f"{dp_dir}/{dmp_name}"
        remote_log = f"{dp_dir}/{log_name}"
        remote_marker = f"{dp_dir}/.bkdone_t{tid}"

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
            # 连接串整体单引号包裹：口令可能因含特殊字符而带双引号，
            # 直接拼接会被 shell 吃掉引号。
            f"\"$EXPDP_BIN\" '{conn_easy}' {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={dmp_name} LOGFILE={log_name} </dev/null\n"
            "RC=$?\n"
            "if [ $RC -ne 0 ]; then\n"
            f"  echo '[fallback] primary expdp failed (rc=$RC), retry with / as sysdba'\n"
            f"  \"$EXPDP_BIN\" \"'/ as sysdba'\" {mode_args} DIRECTORY=DATA_PUMP_DIR "
            f"DUMPFILE={dmp_name} LOGFILE={log_name} </dev/null\n"
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

            # 复用探测：上次 expdp 已导出完成但 dmp 拉回中断 → 跳过重跑 expdp
            remote_sh = ""      # 远端 expdp 包装脚本，成功后一并清理（零残留）
            ready = remote_dump.remote_artifact_ready(client, remote_marker, min_size=1)
            reuse = bool(ready["reusable"])
            dp_space_note = ""
            if not reuse:
                # 写盘前空间预算检查（dmp 体积≈整库；DATA_PUMP_DIR 所在分区
                # 剩余不足时提前失败并给指引，避免撑爆数据库服务器）
                try:
                    dp_space_note = remote_dump.ensure_remote_stage_ok(
                        client, self.task, dp_dir, label="expdp DATA_PUMP_DIR")
                except RuntimeError as e:
                    return BackupResult(
                        success=False, status=BackupStatus.FAILED,
                        message=f"expdp 前置检查未通过：{str(e)[:400]}")
            if reuse:
                self.logger.info("[%s] 复用远端已完成 dmp %s，跳过 expdp 执行",
                                 self.task_name, remote_dmp)
            else:
                # 清掉同名残留（expdp 遇到已存在的 dumpfile 会直接报错退出）
                for _p in (remote_dmp, remote_log):
                    try:
                        sftp.remove(_p)
                    except Exception:
                        pass
                remote_sh = f"/u01/app/oracle/backup/expdp_{ts}.sh"
                with sftp.open(remote_sh, "w") as f:
                    f.write(expdp_sh)
                try:
                    sftp.chmod(remote_sh, 0o755)
                except Exception:
                    pass

            if reuse:
                out_text = f"复用远端已完成 dmp {remote_dmp}，跳过 expdp 执行"
                err_text, rc, duration = "", 0, 0.0
            else:
                inner = f"bash {remote_sh}"
                shell = (f"su - {self._resolve_check_user(ssh_host)} -c "
                         f"{shlex.quote(inner)}")
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

            # 写远端完成标记：下次重试据此复用，**不重跑 expdp**
            if not reuse:
                remote_dump.mark_remote_artifact_done(
                    client, remote_marker, size=int(dmp_attr.st_size or -1),
                    note=f"expdp {ssh_host.get('host_key','')}")

            out_dir = self._output_dir()
            os.makedirs(out_dir, exist_ok=True)
            # 断点续传拉回 dmp（中断后从已传字节继续，不重跑导出）
            res = remote_dump.sftp_pull_resumable(
                client, remote_dmp, os.path.join(out_dir, f"{ts}.dmp"),
                task=self.task, key="expdp.dmp", db_type=f"{self.db_type}_dmp",
                host_key=ssh_host.get("host_key", ""), has_rc=False,
                stable_secs=3, label="expdp dmp", min_size=1024)
            local_dmp = res["path"]
            local_log = os.path.join(out_dir, f"{ts}.log")
            try:
                remote_dump.sftp_pull_resumable(
                    client, remote_log, local_log, task=self.task,
                    key="expdp.log", db_type=f"{self.db_type}_dmp",
                    host_key=ssh_host.get("host_key", ""), has_rc=False,
                    stable_secs=3, label="expdp log", min_size=0)
            except Exception:
                local_log = None  # log 缺失不致命

            # 已完整拉回 → 清理远端 dmp/log、完成标记与 expdp 脚本（服务器不留产物）
            # 注：失败分支在此之前已 return，脚本与日志保留便于排障。
            remote_dump.cleanup_remote_artifacts(
                client, [remote_dmp, remote_log, remote_marker]
                + ([remote_sh] if remote_sh else []))

            size = os.path.getsize(local_dmp)
            checksum = db.sha256_file(local_dmp)
            hk = ssh_host.get("host_key", "remote")
            msg = (f"通过 SSH 在 {hk} 以 oracle 用户执行 expdp({mode_desc})成功，"
                   f"已拉回 dmp: {local_dmp} ({db.human_size(size)})"
                   f"{'（复用上次远端产物，未重跑导出）' if reuse else ''}"
                   f"{'（断点续传）' if res['resumed'] else ''}"
                   f"{'（' + dp_space_note + '）' if dp_space_note else ''}")
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
            restore_conn = f"{username}/{self._quote_password(pw)}@//{self._db_host_for_remote(target_host_info)}:{port}/{service}"
            dp_dir = self._query_dp_dir(client, restore_conn) or self._query_dp_dir(client)
            if not dp_dir:
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message="无法在远端解析 DATA_PUMP_DIR 实际路径，恢复中止")
            # 导入目录属主修正（root 接管时 impdp 写日志报 ORA-29283，171 实测）
            try:
                from core.engines.file import _ssh_exec_pipe as _sep_rst
                _chk = self._resolve_check_user(target_host_info)
                _sep_rst(client, remote_dump._wrap_login(
                    f"mkdir -p {shlex.quote(dp_dir)} && chown -R "
                    f"{_chk}:oinstall {shlex.quote(dp_dir)} 2>/dev/null; "
                    f"chmod 775 {shlex.quote(dp_dir)}"), timeout=30)
            except Exception:
                pass

            # 1) 上传 dmp 到服务端导入目录
            sftp = client.open_sftp()
            try:
                remote_dmp = f"{dp_dir}/{dmp_name}"
                remote_dump.sftp_put_resumable(
                    client, local_dmp, remote_dmp,
                    rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                    label="impdp dmp 推送")
                self.logger.info("[%s] dmp 已推回远端: %s (%s)", self.task_name,
                                 remote_dmp, db.human_size(os.path.getsize(local_dmp)))

                # 2) 远端 impdp（oracle 用户执行，走本机 127.0.0.1 监听）
                impdp_bin = self._resolve_oracle_tool(client, "impdp")
                if not impdp_bin:
                    return BackupResult(
                        success=False, status=BackupStatus.FAILED,
                        message="远端未找到 impdp（已探测 oracle 用户 PATH 与常见 "
                                "ORACLE_HOME 安装目录），请确认数据库软件安装完整")
                mode_args = self._dp_mode_args(
                    self._restore_extra_with_tables(extra, kwargs),
                    for_import=True)[0]
                # UAT 异名 schema / 表改名：Oracle 的「库名不一样」对应
                # REMAP_SCHEMA，表改名对应 REMAP_TABLE（19c 实测路径）。
                # 11g 对 REMAP_TABLE 有分区表等限制，失败时由 impdp 日志如实透出。
                if isinstance(schemas, (list, tuple)) and schemas:
                    src_schema = str(schemas[0]).strip()
                elif schemas:
                    src_schema = str(schemas).split(",")[0].strip()
                else:
                    src_schema = str(extra.get("schema") or extra.get("owner") or "").strip()
                remap_args = ""
                target_schema = str(kwargs.get("target_schema") or "").strip()
                if target_schema:
                    if not src_schema:
                        return BackupResult(
                            success=False, status=BackupStatus.FAILED,
                            backup_path=backup_path,
                            message="REMAP_SCHEMA 需要源 schema：请在任务高级选项配置 "
                                    "schemas（或本次恢复不指定 target_schema）")
                    remap_args += f" REMAP_SCHEMA={src_schema}:{target_schema}"
                renames = kwargs.get("table_renames") or {}
                if renames:
                    parts = []
                    for old, new in renames.items():
                        o, n = str(old).strip(), str(new).strip()
                        if "." not in o and src_schema:
                            o = f"{src_schema}.{o}"
                        parts.append(f"{o}:{n}")
                    remap_args += " REMAP_TABLE=" + ",".join(parts)

                def _impdp_shell(conn_str):
                    _inner = (f"export PATH=$ORACLE_HOME/bin:$PATH; "
                              f"{shlex.quote(impdp_bin)} {shlex.quote(conn_str)} "
                              f"DUMPFILE={dmp_name} LOGFILE={log_name} </dev/null "
                              f"DIRECTORY=DATA_PUMP_DIR "
                              f"TABLE_EXISTS_ACTION=REPLACE {mode_args}{remap_args}")
                    return (f"su - {self._resolve_check_user(target_host_info)} -c "
                            f"{shlex.quote(_inner)}")

                shell = _impdp_shell(restore_conn)
                start = time.time()
                out, err, rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(shell),
                    timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
                # 瞬时监听抖动自动重试（129 实测：UDI-12514/ORA-12514 偶发，
                # 同参数立即重跑即成功；内存 61311831 有该机网络抖动记录）
                transient = ("12514", "12541", "12170", "U DI-12514", "UDI-12514")
                _retry = 0
                while rc != 0 and _retry < 2:
                    _combined_probe = (out.decode("utf-8", "replace") if isinstance(out, bytes)
                                       else (out or "")) + (err or "")
                    if not any(p in _combined_probe for p in transient):
                        break
                    _retry += 1
                    self.logger.warning(
                        "[%s] impdp 遇监听瞬时抖动(12514/12541/12170)，%ds 后重试(%d/2)",
                        self.task_name, 5 * _retry, _retry)
                    time.sleep(5 * _retry)
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(shell),
                        timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
                # sysdba 兜底（171 19c 实测）：口令含 @ 等特殊字符时，
                # Data Pump 连接串无法通过认证/解析（ORA-01017/12154/12514），
                # expdp 路径已有 / as sysdba 兜底；impdp 同样兜底一次。
                _fb_note = ""
                if rc != 0:
                    _probe = ((out.decode("utf-8", "replace") if isinstance(out, bytes)
                               else (out or "")) + (err or ""))
                    if any(p in _probe for p in ("01017", "12154", "12514", "12541", "12170")):
                        self.logger.warning(
                            "[%s] impdp 主连接认证/解析失败，尝试 / as sysdba 兜底导入",
                            self.task_name)
                        # Data Pump 位置参数要求字面单引号包裹（expdp 同款写法），
                        # 直传 "/ as sysdba" 报 LRM-00108（171 19c 实测）
                        out, err, rc = _ssh_exec_pipe(
                            client, remote_dump._wrap_login(_impdp_shell("'/ as sysdba'")),
                            timeout=config.BACKUP_TIMEOUT if hasattr(config, "BACKUP_TIMEOUT") else 3600)
                        _fb_note = "（主连接认证失败，已以 / as sysdba 兜底导入）"
                duration = round(time.time() - start, 3)

                def _dec_impdp_bytes(b):
                    """impdp 输出/日志解码：远端 locale 为 zh_GBK 时是 GBK 编码，
                    utf-8 replace 会把收尾行打成 \ufffd（不可逆），导致
                    "successfully completed at" 收尾判定失效——严格 utf-8 失败
                    时回退 GBK。"""
                    if not isinstance(b, bytes):
                        return b or ""
                    try:
                        return b.decode("utf-8")
                    except UnicodeDecodeError:
                        return b.decode("gbk", "replace")

                out_text = _dec_impdp_bytes(out)
                self.logger.info("[%s] 远端 impdp 返回 rc=%s", self.task_name, rc)

                # 拉回导入日志便于排查
                remote_log = f"{dp_dir}/{log_name}"
                log_tail = ""
                try:
                    with sftp.open(remote_log, "r") as lf:
                        log_tail = _dec_impdp_bytes(lf.read())[-1500:]
                except Exception:
                    pass

                # 成功判定：rc==0；或 rc 非 0 但日志里 impdp 正常收尾且仅有
                # 非致命提示。168 真机实测（11g 全库 dmp 恢复同实例，rc=5）：
                # ORA-31684(对象已存在) 3149 次、ORA-39111(跳过从属对象) 2892 次、
                # ORA-39112/ORA-39083(跳过组件) 数百次——业务数据实际导入成功，
                # 以往按 rc 判失败属误报。另注意远端 locale 为 zh_GBK 时日志是
                # GBK 编码（上方 _dec_impdp_bytes 已做双解码），否则收尾判定失效。
                fatal = False
                if rc != 0:
                    combined = (out_text or "") + "\n" + (log_tail or "")
                    fatal_patterns = (
                        "ORA-00600", "ORA-07445", "ORA-01017", "ORA-39097",
                        "ORA-39001", "UDE-000", "ORA-01157",
                    )
                    if any(p in combined for p in fatal_patterns):
                        fatal = True
                    else:
                        non_fatal = any(m in combined for m in (
                            "ORA-31684",   # USER/对象已存在
                            "ORA-39082",   # 对象类型冲突（可忽略）
                            "ORA-39111",   # 跳过从属对象类型（基本对象已存在）
                            "ORA-39112",   # 跳过部分组件
                            "ORA-39083",   # SQL 创建失败但继续（对象已存在场景）
                        ))
                        # 修复（129 实测 rc=5）：数据行已导入（. imported "SCHEMA".
                        # "TABLE"）但非致命 ORA 提示出现在日志前部、被 log_tail
                        # 截尾后 non_fatal 漏判 → 误报失败。无致命错误且数据行
                        # 已出现导入明细即视为成功。
                        data_imported = '. imported "' in combined or '.imported "' in combined
                        if data_imported:
                            non_fatal = True
                        # impdp 正常收尾（哪怕 rc=5 = completed with errors），
                        # 且无非致命以外问题 → 数据已导入。中文版 impdp（168
                        # zh_GBK locale 实测）收尾为「作业 ... 已经完成」。
                        finished = any(m in combined for m in (
                            "successfully completed at",
                            "已经完成",
                            "imported",
                            "Import completed",
                        ))
                        fatal = not (non_fatal and finished)

                if rc != 0 and fatal:
                    snippet = (out_text or log_tail)[-1200:]
                    hints = []
                    if "12514" in snippet or "12541" in snippet:
                        hints.append(
                            f"监听不认识服务 {service}：请在数据库服务器执行 "
                            f"lsnrctl status 查看实际 Service 名，恢复表单"
                            f"「目标库/实例」应填该服务名（如 orcl11g/orcl）")
                    if "ORA-01017" in snippet:
                        hints.append("认证失败：请核对数据库用户名/口令")
                    hint_txt = ("；" + "；".join(hints)) if hints else ""
                    return BackupResult(
                        success=False, status=BackupStatus.FAILED,
                        duration_sec=duration, stdout=out_text,
                        message=f"远端 impdp 恢复失败(rc={rc}): {snippet}{hint_txt}")

                note = ""
                if rc != 0:
                    note = "（含 ORA-31684/ORA-39082 非致命提示，数据对象已导入）"
                msg = (f"Oracle 逻辑恢复(impdp)成功：dmp 已推回数据库服务器 "
                       f"{target_host_info.get('host_key', '')} 并导入 "
                       f"{service}（TABLE_EXISTS_ACTION=REPLACE，"
                       f"耗时 {duration}s）{note}{_fb_note}")
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

        # 恢复路径必须与备份路径对称（2026-09-17 修复，168 环境 Oracle 11g 实测暴露）：
        # 逻辑备份的实际链路是「SSH 到数据库服务器执行 expdp → dmp 拉回平台本地」，
        # 因此同机恢复也必须把 dmp **推回数据库服务器**用 impdp 导入，而不是去找
        # 平台本机的 imp/impdp。此前未指定跨主机目标时会直接落到本机 imp 分支，
        # 而平台（客户端零安装原则）本就不该装 Oracle 客户端，于是必然失败，
        # 造成「备份成功但恢复不了」的闭环断裂——灾备产品的致命缺陷。
        if (backup_path
                and (backup_path.endswith(".dmp") or backup_path.endswith(".dmp.gz"))
                and os.path.exists(backup_path)):
            ssh_host = None
            try:
                from core import remote_dump as _rd
                ssh_host = _rd.resolve_ssh_host(self.task)
            except Exception as e:
                self.logger.warning("[%s] 解析 SSH 主机失败，回退本机恢复: %s",
                                    self.task_name, e)
            if ssh_host:
                self.logger.info(
                    "[%s] 逻辑备份同机恢复：将 dmp 推回 %s 执行 impdp（与备份链路对称）",
                    self.task_name, ssh_host.get("host_key"))
                return self._restore_logical_remote(backup_path, ssh_host, kwargs)

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

        # PITR 会 RESTORE + RECOVER + OPEN RESETLOGS，属破坏性操作，且按「客户端零安装、
        # 一切远端动作走 SSH」的设计原则，平台侧不得也不该用本机 rman 直连执行
        # （connect target / 只在数据库服务器本机有效）。因此这里只生成脚本并诚实返回
        # 「未执行」，绝不产出假的恢复成功。
        return BackupResult(
            success=False, status=BackupStatus.FAILED,
            backup_path=script, simulated=False,
            message=(f"RMAN PITR 脚本已生成但未执行：{script}；"
                     "请在数据库服务器上（Oracle 用户）执行该脚本，"
                     "或在「实时备份/PITR」中通过平台 SSH 通道下发"))

    def rman_pitr_remote(self, target_time: str, confirm: bool = False,
                         ssh_host: dict = None) -> BackupResult:
        """RMAN 时间点恢复（PITR）——平台 SSH 通道真实执行。

        破坏性操作：SHUTDOWN → RESTORE/RECOVER → OPEN RESETLOGS 会把目标库
        整体回退到 target_time，必须 confirm=True 显式确认。

        前提：ARCHIVELOG 模式 + target_time 之前存在有效 RMAN 全量（0 级）
        备份 + 归档日志在 FRA/备份中可用。

        流程（全部经 SSH 在数据库服务器以 oracle 用户执行）：
          1) sqlplus 预检：log_mode=ARCHIVELOG；
          2) sqlplus：SHUTDOWN IMMEDIATE; STARTUP MOUNT;
          3) rman：RUN { SET UNTIL TIME ...; RESTORE DATABASE; RECOVER DATABASE;
             ALTER DATABASE OPEN RESETLOGS; }
          4) sqlplus 验证实例 OPEN。
        """
        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        if not target_time:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="必须提供 target_time（YYYY-MM-DD HH24:MI:SS）")
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                        str(target_time).strip()):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="target_time 格式必须为 YYYY-MM-DD HH24:MI:SS")
        if not confirm:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=("PITR 为破坏性操作（RESTORE+RECOVER+OPEN RESETLOGS 将把"
                         "目标库整体回退到指定时点），必须显式传 confirm=true"))

        ssh = ssh_host or remote_dump.resolve_ssh_host(self.task)
        if not ssh:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="任务未纳管 SSH 备份机，无法在数据库服务器"
                                        "执行 PITR（本机通道不支持远端恢复）")
        client = remote_dump._connect(ssh)
        check_user = self._resolve_check_user(ssh)
        ts = self._timestamp()

        def ora(sql_text: str, timeout: int = 120) -> tuple:
            """远端 sqlplus 执行（SFTP 写脚本，避免多层引号嵌套）。"""
            sqlfile = f"/tmp/pitr_{ts}_{int(time.time()*1000)%100000}.sql"
            sftp = client.open_sftp()
            try:
                with sftp.open(sqlfile, "w") as f:
                    f.write("connect / as sysdba\nSET PAGESIZE 0 FEEDBACK OFF "
                            "HEADING OFF\n" + sql_text + "\nEXIT;\n")
            finally:
                sftp.close()
            shell = (f"su - {check_user} -c "
                     f"{shlex.quote('sqlplus -s /nolog @' + sqlfile)}")
            out, err, rc = _ssh_exec_pipe(
                client, remote_dump._wrap_login(shell), timeout=timeout)
            txt = out.decode("utf-8", "replace") if isinstance(out, bytes) \
                else str(out or "")
            return txt, err, rc

        def rman_run(rman_script: str, timeout: int = 7200) -> tuple:
            rman_bin = self._resolve_oracle_tool(client, "rman")
            if not rman_bin:
                raise RuntimeError("远端未找到 rman（已探测 oracle 用户 PATH 与 "
                                   "常见 ORACLE_HOME 目录）")
            shfile = f"/tmp/pitr_{ts}.rman"
            sftp = client.open_sftp()
            try:
                with sftp.open(shfile, "w") as f:
                    f.write(rman_script)
            finally:
                sftp.close()
            shell = (f"su - {check_user} -c "
                     f"{shlex.quote(rman_bin + ' target / cmdfile=' + shfile)}")
            return _ssh_exec_pipe(client, remote_dump._wrap_login(shell),
                                  timeout=timeout)

        def has_ora_error(text: str) -> str:
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("ORA-") or s.startswith("RMAN-"):
                    return s[:200]
            return ""

        # 0.5) 备份片回推：物理备份拉回后远端**零残留**，而 RMAN PITR 在数据库
        # 服务器上执行 RESTORE，必须有备份片——否则 ORA-19505 failed to
        # identify file（实测命中）。从平台产物目录推回最近一轮备份片并 CATALOG。
        out_dir = self._output_dir()
        try:
            import glob as _glob
            all_bkps = [(p, os.path.getmtime(p)) for p in _glob.glob(
                os.path.join(out_dir, "ora_bkp_*.bkp"))]
            if all_bkps:
                all_bkps.sort(key=lambda x: -x[1])
                m_pref = re.search(r"(ora_bkp_t\d+)",
                                   os.path.basename(all_bkps[0][0]))
                prefix = m_pref.group(1) if m_pref else ""
                batch = ([p for p, _ in all_bkps
                          if prefix and prefix in os.path.basename(p)]
                         if prefix else [all_bkps[0][0]])
                dp_dir = self._query_dp_dir(client) or "/u01/app/oracle/backup"
                _sftp = client.open_sftp()
                try:
                    for p in batch:
                        rmt = f"{dp_dir}/{os.path.basename(p)}"
                        try:
                            if int(_sftp.stat(rmt).st_size or 0) == \
                                    os.path.getsize(p):
                                continue   # 已在且完整（重试续传）
                        except FileNotFoundError:
                            pass
                        self.logger.info("[%s] PITR: 回推备份片 %s (%s)",
                                         self.task_name, os.path.basename(p),
                                         db.human_size(os.path.getsize(p)))
                        remote_dump.sftp_put_resumable(
                            client, p, rmt,
                            rate_kbps=int(self.task.get("bandwidth_limit") or 0),
                            label="PITR 备份片回推")
                finally:
                    try:
                        _sftp.close()
                    except Exception:  # noqa: BLE001
                        pass
                rman_run(f"CATALOG START WITH '{dp_dir}/' NOPROMPT;\nexit;\n")
                self.logger.info("[%s] PITR: 已回推 %d 个备份片并 CATALOG",
                                 self.task_name, len(batch))
        except Exception as e:  # noqa: BLE001 —— 回推失败不阻断（RMAN 本地能找到则可忽略）
            self.logger.warning("[%s] PITR 备份片回推失败: %s",
                                self.task_name, e)

        try:
            # 1) 预检：ARCHIVELOG
            pre, _e, _rc = ora("SELECT log_mode FROM v$database;")
            if "ARCHIVELOG" not in pre.upper():
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=f"源库非 ARCHIVELOG 模式，无法 PITR（log_mode 输出: "
                            f"{pre.strip()[:60]}）")

            # 2) 停库到 MOUNT
            self.logger.info("[%s] PITR: SHUTDOWN IMMEDIATE + STARTUP MOUNT",
                             self.task_name)
            sh_out, sh_err, sh_rc = ora(
                "SHUTDOWN IMMEDIATE;\nSTARTUP MOUNT;\n"
                "SELECT status FROM v$instance;", timeout=300)
            err_line = has_ora_error(sh_out)
            if err_line:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message=f"停库/加载阶段失败: {err_line}")

            # 3) RMAN RESTORE + RECOVER + OPEN RESETLOGS
            safe_t = str(target_time).strip()
            rman_script = (
                # 不写 connect target /：命令行已 `rman target /`，脚本内重复
                # connect 会报 RMAN-06167 "already connected"（实测命中）
                "RUN {\n"
                f"  SET UNTIL TIME \"TO_DATE('{safe_t}','YYYY-MM-DD HH24:MI:SS')\";\n"
                "  RESTORE DATABASE;\n"
                "  RECOVER DATABASE;\n"
                "}\n"
                "ALTER DATABASE OPEN RESETLOGS;\n"
                "exit;\n")
            self.logger.info("[%s] PITR: RESTORE/RECOVER 到 %s …",
                             self.task_name, safe_t)
            out, err, rc = rman_run(rman_script)
            rman_txt = out.decode("utf-8", "replace") if isinstance(out, bytes) \
                else str(out or "")
            err_line = has_ora_error(rman_txt)
            if rc != 0 or err_line:
                # 恢复失败时尽力把实例拉回 OPEN，避免留下 MOUNT 状态
                try:
                    ora("ALTER DATABASE OPEN RESETLOGS;", timeout=300)
                except Exception:  # noqa: BLE001
                    try:
                        ora("ALTER DATABASE OPEN;", timeout=300)
                    except Exception:  # noqa: BLE001
                        pass
                tail = rman_txt[-500:] if rman_txt else str(err or "")[-500:]
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message=f"RMAN RESTORE/RECOVER 失败: "
                                            f"{err_line or 'rc=' + str(rc)}；"
                                            f"日志尾部: {tail}")

            # 4) 验证实例 OPEN
            st, _e2, _r2 = ora("SELECT status FROM v$instance;", timeout=60)
            opened = "OPEN" in st.upper() and "MOUNTED" not in st.upper()
            return BackupResult(
                success=opened,
                status=(BackupStatus.SUCCESS if opened
                        else BackupStatus.FAILED),
                message=(f"Oracle PITR 完成：已恢复至 {safe_t} 并 OPEN RESETLOGS"
                         f"（实例状态: {'OPEN' if opened else st.strip()[:30]}）；"
                         "incarnate 已变化，请立即做一次全量备份"),
                stdout=rman_txt[-2000:],
            )
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"PITR 执行异常: {e}")

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
