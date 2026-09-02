# -*- coding: utf-8 -*-
"""
跨主机恢复辅助：将备份文件 SFTP 推送到目标主机，SSH 远程执行恢复命令。

支持的恢复类型：
- mysql    : mysql / mariadb 客户端
- postgresql: psql / pg_restore
- oracle   : impdp（服务端工具，需要 DIRECTORY）
- redis    : redis-server（加载 .rdb 文件并启动）
- mongodb  : mongorestore
- file     : tar 解压到指定目录
- kingbase : ksql / sys_restore
- dameng   : dimp 导入

通用流程：
1. SFTP 上传 backup_path 到目标主机的 /tmp/bk_restore_<ts>.<ext>
2. SSH 在目标主机上调用对应恢复命令
3. 流式捕获执行日志
4. 返回结果
"""
import os
import time
import paramiko


def _build_ssh(target_host_info: dict):
    """构造到目标主机的 SSH 客户端（不复用主连接池，因为恢复是一次性操作）。"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect = dict(
        hostname=target_host_info.get("hostname") or "",
        port=int(target_host_info.get("port") or 22),
        username=target_host_info.get("username") or "root",
        timeout=30,
        allow_agent=False, look_for_keys=False,
    )
    pw = target_host_info.get("password") or ""
    auth = target_host_info.get("auth_type") or "password"
    if auth == "key" and target_host_info.get("private_key"):
        import io
        try:
            key = paramiko.RSAKey.from_private_key(io.StringIO(target_host_info["private_key"]))
        except Exception:
            key = paramiko.Ed25519Key.from_private_key(io.StringIO(target_host_info["private_key"]))
        connect["pkey"] = key
    else:
        connect["password"] = pw
    client.connect(**connect)
    return client


def _sftp_upload(client, local_path: str, remote_path: str, log) -> int:
    """SFTP 上传文件并返回远端文件大小。"""
    sftp = client.open_sftp()
    try:
        sftp.put(local_path, remote_path)
        sftp.chmod(remote_path, 0o644)
        st = sftp.stat(remote_path)
        return st.st_size
    finally:
        sftp.close()


def _remote_exec_logged(client, cmd: str, timeout: int = 3600, log=None):
    """在远程执行命令并逐行回调 log(line)，返回 (out, err, rc)。"""
    log = log or (lambda x: None)
    t = client.get_transport()
    sess = t.open_session()
    sess.exec_command(cmd)
    out, err = b"", b""
    start = time.time()
    last_beat = start
    while not sess.exit_status_ready():
        if time.time() - start > timeout:
            sess.close()
            raise RuntimeError(f"远程命令超时({timeout}s)")
        if time.time() - last_beat >= 30:
            log(f"…心跳: 已执行 {int(time.time()-start)}s")
            last_beat = time.time()
        if sess.recv_ready():
            out += sess.recv(65536)
        elif sess.recv_stderr_ready():
            err += sess.recv_stderr(4096)
        else:
            time.sleep(0.05)
    while sess.recv_ready():
        out += sess.recv(65536)
    while sess.recv_stderr_ready():
        err += sess.recv_stderr(4096)
    rc = sess.recv_exit_status()
    return out, err, rc


def _is_real_error(s: str) -> bool:
    up = s.upper()
    if "[ERROR]" in up or up.startswith("ERROR "):
        return True
    if "FATAL" in up and "ERROR" in up:
        return True
    if " [ERROR] " in s and "[MY-" in s:
        return True
    return False


def cross_host_restore(db_type: str, backup_path: str, target_host_info: dict,
                        target_db: str = "", extra: dict = None, log=None) -> dict:
    """跨主机恢复主入口。
    返回 {ok, message, target_path}。
    """
    extra = extra or {}
    log = log or (lambda x: None)
    if not os.path.isfile(backup_path):
        return {"ok": False, "message": f"备份文件不存在: {backup_path}"}

    log(f"跨主机恢复: {db_type} -> {target_host_info.get('hostname')}")
    client = _build_ssh(target_host_info)
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"bk_restore_{ts}_{os.path.basename(backup_path)}"
        remote = f"/tmp/{fname}"
        local_size = os.path.getsize(backup_path)
        log(f"SFTP 上传: {backup_path}({local_size} bytes) -> {remote}")
        up_size = _sftp_upload(client, backup_path, remote, log)
        log(f"上传完成: 远端 {up_size} bytes")

        # 构造恢复命令
        # PostgreSQL/Kingbase 的工具可能不在 PATH（旧版 /usr/bin/psql 无法
        # 完成 SCRAM 认证），需从运行中进程解析正确版本，注入命令构造。
        if db_type == "mysql":
            # MySQL 8.4+ 移除了 RESET MASTER，需根据目标实例版本选择重置语句
            from core.remote_dump import _resolve_remote_bin
            extra = dict(extra)
            mysql_bin = _resolve_remote_bin(client, "mysql") or "mysql"
            mhost, mport = "127.0.0.1", extra.get("source_port") or 3306
            muser = extra.get("source_username") or "root"
            mpw = extra.get("source_password") or ""
            ver_cmd = (f"{mysql_bin} -h {mhost} -P {mport} -u {muser} "
                       f"-p'{mpw}' -N -e 'SELECT VERSION();' 2>/dev/null")
            _o, _e, _rc = _remote_exec_logged(client, ver_cmd, timeout=60, log=lambda x: None)
            _ver = (_o or b"").decode("utf-8", "replace").strip()
            _parts = _ver.split(".")
            _maj = int(_parts[0]) if _parts and _parts[0].isdigit() else 0
            _min = int(_parts[1]) if len(_parts) > 1 and _parts[1].isdigit() else 0
            if _maj > 8 or (_maj == 8 and _min >= 4):
                extra["_mysql_reset_sql"] = "RESET BINARY LOGS AND GTIDS"
            else:
                extra["_mysql_reset_sql"] = "RESET MASTER"
        if db_type == "postgresql":
            from core.remote_dump import _resolve_remote_bin
            extra = dict(extra)
            extra["_pg_psql_bin"] = _resolve_remote_bin(client, "psql") or "psql"
            extra["_pg_restore_bin"] = _resolve_remote_bin(client, "pg_restore") or "pg_restore"
        if db_type == "kingbase":
            # 金仓客户端工具通常不在 root PATH（Server/ClientTools 专用目录），
            # 必须动态解析绝对路径，否则 rc=127 command not found
            from core.remote_dump import _resolve_remote_bin
            extra = dict(extra)
            extra["_kb_restore_bin"] = _resolve_remote_bin(client, "sys_restore") or "sys_restore"
            extra["_kb_ksql_bin"] = _resolve_remote_bin(client, "ksql") or "ksql"
        cmd = _build_restore_cmd(db_type, remote, target_db, extra, target_host_info)
        log(f"远程执行: {cmd[:200]}")
        out, err, rc = _remote_exec_logged(client, cmd, timeout=7200, log=log)
        out_s = out.decode("utf-8", "replace")
        err_s = err.decode("utf-8", "replace")
        for line in (out_s + err_s).splitlines():
            s = line.strip()
            if not s:
                continue
            log(f"REMOTE: {s}")
        ok = rc == 0
        msg = f"恢复{'成功' if ok else '失败'}(rc={rc})"
        return {"ok": ok, "message": msg, "rc": rc,
                "remote_path": remote, "stdout_tail": out_s[-2000:],
                "stderr_tail": err_s[-2000:]}
    except Exception as e:
        log(f"跨主机恢复异常: {e}")
        return {"ok": False, "message": f"跨主机恢复异常: {e}"}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _build_restore_cmd(db_type: str, remote_pkg: str, target_db: str,
                       extra: dict, target_host_info: dict) -> str:
    """构造各 DB 跨主机恢复命令（目标主机上执行）。"""
    base = extra.get("base_dir") or ""
    # 跨主机恢复统一在目标主机本机执行（127.0.0.1），而不是连接源库地址。
    # 端口默认取源任务端口（假设目标机运行同端口实例），可由 target_port 覆盖。
    if db_type == "mysql":
        host = "127.0.0.1"
        port = extra.get("source_port") or 3306
        user = extra.get("source_username") or "root"
        pw = extra.get("source_password") or ""
        pw_esc = pw.replace("'", "'\\''")
        # 先解压 .gz（如果是 .gz）
        actual = remote_pkg
        if remote_pkg.endswith(".gz"):
            actual = remote_pkg[:-3]
            pre = f"gunzip -c '{remote_pkg}' > '{actual}' && "
        else:
            pre = ""
        target = f"'{target_db}'" if target_db else ""
        # 目标库不存在时自动创建：单库 dump 不含 CREATE DATABASE，
        # 否则导入报 ERROR 1049 Unknown database（全实例路径已有自动建库，
        # 单库跨主机路径此前缺失）。
        create_part = ""
        if target_db:
            safe_db = str(target_db).replace("`", "")
            create_part = (
                f"mysql -h {host} -P {port} -u {user} -p'{pw_esc}' "
                f"-e 'CREATE DATABASE IF NOT EXISTS `{safe_db}`' && "
            )
        # 恢复前清空 GTID，避免含 GTID_PURGED 的备份导入时报 1840
        reset_sql = extra.get("_mysql_reset_sql") or "RESET MASTER"
        return (
            f"{pre}{create_part}"
            f"mysql -h {host} -P {port} -u {user} -p'{pw_esc}' -e '{reset_sql}' && "
            f"mysql -h {host} -P {port} -u {user} -p'{pw_esc}' {target} < '{actual}'"
        )

    elif db_type == "postgresql":
        host = "127.0.0.1"
        port = extra.get("source_port") or 5432
        user = extra.get("source_username") or "postgres"
        pw = extra.get("source_password") or ""
        # 优先使用从运行中进程解析出的正确版本工具（避免 PATH 中旧版
        # /usr/bin/psql 因 SCRAM 认证失败）
        psql_bin = extra.get("_pg_psql_bin") or "psql"
        pg_restore_bin = extra.get("_pg_restore_bin") or "pg_restore"
        actual = remote_pkg
        if remote_pkg.endswith(".gz"):
            actual = remote_pkg[:-3]
            pre = f"gunzip -c '{remote_pkg}' > '{actual}' && "
        else:
            pre = ""
        if remote_pkg.endswith(".dump"):
            return (f"export PGPASSWORD='{pw}'; {pre}{pg_restore_bin} "
                    f"-h {host} -p {port} -U {user} -d '{target_db or 'postgres'}' -c '{actual}'")
        target = f"-d '{target_db}'" if target_db else ""
        return (f"export PGPASSWORD='{pw}'; {pre}{psql_bin} "
                f"-h {host} -p {port} -U {user} {target} -f '{actual}'")

    elif db_type == "oracle":
        # Oracle 真实 impdp 恢复：动态解析 impdp 与 DATA_PUMP_DIR（不写死路径），
        # dmp 是 Data Pump 逻辑备份时直接导入；RMAN 物理备份片(.bkp)无法用 impdp，
        # 如实报错（不伪造成功）。
        if not remote_pkg.endswith((".dmp", ".dmp.gz")):
            return (f"echo '[ERROR] RMAN 物理备份片(.bkp)请使用 RMAN 恢复通道"
                    f"（RESTORE/RECOVER），impdp 不适用'; exit 1;")
        svc = extra.get("source_db") or target_db or "orcl"
        port = extra.get("source_port") or 1521
        user = extra.get("source_username") or "system"
        pw = extra.get("source_password") or ""
        conn = f"{user}/{pw}@//127.0.0.1:{port}/{svc}"
        script = f"""#!/bin/bash
