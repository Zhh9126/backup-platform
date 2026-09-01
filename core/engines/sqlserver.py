# -*- coding: utf-8 -*-
"""SQL Server 备份/恢复引擎（Linux + Windows）。

严格遵循 Microsoft 官方 T-SQL 语法（BACKUP/RESTORE (Transact-SQL)）：
- 完整备份：BACKUP DATABASE [db] TO DISK = N'<path>'
            WITH NAME=..., COMPRESSION, CHECKSUM, STATS=10, INIT
- 差异备份：... WITH DIFFERENTIAL, COMPRESSION, CHECKSUM, INIT（基于完整备份基准）
- 日志备份：BACKUP LOG [db] TO DISK = N'<path>' WITH ...（需 FULL/BULK_LOGGED 恢复模式）
- 还原：RESTORE FILELISTONLY 获取逻辑文件名 → RESTORE DATABASE [db] FROM DISK=N'...'
        WITH MOVE ..., REPLACE, RECOVERY
- 校验：RESTORE VERIFYONLY FROM DISK=N'...' WITH CHECKSUM

安全说明：密码通过 sqlcmd 官方环境变量 SQLCMDPASSWORD 注入，绝不进入 argv。

平台约束：平台不装任何依赖、不配环境变量即可用——sqlcmd 在数据库服务器上
（Linux 默认 /opt/mssql-tools/bin），平台通过 SSH 远程执行并动态发现工具；
发现失败时可用任务 extra_options.tool_path 手动兜底；任务级 SSH 凭据（免纳管）
同样适用。Windows 目标（ssh_hosts.os_type=windows）走 cmd 语法。
"""
import os
import time

import config
import core.db as db
from core.engines.base import (
    BackupEngine, BackupType, BackupMode, BackupStatus, BackupResult,
)

# 系统数据库（list_databases 过滤；database_id<=4 即 master/tempdb/model/msdb）
_SYSTEM_DBS = ("master", "tempdb", "model", "msdb")

_LINUX_DEFAULT_BACKUP_DIR = "/var/opt/mssql/backup"
_WIN_DEFAULT_BACKUP_DIR = "C:\\MSSQL\\backup"


def _sh(s: str) -> str:
    """POSIX 单引号转义。"""
    import shlex
    return shlex.quote(str(s or ""))


def _to_text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")


def _q(tsql: str) -> str:
    """bash 双引号包裹 T-SQL（sqlcmd -Q 参数）。

    T-SQL 内的 N'...' 单引号无需处理；需转义双引号/$/反引号，
    反斜杠在双引号内成对转义后还原为原字符，路径不受影响。
    """
    s = (str(tsql).replace("\\", "\\\\").replace('"', '\\"')
         .replace("$", "\\$").replace("`", "\\`"))
    return f'"{s}"'


