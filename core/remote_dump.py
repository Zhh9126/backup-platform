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
import time
import logging

import config
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


def remote_has_tool(ssh_host: dict, tool: str) -> bool:
    """检查远程 SSH 主机上是否存在指定命令。"""
    client = None
    try:
        client = _connect(ssh_host)
        shell = _wrap_login(
            f"command -v {shlex.quote(tool)} >/dev/null 2>&1 || "
            f"which {shlex.quote(tool)} >/dev/null 2>&1"
        )
        from core.engines.file import _ssh_exec_pipe
        _out, _err, rc = _ssh_exec_pipe(client, shell, timeout=30)
        return rc == 0
    except Exception:
        return False
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def _connect(ssh_host: dict):
    from core.engines.file import _get_ssh_client
    return _get_ssh_client(ssh_host["host_key"])


def remote_exec_capture(ssh_host: dict, shell: str, timeout: int = 1800) -> dict:
    """在远端执行一条 shell 命令，捕获 stdout/stderr/returncode。

    供 plugin_installer 远端安装/验证复用。shell 会被 _wrap_login 包裹
    （bash -lc），保证加载 /etc/profile 后 PATH 可用。

    返回: {"returncode": int, "stdout": str, "stderr": str}
    """
    from core.engines.file import _ssh_exec_pipe

    client = None
    try:
        client = _connect(ssh_host)
        out, err, rc = _ssh_exec_pipe(
            client, _wrap_login(shell), timeout=timeout)
        stdout = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
        return {"returncode": rc, "stdout": stdout, "stderr": err or ""}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def sftp_put(ssh_host: dict, local_path: str, remote_path: str) -> None:
    """通过 SFTP 把本地文件上传到远端指定路径（供 plugin_installer 上传离线包）。"""
    client = _connect(ssh_host)
    sftp = client.open_sftp()
    try:
        sftp.put(local_path, remote_path)
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def _remote_compress_pipe(enable: bool, level: int = None) -> str:
    """返回远端「dump | 压缩」管道片段：统一用 zstd。

    与本地 BackupEngine._resolve_compress_algo() 对齐（zstd 压缩率显著高于
    gzip，统一节省磁盘）。远端强制 `zstd -10 -c -`（级别与本地 _ZSTD_LEVEL=10
    对齐）：保证落盘后缀恒为 .zst，恢复端按 .zst 解压必然可逆；若远端确实未装
    zstd，dump 会直接报错，提示运维安装 zstd，而非悄悄回退 gzip 造成后缀/算法
    不一致导致恢复失败。enable=False 时返回空串（不压缩）。
    level: 任务级压缩级别，0/None 用默认 10。
    """
    if not enable:
        return ""
    lvl = level if level else 10
    return f" | zstd -{lvl} -c -"


def _remote_compress_algo(enable: bool) -> str:
    """返回远端实际使用的压缩算法（与 _remote_compress_pipe 一致）。"""
    if not enable:
        return "none"
    return "zstd"


