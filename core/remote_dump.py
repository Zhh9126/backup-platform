# -*- coding: utf-8 -*-
"""
通过 SSH 在数据库服务器上执行原生 dump/restore —— 实现「无 Agent」的真实备份。

背景与必要性：
- 备份平台所在服务器未必安装 mysqldump / pg_dump 等客户端；
- 但被备份的数据库服务器自身必然带有这些客户端（要备份的库上面肯定有相关命令）。

因此当本地缺少客户端时，自动改由 SSH 登录到数据库服务器，在其“本机”执行
dump（连接 127.0.0.1），再把数据流（字节）回传到备份服务器落盘。整个过程：
- 不需要在备份服务器安装任何数据库客户端；
- 不需要在被备份机器部署任何 Agent；
- 数据库账号密码仅用于数据库本身，SSH 走主机纳管中已加密存储的凭据。
"""
import os
import io
import json
import shlex
import tempfile

import core.db as db
from core import ssh_hosts


def resolve_ssh_host(task: dict):
    """解析任务的 SSH 主机（用于远程执行 dump）。

    解析优先级：
    1) extra_options.ssh_host_id 显式指定（数据库任务表单下拉框写入）
    2) 按任务 host（数据库地址）匹配 ssh_hosts 的 hostname 或 host_key
    3) 无匹配则返回 None（调用方将退化为仿真占位）
    """
    extra = {}
    raw = task.get("extra_options")
    if isinstance(raw, dict):
        extra = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            extra = json.loads(raw)
        except Exception:
            extra = {}

    # 1) 显式指定
    hid = extra.get("ssh_host_id") or task.get("ssh_host_id")
    if hid:
        try:
            h = ssh_hosts.get_host(int(hid), include_secret=True)
            if h:
                return h
        except (ValueError, TypeError):
            pass

    # 2) 按数据库地址 IP 匹配
    host = (task.get("host") or "").strip()
    if host:
        for h in ssh_hosts.list_hosts(include_secret=True):
            if h.get("hostname") == host:
                return h
            hk = h.get("host_key", "")
            # host_key 形如 user@1.2.3.4:22
            if "@" in hk:
                ip = hk.rsplit("@", 1)[-1].split(":")[0]
                if ip == host:
                    return h
    return None


def _connect(ssh_host: dict):
    from core.engines.file import _get_ssh_client
    return _get_ssh_client(ssh_host["host_key"])


def _wrap_login(shell_cmd: str) -> str:
    """将远程 shell 命令用 `bash -lc` 包裹。

    必要性：paramiko 的 exec_command 默认使用非交互、非登录 shell，
    **不加载** /etc/profile、~/.bash_profile 等。多数 Linux 把 mysqldump
    / xtrabackup / go 等放在 /usr/local/* 下，仅在 /etc/profile 中追加到
    PATH。直接执行会得到 rc=127 (command not found)。

    用 `bash -lc '...'` 强制作为 login shell 启动，加载 /etc/profile 后
    再执行命令。注意：必须把 shell_cmd 包成单引号字符串传入。
    """
    # 用单引号包，并用 sed 把命令里的单引号转义（'\'' 方式）
    escaped = shell_cmd.replace("'", "'\\''")
    return f"bash -lc '{escaped}'"


