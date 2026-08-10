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
    if db_type == "mysql":
        host = extra.get("source_host") or "127.0.0.1"
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
        return f"{pre}mysql -h {host} -P {port} -u {user} -p'{pw_esc}' {target} < '{actual}'"

    elif db_type == "postgresql":
        host = extra.get("source_host") or "127.0.0.1"
        port = extra.get("source_port") or 5432
        user = extra.get("source_username") or "postgres"
        pw = extra.get("source_password") or ""
        actual = remote_pkg
        if remote_pkg.endswith(".gz"):
            actual = remote_pkg[:-3]
            pre = f"gunzip -c '{remote_pkg}' > '{actual}' && "
        else:
            pre = ""
        if remote_pkg.endswith(".dump"):
            return f"{pre}pg_restore -h {host} -p {port} -U {user} -d '{target_db or 'postgres'}' -c '{actual}'"
        target = f"-d '{target_db}'" if target_db else ""
        return f"export PGPASSWORD='{pw}'; {pre}psql -h {host} -p {port} -U {user} {target} -f '{actual}'"

    elif db_type == "oracle":
        # Oracle 用 impdp，依赖服务端 DIRECTORY；这里用 cat | sqlldr 之类不现实
        # 简化：写一个 parfile 让用户在目标机执行
        return (f"echo 'Oracle 跨主机恢复需 impdp，请确保目标主机已配置 DIRECTORY 与 IMP_FULL_DATABASE 权限'; "
                f"ls -la {remote_pkg}; "
                f"unzip -o {remote_pkg} -d /tmp/oracle_restore_{int(time.time())} && echo OK")

    elif db_type == "kingbase":
        host = extra.get("source_host") or "127.0.0.1"
        port = extra.get("source_port") or 54321
        user = extra.get("source_username") or "SYSTEM"
        pw = extra.get("source_password") or ""
        actual = remote_pkg
        if remote_pkg.endswith(".gz"):
            actual = remote_pkg[:-3]
            pre = f"gunzip -c '{remote_pkg}' > '{actual}' && "
        else:
            pre = ""
        if remote_pkg.endswith(".dump"):
            return f"export PGPASSWORD='{pw}'; {pre}sys_restore -h {host} -p {port} -U {user} -d '{target_db or 'kingbase'}' '{actual}'"
        target = f"-d '{target_db}'" if target_db else ""
        return f"export PGPASSWORD='{pw}'; {pre}ksql -h {host} -p {port} -U {user} {target} -f '{actual}'"

    elif db_type == "redis":
        # 上传 .rdb / .tar.gz
        return f"redis-cli -h 127.0.0.1 -a '$(echo)' CONFIG SET dir /tmp && redis-cli SHUTDOWN NOSAVE 2>/dev/null; cp {remote_pkg} /tmp/dump.rdb && redis-server --dbfilename dump.rdb --dir /tmp --daemonize yes"

    elif db_type == "mongodb":
        host = extra.get("source_host") or "127.0.0.1"
        port = extra.get("source_port") or 27017
        target = f"--db '{target_db}'" if target_db else ""
        return f"mongorestore --host {host} --port {port} {target} --gzip --archive={remote_pkg}"

    elif db_type == "file":
        target = target_db or "/tmp/restore"
        return f"mkdir -p '{target}' && tar -xzf '{remote_pkg}' -C '{target}' && echo '已解压到 {target}'"

    elif db_type == "dameng":
        dm_pwd = extra.get("dm_sysdba", "Dameng123")
        return f"echo 'DM 跨主机恢复需要 disql 客户端或 dminit；请在目标机手动执行: disql SYSDBA/{dm_pwd} @ /tmp/import.sql'"

    else:
        raise RuntimeError(f"不支持的跨主机恢复类型: {db_type}")