def _remote_pv_throttle(task: dict) -> str:
    """返回远端「pv -L」限速管道片段（KB/s → 字节/秒）。

    仅当 task.bandwidth_limit>0 时使用；远端缺 pv 时返回空串（调用方直接拼接，
    无需判空），由上层日志提示限速被跳过。
    """
    try:
        bw = int(task.get("bandwidth_limit") or 0)
    except (TypeError, ValueError):
        bw = 0
    if not bw:
        return ""
    return f" | pv -L {bw * 1024}"


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
    0) PostgreSQL 家族工具：优先从运行中的 postgres 主进程推断 bin 目录。
       该目录中的工具版本与实例严格匹配，可避免 PATH 中旧版 pg_dump 干扰
       （例如 CentOS 自带 /usr/bin/pg_dump 不支持新版 pg_dump 语法）。
    1) bash -lc 'command -v <tool>'   （加载 /etc/profile 后查 PATH）
    2) find 在常见位置查找
       MySQL/MariaDB: /usr/local/mysql*/bin、/usr/local/mariadb*/bin、/opt/mysql*/bin
       PostgreSQL:    /usr/pgsql-*/bin、/usr/lib/postgresql/*/bin
       通用:           /usr/bin /usr/local/bin /opt
    """
    from core.engines.file import _ssh_exec_pipe

    # 0) PostgreSQL 家族工具：从运行中的 postgres 主进程定位 bin 目录。
    #    不少环境把 PG 二进制放在自定义目录（如 /pgdb/pgsql/bin），且未加入
    #    PATH；主进程路径必然真实存在且版本与实例严格匹配，据此可推断
    #    pg_dump/psql/pg_basebackup/pg_restore 位置，避免 PATH 中旧版工具
    #    导致 "invalid option" 类失败。
    if tool.startswith("pg_") or tool == "psql":
        try:
            proc_cmd = (
                r"ps -eo cmd= | awk '/\/postgres(\s|$)/ && !/awk/ && !/grep/ {print $1; exit}'"
            )
            out2, _, rc2 = _ssh_exec_pipe(
                client, _wrap_login(proc_cmd), timeout=20
            )
            if rc2 == 0 and out2:
                pg_exe = out2.decode("utf-8", errors="replace").strip().split()[0]
                # 注意：pg_exe 是 Linux 路径，不能用 os.path.join（Windows 平台会产生反斜杠）
                pg_bin = pg_exe.rsplit("/", 1)[0]
                candidate = f"{pg_bin}/{tool}"
                verify_cmd = f"test -x {shlex.quote(candidate)} && echo {candidate}"
                out3, _, rc3 = _ssh_exec_pipe(
                    client, _wrap_login(verify_cmd), timeout=10
                )
                if rc3 == 0 and out3:
                    found = out3.decode("utf-8", errors="replace").strip()
                    if found.startswith("/"):
                        return found
        except Exception:
            pass

    common_dirs = (
        "/usr/bin /usr/sbin /usr/local/bin /usr/local/sbin /opt "
        "/usr/local/mysql/bin /usr/local/mariadb/bin "
        "/usr/pgsql-*/bin /usr/lib/postgresql/*/bin "
        "/usr/local/pgsql/bin /var/lib/pgsql/*/bin /opt/PostgreSQL/*/bin /opt/pg/*/bin "
        "/pgdb/pgsql/bin /pgdb/*/bin"
    )
    # 单条命令：先 command -v，再用 find 兜底
    cmd = (
        f"command -v {shlex.quote(tool)} 2>/dev/null || "
        f"find {common_dirs} -maxdepth 4 -name {shlex.quote(tool)} -type f 2>/dev/null | head -1"
    )
    shell = _wrap_login(cmd)
    try:
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
    # command -v 可能输出 "alias xxx='...'" 之类的非路径；这里只接受绝对路径
    for c in candidates:
        if c.startswith("/"):
            return c

    return None


def _connect_isolated(ssh_host: dict):
    """建立一条独立 SSH 连接（不经过连接池），用完即关，避免影响主流程的连接池。

    地址解析优先级与 _connect 保持一致：优先 host_key，其次 hostname/host/ip。
    """
    import paramiko
    host_key = ssh_host.get("host_key")
    host = (ssh_host.get("hostname") or ssh_host.get("host") or ssh_host.get("ip"))
    if not host and not host_key:
        raise RuntimeError("ssh_host 缺少 host/ip")
    port = int(ssh_host.get("port") or 22)
    user = ssh_host.get("username") or "root"
    pw = ssh_host.get("password")
    if not pw and host_key:
        row = db.query_one(
            "SELECT password FROM ssh_hosts WHERE host_key=? LIMIT 1",
            (host_key,),
        )
        if row:
            pw = db.decrypt_secret(row["password"] or "")
    if host_key:
        # 复用 _connect 的地址解析逻辑（user@host:port 形式）
        addr = host_key
        if ":" in addr and not addr.startswith("["):
            parts = addr.rsplit(":", 1)
            _addr = parts[0]
            try:
                _port = int(parts[1])
            except ValueError:
                _port = 22
        else:
            _addr = addr
            _port = 22
        if "@" in _addr:
            _user, _hostname = _addr.split("@", 1)
        else:
            _user, _hostname = "root", _addr
        host, port, user = _hostname, _port, _user
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=pw, timeout=15,
                   allow_agent=False, look_for_keys=False)
    try:
        t = client.get_transport()
        if t is not None:
            t.set_keepalive(30)
    except Exception:
        pass
    return client