def _resolve_remote_bin(client, tool: str) -> str | None:
    """在远端 SSH 上检测指定工具的实际路径。

    返回绝对路径；若找不到返回 None。

    探测顺序：
    1) bash -lc 'command -v <tool>'   （加载 /etc/profile 后查 PATH）
    2) find 在常见位置查找
       MySQL/MariaDB: /usr/local/mysql*/bin、/usr/local/mariadb*/bin、/opt/mysql*/bin
       PostgreSQL:    /usr/pgsql-*/bin、/usr/lib/postgresql/*/bin
       通用:           /usr/bin /usr/local/bin /opt
    """
    common_dirs = (
        "/usr/bin /usr/sbin /usr/local/bin /usr/local/sbin /opt "
        "/usr/local/mysql/bin /usr/local/mariadb/bin "
        "/usr/pgsql-15/bin /usr/pgsql-14/bin /usr/pgsql-13/bin "
        "/usr/lib/postgresql/15/bin /usr/lib/postgresql/14/bin"
    )
    # 单条命令：先 command -v，再用 find 兜底
    cmd = (
        f"command -v {shlex.quote(tool)} 2>/dev/null || "
        f"find {common_dirs} -maxdepth 4 -name {shlex.quote(tool)} -type f 2>/dev/null | head -1"
    )
    shell = _wrap_login(cmd)
    try:
        from core.engines.file import _ssh_exec_pipe
        out, err, rc = _ssh_exec_pipe(client, shell, timeout=30)
    except Exception:
        return None
    if rc != 0:
        return None
    # out 是 bytes，需要 decode
    try:
        text = out.decode("utf-8", errors="replace")
    except Exception:
        return None
    candidates = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not candidates:
        return None
    # command -v 可能输出 "alias xxx='...'" 之类的非路径；这里只接受绝对路径
    for c in candidates:
        if c.startswith("/"):
            return c
    return None


# ----------------------------- 远程 DUMP -----------------------------

def _remote_mysql_dump(task: dict, ssh_host: dict, compress: int) -> bytes:
    """在远端数据库服务器以 mysqldump 导出，返回原始字节（可选 gzip 压缩）。

    支持四种备份范围（按 extra_options / task.db_name 自动判定）：
    1) extra.schemas 非空  → --databases schema1 schema2 ...
    2) extra.tables 非空   → --databases <db_name> table1 table2 ...
    3) task.db_name 非空   → --databases <db_name>
    4) 上述都为空          → --all-databases（全实例）

    关键修复（paramiko PATH 问题）：
    1) 先用 _resolve_remote_bin 探测 mysqldump 真实路径（绕开非交互 shell 的 PATH 缺失）
    2) shell 命令用 _wrap_login 包裹（即 `bash -lc '...'`），强制加载 /etc/profile
    """
    client = _connect(ssh_host)
    sftp = client.open_sftp()
    cnf_local = tempfile.mktemp(suffix=".cnf")
    user = task.get("username") or "root"
    pw = db.decrypt_secret(task.get("password") or "")
    # 用二进制写，避免 Windows \r\n 被传到远端导致 mysql 客户端解析失败
    with open(cnf_local, "wb") as f:
        f.write(f"[client]\nuser={user}\npassword={pw}\n".encode("utf-8"))
    remote_cnf = "/tmp/bk_rdump.cnf"
    try:
        sftp.put(cnf_local, remote_cnf)
        try:
            sftp.chmod(remote_cnf, 0o600)
        except Exception:
            pass
    finally:
        os.remove(cnf_local)

    # 1) 探测 mysqldump 真实路径
    mysqldump_bin = _resolve_remote_bin(client, "mysqldump")
    if not mysqldump_bin:
        raise RuntimeError(
            "远端主机未找到 mysqldump（PATH 与 /usr/local/mysql*/bin、/usr/local/mariadb*/bin、/opt 均无）。"
            "请在远端安装 mysql-client 或 xtrabackup 后重试。"
        )

    # 2) 解析备份范围
    db_name = task.get("db_name") or ""
    port = task.get("port") or 3306
    # 解析 extra_options（兼容 str/dict）
    extra = {}
    raw_eo = task.get("extra_options")
    if isinstance(raw_eo, dict):
        extra = raw_eo
    elif isinstance(raw_eo, str) and raw_eo.strip():
        try:
            extra = json.loads(raw_eo)
        except Exception:
            extra = {}
    tables = [str(t).strip() for t in (extra.get("tables") or []) if str(t).strip()]
    schemas = [str(s).strip() for s in (extra.get("schemas") or []) if str(s).strip()]
    schema_only = bool(extra.get("schema_only"))
    data_only = bool(extra.get("data_only"))
    # 默认开 single-transaction / routines / triggers / events，可通过 extra 显式关
    use_st = extra.get("single_transaction") is not False
    use_routines = extra.get("routines") is not False
    use_triggers = extra.get("triggers") is not False
    use_events = extra.get("events") is not False

    # 3) 组装 mysqldump 参数
    args = [
        mysqldump_bin, f"--defaults-extra-file={remote_cnf}",
        "-h", "127.0.0.1", "-P", str(port),
    ]
    if use_st: args.append("--single-transaction")
    if use_routines: args.append("--routines")
    if use_triggers: args.append("--triggers")
    if use_events: args.append("--events")
    args.append("--default-character-set=utf8mb4")

    if tables:
        # 指定表：mysqldump <db> t1 t2 ...
        if not db_name:
            raise RuntimeError("指定表（tables）时必须同时填写 task.db_name 库名")
        args.append(db_name)
        args.extend(tables)
    elif schemas:
        # 指定多库：--databases s1 s2 ...
        args.append("--databases")
        args.extend(schemas)
    elif db_name:
        # 单库：--databases <db>
        args.append("--databases")
        args.append(db_name)
    else:
        # 全实例：--all-databases
        args.append("--all-databases")

    if schema_only:
        args.append("--no-data")
    if data_only:
        args.append("--no-create-info")
    # 额外原样透传（power user 自行配置，比如 --set-gtid-purged=OFF）
    if extra.get("extra_args"):
        try:
            args.extend(shlex.split(str(extra["extra_args"])))
        except Exception:
            pass

    # 4) 包装 shell：set -o pipefail + bash -lc
    shell = "set -o pipefail; " + " ".join(shlex.quote(a) for a in args)
    if compress:
        shell += " | gzip -c"
    wrapped = _wrap_login(shell)
    try:
        from core.engines.file import _ssh_exec_pipe
        out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
        if rc != 0:
            raise RuntimeError(f"远程 mysqldump 失败(rc={rc}, bin={mysqldump_bin}): {err[:600]}")
        # 防御：mysqldump 异常时也可能产生极小的 .gz（gzip 头/尾各 10 字节 = 20 字节空文件）
        if compress and len(out) <= 20:
            raise RuntimeError(
                f"远程 mysqldump 疑似失败：gzip 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
            )
        return out
    finally:
        try:
            sftp.remove(remote_cnf)
        except Exception:
            pass
        sftp.close()