set -u
IMPDP=$(su - oracle -c 'command -v impdp' 2>/dev/null | head -1)
if [ -z "$IMPDP" ]; then
  IMPDP=$(find /u01/app/oracle/product/*/*/bin /u01/app/oracle/*/bin -maxdepth 1 -name impdp -type f 2>/dev/null | head -1)
fi
if [ -z "$IMPDP" ]; then
  echo "[ERROR] 目标主机未找到 impdp（oracle 用户 PATH 与常见 ORACLE_HOME 目录均无）"
  exit 127
fi
echo "IMPDP=$IMPDP"
DP=$(su - oracle -c "sqlplus -s / as sysdba" <<'EOSQL' 2>/dev/null | grep '^/' | head -1
SET HEADING OFF
SET PAGESIZE 0
SET FEEDBACK OFF
SELECT directory_path FROM all_directories WHERE directory_name = 'DATA_PUMP_DIR';
EXIT;
EOSQL
)
if [ -z "$DP" ]; then
  echo "[ERROR] 无法在目标主机解析 DATA_PUMP_DIR 实际路径"
  exit 1
fi
echo "DP=$DP"
cp '{remote_pkg}' "$DP/platform_restore.dmp" || exit 1
chown oracle:oinstall "$DP/platform_restore.dmp" 2>/dev/null
su - oracle -c "$IMPDP '{conn}' DIRECTORY=DATA_PUMP_DIR DUMPFILE=platform_restore.dmp LOGFILE=platform_restore.log TABLE_EXISTS_ACTION=REPLACE"
RC=$?
tail -30 "$DP/platform_restore.log" 2>/dev/null
exit $RC
"""
        import base64
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return f"echo {b64} | base64 -d | bash"

    elif db_type == "kingbase":
        host = "127.0.0.1"
        port = extra.get("source_port") or 54321
        user = extra.get("source_username") or "SYSTEM"
        pw = extra.get("source_password") or ""
        pw_esc = pw.replace("'", "'\\''")
        # V8 兼容 PGPASSWORD；V9 起读 KINGBASE_PASSWORD，两个都导出
        env = f"export PGPASSWORD='{pw_esc}'; export KINGBASE_PASSWORD='{pw_esc}';"
        restore_bin = extra.get("_kb_restore_bin") or "sys_restore"
        ksql_bin = extra.get("_kb_ksql_bin") or "ksql"
        actual = remote_pkg
        if remote_pkg.endswith(".gz"):
            actual = remote_pkg[:-3]
            pre = f"gunzip -c '{remote_pkg}' > '{actual}' && "
        else:
            pre = ""
        if remote_pkg.endswith(".dump"):
            return f"{env} {pre}{restore_bin} -h {host} -p {port} -U {user} -d '{target_db or 'kingbase'}' '{actual}'"
        target = f"-d '{target_db}'" if target_db else ""
        return f"{env} {pre}{ksql_bin} -h {host} -p {port} -U {user} {target} -f '{actual}'"

    elif db_type == "redis":
        # 上传 .rdb / .tar.gz
        return f"redis-cli -h 127.0.0.1 -a '$(echo)' CONFIG SET dir /tmp && redis-cli SHUTDOWN NOSAVE 2>/dev/null; cp {remote_pkg} /tmp/dump.rdb && redis-server --dbfilename dump.rdb --dir /tmp --daemonize yes"

    elif db_type == "mongodb":
        host = "127.0.0.1"
        port = extra.get("source_port") or 27017
        target = f"--db '{target_db}'" if target_db else ""
        return f"mongorestore --host {host} --port {port} {target} --gzip --archive={remote_pkg}"

    elif db_type == "file":
        target = target_db or "/tmp/restore"
        return f"mkdir -p '{target}' && tar -xzf '{remote_pkg}' -C '{target}' && echo '已解压到 {target}'"

    elif db_type == "dameng":
        # DM 真实 dimp 恢复：动态解析 dimp（dmdba 用户 profile → 常见安装目录），
        # 找不到时如实报错（不伪造成功）。
        # SYSDBA 密码：优先 extra_options.dm_sysdba，其次复用任务连接密码
        dm_pwd = (extra.get("dm_sysdba") or extra.get("source_password")
                  or "Dameng123")
        # 密码含特殊字符（@ 等）时需双引号包裹，否则达梦连接串被截断
        if dm_pwd and not dm_pwd.isalnum():
            dm_pwd = '"{0}"'.format(dm_pwd)
        dm_port = extra.get("source_port") or 5236
        script = f"""#!/bin/bash
set -u
DIMP=$(su - dmdba -c 'command -v dimp' 2>/dev/null | head -1)
if [ -z "$DIMP" ]; then
  DIMP=$(find /dm8/bin /opt/dmdbms/bin /home/dmdba/dmdbms/bin /opt/dm*/bin /dm*/bin -maxdepth 2 -name dimp -type f 2>/dev/null | head -1)
fi
if [ -z "$DIMP" ]; then
  echo "[ERROR] 目标主机未找到 dimp（dmdba 用户 PATH 与常见安装目录均无）"
  exit 127
fi
echo "DIMP=$DIMP"
DM_FDIR=$(dirname '{remote_pkg}')
DM_FBASE=$(basename '{remote_pkg}')
"$DIMP" SYSDBA/'{dm_pwd}'@localhost:{dm_port} FILE="$DM_FBASE" DIRECTORY="$DM_FDIR" LOG=/tmp/dm_restore_{int(time.time())}.log
exit $?
"""
        import base64
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return f"echo {b64} | base64 -d | bash"

    else:
        raise RuntimeError(f"不支持的跨主机恢复类型: {db_type}")