def remote_has_tool(ssh_host: dict, tool: str) -> bool:
    """检查远端 SSH 主机上 tool 命令是否存在且可执行（使用独立连接，不污染连接池）。"""
    client = None
    try:
        client = _connect_isolated(ssh_host)
        return _resolve_remote_bin(client, tool) is not None
    except Exception:
        return False
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


# ----------------------------- 远程 DUMP -----------------------------

def _remote_mysql_dump(task: dict, ssh_host: dict, compress: int, extra_args: str = "") -> bytes:
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
        mysqldump_bin, f"--defaults-file={remote_cnf}",
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
    # 默认禁用 GTID_PURGED；调用方显式传入 extra_args 时优先使用调用方参数。
    # 用户仍可通过 extra_options.extra_args 或 extra_options.gtid_purged=true 覆盖。
    if extra.get("gtid_purged"):
        pass
    elif extra_args:
        try:
            args.extend(shlex.split(str(extra_args)))
        except Exception:
            pass
    elif extra.get("extra_args"):
        try:
            args.extend(shlex.split(str(extra["extra_args"])))
        except Exception:
            pass
    else:
        # 默认禁用 GTID_PURGED；但 MySQL 5.5 及更早无 GTID，
        # --set-gtid-purged 选项本身不存在会直接报错，故按版本跳过。
        try:
            import re as _re
            # mysql 客户端通常与 mysqldump 同目录
            _mysql_bin = os.path.dirname(mysqldump_bin) + "/mysql"
            _vshell = (
                f"{shlex.quote(_mysql_bin)} --defaults-file={shlex.quote(remote_cnf)} "
                f"-h 127.0.0.1 -P {shlex.quote(str(port))} -N -e \"SELECT VERSION();\""
            )
            from core.engines.file import _ssh_exec_pipe
            _vout, _verr, _vrc = _ssh_exec_pipe(client, _wrap_login(_vshell), timeout=30)
            _vres = _vout.decode("utf-8", "replace") if isinstance(_vout, bytes) else str(_vout or "")
            _m = _re.search(r"(\d+)\.(\d+)\.", _vres)
            _maj, _min = (int(_m.group(1)), int(_m.group(2))) if _m else (8, 0)
        except Exception:
            _maj, _min = (8, 0)
        if (_maj, _min) >= (5, 6):
            args.append("--set-gtid-purged=OFF")

    # 4) 包装 shell：set -o pipefail + bash -lc
    shell = "set -o pipefail; " + " ".join(shlex.quote(a) for a in args)
    # 统一压缩：与本地逻辑备份对齐，全局开启压缩时走 zstd（而非固定 gzip）
    enable = bool(compress) and getattr(config, "COMPRESS_BY_DEFAULT", True)
    # 任务级压缩级别（>0 时覆盖默认 10）
    try:
        clvl = int(task.get("compress_level") or 0)
    except (TypeError, ValueError):
        clvl = 0
    # 远端缺 zstd 时降级为不压缩，避免整条管道 rc=127 导致备份失败
    if enable and not remote_has_tool(ssh_host, "zstd"):
        logging.getLogger(__name__).warning(
            "[remote_dump] 远端主机 %s 未安装 zstd，降级为不压缩（后续安装 zstd 可恢复高压缩率）",
            ssh_host.get("host") or ssh_host.get("ip"),
        )
        enable = False
    shell += _remote_pv_throttle(task)
    shell += _remote_compress_pipe(enable, clvl)
    # 远端未启用压缩时，落盘后缀不使用 .zst，恢复端按普通 sql 处理
    if not enable:
        nonlocal_out_suffix = ".sql"
    else:
        nonlocal_out_suffix = ".sql.zst"
    wrapped = _wrap_login(shell)
    try:
        from core.engines.file import _ssh_exec_pipe
        out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
        if rc != 0:
            raise RuntimeError(f"远程 mysqldump 失败(rc={rc}, bin={mysqldump_bin}): {err[:600]}")
        # 防御：mysqldump 异常时也可能产生极小的压缩产物
        # （zstd 帧头 ~15 字节；20 字节阈值覆盖空文件）
        if enable and len(out) <= 20:
            raise RuntimeError(
                f"远程 mysqldump 疑似失败：zstd 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
            )
        return out, enable
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
    # 注意：不使用 "-f -"（显式指定 stdout）。某些环境（如 PG 14.24 / CentOS7）
    # 下 pg_dump 的 "-f -" 参数异常导致输出 0 字节；不带 -f 时 pg_dump 默认输出到
    # stdout，行为一致且兼容性更好。
    base = (
        f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
        f"{pgdump_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} {fmt}"
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
    # 压缩策略：远程 pg_dump 已通过 -Fc 自带 zlib 压缩，无需再外挂 gzip/zstd，
    # 外挂会造成双重压缩（更慢且压缩率反而略差）。故 compress 时直接输出 -Fc，
    # 落盘后缀保持 .dump，恢复端用 pg_restore，不破坏已有恢复流程。
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程 pg_dump 失败(rc={rc}, bin={pgdump_bin}): {err[:600]}")
    if compress and len(out) <= 20:
        raise RuntimeError(
            f"远程 pg_dump 疑似失败：-Fc 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
        )
    return out


def _remote_kingbase_dump(task: dict, ssh_host: dict, compress: int) -> bytes:
    """在远端数据库服务器以 sys_dump 导出，返回原始字节（可选 gzip 压缩）。"""
    client = _connect(ssh_host)
    dump_bin = _resolve_remote_bin(client, "sys_dump")
    if not dump_bin:
        raise RuntimeError(
            "远端主机未找到 sys_dump（PATH 与 /opt/Kingbase/ES/V*/Server/bin、/usr/bin 均无）。"
            "请在远端安装 KingbaseES 客户端后重试。"
        )
    user = task.get("username") or "system"
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""
    port = task.get("port") or 54321
    fmt = "-Fc" if compress else "-Fp"

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

    base = (
        f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
        f"{dump_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} {fmt}"
    )

    if tables:
        if not db_name:
            raise RuntimeError("指定表（tables）时必须同时填写 task.db_name 库名")
        target_args = f"-d {shlex.quote(db_name)} " + " ".join(f"-t {shlex.quote(t)}" for t in tables)
    elif schemas:
        target_args = " ".join(f"-n {shlex.quote(s)}" for s in schemas)
    elif db_name:
        target_args = f"-d {shlex.quote(db_name)}"
    else:
        target_args = "--all-databases"

    extra_args = ""
    if extra.get("extra_args"):
        try:
            shlex.split(str(extra["extra_args"]))
            extra_args = " " + str(extra["extra_args"])
        except Exception:
            pass

    shell = f"{base} {target_args}{extra_args}"
    # 压缩策略：远程 sys_dump 已通过 -Fc 自带 zlib 压缩，无需再外挂 gzip/zstd，
    # 否则双重压缩（更慢且压缩率略差）。compress 时直接输出 -Fc，落盘 .dump，
    # 恢复端用 sys_restore，保持与本地一致、不破坏恢复流程。
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程 sys_dump 失败(rc={rc}, bin={dump_bin}): {err[:600]}")
    if compress and len(out) <= 20:
        raise RuntimeError(
            f"远程 sys_dump 疑似失败：-Fc 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
        )
    return out


def _remote_redis_dump(task: dict, ssh_host: dict) -> bytes:
    """在远端备份机执行 redis-cli --rdb -，把 RDB 数据流拉回到本地。"""
    client = _connect(ssh_host)
    redis_cli = _resolve_remote_bin(client, "redis-cli")
    if not redis_cli:
        raise RuntimeError(
            "远端主机未找到 redis-cli（PATH 与 /usr/bin、/usr/local/bin 均无）。"
            "请在远端安装 Redis 客户端后重试。"
        )
    host = task.get("host") or "127.0.0.1"
    port = task.get("port") or 6379
    pw = db.decrypt_secret(task.get("password") or "")
    auth_args = ""
    if pw:
        auth_args = f" -a {shlex.quote(pw)}"
    shell = (
        f"{redis_cli} -h {shlex.quote(host)} -p {port}{auth_args} --rdb -"
    )
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程 redis-cli --rdb 失败(rc={rc}, bin={redis_cli}): {err[:600]}")
    if len(out) <= 20:
        raise RuntimeError(f"远程 redis-cli --rdb 疑似失败：仅 {len(out)} 字节（stderr: {err[:200]}）")
    return out


def _remote_mongodb_dump(task: dict, ssh_host: dict, compress: int) -> bytes:
    """在远端备份机执行 mongodump --archive，把归档流拉回到本地。"""
    client = _connect(ssh_host)
    mongodump = _resolve_remote_bin(client, "mongodump")
    if not mongodump:
        raise RuntimeError(
            "远端主机未找到 mongodump（PATH 与 /usr/bin、/usr/local/bin 均无）。"
            "请在远端安装 MongoDB Database Tools 后重试。"
        )
    host = task.get("host") or "127.0.0.1"
    port = task.get("port") or 27017
    user = task.get("username") or ""
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""

    auth_args = ""
    if user:
        auth_args += f" --username {shlex.quote(user)}"
    if pw:
        auth_args += f" --password {shlex.quote(pw)}"

    db_args = f" --db {shlex.quote(db_name)}" if db_name else ""

    shell = f"{mongodump} --host {shlex.quote(host)} --port {port}{auth_args}{db_args} --archive"
    # 统一压缩：--archive 输出到 stdout，外挂 zstd（与本地 mongodump 对齐），
    # 节省磁盘；恢复端按落盘后缀 .archive.zst 用 zstd 解压后 --archive 导入。
    # 远端缺 zstd 时降级为不压缩，避免整条管道 rc=127 导致备份失败。
    if compress and not remote_has_tool(ssh_host, "zstd"):
        logging.getLogger(__name__).warning(
            "[remote_dump] 远端主机 %s 未安装 zstd，MongoDB 远程备份降级为不压缩",
            ssh_host.get("host") or ssh_host.get("ip"),
        )
        compress = False
    if compress:
        shell += " | zstd -10 -c -"
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程 mongodump 失败(rc={rc}, bin={mongodump}): {err[:600]}")
    if compress and len(out) <= 20:
        raise RuntimeError(f"远程 mongodump 疑似失败：zstd 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）")
    return out, bool(compress)


def remote_exec_and_fetch(ssh_host: dict, remote_cmd: str, remote_path: str,
                          timeout: int = 7200) -> bytes:
    """在远端执行命令，并通过 SFTP 取回产物文件。

    适用于 Oracle expdp / 达梦 dexp 等无法直接输出到 stdout 的工具：
    命令在远端生成文件后，通过 SFTP 把文件拉取回备份平台。
    """
    client = _connect(ssh_host)
    try:
        wrapped = _wrap_login(remote_cmd)
        from core.engines.file import _ssh_exec_pipe
        _out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"远程命令失败(rc={rc}): {err[:600]}")
        sftp = client.open_sftp()
        try:
            with io.BytesIO() as buf:
                sftp.getfo(remote_path, buf)
                buf.seek(0)
                return buf.read()
        finally:
            sftp.close()
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def remote_db_dump(task: dict, ssh_host: dict, db_type: str, compress: int = 0,
                   extra_args: str = "") -> tuple:
    """统一入口：在数据库服务器本地执行 dump 并返回 (原始字节, 是否压缩)。

    - 返回元组 (data: bytes, compressed: bool)，供调用方决定落盘后缀与
      compress_algo；当远端缺少压缩工具而降级为不压缩时，compressed=False，
      调用方应以 .sql 落盘而非 .sql.zst，保证恢复可逆。
    - extra_args: 透传给 dump 命令的额外参数（目前 MySQL 用）。
    """
    if db_type == "mysql":
        return _remote_mysql_dump(task, ssh_host, compress, extra_args)
    if db_type == "postgresql":
        return _remote_pg_dump(task, ssh_host, compress), bool(compress)
    if db_type == "kingbase":
        return _remote_kingbase_dump(task, ssh_host, compress), bool(compress)
    if db_type == "redis":
        return _remote_redis_dump(task, ssh_host), False
    if db_type == "mongodb":
        return _remote_mongodb_dump(task, ssh_host, compress), bool(compress)
    raise RuntimeError(f"不支持的远程 dump 类型: {db_type}")