def _remote_pg_dump(task: dict, ssh_host: dict, compress: int) -> bytes:
    """在远端数据库服务器以 pg_dump 导出，返回原始字节（可选 gzip 压缩）。

    支持多模式：
    1) extra.schemas 非空  → -n s1 -n s2 ...（多 schema）
    2) extra.tables 非空   → -t tbl1 -t tbl2 ...（限定表）
    3) task.db_name 非空   → -d <db>
    4) 上述都为空          → --all-databases
    """
    client = _connect(ssh_host)
    # 探测 pg_dump 真实路径
    pgdump_bin = _resolve_remote_bin(client, "pg_dump")
    if not pgdump_bin:
        raise RuntimeError(
            "远端主机未找到 pg_dump（PATH 与 /usr/pgsql-*/bin、/usr/lib/postgresql/*/bin 均无）。"
            "请在远端安装 postgresql-client 后重试。"
        )
    user = task.get("username") or "postgres"
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""
    port = task.get("port") or 5432
    fmt = "-Fc" if compress else "-Fp"

    # 解析 extra_options
    extra = {}
    raw_eo = task.get("extra_options")
    if isinstance(raw_eo, dict):
        extra = raw_eo
    elif isinstance(raw_eo, str) and raw_eo.strip():
        try:
            extra = json.loads(raw_eo)
        except Exception:
            extra = {}
    schemas = [str(s).strip() for s in (extra.get("schemas") or []) if str(s).strip()]
    tables = [str(t).strip() for t in (extra.get("tables") or []) if str(t).strip()]

    # 基础 args
    base = (
        f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
        f"{pgdump_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} {fmt} -f -"
    )

    if tables:
        # 单 db + 多表
        if not db_name:
            raise RuntimeError("指定表（tables）时必须同时填写 task.db_name 库名")
        target_args = f"-d {shlex.quote(db_name)} " + " ".join(f"-t {shlex.quote(t)}" for t in tables)
    elif schemas:
        # 多 schema
        target_args = " ".join(f"-n {shlex.quote(s)}" for s in schemas)
    elif db_name:
        target_args = f"-d {shlex.quote(db_name)}"
    else:
        # 全实例
        target_args = "--all-databases"

    # 额外原样透传
    extra_args = ""
    if extra.get("extra_args"):
        try:
            shlex.split(str(extra["extra_args"]))
            extra_args = " " + str(extra["extra_args"])
        except Exception:
            pass

    shell = f"{base} {target_args}{extra_args}"
    if compress:
        shell += " | gzip -c"
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程 pg_dump 失败(rc={rc}, bin={pgdump_bin}): {err[:600]}")
    if compress and len(out) <= 20:
        raise RuntimeError(
            f"远程 pg_dump 疑似失败：gzip 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
        )
    return out