class SQLServerEngine(BackupEngine):
    """SQL Server 备份引擎（BACKUP/RESTORE T-SQL，经 sqlcmd 执行）。"""

    db_type = "sqlserver"
    display_name = "SQL Server"
    # 本机回退执行时依赖 sqlcmd；远程执行时工具在数据库服务器上自动发现
    required_clients = ["sqlcmd"]
    physical_bundled_tools = []

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _is_win(self, ssh_host: dict = None) -> bool:
        """目标是否 Windows（ssh_hosts.os_type=windows）。"""
        return str((ssh_host or {}).get("os_type") or "").lower() == "windows"

    def _task_tool_path(self) -> str:
        """任务级工具路径兜底（extra_options.tool_path）。"""
        from core.remote_dump import task_tool_path
        return task_tool_path(self.task)

    def _parse_extra(self) -> dict:
        raw = self.task.get("extra_options")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            import json
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    def _exec_tsql(self, tsql: str, ssh_host: dict = None, timeout: int = 7200):
        """执行 T-SQL，返回 (rc, stdout, stderr)。

        优先经 SSH 在数据库服务器上执行（sqlcmd -S 127.0.0.1,<port>）；
        未传 ssh_host 时自动按任务解析（含任务级 SSH 凭据，免纳管），
        完全无 SSH 通道时回退本机 sqlcmd 连任务地址。
        密码走 SQLCMDPASSWORD 环境变量。
        """
        if ssh_host is None:
            from core import remote_dump
            ssh_host = remote_dump.resolve_ssh_host(self.task)
        user = self.task.get("username") or "sa"
        pw = db.decrypt_secret(self.task.get("password") or "")
        port = int(self.task.get("port") or 1433)
        tp = self._task_tool_path()

        from core import remote_dump
        from core.engines.file import _ssh_exec_pipe

        if ssh_host is not None:
            client = remote_dump._connect(ssh_host)
            try:
                if self._is_win(ssh_host):
                    # Windows：cmd 语法，set 注入密码后执行；-b 使 T-SQL 错误返回非零
                    safe_pw = str(pw).replace("^", "^^")
                    inner = (f"set SQLCMDPASSWORD={safe_pw}&& "
                             f"sqlcmd -S 127.0.0.1,{port} -U {user} -W -b -Q \"{tsql}\"")
                    out, err, rc = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(inner), timeout=timeout)
                    return rc, _to_text(out), _to_text(err)
                # Linux
                sqlcmd = remote_dump.resolve_remote_tool(
                    ssh_host, "sqlcmd", extra_paths=tp)
                if not sqlcmd:
                    raise RuntimeError(
                        "远端主机未找到 sqlcmd（/opt/mssql-tools/bin 与 PATH 均无）。"
                        "请在远端安装 mssql-tools，或在任务 extra_options.tool_path "
                        "填写其目录")
                # -b：T-SQL 报错时 sqlcmd 返回非零退出码（官方参数），
                # 否则 BACKUP/RESTORE 失败也会被 rc=0 掩盖
                script = (
                    f"set -o pipefail; "
                    f"export SQLCMDPASSWORD={_sh(pw)}; "
                    f"{_sh(sqlcmd)} -S 127.0.0.1,{port} -U {_sh(user)} -W -b "
                    f"-Q {_q(tsql)}"
                )
                out, err, rc = _ssh_exec_pipe(
                    client, remote_dump._wrap_login(script), timeout=timeout)
                return rc, _to_text(out), _to_text(err)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        # 本机回退：sqlcmd 直连任务地址
        import subprocess
        import shutil as _shutil
        sqlcmd = _shutil.which("sqlcmd")
        if not sqlcmd and tp:
            for d in tp.split(":"):
                cand = os.path.join(d, "sqlcmd")
                if os.path.isfile(cand):
                    sqlcmd = cand
                    break
        if not sqlcmd:
            raise RuntimeError("本机未找到 sqlcmd，且任务未配置 SSH 通道")
        env = os.environ.copy()
        if pw:
            env["SQLCMDPASSWORD"] = pw
        cmd = [sqlcmd, "-S", f"{self.task.get('host')},{port}", "-U", user,
               "-W", "-b", "-Q", tsql]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""

    def _backup_dir(self, ssh_host: dict) -> str:
        """解析服务器端备份目录：extra.backup_dir > 实例默认备份目录 > 平台默认。"""
        custom = str(self._parse_extra().get("backup_dir") or "").strip()
        if custom:
            return custom.rstrip("/\\")
        win = self._is_win(ssh_host)
        try:
            val = self._instance_path(ssh_host, "InstanceDefaultBackupPath")
            if val:
                return val
        except Exception:
            pass
        return _WIN_DEFAULT_BACKUP_DIR if win else _LINUX_DEFAULT_BACKUP_DIR

    # ------------------------------------------------------------------
    # 备份（BACKUP DATABASE/LOG TO DISK → SFTP 拉回）
    # ------------------------------------------------------------------
    def backup(self, backup_type: BackupType) -> BackupResult:
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        return self._backup_remote(ssh_host, backup_type)

    def _backup_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        from core.engines.file import _ssh_exec_pipe
        from core import remote_dump

        db_name = (self.task.get("db_name") or "").strip()
        if not db_name:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, simulated=False,
                message="SQL Server 备份必须指定库名（db_name）；"
                        "系统库 master/tempdb/model/msdb 请勿作为业务备份对象")

        bt = backup_type if isinstance(backup_type, BackupType) \
            else BackupType(str(backup_type))
        if bt == BackupType.INCREMENTAL:
            # SQL Server 的真正增量 = 事务日志备份（需 FULL 恢复模式）
            tsql_op, ext = "LOG", ".trn"
        elif bt == BackupType.DIFFERENTIAL:
            tsql_op, ext = "DATABASE", ".diff"
        else:
            tsql_op, ext = "DATABASE", ".bak"

        win = self._is_win(ssh_host) if ssh_host else False
        try:
            bdir = self._backup_dir(ssh_host)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                simulated=False,
                                message=f"解析备份目录失败: {e}")

        ts = self._timestamp()
        fname = f"{db_name}_{bt.value}_{ts}{ext}"
        sep = "\\" if win else "/"
        disk_path = f"{bdir}{sep}{fname}"

        # 确保目录存在（Linux 归属 mssql 用户，避免“操作系统错误 5/访问被拒绝”）
        if ssh_host and not win:
            from core import remote_dump as _rd
            client = _rd._connect(ssh_host)
            try:
                prep = (f"mkdir -p {_sh(bdir)} && chown mssql:mssql {_sh(bdir)} "
                        f"2>/dev/null || true")
                _ssh_exec_pipe(client, _rd._wrap_login(prep), timeout=60)
            except Exception:
                pass
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        # 官方 T-SQL：BACKUP DATABASE/LOG ... TO DISK WITH COMPRESSION/CHECKSUM
        if bt == BackupType.INCREMENTAL:
            tsql = (f"BACKUP LOG [{db_name}] TO DISK = N'{disk_path}' "
                    f"WITH NAME = N'backup-platform', COMPRESSION, CHECKSUM, "
                    f"STATS = 10, INIT")
        elif bt == BackupType.DIFFERENTIAL:
            tsql = (f"BACKUP DATABASE [{db_name}] TO DISK = N'{disk_path}' "
                    f"WITH DIFFERENTIAL, COMPRESSION, CHECKSUM, STATS = 10, INIT")
        else:
            tsql = (f"BACKUP DATABASE [{db_name}] TO DISK = N'{disk_path}' "
                    f"WITH NAME = N'backup-platform', COMPRESSION, CHECKSUM, "
                    f"STATS = 10, INIT")

        start = time.time()
        try:
            rc, out, err = self._exec_tsql(tsql, ssh_host=ssh_host)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                simulated=False,
                                message=f"SQL Server 备份执行失败: {e}")
        if rc != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED, simulated=False,
                stdout=out, stderr=err,
                message=f"BACKUP 失败(rc={rc}): {(err or out)[:500]}")

        # 服务器端产物存在性与大小
        size = 0
        if ssh_host:
            try:
                client = remote_dump._connect(ssh_host)
                try:
                    if win:
                        check = (f"powershell -NoProfile -Command \""
                                 f"if (Test-Path '{disk_path}') "
                                 f"{{ (Get-Item '{disk_path}').Length }} "
                                 f"else {{ 'missing' }}\"")
                    else:
                        check = (f"stat -c %s {_sh(disk_path)} 2>/dev/null "
                                 f"|| echo missing")
                    _, out2, _rc2 = _ssh_exec_pipe(
                        client, remote_dump._wrap_login(check), timeout=60)
                    txt = _to_text(out2).strip()
                    size = 0 if txt == "missing" else int(txt or 0)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            except Exception:
                pass

        # SFTP 拉回产物
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, fname)
        try:
            client = remote_dump._connect(ssh_host)
            sftp = client.open_sftp()
            try:
                # Windows OpenSSH 的 SFTP 通常接受正斜杠路径
                remote_ref = disk_path.replace("\\", "/") if win else disk_path
                sftp.get(remote_ref, local_path)
            finally:
                sftp.close()
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as e:
            if size > 0:
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=disk_path, size_bytes=size,
                    duration_sec=round(time.time() - start, 3),
                    simulated=False,
                    message=f"BACKUP 成功但产物拉回失败（保留在服务器 {disk_path}）: {e}")
            return BackupResult(
                success=False, status=BackupStatus.FAILED, simulated=False,
                message=f"备份产物拉回失败: {e}")

        size = os.path.getsize(local_path) or size
        checksum = db.sha256_file(local_path)
        hk = (ssh_host or {}).get("host_key", "remote")
        if bt == BackupType.INCREMENTAL:
            op_label = "日志备份(BACKUP LOG)"
        elif bt == BackupType.DIFFERENTIAL:
            op_label = "差异备份(WITH DIFFERENTIAL)"
        else:
            op_label = "完整备份(BACKUP DATABASE)"
        msg = (f"SQL Server {op_label}成功: [{db_name}] → "
               f"{os.path.basename(local_path)} ({db.human_size(size)})，"
               f"经 SSH 自 {hk} 拉回")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=local_path, size_bytes=size,
            duration_sec=round(time.time() - start, 3),
            stdout=out, stderr=err, simulated=False,
            checksum=checksum, message=msg)

    # ------------------------------------------------------------------
    # 恢复（SFTP 推送 → RESTORE FILELISTONLY → RESTORE DATABASE WITH MOVE/REPLACE）
    # ------------------------------------------------------------------
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message="SQL Server 恢复需要 SSH 通道推送 .bak 到服务器"
                        "（请纳管主机或在任务中配置 SSH 凭据）")
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message=f"备份文件不存在: {backup_path}")

        target_db = (kwargs.get("target_db") or self.task.get("db_name") or "").strip()
        if not target_db:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message="恢复必须指定目标库名")

        win = self._is_win(ssh_host)
        sep = "\\" if win else "/"
        try:
            bdir = self._backup_dir(ssh_host)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                backup_path=backup_path, simulated=False,
                                message=f"解析备份目录失败: {e}")

        fname = os.path.basename(backup_path)
        disk_path = f"{bdir}{sep}{fname}"

        # 1) SFTP 推送 .bak 到服务器备份目录
        try:
            client = remote_dump._connect(ssh_host)
            sftp = client.open_sftp()
            try:
                remote_ref = disk_path.replace("\\", "/") if win else disk_path
                sftp.put(backup_path, remote_ref)
            finally:
                sftp.close()
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                message=f"推送备份文件到服务器失败: {e}")

        # 2) RESTORE FILELISTONLY 获取逻辑文件名（官方推荐，配合 WITH MOVE）
        moves = ""
        try:
            rc, out, _e = self._exec_tsql(
                f"RESTORE FILELISTONLY FROM DISK = N'{disk_path}'",
                ssh_host=ssh_host, timeout=300)
            if rc == 0:
                moves = self._build_move_clauses(out, target_db, win, ssh_host)
        except Exception:
            moves = ""  # 解析失败时不阻塞：同实例恢复通常无需 MOVE

        # 3) 官方还原：RESTORE DATABASE ... WITH MOVE, REPLACE, RECOVERY
        tsql = (f"RESTORE DATABASE [{target_db}] FROM DISK = N'{disk_path}' "
                f"{moves}WITH REPLACE, RECOVERY")
        start = time.time()
        rc, out, err = self._exec_tsql(tsql, ssh_host=ssh_host)
        if rc != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, simulated=False,
                stdout=out, stderr=err,
                message=f"RESTORE 失败(rc={rc}): {(err or out)[:500]}")

        # 4) 清理服务器端临时 .bak（best-effort）
        self._cleanup_remote_file(ssh_host, disk_path, win)

        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=backup_path,
            duration_sec=round(time.time() - start, 3),
            stdout=out, simulated=False,
            message=f"SQL Server 恢复成功: [{target_db}] FROM DISK=N'{fname}' "
                    f"(WITH REPLACE, RECOVERY)")

    def _cleanup_remote_file(self, ssh_host: dict, disk_path: str, win: bool):
        try:
            from core.engines.file import _ssh_exec_pipe
            from core import remote_dump
            client = remote_dump._connect(ssh_host)
            try:
                if win:
                    cleanup = (f"powershell -NoProfile -Command "
                               f"\"Remove-Item -Force '{disk_path}' "
                               f"-ErrorAction SilentlyContinue\"")
                else:
                    cleanup = f"rm -f {_sh(disk_path)}"
                _ssh_exec_pipe(client, remote_dump._wrap_login(cleanup), timeout=60)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception:
            pass

    def _build_move_clauses(self, filelistonly_out: str, target_db: str,
                            win: bool, ssh_host: dict) -> str:
        """解析 RESTORE FILELISTONLY 输出，构造 WITH MOVE 子句。"""
        try:
            data_dir = self._instance_path(ssh_host, "InstanceDefaultDataPath")
            log_dir = self._instance_path(ssh_host, "InstanceDefaultLogPath")
        except Exception:
            return ""
        if not data_dir:
            return ""
        sep = "\\" if win else "/"
        moves = []
        for line in (filelistonly_out or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            logical, ftype = parts[0], (parts[2] or "").upper()[:1]
            if not logical or logical in ("LogicalName",) or logical.startswith("-"):
                continue
            if ftype not in ("D", "L"):
                continue
            ext = ".mdf" if ftype == "D" else ".ldf"
            dest_dir = data_dir if ftype == "D" else (log_dir or data_dir)
            fname = f"{target_db}_{logical.replace(' ', '_')}{ext}"
            dest = f"{dest_dir}{sep}{fname}"
            moves.append(f"MOVE N'{logical}' TO N'{dest}',")
        return " ".join(moves) + " " if moves else ""

    def _instance_path(self, ssh_host: dict, prop: str) -> str:
        """查询 SERVERPROPERTY 路径属性，过滤 sqlcmd 表头/分隔线/行数脚注。"""
        rc, out, _e = self._exec_tsql(
            f"SELECT CAST(SERVERPROPERTY('{prop}') AS nvarchar(260));",
            ssh_host=ssh_host, timeout=60)
        if rc != 0:
            return ""
        for ln in (out or "").splitlines():
            s = ln.strip()
            if not s or s.lower() == "null":
                continue
            if s.startswith("(") or set(s) <= {"-"} or s.lower() == prop.lower():
                continue
            return s.rstrip("/\\")
        return ""

    # ------------------------------------------------------------------
    # 库清单 / 校验
    # ------------------------------------------------------------------
    def list_databases(self) -> list:
        """列出业务库（database_id>4 即排除 master/tempdb/model/msdb，且 state=0）。"""
        try:
            rc, out, _e = self._exec_tsql(
                "SELECT name FROM sys.databases WHERE database_id > 4 "
                "AND state = 0 ORDER BY name;", timeout=60)
        except Exception:
            return []
        if rc != 0:
            return []
        # 过滤 sqlcmd 表头/分隔线/行数脚注
        skip_prefixes = ("(", "-",)
        return [ln.strip() for ln in (out or "").splitlines()
                if ln.strip() and ln.strip() not in _SYSTEM_DBS
                and not ln.strip().startswith(skip_prefixes)
                and ln.strip() != "name"]

    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """恢复校验：文件存在/SHA256 + 官方 RESTORE VERIFYONLY WITH CHECKSUM。"""
        options = options or {}
        backup_path = (record.get("backup_path") or record.get("output_path") or "")
        if not backup_path or not os.path.exists(backup_path):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"备份文件不存在: {backup_path}")
        checksum = record.get("checksum") or ""
        if checksum.startswith("sha256:"):
            import hashlib
            h = hashlib.sha256()
            with open(backup_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != checksum.split(":", 1)[1]:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="SHA256 校验不一致，备份文件已损坏")

        # RESTORE VERIFYONLY（官方校验语法）：需把产物放回服务器备份目录
        from core import remote_dump
        ssh_host = remote_dump.resolve_ssh_host(self.task)
        if not ssh_host:
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                message="文件存在且 SHA256 校验通过"
                                        "（未配置 SSH，跳过 RESTORE VERIFYONLY）")
        win = self._is_win(ssh_host)
        sep = "\\" if win else "/"
        try:
            bdir = self._backup_dir(ssh_host)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"解析备份目录失败: {e}")
        fname = os.path.basename(backup_path)
        disk_path = f"{bdir}{sep}{fname}"
        client = remote_dump._connect(ssh_host)
        sftp = client.open_sftp()
        try:
            remote_ref = disk_path.replace("\\", "/") if win else disk_path
            sftp.put(backup_path, remote_ref)
        finally:
            sftp.close()
            try:
                client.close()
            except Exception:
                pass
        rc, out, err = self._exec_tsql(
            f"RESTORE VERIFYONLY FROM DISK = N'{disk_path}' WITH CHECKSUM;",
            ssh_host=ssh_host, timeout=1800)
        self._cleanup_remote_file(ssh_host, disk_path, win)
        if rc != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message=f"RESTORE VERIFYONLY 失败(rc={rc}): {(err or out)[:400]}")
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            message="SHA256 校验通过；服务器端 RESTORE VERIFYONLY "
                                    "WITH CHECKSUM 通过（备份可恢复）")