# ------------------- 远程物理备份（pg_basebackup / sys_basebackup 等） -------------------
#
# 统一入口：所有“服务端流式物理备份”型数据库（PostgreSQL / Kingbase / 其他兼容
# PG 协议的库）共用同一套逻辑，避免每个引擎把路径、端口、用户名、临时目录写死。
# 通过参数驱动，调用方只传入工具名与少量差异项即可。

def remote_physical_backup(task: dict, ssh_host: dict, *, tool: str,
                           default_port: int, default_user: str,
                           extra_args_key: str = "pg_basebackup_extra_args",
                           tool_label: str = None) -> dict:
    """在远端数据库服务器执行流式物理备份（pg_basebackup / sys_basebackup 等）。

    参数
    ----
    task        : 备份任务 dict（含 host/port/username/password/extra_options）
    ssh_host    : SSH 主机 dict（已纳管，用于连接）
    tool        : 远端备份工具命令名（如 pg_basebackup / sys_basebackup）
    default_port: 该库默认端口（任务未配置时回退）
    default_user: 该库默认用户名（任务未配置时回退）
    extra_args_key: extra_options 中透传额外参数的 key
    tool_label  : 报错展示用中文名（默认取 tool）

    返回
    ----
    dict: {
        "ok": bool,
        "rc": int,
        "stdout": str, "stderr": str,
        "remote_dir": str,          # 远端生成的 tar 包目录
        "message": str,             # 失败时的可读说明（含权限提示）
    }
    """
    from core.engines.file import _ssh_exec_pipe
    from core.engines.base import BackupStatus

    tool_label = tool_label or tool
    port = int(task.get("port") or default_port)
    user = task.get("username") or default_user
    pw = db.decrypt_secret(task.get("password") or "")

    client = _connect(ssh_host)
    resolved = _resolve_remote_bin(client, tool)
    if not resolved:
        return {
            "ok": False, "rc": 127, "stdout": "", "stderr": "", "remote_dir": "",
            "message": (f"远端主机未找到 {tool}（已在 PATH 及常见安装目录/search，"
                        f"如 /usr/bin、/opt/Kingbase/ES/V*/Server/bin、/pgdb/pgsql/bin 等）。"
                        f"请在数据库服务器安装客户端并加入 PATH，或前往【备份插件】页安装。"),
        }

    # SSH 主机即数据库服务器时，-h 用 127.0.0.1；否则用任务 host
    hk = ssh_host.get("host_key", "")
    ssh_ip = hk.rsplit("@", 1)[-1].split(":")[0] if "@" in hk else ""
    db_host = task.get("host") or "127.0.0.1"
    db_host = "127.0.0.1" if (ssh_ip and ssh_ip == str(db_host)) else str(db_host)

    # 临时目录用时间戳避免冲突，且回退前先清理
    ts = time.strftime("%Y%m%d_%H%M%S")
    remote_tmp = f"/tmp/{tool}_bkp_{ts}"
    prep = f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}"
    _ssh_exec_pipe(client, _wrap_login(prep), timeout=60)

    # extra_options 透传额外参数（如 --verbose、--exclude）
    extra = []
    try:
        raw = task.get("extra_options")
        if raw:
            data = json.loads(raw)
            val = data.get(extra_args_key)
            if isinstance(val, list):
                extra = [str(x) for x in val]
            elif isinstance(val, str):
                extra = shlex.split(val)
    except Exception:
        extra = []

    inner = (
        f"export PGPASSWORD={shlex.quote(pw)}; "
        f"{resolved} -h {shlex.quote(db_host)} -p {port} -U {shlex.quote(user)} "
        f"-D {remote_tmp} -Ft -z --checkpoint=fast --no-password"
    )
    if extra:
        inner += " " + " ".join(shlex.quote(a) for a in extra)
    wrapped = _wrap_login(inner)
    start = time.time()
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
    duration = round(time.time() - start, 3)
    out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out

    if rc != 0:
        snippet = (out_text or err)[-1200:]
        hint = ""
        if "Permission denied" in snippet or "could not open file" in snippet:
            hint = (
                "；提示：数据目录(PGDATA)中存在服务端 OS 用户无权限读取的文件，"
                "请在数据库服务器上修正该文件权限（如 "
                "chown postgres:postgres <file> && chmod 644 <file>），"
                "或将其移出 PGDATA"
            )
        return {
            "ok": False, "rc": rc, "stdout": out_text, "stderr": err,
            "remote_dir": "",
            "message": f"远端 {tool_label} 物理备份失败(rc={rc}): {snippet}{hint}",
        }
    return {
        "ok": True, "rc": 0, "stdout": out_text, "stderr": err,
        "remote_dir": remote_tmp,
        "message": f"远端 {tool_label} 物理备份执行成功(rc=0)，产物在 {remote_tmp}",
    }