def remote_db_dump(task: dict, ssh_host: dict, db_type: str, compress: int) -> bytes:
    """统一入口：在数据库服务器本地执行 dump 并返回原始字节。"""
    if db_type == "mysql":
        return _remote_mysql_dump(task, ssh_host, compress)
    if db_type == "postgresql":
        return _remote_pg_dump(task, ssh_host, compress)
    raise RuntimeError(f"不支持的远程 dump 类型: {db_type}")


# ----------------------------- 远程 LIST DATABASES -----------------------------

def _remote_list_mysql_databases(task: dict, ssh_host: dict) -> list:
    """通过 SSH 在数据库服务器上跑 SHOW DATABASES，绕过本机无客户端的限制。"""
    client = _connect(ssh_host)
    sftp = client.open_sftp()
    cnf_local = tempfile.mktemp(suffix=".cnf")
    user = task.get("username") or "root"
    pw = db.decrypt_secret(task.get("password") or "")
    with open(cnf_local, "wb") as f:
        f.write(f"[client]\nuser={user}\npassword={pw}\n".encode("utf-8"))
    remote_cnf = "/tmp/bk_list.cnf"
    try:
        sftp.put(cnf_local, remote_cnf)
        try:
            sftp.chmod(remote_cnf, 0o600)
        except Exception:
            pass
    finally:
        os.remove(cnf_local)
    # 探测 mysql 客户端路径
    mysql_bin = _resolve_remote_bin(client, "mysql") or "mysql"
    port = task.get("port") or 3306
    shell = (
        f"set -o pipefail; {mysql_bin} --defaults-extra-file={remote_cnf} "
        f"-h 127.0.0.1 -P {port} -N -B -e 'SHOW DATABASES'"
    )
    wrapped = _wrap_login(shell)
    try:
        from core.engines.file import _ssh_exec_pipe
        out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=30)
        if rc != 0:
            raise RuntimeError(f"远程 SHOW DATABASES 失败: {err[:300]}")
        # 解析输出：每行一个库名
        dbs = []
        for line in out.splitlines():
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            dbs.append(line)
        return dbs
    finally:
        try:
            sftp.remove(remote_cnf)
        except Exception:
            pass
        sftp.close()


def _remote_list_pg_databases(task: dict, ssh_host: dict) -> list:
    """通过 SSH 在 PG/kingbase 上跑 SELECT datname FROM pg_database。"""
    client = _connect(ssh_host)
    user = task.get("username") or "postgres"
    pw = db.decrypt_secret(task.get("password") or "")
    pg_bin = _resolve_remote_bin(client, "psql") or "psql"
    port = task.get("port") or 5432
    shell = (
        f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
        f"{pg_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d postgres -tA -c "
        f'"SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"'
    )
    wrapped = _wrap_login(shell)
    try:
        from core.engines.file import _ssh_exec_pipe
        out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=30)
        if rc != 0:
            raise RuntimeError(f"远程 SELECT pg_database 失败: {err[:300]}")
        dbs = []
        for line in out.splitlines():
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            dbs.append(line)
        return dbs
    finally:
        # 没有 cnf 要清理
        pass


def remote_list_databases(task: dict, db_type: str) -> list:
    """统一入口：通过 SSH 列出数据库/Schema（绕过本机无客户端）。

    返回 list[str]，抛 RuntimeError 给清晰错误。
    """
    ssh_host = resolve_ssh_host(task)
    if not ssh_host:
        raise RuntimeError(
            "未找到匹配的 SSH 主机：无法直接连数据库服务器。请在『任务编辑 -> 高级选项』"
            "配置 SSH 主机（先到『文件备份』页纳管数据库服务器）。"
        )
    if db_type in ("mysql", "mariadb"):
        return _remote_list_mysql_databases(task, ssh_host)
    if db_type in ("postgresql", "kingbase"):
        return _remote_list_pg_databases(task, ssh_host)
    raise RuntimeError(f"暂不支持为 {db_type} 拉取库/schema 列表")


# ----------------------------- 远程 RESTORE -----------------------------

def _remote_mysql_restore(task: dict, ssh_host: dict, dump_bytes: bytes) -> None:
    client = _connect(ssh_host)
    sftp = client.open_sftp()
    cnf_local = tempfile.mktemp(suffix=".cnf")
    user = task.get("username") or "root"
    pw = db.decrypt_secret(task.get("password") or "")
    # 用二进制写，避免 Windows \r\n 传到远端导致 mysql 客户端解析失败
    with open(cnf_local, "wb") as f:
        f.write(f"[client]\nuser={user}\npassword={pw}\n".encode("utf-8"))
    remote_cnf = "/tmp/bk_rrestore.cnf"
    try:
        sftp.put(cnf_local, remote_cnf)
        try:
            sftp.chmod(remote_cnf, 0o600)
        except Exception:
            pass
    finally:
        os.remove(cnf_local)
    port = task.get("port") or 3306
    # 探测 mysql 真实路径（与 dump 同理，避免 PATH 缺失）
    mysql_bin = _resolve_remote_bin(client, "mysql") or "mysql"
    shell = (
        f"set -o pipefail; {mysql_bin} --defaults-extra-file={remote_cnf} "
        f"-h 127.0.0.1 -P {port}"
    )
    wrapped = _wrap_login(shell)
    try:
        from core.engines.file import _ssh_exec_pipe
        _out, err, rc = _ssh_exec_pipe(
            client, wrapped, input_data=dump_bytes, timeout=3600)
        if rc != 0:
            raise RuntimeError(f"远程 mysql 恢复失败(rc={rc}): {err[:600]}")
    finally:
        try:
            sftp.remove(remote_cnf)
        except Exception:
            pass
        sftp.close()


def _remote_pg_restore(task: dict, ssh_host: dict, dump_bytes: bytes,
                       is_custom: bool) -> None:
    user = task.get("username") or "postgres"
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""
    port = task.get("port") or 5432
    # 探测工具路径
    client = _connect(ssh_host)
    if is_custom:
        tool = _resolve_remote_bin(client, "pg_restore") or "pg_restore"
        shell = (
            f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
            f"{tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} "
            f"-d {shlex.quote(db_name)} -c -C"
        )
    else:
        tool = _resolve_remote_bin(client, "psql") or "psql"
        shell = (
            f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
            f"{tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} "
            f"-d {shlex.quote(db_name)}"
        )
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    _out, err, rc = _ssh_exec_pipe(
        client, wrapped, input_data=dump_bytes, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程恢复失败(rc={rc}): {err[:600]}")


def remote_db_restore(task: dict, ssh_host: dict, db_type: str,
                      dump_bytes: bytes, is_custom: bool = False) -> None:
    """统一入口：将本地 dump 字节流经 SSH 灌入数据库服务器。"""
    if db_type == "mysql":
        _remote_mysql_restore(task, ssh_host, dump_bytes)
    elif db_type == "postgresql":
        _remote_pg_restore(task, ssh_host, dump_bytes, is_custom)
    else:
        raise RuntimeError(f"不支持的远程恢复类型: {db_type}")