def _pull_remote_tars(client, remote_dir: str, out_dir: str) -> list:
    """从远端目录拉取 *.tar[.gz] 到本地 out_dir，返回 [(local_path, size)]。"""
    os.makedirs(out_dir, exist_ok=True)
    sftp = client.open_sftp()
    try:
        pieces = []
        for attr in sftp.listdir_attr(remote_dir):
            fname = attr.filename
            if fname.endswith((".tar.gz", ".tar")):
                remote_path = f"{remote_dir}/{fname}"
                local_path = os.path.join(out_dir, fname)
                sftp.get(remote_path, local_path)
                pieces.append((local_path, attr.st_size))
        return pieces
    finally:
        try:
            sftp.close()
        except Exception:
            pass


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
        f"set -o pipefail; {mysql_bin} --defaults-file={remote_cnf} "
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

    # 恢复前清空 GTID，避免导入含 GTID_PURGED 的备份时报 1840
    reset_sql = "RESET MASTER;"
    reset_cmd = (
        f"set -o pipefail; echo {shlex.quote(reset_sql)} | {mysql_bin} "
        f"--defaults-file={remote_cnf} -h 127.0.0.1 -P {port}"
    )
    reset_wrapped = _wrap_login(reset_cmd)
    try:
        from core.engines.file import _ssh_exec_pipe
        _out, err, rc = _ssh_exec_pipe(
            client, reset_wrapped, input_data=b"", timeout=300)
        if rc != 0:
            # 8.4+ 语法改为 RESET BINARY LOGS AND GTID_EXECUTION
            reset_sql2 = "RESET BINARY LOGS AND GTID_EXECUTION;"
            reset_cmd2 = (
                f"set -o pipefail; echo {shlex.quote(reset_sql2)} | {mysql_bin} "
                f"--defaults-file={remote_cnf} -h 127.0.0.1 -P {port}"
            )
            _out2, err2, rc2 = _ssh_exec_pipe(
                client, _wrap_login(reset_cmd2), input_data=b"", timeout=300)
            if rc2 != 0:
                # 非致命：继续尝试导入，让后续错误更直观
                pass
    except Exception:
        pass

    shell = (
        f"set -o pipefail; {mysql_bin} --defaults-file={remote_cnf} "
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
    from core.engines.file import _ssh_exec_pipe

    # 0) 远程先 DROP+CREATE 目标库，保证干净恢复。
    #    pg_restore 的 "-C" 在目标库同名已存在时会因 "cannot drop the
    #    currently open database" 失败，导致旧对象残留，这里改为两步建库。
    if db_name:
        psql_tool = _resolve_remote_bin(client, "psql") or "psql"
        safe_db = db_name.replace('"', '""')
        prep = (
            f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
            f"{psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d postgres "
            f"-c 'DROP DATABASE IF EXISTS \"{safe_db}\" WITH (FORCE);' "
            f"&& {psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d postgres "
            f"-c 'CREATE DATABASE \"{safe_db}\";'"
        )
        _out, perr, prc = _ssh_exec_pipe(
            client, _wrap_login(prep), input_data=b"", timeout=300)
        if prc != 0:
            raise RuntimeError(f"远程重建目标库 {db_name} 失败(rc={prc}): {perr[:600]}")

    if is_custom:
        tool = _resolve_remote_bin(client, "pg_restore") or "pg_restore"
        shell = (
            f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
            f"{tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} "
            f"-d {shlex.quote(db_name)}"
        )
    else:
        tool = _resolve_remote_bin(client, "psql") or "psql"
        shell = (
            f"set -o pipefail; export PGPASSWORD={shlex.quote(pw)}; "
            f"{tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} "
            f"-d {shlex.quote(db_name)}"
        )
    wrapped = _wrap_login(shell)
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
