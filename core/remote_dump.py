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
import re
import shlex
import tempfile
import time
import logging
import contextvars

import config
import core.db as db
from core import ssh_hosts
from core.logical_full import SYSTEM_DBS


def resolve_ssh_host(task: dict):
    """解析任务的 SSH 主机（用于远程执行 dump）。

    解析优先级：
    0) 任务自带 SSH 凭据（extra_options.ssh_cred，API 层已加密保存密码）——
       **无需纳管主机**，适合"数据库与 SSH 同机"的快速接入场景；
    1) extra_options.ssh_host_id 显式指定（数据库任务表单下拉框写入）
    2) 按任务 host（数据库地址）匹配 ssh_hosts 的 hostname 或 host_key
    3) 无匹配则返回 None（调用方将回退本机执行）
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

    # 0) 任务自带凭据（免纳管）：{host, port, username, password(密文)}
    cred = extra.get("ssh_cred") or {}
    if isinstance(cred, dict) and (cred.get("host") or cred.get("hostname")):
        ch = (cred.get("host") or cred.get("hostname") or "").strip()
        cp = int(cred.get("port") or 22)
        cu = (cred.get("username") or "root").strip()
        enc_pw = cred.get("password") or ""
        pw = db.decrypt_secret(enc_pw) if enc_pw else ""
        return {
            "name": "task-direct",
            "host_key": f"{cu}@{ch}:{cp}",
            "hostname": ch,
            "port": cp,
            "username": cu,
            "password": pw,
            "auth_type": "password",
            "has_password": bool(pw),
            "os_type": cred.get("os_type") or "linux",
            "remark": "任务自带 SSH 凭据（未纳管）",
        }

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


def task_tool_path(task: dict) -> str:
    """任务级工具路径覆盖（extra_options.tool_path）。

    自动探测不到备份命令时的**手动兜底**：填写数据库服务器上备份命令所在的
    bin 目录（冒号/分号分隔多个），会作为 PATH 前缀注入远端命令与本机回退执行，
    并优先于常见目录 glob 参与探测。返回规范化的冒号分隔串（无则空串）。
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
    tp = str(extra.get("tool_path") or "").strip()
    if not tp:
        return ""
    parts = []
    for seg in tp.replace(";", ":").replace(",", ":").split(":"):
        seg = seg.strip().rstrip("/\\")
        if seg and seg not in parts:
            parts.append(seg)
    return ":".join(parts)


def _tool_path_export(tool_path: str) -> str:
    """生成注入 PATH 的 export 语句（远端脚本首行用），空则空串。"""
    if not tool_path:
        return ""
    return f"export PATH={shlex.quote(tool_path)}:$PATH; "


def parse_task_env_vars(task: dict) -> dict:
    """解析任务级自定义环境变量（extra_options.env_vars）。

    支持两种存储形态：
    - dict：{"KEY": "VALUE", ...}
    - str：每行一条 KEY=VALUE（兼容 ; 分隔）；# 开头视为注释
    返回 {KEY: VALUE}；键名不符合 shell 变量规则的条目被忽略。
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
    raw_env = extra.get("env_vars") if isinstance(extra, dict) else None
    result: dict = {}
    if isinstance(raw_env, dict):
        items = list(raw_env.items())
    elif isinstance(raw_env, str) and raw_env.strip():
        items = []
        for line in raw_env.replace(";", "\n").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            items.append((k.strip(), v.strip()))
    else:
        items = []
    for k, v in items:
        k = str(k).strip()
        if not k or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            continue
        result[k] = str(v)
    return result


def task_env_export(task: dict) -> str:
    """生成任务级环境变量的 export 前缀（远端脚本用），无则空串。

    PATH 特殊处理：用户配置的 PATH 以「前缀」方式合并（PATH=xxx:$PATH），
    而非直接覆盖，避免清空系统查找路径导致命令全部找不到。
    """
    env_vars = parse_task_env_vars(task)
    if not env_vars:
        return ""
    parts = []
    user_path = env_vars.pop("PATH", None)
    for k, v in env_vars.items():
        parts.append(f"export {k}={shlex.quote(v)}; ")
    if user_path:
        parts.append(f"export PATH={shlex.quote(user_path)}:$PATH; ")
    return "".join(parts)


# 当前执行上下文的任务环境变量 export 前缀。
# 由 scheduler 在调用 engine.run_backup()/run_restore() 前设置，
# _wrap_login 读取并注入到所有远程 SSH 命令（覆盖全部引擎与自定义脚本）。
_task_env_export_cv: contextvars.ContextVar = contextvars.ContextVar(
    "bp_task_env_export", default="")


def set_task_env_export(export_str: str):
    """设置当前上下文的任务环境变量前缀，返回 token 供 reset。"""
    return _task_env_export_cv.set(export_str or "")


def reset_task_env_export(token) -> None:
    _task_env_export_cv.reset(token)


def current_task_env_export() -> str:
    return _task_env_export_cv.get()


def remote_has_tool(ssh_host: dict, tool: str, check_user: str = None,
                    extra_paths: str = None) -> bool:
    """检查远程 SSH 主机上是否存在指定命令（独立连接，不污染连接池）。

    check_user：以指定用户身份探测（su - <user> -c），用于工具仅在某个
    服务运行用户的 profile PATH 中可见的场景（如 Oracle 的 expdp/rman
    只在 oracle 用户环境变量中）。默认以 SSH 登录用户探测。
    extra_paths：任务级工具路径（PATH 前缀），自动探测不到时的手动兜底。
    """
    client = None
    try:
        client = _connect_isolated(ssh_host)
        tp_export = _tool_path_export(extra_paths)
        if check_user:
            inner = tp_export + f"command -v {shlex.quote(tool)} >/dev/null 2>&1"
            probe = (f"su - {shlex.quote(check_user)} -c "
                     f"{shlex.quote(inner)}")
            shell = _wrap_login(probe)
            from core.engines.file import _ssh_exec_pipe
            _out, _err, rc = _ssh_exec_pipe(client, shell, timeout=30)
            return rc == 0
        if tp_export:
            shell = _wrap_login(tp_export + f"command -v {shlex.quote(tool)} >/dev/null 2>&1")
            from core.engines.file import _ssh_exec_pipe
            _out, _err, rc = _ssh_exec_pipe(client, shell, timeout=30)
            return rc == 0
        return _resolve_remote_bin(client, tool) is not None
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
    # 任务级 SSH 凭据（resolve_ssh_host 构造的临时主机）直接带密码；
    # 已纳管主机 dict 也含解密后密码，传入可省一次 DB 查询。
    pw = ssh_host.get("password") or None
    return _get_ssh_client(ssh_host["host_key"], password=pw)


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
    # 任务级自定义环境变量（extra_options.env_vars）统一注入：
    # 覆盖所有引擎的远程命令与自定义脚本，且不影响工具探测的 PATH 前缀逻辑
    prefix = _task_env_export_cv.get() or ""
    # 用单引号包，并用 sed 把命令里的单引号转义（'\'' 方式）
    escaped = (prefix + shell_cmd).replace("'", "'\\''")
    return f"bash -lc '{escaped}'"


def _resolve_remote_bin(client, tool: str, extra_paths: str = None) -> str | None:
    """在远端 SSH 上检测指定工具的实际路径。

    返回绝对路径；若找不到返回 None。

    extra_paths：任务级工具路径（PATH 前缀，冒号分隔），**最高优先级**——
    注入 command -v 的 PATH 且加入 find 候选目录，用于数据库服务器
    未配置环境变量时的手动兜底。

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

    # 0) PostgreSQL / Kingbase 家族工具：从运行中的数据库主进程定位 bin 目录。
    #    不少环境把 PG/金仓 二进制放在自定义目录（如 /pgdb/pgsql/bin、
    #    /opt/Kingbase/ES/V9/KESRealPro/*/Server/bin），且未加入 PATH；
    #    主进程路径必然真实存在且版本与实例严格匹配，据此可推断
    #    pg_dump/psql/ksql/sys_dump 等工具位置，避免 PATH 中旧版工具
    #    导致 "invalid option" 或 rc=127 类失败。
    _pg_like = ("ksql", "sys_dump", "sys_restore", "sys_basebackup",
                "sys_receivewal", "sys_dumpall")
    # openGauss：主进程名为 gaussdb，工具名为 gs_*（gs_dump/gs_basebackup/...）
    _gs_like = ("gs_dump", "gs_dumpall", "gs_restore", "gsql", "gs_basebackup",
                "gs_ctl", "gs_probackup", "gs_verifybackup")
    if (tool.startswith("pg_") or tool == "psql"
            or tool in _pg_like or tool in _gs_like):
        try:
            if tool in _gs_like:
                # openGauss：主进程名 gaussdb，客户端工具与其同目录（GAUSSHOME/bin）
                proc_cmd = (
                    r"ps -eo cmd= | awk '/\/gaussdb(\s|$)/ && !/awk/ && !/grep/ {print $1; exit}'"
                )
            elif tool in _pg_like:
                # 金仓：主进程名为 kingbase，工具在其 bin 目录
                proc_cmd = (
                    r"ps -eo cmd= | awk '/\/kingbase(\s|$)/ && !/awk/ && !/grep/ {print $1; exit}'"
                )
            else:
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
        "/usr/bin /usr/sbin /usr/local/bin /usr/local/sbin /bin /sbin /opt "
        "/usr/local/mysql/bin /usr/local/mariadb/bin /opt/mysql*/bin /opt/database/bin "
        "/usr/pgsql-*/bin /usr/lib/postgresql/*/bin "
        "/usr/local/pgsql/bin /var/lib/pgsql/*/bin /opt/PostgreSQL/*/bin /opt/pg/*/bin "
        "/pgdb/pgsql/bin /pgdb/*/bin "
        # Oracle：静默安装常见目录（ORACLE_HOME 可能为 product/版本/dbhome_n 结构）
        "/u01/app/oracle/product/*/*/bin /u01/app/oracle/*/bin /home/oracle/product/*/*/bin "
        # DM 达梦
        "/dm8/bin /opt/dmdbms/bin /home/dmdba/dmdbms/bin /opt/dm*/bin "
        # 人大金仓（V9 为 KESRealPro/<版本>/Server|ClientTools/bin 布局）
        "/opt/Kingbase/ES/V*/bin /opt/kingbase/*/bin /KingbaseES/V*/bin "
        "/opt/Kingbase/ES/V*/KESRealPro/*/Server/bin "
        "/opt/Kingbase/ES/V*/KESRealPro/*/ClientTools/bin "
        # openGauss（企业版/社区版安装布局：GAUSSHOME/bin 存放 gs_* 工具）
        "/opt/soft/openGauss/*/bin /opt/opengauss/*/bin /opt/openGauss/*/bin "
        "/usr/local/opengauss/bin /usr/local/openGauss/bin "
        "/opt/huawei/install/app/bin /opt/huawei/*/bin /opt/gauss/*/bin "
        "/gauss/*/bin /gaussdb/*/bin /opt/gaussdb/*/bin /home/omm/*/bin "
        # Redis / MongoDB
        "/usr/local/redis*/bin /usr/local/redis/bin /opt/redis*/bin "
        "/usr/local/mongodb*/bin /opt/mongodb*/bin /usr/local/mongodb/bin "
        # 通用自部署目录
        "/usr/local/*/bin /data/*/bin /data/*/*/bin /app/*/bin"
    )
    # 单条命令：先 command -v，再用 find 兜底（find 只在声明过的候选目录中找，
    # 绝不硬编码任何单一安装路径 —— 全部以 glob/枚举动态发现）
    tp = extra_paths or ""
    tp_export = _tool_path_export(tp)
    if tp:
        common_dirs = tp + " " + common_dirs
    cmd = (
        f"{tp_export}command -v {shlex.quote(tool)} 2>/dev/null || "
        f"find {common_dirs} -maxdepth 4 -name {shlex.quote(tool)} -type f -perm -u+x 2>/dev/null | head -1"
    )
    shell = _wrap_login(cmd)
    try:
        out, err, rc = _ssh_exec_pipe(client, shell, timeout=45)
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


_remote_bin_cache: dict = {}


def resolve_remote_tool(ssh_host: dict, tool: str, check_user: str = None,
                        extra_paths: str = None) -> str:
    """解析远端数据库自带工具的绝对路径（带缓存），找不到返回 ""。

    解析顺序（绝不硬编码单一路径，全部动态发现）：
    0) extra_paths（任务级 tool_path，PATH 前缀）——手动兜底最高优先级；
    1) 以 check_user（如 oracle）身份 `command -v`（该用户的 profile PATH）；
    2) 以 SSH 登录用户 bash -lc `command -v`（/etc/profile）；
    3) 常见安装目录 glob 枚举 + find（覆盖 Oracle/DM/金仓/MySQL/PG/Redis/Mongo）。
    结果按 (host_key, tool, check_user, extra_paths) 缓存，避免同一任务内反复探测。
    """
    key = (ssh_host.get("host_key") or ssh_host.get("hostname") or "", tool,
           check_user or "", extra_paths or "")
    if key in _remote_bin_cache:
        return _remote_bin_cache[key]

    path = ""
    client = None
    try:
        client = _connect_isolated(ssh_host)
        tp_export = _tool_path_export(extra_paths)
        if check_user:
            from core.engines.file import _ssh_exec_pipe
            inner = tp_export + f"command -v {shlex.quote(tool)} 2>/dev/null"
            probe = (f"su - {shlex.quote(check_user)} -c "
                     f"{shlex.quote(inner)}")
            out, _err, rc = _ssh_exec_pipe(client, _wrap_login(probe), timeout=30)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("/"):
                    path = line
                    break
        if not path:
            path = _resolve_remote_bin(client, tool, extra_paths=extra_paths) or ""
    except Exception:
        path = ""
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    if path:
        _remote_bin_cache[key] = path
    return path


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


# ----------------------------- 远程 DUMP -----------------------------

def task_extra_dict(task: dict) -> dict:
    """解析任务 extra_options（兼容 dict 与 JSON 字符串两种形态）。"""
    raw = task.get("extra_options")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def transfer_params(task: dict) -> dict:
    """解析任务级「大流量传输」参数（高级选项 / extra_options 覆盖全局默认）。

    为什么要这套参数：10GB 级库在受限链路上一次要数小时，而旧实现把 dump
    超时写死 3600s、重试间隔写死 5s 指数退避，且失败后**从头重跑**。现把这些
    全部开放到任务级高级选项：
      cmd_timeout     单次执行总时长上限（秒），0 = 不限（大库备份推荐）
      idle_timeout    连续无数据输出上限（秒），0 = 不限（区分"大表读得慢"与"真卡死"）
      retry_max       失败重试次数
      retry_interval  重试间隔（秒，默认 60）
      resume          是否启用断点续传（中断后从已传输字节继续，不重跑 dump）
      rate_kbps       平台侧限速 KB/s（0=不限，不依赖远端 pv）
    """
    extra = task_extra_dict(task)

    def _int(*vals, default: int = 0) -> int:
        for v in vals:
            if v is None or v == "":
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return default

    resume = extra.get("resume_enabled")
    if resume is None:
        resume = getattr(config, "BACKUP_RESUME_ENABLED", True)
    else:
        resume = str(resume).lower() in ("1", "true", "yes", "on")

    return {
        "cmd_timeout": _int(extra.get("cmd_timeout"),
                            getattr(config, "BACKUP_CMD_TIMEOUT", 0), default=0),
        "idle_timeout": _int(extra.get("idle_timeout"),
                             getattr(config, "BACKUP_IDLE_TIMEOUT", 1800), default=1800),
        "retry_max": _int(extra.get("retry_max"),
                          getattr(config, "BACKUP_RETRY_MAX", 3), default=3),
        "retry_interval": _int(extra.get("retry_interval"),
                               getattr(config, "BACKUP_RETRY_INTERVAL", 60), default=60),
        "resume": bool(resume),
        "resume_ttl": _int(getattr(config, "BACKUP_RESUME_TTL", 43200), default=43200),
        "rate_kbps": _int(task.get("bandwidth_limit"), default=0),
        "stable_secs": _int(extra.get("stable_secs"),
                            getattr(config, "BACKUP_STABLE_SECS", 20), default=20),
        "keep_remote_done": str(
            extra.get("keep_remote_done",
                      getattr(config, "BACKUP_REMOTE_KEEP_DONE", True))
        ).lower() in ("1", "true", "yes", "on"),
    }


def _resolve_mysql_dump_bin(client, task: dict, remote_cnf: str) -> str:
    """探测远端 mysqldump 真实路径，并按 MySQL/MariaDB 风味自动校正。

    必要性：paramiko exec_command 走非交互 shell，PATH 常缺 /usr/local/mysql/bin；
    同机共存 MySQL 与 MariaDB 客户端时 /usr/bin/mysqldump 可能被 MariaDB 版抢占
    （--set-gtid-purged 仅 MySQL 支持，风味不匹配会直接 unknown variable 失败）。
    """
    mysqldump_bin = _resolve_remote_bin(client, "mysqldump")
    if not mysqldump_bin:
        raise RuntimeError(
            "远端主机未找到 mysqldump（PATH 与 /usr/local/mysql*/bin、/usr/local/mariadb*/bin、/opt 均无）。"
            "请在远端安装 mysql-client 或 xtrabackup 后重试。"
        )

    try:
        from core.engines.file import _ssh_exec_pipe as _sep
        _mysql_cli = os.path.dirname(mysqldump_bin) + "/mysql"
        _vo, _ve, _vrc = _sep(
            client,
            _wrap_login(f"{shlex.quote(_mysql_cli)} --defaults-file={shlex.quote(remote_cnf)} "
                        f"-h 127.0.0.1 -P {int(task.get('port') or 3306)} -N -e \"SELECT VERSION();\" 2>/dev/null"),
            timeout=30)
        _server_ver = (_vo.decode("utf-8", "replace")
                       if isinstance(_vo, bytes) else str(_vo or "")).strip().splitlines()
        _server_ver = _server_ver[-1].strip() if _server_ver else ""
    except Exception:
        _server_ver = ""

    if _server_ver:
        def _dump_flavor(p: str) -> str:
            try:
                o, _e, _rc = _sep(client, _wrap_login(
                    f"{shlex.quote(p)} --version 2>/dev/null"), timeout=30)
                t = o.decode("utf-8", "replace") if isinstance(o, bytes) else str(o or "")
                return "mariadb" if "mariadb" in t.lower() else "mysql"
            except Exception:
                return ""
        want = "mariadb" if "mariadb" in _server_ver.lower() else "mysql"
        if _dump_flavor(mysqldump_bin) != want:
            try:
                fo, _fe, _frc = _sep(client, _wrap_login(
                    "find /usr/local /opt -maxdepth 4 -type f -name mysqldump "
                    "2>/dev/null | head -20"), timeout=30)
                ftxt = fo.decode("utf-8", "replace") if isinstance(fo, bytes) else str(fo or "")
                for cand in [c.strip() for c in ftxt.splitlines() if c.strip()]:
                    if cand != mysqldump_bin and _dump_flavor(cand) == want:
                        mysqldump_bin = cand
                        break
            except Exception:
                pass
    return mysqldump_bin


def _remote_server_flavor(client, task: dict, mysqldump_bin: str,
                          remote_cnf: str) -> str:
    """问目标端真实 DBMS 风味：``"mariadb"`` / ``"mysql"`` / ``""``（探测失败）。

    为什么不能信任务的 ``db_type``：客户常把 MariaDB 实例登记成 ``mysql``
    （反之亦然）。``--set-gtid-purged`` 只有 MySQL/Percona 的 mysqldump 认，
    一旦按错误风味拼参，MariaDB 目标会 100% 报
    ``unknown variable 'set-gtid-purged=OFF'``（rc=7）直接失败。
    """
    try:
        from core.engines.file import _ssh_exec_pipe as _sep
        cli = shlex.quote(os.path.dirname(mysqldump_bin) + "/mysql")
        port = int(task.get("port") or 3306)
        o, _e, _rc = _sep(
            client,
            _wrap_login(f"{cli} --defaults-file={shlex.quote(remote_cnf)} "
                        f"-h 127.0.0.1 -P {port} -N -e \"SELECT VERSION();\" "
                        f"2>/dev/null"), timeout=30)
        txt = o.decode("utf-8", "replace") if isinstance(o, bytes) else str(o or "")
        lines = [x.strip() for x in txt.strip().splitlines() if x.strip()]
        ver = lines[-1] if lines else ""
        if not ver:
            return ""
        return "mariadb" if "mariadb" in ver.lower() else "mysql"
    except Exception:  # noqa: BLE001 - 探测失败时按调用方的判断继续，不阻断备份
        return ""


def _build_mysql_dump_shell(client, task: dict, ssh_host: dict, mysqldump_bin: str,
                            remote_cnf: str, compress: int, extra_args: str = "") -> dict:
    """只负责「组装 mysqldump 命令行」，不负责执行。

    抽出来的原因：旧的「stdout 直传」与新的「远端落盘 + 断点续传」两条通道
    必须共用同一套参数，否则两边行为会慢慢漂移（--set-gtid-purged、压缩、
    XML 格式、schema_only/data_only 等一堆分支）。

    返回 dict：
      fmt     "single"（单库/多库/指定表）或 "multi-db-tar"（全实例逐库 tar）
      shell   未 wrap 的 shell 片段（fmt=multi-db-tar 时为空串）
      enable  是否启用 zstd 压缩
      suffix  建议落盘后缀（.sql.zst / .sql / .tar.gz）
      port    端口（全实例分支需要）
      extra   解析后的 extra_options（全实例分支需要）
    """
    db_name = task.get("db_name") or ""
    port = task.get("port") or 3306
    extra = task_extra_dict(task)
    # 按**目标端真实风味**纠偏参数：任务登记成 mysql 但实例其实是 MariaDB 时，
    # 这里必须剔掉 MariaDB 的 mysqldump 不认识的参数，否则 rc=7 必失败。
    flavor = _remote_server_flavor(client, task, mysqldump_bin, remote_cnf)
    if flavor == "mariadb" and "--set-gtid-purged" in (extra_args or ""):
        extra_args = " ".join(
            a for a in (extra_args or "").split()
            if not a.startswith("--set-gtid-purged"))
        logging.getLogger(__name__).warning(
            "[remote_dump] 任务登记的风味是 %s，但目标端实测为 MariaDB：已剔除 "
            "MariaDB 的 mysqldump 不支持的参数（--set-gtid-purged）",
            task.get("db_type") or "mysql")
    tables = [str(t).strip() for t in (extra.get("tables") or []) if str(t).strip()]
    schemas = [str(s).strip() for s in (extra.get("schemas") or []) if str(s).strip()]
    schema_only = bool(extra.get("schema_only"))
    data_only = bool(extra.get("data_only"))
    use_st = extra.get("single_transaction") is not False
    use_routines = extra.get("routines") is not False
    use_triggers = extra.get("triggers") is not False
    use_events = extra.get("events") is not False

    args = [
        mysqldump_bin, f"--defaults-file={remote_cnf}",
        "-h", "127.0.0.1", "-P", str(port),
    ]
    if use_st: args.append("--single-transaction")
    if use_routines: args.append("--routines")
    if use_triggers: args.append("--triggers")
    if use_events: args.append("--events")
    args.append("--default-character-set=utf8mb4")

    try:
        from core import dump_format as _dfmt
        _spec = _dfmt.resolve(task.get("db_type") or "mysql", extra, compress=bool(compress))
        if _spec.get("flag") == "--xml":
            args.append("--xml")
    except Exception:
        pass

    if tables:
        if not db_name:
            raise RuntimeError("指定表（tables）时必须同时填写 task.db_name 库名")
        args.append(db_name)
        args.extend(tables)
    elif schemas:
        args.append("--databases")
        args.extend(schemas)
    elif db_name:
        args.append("--databases")
        args.append(db_name)
    else:
        return {"fmt": "multi-db-tar", "shell": "", "enable": True,
                "suffix": ".tar.gz", "port": int(port), "extra": extra,
                "flavor": flavor}

    if schema_only:
        args.append("--no-data")
    if data_only:
        args.append("--no-create-info")
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
        try:
            import re as _re
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
            _maj, _min, _vres = (8, 0), ""
        if (_maj, _min) >= (5, 6) and "mariadb" not in _vres.lower():
            args.append("--set-gtid-purged=OFF")

    shell = "set -o pipefail; " + " ".join(shlex.quote(a) for a in args)
    enable = bool(compress) and getattr(config, "COMPRESS_BY_DEFAULT", True)
    try:
        clvl = int(task.get("compress_level") or 0)
    except (TypeError, ValueError):
        clvl = 0
    # 远端缺 zstd 时降级为不压缩（落盘后缀随之改为 .sql，恢复端可直接灌入）。
    # 注意：用**当前已建立的连接**探测，避免 remote_has_tool 另起连接失败时
    # 把"连不上"误判成"没装 zstd"——那会让大库备份白白丢掉压缩。
    if enable:
        _has_zstd = False
        try:
            from core.engines.file import _ssh_exec_pipe as _sep2
            _zo, _ze, _zrc = _sep2(client, _wrap_login("command -v zstd 2>/dev/null"), timeout=30)
            _ztxt = _zo.decode("utf-8", "replace") if isinstance(_zo, bytes) else str(_zo or "")
            _has_zstd = bool(_ztxt.strip()) and _zrc == 0
        except Exception:
            _has_zstd = remote_has_tool(ssh_host, "zstd")   # 兜底：另起连接再试一次
        if not _has_zstd:
            enable = False
            logging.getLogger(__name__).warning(
                "[remote_dump] 远端主机 %s 未检测到 zstd，降级为不压缩",
                ssh_host.get("host") or ssh_host.get("ip"))
    # 限速统一由平台侧实现（_ssh_exec_stream.rate_kbps），不再依赖远端 pv
    shell += _remote_compress_pipe(enable, clvl)
    return {"fmt": "single", "shell": shell, "enable": enable,
            "suffix": (".sql.zst" if enable else ".sql"),
            "port": int(port), "extra": extra}


def _remote_mysql_dump(task: dict, ssh_host: dict, compress: int, extra_args: str = "") -> tuple:
    """在远端数据库服务器以 mysqldump 导出，返回 (原始字节, 产物格式, 是否压缩)。

    支持四种备份范围（按 extra_options / task.db_name 自动判定）：
    1) extra.schemas 非空  → --databases schema1 schema2 ...
    2) extra.tables 非空   → --databases <db_name> table1 table2 ...
    3) task.db_name 非空   → --databases <db_name>
    4) 上述都为空          → 全实例（逐库 .sql → tar.gz + manifest，
                              默认排除系统库；extra.include_system_dbs=true 包含）

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

    # 1) 探测 mysqldump 真实路径（含 MySQL/MariaDB 风味校正）
    mysqldump_bin = _resolve_mysql_dump_bin(client, task, remote_cnf)

    # 2) 组装 mysqldump 命令行（与「远端落盘 + 断点续传」通道共用同一套参数，
    #    避免两条通道的 --set-gtid-purged / 压缩 / 格式分支各自漂移）
    spec = _build_mysql_dump_shell(client, task, ssh_host, mysqldump_bin,
                                   remote_cnf, compress, extra_args)
    if spec["fmt"] == "multi-db-tar":
        # 全实例：逐库 .sql → tar.gz（默认排除系统库）
        data = _remote_mysql_full_instance_tar(
            client, mysqldump_bin, remote_cnf, spec["port"], spec["extra"],
            spec.get("flavor") or "")
        return data, "multi-db-tar", True
    shell = spec["shell"]
    enable = spec["enable"]

    wrapped = _wrap_login(shell)
    p = transfer_params(task)
    try:
        from core.engines.file import _ssh_exec_pipe
        # 超时改为任务级（默认不限时长，只由空闲超时兜底），
        # 避免 10GB 级备份被写死的 3600s 砍掉后从头重跑。
        out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=p["cmd_timeout"])
        if rc != 0:
            raise RuntimeError(f"远程 mysqldump 失败(rc={rc}, bin={mysqldump_bin}): {err[:600]}")
        # 防御：mysqldump 异常时也可能产生极小的压缩产物
        # （zstd 帧头 ~15 字节；20 字节阈值覆盖空文件）
        if enable and len(out) <= 20:
            raise RuntimeError(
                f"远程 mysqldump 疑似失败：zstd 压缩后仅 {len(out)} 字节（stderr: {err[:200]}）"
            )
        return out, "single", enable
    finally:
        try:
            sftp.remove(remote_cnf)
        except Exception:
            pass
        sftp.close()


def _remote_pg_dump(task: dict, ssh_host: dict, compress: int) -> tuple:
    """在远端数据库服务器以 pg_dump 导出，返回 (原始字节, 产物格式)。

    支持多模式：
    1) extra.schemas 非空  → -n s1 -n s2 ...（多 schema）
    2) extra.tables 非空   → -t tbl1 -t tbl2 ...（限定表）
    3) task.db_name 非空   → -d <db>
    4) 上述都为空          → 全实例（逐库 tar + manifest；extra.all_db_mode
                              == "dumpall" 时改为 sys_dumpall 整实例 SQL 流）
    """
    return _pg_family_dump(task, ssh_host, "postgresql", compress)


def _remote_kingbase_dump(task: dict, ssh_host: dict, compress: int) -> tuple:
    """在远端数据库服务器以 sys_dump 导出，返回 (原始字节, 产物格式)。

    工具解析使用 resolve_remote_tool（kingbase 用户 profile 优先），兼容
    V8/V9 客户端工具不在 root PATH 的现场（如 ClientTools/Server 自定义目录）。
    全实例语义见 _pg_family_dump（PG 系不存在 --all-databases 参数）。
    """
    return _pg_family_dump(task, ssh_host, "kingbase", compress)


def _remote_opengauss_dump(task: dict, ssh_host: dict, compress: int) -> tuple:
    """在远端数据库服务器以 gs_dump 导出，返回 (原始字节, 产物格式)。

    openGauss 客户端工具通常只对 omm 用户可见（GAUSSHOME/bin），工具解析走
    resolve_remote_tool(check_user=omm)；gs_* 动态库依赖由 LD_LIBRARY_PATH 兜底。
    全实例语义见 _pg_family_dump（openGauss 无 --all-databases，逐库 tar.gz）。
    """
    return _pg_family_dump(task, ssh_host, "opengauss", compress)


# 旧实现占位（由下方 PG 系共用实现整体接管）：
# ---------------------------------------------------------------------------
# PG 系（PostgreSQL / KingbaseES）共用 dump 实现
# ---------------------------------------------------------------------------
# 关键修正：pg_dump/sys_dump 是单库工具，不存在 mysqldump 的 --all-databases
# 参数。全实例备份 = 枚举库 → 逐库 dump（-Fc，各自一致性快照）+
# dumpall --globals-only（角色/表空间等全局对象）→ 打包 tar.gz + manifest.json。

# 各库类型差异点（工具名候选 / 系统目录 / 维护库候选 / 密码环境变量）
_PG_FAMILY_TOOLING = {
    "postgresql": {
        "label": "PostgreSQL",
        "dump_tool": "pg_dump",
        "restore_candidates": ("pg_restore",),
        "query_candidates": ("psql",),
        "dumpall_candidates": ("pg_dumpall",),
        "catalog_table": "pg_database",
        "maint_candidates": ("postgres", "template1"),
        "default_port": 5432,
        "default_user": "postgres",
        "check_user": None,
        "env_exports": ("PGPASSWORD",),
        "db_type": "postgresql",
    },
    "kingbase": {
        "label": "KingbaseES",
        "dump_tool": "sys_dump",
        "restore_candidates": ("sys_restore", "pg_restore"),
        "query_candidates": ("ksql", "sys_psql", "psql"),
        "dumpall_candidates": ("sys_dumpall", "kb_dumpall", "ksy_dumpall", "pg_dumpall"),
        # V8/V009R003 系统目录为 sys_database；V9R1 为 pg_database——运行时探测
        "catalog_candidates": ("sys_database", "pg_database"),
        "maint_candidates": ("test", "postgres", "security", "template1"),
        "default_port": 54321,
        "default_user": "system",
        "check_user": "kingbase",
        # V8 兼容 PGPASSWORD；V9 起用 KINGBASE_PASSWORD，两个都注入最稳
        "env_exports": ("KINGBASE_PASSWORD", "PGPASSWORD"),
        "db_type": "kingbase",
    },
    # openGauss（华为开源高斯，PG 9.2 内核分支）：
    # - 工具族名为 gs_*：gs_dump/gs_dumpall/gs_restore/gsql
    # - 集群属主为 omm，客户端工具通常只在其 profile PATH 中可见 → check_user=omm
    # - gs_* 动态链接 GAUSSHOME/lib，远端执行需注入 LD_LIBRARY_PATH（set_lib_path）
    # - 密码统一走 PGPASSWORD（gsql/gs_dump/gs_basebackup 均支持）
    "opengauss": {
        "label": "openGauss",
        "dump_tool": "gs_dump",
        "restore_candidates": ("gs_restore",),
        "query_candidates": ("gsql", "psql"),
        "dumpall_candidates": ("gs_dumpall", "pg_dumpall"),
        "catalog_table": "pg_database",
        "maint_candidates": ("postgres", "template1"),
        "default_port": 5432,
        "default_user": "omm",
        "check_user": "omm",
        "env_exports": ("PGPASSWORD",),
        "db_type": "opengauss",
        "set_lib_path": True,
    },
}


def _pg_family_parse_extra(task: dict) -> dict:
    """解析 extra_options（dict 或 JSON 字符串），容忍脏数据。"""
    raw_eo = task.get("extra_options")
    if isinstance(raw_eo, dict):
        return raw_eo
    if isinstance(raw_eo, str) and raw_eo.strip():
        try:
            return json.loads(raw_eo)
        except Exception:
            return {}
    return {}


def _pg_family_env_exports(cfg: dict, pw: str, bin_path: str = "") -> str:
    """密码只走环境变量，不进 argv（各类型注入自己认的环境变量集）。

    openGauss（cfg.set_lib_path）额外注入 GAUSSHOME/PATH/LD_LIBRARY_PATH：
    gs_* 客户端工具默认从 <GAUSSHOME>/lib 加载动态库，且 GAUSSHOME 往往只在
    omm 用户的 profile 中定义；SSH 非登录 shell 下直接调用绝对路径会报
    "error while loading shared libraries"，故按工具路径反推 GAUSSHOME 兜底。
    """
    exports = " ".join(f"export {e}={shlex.quote(pw)};" for e in cfg["env_exports"])
    if cfg.get("set_lib_path"):
        if bin_path:
            guess = f"$(dirname $(dirname {shlex.quote(bin_path)}))"
        else:
            guess = '"${GAUSSHOME:-}"'
        exports += (
            f' [ -n "${{GAUSSHOME:-}}" ] || export GAUSSHOME={guess};'
            ' export LD_LIBRARY_PATH="$GAUSSHOME/lib:${LD_LIBRARY_PATH:-}";'
            ' export PATH="$GAUSSHOME/bin:$PATH";'
        )
    return exports


def _pg_family_resolve_query_bin(client, cfg) -> str:
    """定位交互式 SQL 客户端（ksql/sys_psql/psql），用于枚举库与建库。"""
    for name in cfg["query_candidates"]:
        p = _resolve_remote_bin(client, name)
        if p:
            return p
    return ""


def _pg_family_resolve_dumpall_bin(client, cfg) -> str:
    """定位 dumpall 工具（sys_dumpall/kb_dumpall/pg_dumpall，版本间命名不一）。"""
    for name in cfg["dumpall_candidates"]:
        p = _resolve_remote_bin(client, name)
        if p:
            return p
    return ""


def _pg_family_full_instance_tar(client, cfg: dict, dump_bin: str,
                                 user: str, pw: str, port: int,
                                 include_sys: bool = False,
                                 tool_path: str = "") -> bytes:
    """全实例备份：一次 SSH 会话内逐库 dump + globals + manifest → tar.gz 流。

    每库 -Fc（独立一致性快照，支持并行恢复）；单库失败即整体失败（rc=32）；
    dumpall 缺失/失败仅降级跳过 globals（在 manifest 中标记），不阻塞备份。
    默认排除系统库（SYSTEM_DBS），include_sys=True 时包含。
    """
    from core.engines.file import _ssh_exec_pipe

    query_bin = _pg_family_resolve_query_bin(client, cfg)
    if not query_bin:
        raise RuntimeError(
            f"远端主机未找到 SQL 客户端（{'/'.join(cfg['query_candidates'])}），"
            "无法枚举数据库清单以执行全实例备份。")
    dumpall_bin = _pg_family_resolve_dumpall_bin(client, cfg)

    env = _pg_family_env_exports(cfg, pw, bin_path=dump_bin)
    # 系统目录表：V8/V009R003=sys_database、V9R1=pg_database —— 候选探测
    catalogs = cfg.get("catalog_candidates") or (cfg["catalog_table"],)
    maints = " ".join(cfg["maint_candidates"])
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # 备份记录归属的 db_type：优先取 cfg 显式声明（openGauss 等新增类型）
    db_type = cfg.get("db_type") or (
        "kingbase" if cfg["dump_tool"].startswith("sys_") else "postgresql")

    # 系统库排除：SQL 层 NOT IN 过滤（include_sys 时不过滤）
    sys_dbs = SYSTEM_DBS.get(db_type) or ()
    if include_sys or not sys_dbs:
        cond = "NOT datistemplate"
    else:
        excl = ",".join(f"'{d}'" for d in sys_dbs)
        cond = f"NOT datistemplate AND datname NOT IN ({excl})"

    lines = [
        "set -eu",
        env,
        _tool_path_export(tool_path) or "true;",
        f"DUMP_BIN={shlex.quote(dump_bin)}",
        f"QUERY_BIN={shlex.quote(query_bin)}",
        f"DUMPALL_BIN={shlex.quote(dumpall_bin or '')}",
        f"PORT={int(port)}",
        f"USERQ={shlex.quote(user)}",
        'WORK=$(mktemp -d /tmp/bp_fullinst.XXXXXX)',
        "trap 'rm -rf \"$WORK\"' EXIT",
        'mkdir -p "$WORK/dbs"',
        f'MAINTS="{maints}"',
        f'CATALOGS="{(" ".join(catalogs))}"',
        'DBS=""',
        "for MDB in $MAINTS; do",
        '  for CAT in $CATALOGS; do',
        f'    if DBS=$("$QUERY_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -d "$MDB" -t -A '
        f'-c "SELECT datname FROM $CAT WHERE {cond} ORDER BY 1" 2>/dev/null) '
        '&& [ -n "$DBS" ]; then MAINT="$MDB"; break 2; fi',
        "  done",
        "done",
        '[ -n "${DBS:-}" ] || { echo "no backupable databases after filtering'
        ' (system dbs excluded; set include_system_dbs=true to include)" >&2; exit 31; }',
        'for d in $DBS; do',
        '  "$DUMP_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -Fc -f "$WORK/dbs/$d.dump" "$d"'
        ' || { echo "dump failed for db $d" >&2; exit 32; }',
        "done",
        'GLOBALS="no"',
        'if [ -n "$DUMPALL_BIN" ]; then',
        '  if "$DUMPALL_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -g > "$WORK/globals.sql" '
        '2>"$WORK/globals.err" && [ -s "$WORK/globals.sql" ]; then GLOBALS="yes"; '
        'else GLOBALS="failed"; fi',
        "fi",
        'DBJSON=$(printf \'%s\\n\' "$DBS" | awk \'BEGIN{ORS="";first=1} '
        '{if(!first)print ","; printf "\\"%s\\"",$0; first=0}\')',
        f'printf \'{{"format":"multi-db-tar","db_type":"{db_type}",'
        '"generated_at":"' + ts + '","globals":"%s","include_system_dbs":'
        + ("true" if include_sys else "false") + ',"databases":[%s]}\' '
        '"$GLOBALS" "$DBJSON" > "$WORK/manifest.json"',
        'if [ -s "$WORK/globals.sql" ]; then',
        '  tar -czf - -C "$WORK" manifest.json dbs globals.sql',
        "else",
        '  tar -czf - -C "$WORK" manifest.json dbs',
        "fi",
    ]
    script = "\n".join(lines)
    wrapped = _wrap_login(script)
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
    if rc != 0:
        raise RuntimeError(
            f"远程全实例备份失败(rc={rc}, bin={dump_bin}): {err[:800]}")
    if len(out) <= 100:
        raise RuntimeError(
            f"远程全实例备份疑似失败：tar 流仅 {len(out)} 字节（stderr: {err[:200]}）")
    return out


def _pg_family_dumpall_stream(client, cfg: dict,
                              user: str, pw: str, port: int,
                              tool_path: str = "") -> bytes:
    """整实例 SQL 流模式（extra.all_db_mode="dumpall"）：dumpall 直接输出。

    纯 SQL 文本、单文件；大库恢复较慢，但最贴近原生全实例语义。
    dumpall 工具缺失时报错并给出说明。
    """
    from core.engines.file import _ssh_exec_pipe
    dumpall_bin = _pg_family_resolve_dumpall_bin(client, cfg)
    if not dumpall_bin:
        raise RuntimeError(
            f"远端主机未找到 dumpall 工具（{'/'.join(cfg['dumpall_candidates'])}）。"
            "可在 extra_options 中去掉 all_db_mode 使用默认逐库 tar 模式。")
    env = _pg_family_env_exports(cfg, pw, bin_path=dumpall_bin)
    shell = (
        f"set -o pipefail; {_tool_path_export(tool_path)}{env} "
        f"{dumpall_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)}"
    )
    out, err, rc = _ssh_exec_pipe(client, _wrap_login(shell), timeout=7200)
    if rc != 0:
        raise RuntimeError(
            f"远程 dumpall 失败(rc={rc}, bin={dumpall_bin}): {err[:600]}")
    return out


def _pg_family_dump(task: dict, ssh_host: dict, db_type: str, compress: int) -> tuple:
    """PG 系统一 dump 入口。返回 (data, fmt)：

    - fmt="single"       单库/多表/多 schema dump（-Fc 自带压缩或 -Fp 纯文本）
    - fmt="multi-db-tar" 全实例逐库 tar.gz（含 manifest.json + globals.sql）
    - fmt="dumpall"      整实例 SQL 流（extra.all_db_mode="dumpall" 时）
    """
    cfg = _PG_FAMILY_TOOLING[db_type]
    client = _connect(ssh_host)
    tp = task_tool_path(task)
    dump_bin = resolve_remote_tool(
        ssh_host, cfg["dump_tool"], check_user=cfg["check_user"], extra_paths=tp)
    if not dump_bin:
        raise RuntimeError(
            f"远端主机未找到 {cfg['dump_tool']}"
            "（root/数据库用户 PATH 与常见安装目录均无）。"
            "请确认数据库服务端/客户端工具已安装后重试。")

    user = task.get("username") or cfg["default_user"]
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""
    port = int(task.get("port") or cfg["default_port"])
    # 导出格式：由任务 extra_options.dump_format 决定（auto 时保持历史行为：
    # 开压缩 -Fc / 未开压缩 -Fp）。目录格式 -Fd 需在远端打包成 tar 流再拉回。
    from core import dump_format as _dfmt
    extra0 = _pg_family_parse_extra(task)
    spec = _dfmt.resolve(db_type, extra0, compress=bool(compress))
    fmt_flag = spec.get("flag") or ("-Fc" if compress else "-Fp")
    is_dir_fmt = bool(spec.get("dir"))
    extra = extra0
    schemas = [str(s).strip() for s in (extra.get("schemas") or []) if str(s).strip()]
    tables = [str(t).strip() for t in (extra.get("tables") or []) if str(t).strip()]

    # ---- 全实例：逐库 tar / dumpall（勾选全部库或库名为空，且未指定表/schema）----
    if ((extra.get("use_all_db") or not db_name)
            and not schemas and not tables):
        # 默认仅备份业务库（排除系统库），extra.include_system_dbs=true 时包含
        include_sys = bool(extra.get("include_system_dbs"))
        if (extra.get("all_db_mode") or "").strip().lower() == "dumpall":
            return _pg_family_dumpall_stream(
                client, cfg, user, pw, port, tool_path=tp), "dumpall"
        return _pg_family_full_instance_tar(
            client, cfg, dump_bin, user, pw, port, include_sys,
            tool_path=tp), "multi-db-tar"

    # ---- 单库/多表/多 schema（原有行为）----
    # 注意：不使用 "-f -"（显式指定 stdout）。某些环境下 pg_dump 的 "-f -"
    # 参数异常导致输出 0 字节；不带 -f 时默认输出 stdout，行为一致且兼容性更好。
    env = _pg_family_env_exports(cfg, pw, bin_path=dump_bin)
    base = (
        f"set -o pipefail; {_tool_path_export(tp)}{env} "
        f"{dump_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} {fmt_flag}"
    )

    if tables:
        if not db_name:
            raise RuntimeError("指定表（tables）时必须同时填写库名（db_name）")
        target_args = f"-d {shlex.quote(db_name)} " + " ".join(
            f"-t {shlex.quote(t)}" for t in tables)
    elif schemas:
        target_args = " ".join(f"-n {shlex.quote(s)}" for s in schemas)
    else:
        target_args = f"-d {shlex.quote(db_name)}"

    extra_args = ""
    if extra.get("extra_args"):
        try:
            shlex.split(str(extra["extra_args"]))
            extra_args = " " + str(extra["extra_args"])
        except Exception:
            pass

    if is_dir_fmt:
        # 目录格式（-Fd）：远端先 dump 到临时目录，再打成 tar.gz 流拉回；
        # 落盘后缀由调用方按 dump_format 决定（.dir.tar.gz）。
        work = f"/tmp/.bk_pgdir_{os.getpid()}_{int(time.time())}"
        shell = (
            f"set -o pipefail; {_tool_path_export(tp)}{env} "
            f"rm -rf {work} && mkdir -p {work}/db && "
            f"{dump_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} {fmt_flag} "
            f"-f {work}/db {target_args}{extra_args} && "
            f"tar -C {work} -czf - db; RC=$?; rm -rf {work}; exit $RC"
        )
    else:
        shell = f"{base} {target_args}{extra_args}"
    # 压缩策略：-Fc 自带 zlib 压缩，不外挂 gzip/zstd 以免双重压缩；
    # compress 时落盘 .dump，恢复端用 pg_restore/sys_restore。
    wrapped = _wrap_login(shell)
    from core.engines.file import _ssh_exec_pipe
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=3600)
    if rc != 0:
        raise RuntimeError(
            f"远程 {cfg['dump_tool']} 失败(rc={rc}, bin={dump_bin}): {err[:600]}")
    if compress and len(out) <= 20:
        raise RuntimeError(
            f"远程 {cfg['dump_tool']} 疑似失败：-Fc 压缩后仅 {len(out)} 字节"
            f"（stderr: {err[:200]}）")
    return out, "single"



def _remote_redis_dump(task: dict, ssh_host: dict) -> bytes:
    """在远端备份机执行 redis-cli --rdb -，把 RDB 数据流拉回到本地。"""
    client = _connect(ssh_host)
    redis_cli = _resolve_remote_bin(client, "redis-cli")
    if not redis_cli:
        raise RuntimeError(
            "远端主机未找到 redis-cli，按数据库服务器零安装原则，"
            "将回退平台服务端 redis-cli 直连远程执行。"
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
            "远端主机未找到 mongodump，按数据库服务器零安装原则，"
            "将回退平台服务端 mongodump 直连远程执行。"
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

    # 导出格式（extra_options.dump_format）：archive(默认单文件归档) /
    # gzip(mongodump 自带 --gzip 压缩归档) / directory(--out 目录，远端打 tar.gz 流)
    from core import dump_format as _dfmt
    spec = _dfmt.resolve("mongodb", _pg_family_parse_extra(task), compress=bool(compress))
    base_shell = f"{mongodump} --host {shlex.quote(host)} --port {port}{auth_args}{db_args}"
    if spec.get("dir"):
        work = f"/tmp/.bk_mongo_{os.getpid()}_{int(time.time())}"
        shell = (f"set -o pipefail; rm -rf {work} && mkdir -p {work} && "
                 f"{base_shell} --out {work} && tar -C {work} -czf - .; RC=$?; "
                 f"rm -rf {work}; exit $RC")
        wrapped = _wrap_login(shell)
        from core.engines.file import _ssh_exec_pipe
        # 超时改为任务级（默认不限时长，只由空闲超时兜底），避免大库被 3600s 砍掉后重跑
        out, err, rc = _ssh_exec_pipe(
            client, wrapped, timeout=transfer_params(task)["cmd_timeout"])
        if rc != 0:
            raise RuntimeError(f"远程 mongodump(目录格式)失败(rc={rc}, bin={mongodump}): {err[:600]}")
        if len(out) <= 100:
            raise RuntimeError(f"远程 mongodump(目录格式)疑似失败：仅 {len(out)} 字节"
                               f"（stderr: {err[:200]}）")
        return out, False, "single"
    if spec.get("value") == "gzip":
        # mongodump 自带 gzip：不再外挂 zstd，避免双重压缩
        shell = base_shell + " --archive --gzip"
        wrapped = _wrap_login(shell)
        from core.engines.file import _ssh_exec_pipe
        # 超时改为任务级（默认不限时长，只由空闲超时兜底），避免大库被 3600s 砍掉后重跑
        out, err, rc = _ssh_exec_pipe(
            client, wrapped, timeout=transfer_params(task)["cmd_timeout"])
        if rc != 0:
            raise RuntimeError(f"远程 mongodump 失败(rc={rc}, bin={mongodump}): {err[:600]}")
        if len(out) <= 20:
            raise RuntimeError(f"远程 mongodump 疑似失败：仅 {len(out)} 字节（stderr: {err[:200]}）")
        return out, False, "single"

    shell = base_shell + " --archive"
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
    return out, bool(compress), "single"


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


# --------------------------------------------------------------------------- #
# 远端落盘 + 断点续传（10GB 级备份能否跑完的关键）
#
# 背景：旧实现把 mysqldump 的 stdout 直接经 SSH 管道拉回平台内存。管道是流式
# 的，**没有偏移量语义**——一旦超时/断链，只能从头重跑 dump。用户实测 10GB
# 库在 600KB/s 链路上传了 1.4GB 后被 3600s 超时砍掉，重试又从 0 开始，永远跑不完。
#
# 现改为两段式：
#   1) 远端后台导出（setsid nohup，SSH 断开也继续，不占平台带宽）
#   2) 平台按 offset 增量拉取（边导出边传），中断后从已拉字节继续，**不重跑导出**
# --------------------------------------------------------------------------- #

_SFTP_READ_CHUNK = 4 * 1024 * 1024


def _remote_stage_dir() -> str:
    """远端暂存目录（关键点：不能装任何东西，只是临时落盘）。"""
    return getattr(config, "BACKUP_REMOTE_STAGE", "/tmp/bk_stage")


def cleanup_remote_stage_dirs(client, *paths, timeout: int = 40) -> None:
    """回收平台自己在远端 mkdir 出来的暂存目录（**只删空目录**）。

    背景：产物拉回后我们会 ``rm -rf`` 掉产物本身，但 ``/tmp/bk_stage`` 这类由平台
    创建的目录会留下空壳。它虽然不占空间，却与「目标端零残留」的承诺不符，也会被
    ``core.agentless.audit`` 的 A2 分节如实判为 WARN。

    刻意用 ``rmdir`` 而非 ``rm -rf``：只要目录里还有本平台其他任务的产物、
    **或客户自己的任何东西**，系统就会拒绝删除（Directory not empty），
    因此不会误删，也不需要为并发加锁。
    """
    from core.engines.file import _ssh_exec_pipe as _pipe   # 局部导入避免循环依赖

    log = logging.getLogger(__name__)
    for d in _stage_dir_candidates(paths):
        try:
            _out, err, rc = _pipe(client, _wrap_login(
                "rmdir %s" % shlex.quote(d)), timeout=timeout)
            err = (err or "").strip() if isinstance(err, str) else \
                (err.decode("utf-8", "replace").strip() if err else "")
            log.info("[remote_dump] 回收远端空目录 %s: %s",
                     d, "已回收" if rc == 0 else "保留（%s）" % (err[:120] or rc))
        except Exception as e:  # noqa: BLE001 - best-effort，失败不影响备份结果
            log.warning("[remote_dump] 回收远端空目录 %s 异常: %s", d, str(e)[:120])


def _stage_dir_candidates(paths) -> list[str]:
    """把产物路径展开成"平台自己创建的那几层目录"，从深到浅去重。

    **绝不越过根级目录**：像 ``/tmp``、``/var`` 这类由操作系统/客户使用的目录，
    一律不作为候选（早期实现曾一路展开到 ``/tmp`` 本身并在其为空时被 rmdir 掉，
    ——已修。这里的要求是：宁可留一个空目录，也不能碰别人的目录。
    """
    out: list[str] = []
    for raw in paths or ():
        cur = str(raw or "").rstrip("/")
        cur = cur.rsplit("/", 1)[0] if "/" in cur else ""   # 产物本身是文件，只要它的上级目录
        while cur:
            parent = cur.rsplit("/", 1)[0]
            if (not parent) or parent == "/" or cur.count("/") < 2:
                break
            if cur not in out:
                out.append(cur)
            cur = parent
    return out


def _resume_part_path(final_path: str, task: dict, db_type: str, key: str = "") -> str:
    """本地半成品路径：**与时间戳无关**，否则重试换了产物名就命中不了断点。

    key: 同一任务可能有多份产物（RMAN 多个备份片 / PG 多个 tar / 脚本产物多文件），
         用 key 区分，避免互相覆盖断点。
    """
    d = os.path.dirname(final_path) or "."
    suffix = ""
    if key:
        suffix = "_" + re.sub(r"[^0-9A-Za-z._-]", "_", str(key))[:60]
    return os.path.join(d, f".resume_t{int(task.get('id') or 0)}_{db_type}{suffix}.part")


def _meta_read(part_path: str) -> dict:
    try:
        if not os.path.exists(part_path + ".meta"):
            return {}
        with open(part_path + ".meta", "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _meta_write(part_path: str, meta: dict) -> None:
    try:
        tmp = part_path + ".meta.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, part_path + ".meta")
    except Exception:
        pass


def _meta_drop(part_path: str) -> None:
    for p in (part_path, part_path + ".meta", part_path + ".meta.tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


def _meta_usable(meta: dict, ssh_host: dict, spec: dict, p: dict) -> bool:
    """断点是否可续用：同一主机 + 压缩/格式参数一致 + 未过期。"""
    if not meta.get("remote_path"):
        return False
    if (meta.get("host") or "") != (ssh_host.get("host") or ssh_host.get("ip") or ""):
        return False
    if bool(meta.get("compressed")) != bool(spec.get("enable")):
        return False
    if (meta.get("suffix") or "") != (spec.get("suffix") or ""):
        return False
    try:
        age = time.time() - float(meta.get("started_at") or 0)
    except (TypeError, ValueError):
        age = 0
    return age <= float(p.get("resume_ttl") or 43200)


def _remote_breakpoint_alive(sftp, meta: dict) -> bool:
    """远端断点是否还活着：`.part` 存在且长度不小于本地已传偏移。

    被判死的典型场景：客户清了 /tmp、目标机重启、或上一次断点被外部/上一次失败
    清理过，而本地 meta 还在（且在 TTL 内）。此时若照旧走「续传」，就会一直等一个
    永远不会出现的产物，每次都超时失败——实测线上任务连续多轮失败且报错里的
    remote_path 始终是同一个远古 tag。这种情况必须作废断点、重新导出。
    """
    rp = meta.get("remote_path")
    if not rp:
        return False
    try:
        st = sftp.stat(rp)
        rsize = int(st.st_size or 0)
    except Exception:  # noqa: BLE001 - 取不到 = 远端产物已不存在
        return False
    try:
        offset = int(meta.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    if offset and rsize < offset:      # 远端产物比已传的还短 → 被截断/重建过
        return False
    return True


def _cleanup_remote(sftp, meta: dict) -> None:
    """清理远端暂存产物（成功/断点作废时；best-effort，失败不影响主流程）。

    除了产物自身，还会顺带回收平台**自己创建的**那几层暂存目录
    （``/tmp/bk_stage/fixed`` 之类）：只回收已经变成空目录的层，里面只要还有
    任何东西（客户的、其他任务的、未完成的续传分片）系统都会拒绝删除。
    """
    paths = [meta.get(k) for k in ("remote_path", "rc_path", "err_path", "sh_path")]
    for path in paths:
        if not path:
            continue
        try:
            sftp.remove(path)
        except Exception:  # noqa: BLE001 - best-effort
            pass
    for d in _stage_dir_candidates(paths):
        try:
            sftp.rmdir(d)
        except Exception:  # noqa: BLE001 - 非空目录会失败，正是我们要的保护
            pass


def _remote_rc_ready(sftp, rc_path: str):
    """远端导出是否结束：返回 (ready, rc)。rc!=0 表示导出失败。"""
    try:
        st = sftp.stat(rc_path)
        if int(st.st_size or 0) <= 0:
            return False, -1
        with sftp.open(rc_path, "r") as f:
            raw = f.read().decode("utf-8", "replace").strip()
        if not raw:
            return False, -1
        return True, int(raw.splitlines()[-1])
    except (IOError, OSError):
        return False, -1
    except Exception:
        return False, -1


def _sftp_read_tail(sftp, path: str, limit: int = 800) -> str:
    try:
        with sftp.open(path, "r") as f:
            txt = f.read().decode("utf-8", "replace")
        return txt[-limit:]
    except Exception:
        return ""


def _start_remote_dump(client, sftp, spec: dict, stage: str, sh_path: str,
                       remote_path: str, err_path: str, rc_path: str) -> None:
    """在远端后台启动 dump（setsid 脱离 SSH 会话，平台断线也不中断导出）。"""
    # 先确保暂存目录存在：SFTP 不会递归创建父目录，否则写脚本直接 ENOENT
    try:
        sftp.stat(stage)
    except Exception:
        try:
            sftp.mkdir(stage)
        except Exception:
            pass
    # exec 2> err：整条管道（含 mysqldump 自身）的 stderr 都要落文件。
    # 之前把 2> 写在管道末尾只捕获了 zstd 的 stderr，导出失败时查不到原因。
    script = (
        "#!/bin/bash\n"
        f"exec 2> {shlex.quote(err_path)}\n"
        f"{spec['shell']} > {shlex.quote(remote_path)}\n"
        f"RC=$?\n"
        f"echo $RC > {shlex.quote(rc_path)}\n"
        "exit $RC\n"
    )
    with sftp.open(sh_path, "wb") as rf:
        rf.write(script.encode("utf-8"))
    try:
        sftp.chmod(sh_path, 0o700)
    except Exception:
        pass
    from core.engines.file import _ssh_exec_pipe
    start_cmd = _wrap_login(
        f"mkdir -p {shlex.quote(stage)} && rm -f {shlex.quote(rc_path)} && "
        f"setsid nohup bash -lc {shlex.quote(sh_path)} </dev/null >/dev/null 2>&1 & "
        f"sleep 1; echo started")
    _o, _e, _rc = _ssh_exec_pipe(client, start_cmd, timeout=60)
    logging.getLogger(__name__).info(
        "[remote_dump] 远端后台导出已启动: %s -> %s", sh_path, remote_path)


def _sftp_pull_incremental(sftp, remote_path: str, rc_path: str, local_path: str,
                           meta: dict, p: dict, label: str = "",
                           rate_kbps: int = 0, has_rc: bool = True,
                           stable_secs: float = None) -> int:
    """按 offset 增量拉取远端文件：边导出边传，中断后从断点继续。

    has_rc: 远端是否有「完成标记」（rc 文件）。逻辑备份产物由 dump 脚本写 rc；
            **固定产物**（xtrabackup / pg_basebackup / tar 流 / expdp .dmp /
            SQL Server .bak 等）没有 rc，改由 stable_secs 判定写盘结束。
    stable_secs: 固定产物专用——远端文件连续 N 秒不再增长即认为写完。
                 默认取任务级 stable_secs（config.BACKUP_STABLE_SECS，20s）。

    返回已拉取的总字节数（含断点前已有的部分）。
    """
    import time as _t
    has_rc = bool(has_rc and rc_path)
    try:
        stable_secs = float(stable_secs if stable_secs is not None
                            else (p.get("stable_secs") or 20))
    except (TypeError, ValueError):
        stable_secs = 20.0
    _stable_since = None
    offset = int(meta.get("offset") or 0)
    if os.path.exists(local_path):
        try:
            real = os.path.getsize(local_path)
            if real < offset:
                offset = real          # 本地半成品被截断过 → 以实际大小为准
        except OSError:
            offset = 0
    else:
        offset = 0

    # 远端刚启动时产物文件可能还没创建 → 等它出现（最长等 60s，期间若已判失败则报错）
    rf = None
    _wait_start = _t.time()
    while rf is None:
        try:
            rf = sftp.open(remote_path, "rb")
            break
        except (IOError, OSError):
            if has_rc:
                _ready0, _rc0 = _remote_rc_ready(sftp, rc_path)
                if _ready0:
                    raise RuntimeError(
                        f"远端导出已结束(rc={_rc0})但产物文件不存在，无法续传")
                _wait_limit = 60
            else:
                _wait_limit = 300          # 固定文件：物理备份可能仍在补齐
            if _t.time() - _wait_start > _wait_limit:
                raise RuntimeError(
                    f"等待远端产物生成超时（{_wait_limit}s）：{remote_path}")
            _t.sleep(0.5)
    try:
        # paramiko 默认单请求 32KB，大文件下请求往返成为瓶颈 → 调大到 512KB
        try:
            rf.MAX_REQUEST_SIZE = 512 * 1024
        except Exception:
            pass
        base_offset = offset      # 限速预算起点（续传时已有进度不计入）
        lf = open(local_path, "ab" if offset else "wb")
        start = _t.time()
        last_hb = start
        hb_bytes = offset
        idle_since = _t.time()
        meta["offset"] = offset
        _meta_write(local_path, meta)
        try:
            while True:
                try:
                    rsize = int(sftp.stat(remote_path).st_size)
                except (IOError, OSError):
                    rsize = 0
                if rsize > offset:
                    rf.seek(offset)
                    while offset < rsize:
                        chunk = rf.read(min(_SFTP_READ_CHUNK, rsize - offset))
                        if not chunk:
                            break
                        lf.write(chunk)
                        offset += len(chunk)
                        idle_since = _t.time()
                        # 平台侧限速（KB/s）：按"应耗时"补 sleep，不依赖远端 pv
                        if rate_kbps > 0:
                            _expect = (offset - base_offset) / (rate_kbps * 1024.0)
                            _drift = _expect - (_t.time() - start)
                            if _drift > 0.05:
                                _t.sleep(min(_drift, 2.0))
                        now = _t.time()
                        if now - last_hb >= 30:
                            spd = (offset - hb_bytes) / max(now - last_hb, 0.001) / (1024 * 1024)
                            try:
                                logging.getLogger(__name__).info(
                                    "[remote_dump] 拉取进度%s: 已传 %s，速率 %.1f MB/s",
                                    f"（{label}）" if label else "",
                                    db.human_size(offset), spd)
                                from core import oplog
                                op = oplog.current()
                                if op:
                                    op.info("备份数据拉取进度: 已传 %s（速率 %.1f MB/s，耗时 %.0fs）%s",
                                            db.human_size(offset), spd, now - start,
                                            f"[{label}]" if label else "")
                            except Exception:
                                pass
                            last_hb = now
                            hb_bytes = offset
                            lf.flush()
                            meta["offset"] = offset
                            _meta_write(local_path, meta)
                    lf.flush()
                    meta["offset"] = offset
                    _meta_write(local_path, meta)
                now = _t.time()
                if has_rc:
                    ready, rc = _remote_rc_ready(sftp, rc_path)
                    if ready:
                        if rc != 0:
                            break
                        try:
                            rsize2 = int(sftp.stat(remote_path).st_size)
                        except (IOError, OSError):
                            rsize2 = offset
                        if offset >= rsize2:
                            break
                        continue
                else:
                    # 固定文件（物理备份产物 / expdp dmp / .bak 等）：已读满且
                    # 连续 stable_secs 秒不再增长 → 认为远端写盘结束
                    try:
                        rsize2 = int(sftp.stat(remote_path).st_size)
                    except (IOError, OSError):
                        rsize2 = offset
                    if offset >= rsize2:
                        if _stable_since is None:
                            _stable_since = now
                        elif (now - _stable_since) >= stable_secs:
                            break
                    else:
                        _stable_since = None
                if p.get("cmd_timeout") and (now - start) > float(p["cmd_timeout"]):
                    raise RuntimeError(
                        f"备份传输超时（{p['cmd_timeout']}s，已传 {db.human_size(offset)}），"
                        f"断点已保留，下一次执行自动续传")
                if p.get("idle_timeout") and (now - idle_since) > float(p["idle_timeout"]):
                    raise RuntimeError(
                        f"远端导出空闲超时（{p['idle_timeout']}s 无新增数据，"
                        f"已传 {db.human_size(offset)}）")
                _t.sleep(0.5)
        finally:
            lf.close()
            meta["offset"] = offset
            _meta_write(local_path, meta)
    finally:
        try:
            rf.close()
        except Exception:
            pass
    return offset


# --------------------------------------------------------------------------- #
# 固定产物续传：物理备份 tar 流 / Oracle Data Pump .dmp / SQL Server .bak /
#              达梦备份集 tar / 自定义脚本产物 / 文件备份归档
#
# 与逻辑备份（mysqldump 等）的差别只有两点：
#   1) **不产生 rc 标记文件** → 改用「远端文件连续 stable_secs 秒不再增长」判定
#      写盘结束（通道层 _sftp_pull_incremental 已支持该分支）；
#   2) 产物生成代价极高（xtrabackup 可能跑数小时）→ 拉回中断时**绝不能重跑备份**。
#      因此远端路径对同一任务**确定性**，且备份成功后写 `.bkdone` 完成标记；
#      重试时先探测标记，命中则跳过备份执行，直接从断点续传拉回。
# --------------------------------------------------------------------------- #

def _remote_fixed_dir() -> str:
    """固定产物的远端目录（与逻辑备份暂存分开，便于排查与清理）。"""
    return _remote_stage_dir().rstrip("/") + "/fixed"


def fixed_remote_path(task: dict, db_type: str, name: str) -> str:
    """固定产物的确定性远端路径（**不含随机时间戳**）。"""
    tid = int(task.get("id") or 0)
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(name or "artifact"))[:80]
    return f"{_remote_fixed_dir()}/t{tid}_{db_type}_{safe}"


def marker_path(target: str, is_dir: bool = False) -> str:
    """完成标记路径：文件 → `<path>.bkdone`；目录 → `<dir>/.bkdone`。"""
    t = str(target or "").rstrip("/")
    return f"{t}/.bkdone" if is_dir else f"{t}.bkdone"


def remote_artifact_ready(client, target: str, is_dir: bool = False,
                          min_size: int = 0) -> dict:
    """探测远端固定产物能否直接复用（跳过重跑备份）。

    返回 {exists, size, done, reusable}：
      exists   产物存在且有内容
      done     存在完成标记（上次备份已跑完）
      reusable exists and done and size >= min_size
    """
    out = {"exists": False, "size": 0, "done": False, "reusable": False}
    sftp = None
    try:
        sftp = client.open_sftp()
        try:
            if is_dir:
                sizes = [int(a.st_size or 0) for a in sftp.listdir_attr(target)
                         if not a.filename.startswith(".")]
                out["size"] = sum(sizes)
                out["exists"] = bool(sizes)
            else:
                out["size"] = int(sftp.stat(target).st_size or 0)
                out["exists"] = out["size"] > 0
        except (IOError, OSError):
            out["exists"] = False
        try:
            out["done"] = int(sftp.stat(marker_path(target, is_dir)).st_size or 0) > 0
        except (IOError, OSError):
            out["done"] = False
        out["reusable"] = bool(out["exists"] and out["done"]
                               and out["size"] >= int(min_size or 0))
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[remote_dump] 探测远端产物失败(%s): %s", target, e)
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
    return out


def mark_remote_artifact_done(client, target: str, is_dir: bool = False,
                              size: int = -1, note: str = "") -> None:
    """写远端完成标记（等价于逻辑备份的 rc 文件），供重试时判定可复用。"""
    sftp = None
    try:
        sftp = client.open_sftp()
        with sftp.open(marker_path(target, is_dir), "w") as f:
            f.write(f"size={int(size)}\n{note}\n")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[remote_dump] 写完成标记失败(%s): %s", target, e)
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass


def _sftp_rmtree(sftp, path: str) -> None:
    """递归删除远端目录（best-effort；远端不留任何平台产物）。"""
    try:
        attrs = sftp.listdir_attr(path)
    except Exception:
        attrs = None
    if attrs is None:
        try:
            sftp.remove(path)
        except Exception:
            pass
        return
    for a in attrs:
        child = f"{path.rstrip('/')}/{a.filename}"
        try:
            import stat as _stat
            if _stat.S_ISDIR(a.st_mode or 0):
                _sftp_rmtree(sftp, child)
            else:
                sftp.remove(child)
        except Exception:
            pass
    try:
        sftp.rmdir(path)
    except Exception:
        pass


def cleanup_remote_artifacts(client, targets, dirs=()) -> None:
    """清理远端固定产物与完成标记（成功拉回后调用；best-effort）。

    设计约束（用户硬性要求）：绝不把备份工具/产物留在数据库服务器上。
    """
    sftp = None
    try:
        sftp = client.open_sftp()
        for t in (targets or []):
            for p in (str(t), marker_path(str(t))):
                try:
                    sftp.remove(p)
                except Exception:
                    pass
        for d in (dirs or []):
            d = str(d).rstrip("/")
            try:
                sftp.remove(marker_path(d, True))
            except Exception:
                pass
            _sftp_rmtree(sftp, d)
    except Exception:
        pass
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass


def _meta_usable_fixed(meta: dict, remote_path: str, host_key: str, p: dict) -> bool:
    """固定产物断点是否可续用：同一远端产物 + 同一主机 + 未过期。

    注意与逻辑备份的 _meta_usable 不同——固定产物没有压缩/后缀参数，判定依据是
    「远端路径完全一致」，因为它是确定性路径且代表同一份产物。
    """
    if not meta.get("remote_path") or meta.get("remote_path") != remote_path:
        return False
    if host_key and (meta.get("host") or "") != host_key:
        return False
    try:
        age = time.time() - float(meta.get("started_at") or 0)
    except (TypeError, ValueError):
        age = 0
    return age <= float(p.get("resume_ttl") or 43200)


def sftp_pull_resumable(client, remote_path: str, local_path: str, *,
                        task: dict, key: str = "", db_type: str = "fixed",
                        host_key: str = "", has_rc: bool = False,
                        rc_path: str = "", stable_secs: float = None,
                        label: str = "", min_size: int = 0) -> dict:
    """固定产物的断点续传拉回（中断后从已传字节继续，**不重跑备份**）。

    has_rc=True 时按 rc 标记判定结束（逻辑备份形态），否则按 stable_secs 判定。

    返回 dict(path, size, sha256, resumed)。
    """
    p = transfer_params(task)
    resume = bool(p.get("resume"))
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    part_path = _resume_part_path(local_path, task, db_type, key=key)
    meta = _meta_read(part_path) if resume else {}
    resumed = False
    log = logging.getLogger(__name__)
    if meta and _meta_usable_fixed(meta, remote_path, host_key, p):
        resumed = True
        log.info("[remote_dump] 固定产物命中断点续传: %s 本地已传 %s",
                 remote_path, db.human_size(int(meta.get("offset") or 0)))
        try:
            from core import oplog
            op = oplog.current()
            if op:
                op.info("命中断点续传：从 %s 处继续拉取（%s）",
                        db.human_size(int(meta.get("offset") or 0)),
                        label or os.path.basename(remote_path))
        except Exception:
            pass
    else:
        _meta_drop(part_path)
        meta = {
            "task_id": int(task.get("id") or 0),
            "host": host_key,
            "remote_path": remote_path,
            "kind": db_type,
            "started_at": time.time(),
            "offset": 0,
        }
        _meta_write(part_path, meta)

    sftp = client.open_sftp()
    try:
        try:
            sftp.stat(remote_path)
        except (IOError, OSError):
            raise RuntimeError(f"远端产物不存在，无法拉取：{remote_path}")
        _sftp_pull_incremental(
            sftp, remote_path, rc_path, part_path, meta, p,
            label=label or db_type, rate_kbps=int(p.get("rate_kbps") or 0),
            has_rc=bool(has_rc), stable_secs=stable_secs)
    finally:
        try:
            sftp.close()
        except Exception:
            pass

    size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    if min_size and size < int(min_size):
        raise RuntimeError(
            f"拉回产物过小（{size} < {min_size} 字节），疑似远端写入不完整；"
            f"断点已保留，重试将自动续传")
    os.replace(part_path, local_path)
    _meta_drop(part_path)
    log.info("[remote_dump] 固定产物已拉回: %s (%s)%s", local_path,
             db.human_size(size), "（续传）" if resumed else "")
    return {"path": local_path, "size": size,
            "sha256": db.sha256_file(local_path), "resumed": resumed}


def sftp_pull_dir_resumable(client, remote_dir: str, out_dir: str, *,
                            task: dict, db_type: str = "fixed_phys",
                            host_key: str = "", patterns=(), label: str = "",
                            min_size: int = 0) -> list:
    """目录形态固定产物（pg_basebackup 多份 tar / 自定义脚本多文件）逐文件续传拉回。

    返回 [(local_path, size, resumed), ...]。
    """
    sftp = client.open_sftp()
    try:
        names = []
        for attr in sftp.listdir_attr(remote_dir):
            fn = attr.filename
            if fn.startswith(".") or int(attr.st_size or 0) <= 0:
                continue
            if patterns and not str(fn).endswith(tuple(patterns)):
                continue
            names.append(fn)
    finally:
        try:
            sftp.close()
        except Exception:
            pass
    os.makedirs(out_dir, exist_ok=True)
    out = []
    for fn in sorted(names):
        rp = f"{remote_dir}/{fn}"
        lp = os.path.join(out_dir, fn)
        res = sftp_pull_resumable(
            client, rp, lp, task=task, key=fn, db_type=db_type,
            host_key=host_key, label=f"{label or db_type}:{fn}", min_size=min_size)
        out.append((res["path"], res["size"], res["resumed"]))
    return out


def _remote_mysql_dump_to_file(task: dict, ssh_host: dict, final_path: str,
                               compress: int, extra_args: str = "",
                               resume: bool = True) -> dict:
    """远端 mysqldump → 远端暂存 → 平台 SFTP 增量续拉（断点续传）。

    返回 dict(path, size, compressed, fmt, suffix, sha256, resumed)。
    """
    p = transfer_params(task)
    resume = bool(resume and p.get("resume"))
    client = _connect(ssh_host)
    sftp = client.open_sftp()
    cnf_local = tempfile.mktemp(suffix=".cnf")
    user = task.get("username") or "root"
    pw = db.decrypt_secret(task.get("password") or "")
    with open(cnf_local, "wb") as f:
        f.write(f"[client]\nuser={user}\npassword={pw}\n".encode("utf-8"))
    remote_cnf = "/tmp/bk_rdump.cnf"
    sftp.put(cnf_local, remote_cnf)
    try:
        sftp.chmod(remote_cnf, 0o600)
    except Exception:
        pass
    os.remove(cnf_local)

    part_path = _resume_part_path(final_path, task, "mysql")
    meta = _meta_read(part_path) if resume else {}
    resumed = False
    try:
        mysqldump_bin = _resolve_mysql_dump_bin(client, task, remote_cnf)
        spec = _build_mysql_dump_shell(client, task, ssh_host, mysqldump_bin,
                                       remote_cnf, compress, extra_args)
        if spec["fmt"] == "multi-db-tar":
            # 全实例：远端逐库 dump 后 tar.gz 拉回（产物本身是 tar 流，暂无续传形态）
            data = _remote_mysql_full_instance_tar(
                client, mysqldump_bin, remote_cnf, spec["port"], spec["extra"],
                spec.get("flavor") or "")
            with open(final_path, "wb") as lf:
                lf.write(data)
            return {"path": final_path, "size": len(data), "compressed": True,
                    "fmt": "multi-db-tar", "suffix": ".tar.gz",
                    "sha256": db.sha256_file(final_path), "resumed": False}

        stage = _remote_stage_dir()
        if meta and _meta_usable(meta, ssh_host, spec, p) \
                and not _remote_breakpoint_alive(sftp, meta):
            # 断点还"在 TTL 内"，但远端产物已经不存在（客户清过 /tmp、目标机重启、
            # 或上次失败后清理过）→ 不作废就会每轮都傻等超时。
            logging.getLogger(__name__).warning(
                "[remote_dump] 远端断点 %s 已失效（产物不存在或被截断），"
                "作废并重新导出", meta.get("remote_path"))
            _cleanup_remote(sftp, meta)
            _meta_drop(part_path)
            meta = {}

        if meta and _meta_usable(meta, ssh_host, spec, p):
            resumed = True
            logging.getLogger(__name__).info(
                "[remote_dump] 命中断点续传: 远端 %s，本地已传 %s",
                meta.get("remote_path"), db.human_size(int(meta.get("offset") or 0)))
            try:
                from core import oplog
                op = oplog.current()
                if op:
                    op.info("命中断点续传：从 %s 处继续拉取（远端 %s）",
                            db.human_size(int(meta.get("offset") or 0)),
                            meta.get("remote_path"))
            except Exception:
                pass
        else:
            if meta:
                _cleanup_remote(sftp, meta)     # 断点过期/参数变化 → 清旧残留
            _meta_drop(part_path)
            tag = f"t{int(task.get('id') or 0)}_{int(time.time())}"
            meta = {
                "task_id": int(task.get("id") or 0),
                "host": ssh_host.get("host") or ssh_host.get("ip") or "",
                "compressed": bool(spec["enable"]),
                "suffix": spec["suffix"],
                "remote_path": f"{stage}/bk_dump_{tag}.part",
                "rc_path": f"{stage}/bk_dump_{tag}.rc",
                "err_path": f"{stage}/bk_dump_{tag}.err",
                "sh_path": f"{stage}/bk_dump_{tag}.sh",
                "started_at": time.time(),
                "offset": 0,
            }
            _start_remote_dump(client, sftp, spec, stage, meta["sh_path"],
                               meta["remote_path"], meta["err_path"], meta["rc_path"])
            _meta_write(part_path, meta)

        _sftp_pull_incremental(sftp, meta["remote_path"], meta["rc_path"],
                               part_path, meta, p,
                               label=f"mysqldump{' 续传' if resumed else ''}",
                               rate_kbps=int(p.get("rate_kbps") or 0))

        ready, rc = _remote_rc_ready(sftp, meta["rc_path"])
        if ready and rc != 0:
            # 导出进程已终止（而非被中断）：这次没有可续传的余地，必须当场清掉
            # 远端暂存，否则 .part/.rc/.err/.sh 会一直躺在客户机器上。
            # 真实例：task=27 mysqldump 报 unknown variable 'set-gtid-purged=OFF'，
            # 失败退出后目标端残留了整套 bk_dump_* 文件。
            _cleanup_remote(sftp, meta)
            _meta_drop(part_path)
            raise RuntimeError(
                f"远程 mysqldump 失败(rc={rc}, bin={mysqldump_bin}): "
                f"{_sftp_read_tail(sftp, meta['err_path'])}")
        if not ready:
            raise RuntimeError("远端导出未正常结束（无完成标记），断点已保留，重试将自动续传")
        rsize = int(sftp.stat(meta["remote_path"]).st_size)
        if spec["enable"] and rsize <= 20:
            _cleanup_remote(sftp, meta)
            _meta_drop(part_path)
            raise RuntimeError(
                f"远程 mysqldump 疑似失败：压缩后仅 {rsize} 字节"
                f"（stderr: {_sftp_read_tail(sftp, meta['err_path'], 200)}）")

        os.replace(part_path, final_path)
        _meta_drop(part_path)
        _cleanup_remote(sftp, meta)
        return {"path": final_path, "size": os.path.getsize(final_path),
                "compressed": bool(spec["enable"]), "fmt": spec["fmt"],
                "suffix": spec["suffix"], "sha256": db.sha256_file(final_path),
                "resumed": resumed}
    finally:
        try:
            sftp.remove(remote_cnf)
        except Exception:
            pass
        try:
            sftp.close()
        except Exception:
            pass


def remote_db_dump_to_file(task: dict, ssh_host: dict, db_type: str, final_path: str,
                           compress: int = 0, extra_args: str = "",
                           resume: bool = True) -> dict:
    """统一入口：把远端 dump **直接落盘**到 final_path（内存恒定，可断点续传）。

    与旧 remote_db_dump 的差别：不再把整个产物（可能 10GB）攒在内存里再写盘。
    目前 MySQL/MariaDB 走「远端落盘 + SFTP 增量续拉」的完整续传通道；其余类型
    先落盘（已受益于 SSH 管道 O(n²) 拷贝修复），后续按同一模式接入。
    """
    if (db_type or "").lower() in ("mysql", "mariadb"):
        return _remote_mysql_dump_to_file(task, ssh_host, final_path, compress,
                                          extra_args, resume)
    data, compressed, fmt = remote_db_dump(task, ssh_host, db_type, compress, extra_args)
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    with open(final_path, "wb") as f:
        f.write(data)
    return {"path": final_path, "size": len(data), "compressed": bool(compressed),
            "fmt": fmt, "suffix": "", "sha256": db.sha256_file(final_path),
            "resumed": False}


def remote_db_dump(task: dict, ssh_host: dict, db_type: str, compress: int = 0,
                   extra_args: str = "") -> tuple:
    """统一入口：在数据库服务器本地执行 dump 并返回 (原始字节, 是否压缩, 产物格式)。

    - 返回元组 (data: bytes, compressed: bool, fmt: str)，供调用方决定落盘
      后缀与 compress_algo；当远端缺少压缩工具而降级为不压缩时，compressed=False，
      调用方应以 .sql 落盘而非 .sql.zst，保证恢复可逆。
    - fmt: "single"（单库 dump）/"multi-db-tar"（PG 系全实例逐库 tar.gz）/
      "dumpall"（PG 系整实例 SQL 流）。MySQL/Redis/MongoDB 恒为 "single"。
    - extra_args: 透传给 dump 命令的额外参数（目前 MySQL 用）。
    """
    if db_type == "mysql":
        data, fmt, compressed = _remote_mysql_dump(task, ssh_host, compress, extra_args)
        return data, compressed, fmt
    if db_type == "postgresql":
        data, fmt = _remote_pg_dump(task, ssh_host, compress)
        return data, bool(compress) and fmt == "single", fmt
    if db_type == "kingbase":
        data, fmt = _remote_kingbase_dump(task, ssh_host, compress)
        return data, bool(compress) and fmt == "single", fmt
    if db_type == "opengauss":
        # openGauss 与 PG 同源（gs_dump），复用 PG 系 dump 通道（含全实例逐库 tar）
        data, fmt = _remote_opengauss_dump(task, ssh_host, compress)
        return data, bool(compress) and fmt == "single", fmt
    if db_type == "redis":
        return _remote_redis_dump(task, ssh_host), False, "single"
    if db_type == "mongodb":
        data, compressed, fmt = _remote_mongodb_dump(task, ssh_host, compress)
        return data, compressed, fmt
    raise RuntimeError(f"不支持的远程 dump 类型: {db_type}")


# ------------------- 远程物理备份（pg_basebackup / sys_basebackup 等） -------------------
#
# 统一入口：所有“服务端流式物理备份”型数据库（PostgreSQL / Kingbase / 其他兼容
# PG 协议的库）共用同一套逻辑，避免每个引擎把路径、端口、用户名、临时目录写死。
# 通过参数驱动，调用方只传入工具名与少量差异项即可。

# pg_basebackup / sys_basebackup 的默认基础参数（tar 流式 + 压缩 + 快速检查点）
_PG_BASEBACKUP_FLAGS = ["-Ft", "-z", "--checkpoint=fast", "--no-password"]

def remote_physical_backup(task: dict, ssh_host: dict, *, tool: str,
                           default_port: int, default_user: str,
                           extra_args_key: str = "pg_basebackup_extra_args",
                           tool_label: str = None, check_user: str = None,
                           base_flags: list = None,
                           extra_env: dict = None) -> dict:
    """在远端数据库服务器执行流式物理备份（pg_basebackup / sys_basebackup / gs_basebackup）。

    base_flags: 覆盖默认的基础参数（默认 -Ft -z --checkpoint=fast --no-password）。
                openGauss 的 gs_basebackup 不支持 --checkpoint 且需要显式 -X stream，
                由调用方传入 ["-Ft", "-z", "-X", "stream", "-w"]。
    extra_env : 额外环境变量（如 openGauss 的 GAUSSHOME/LD_LIBRARY_PATH）。

    参数
    ----
    task        : 备份任务 dict（含 host/port/username/password/extra_options）
    ssh_host    : SSH 主机 dict（已纳管，用于连接）
    tool        : 远端备份工具命令名（如 pg_basebackup / sys_basebackup）
    default_port: 该库默认端口（任务未配置时回退）
    default_user: 该库默认用户名（任务未配置时回退）
    extra_args_key: extra_options 中透传额外参数的 key
    tool_label  : 报错展示用中文名（默认取 tool）
    check_user  : 工具探测用户（如 kingbase/postgres：该服务端 OS 用户的
                  profile PATH 通常才含有对应客户端工具）

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
    resolved = resolve_remote_tool(ssh_host, tool, check_user=check_user)
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

    # 固定产物形态：远端目录**确定性**（不含随机时间戳）+ 完成标记。
    # 拉回中断时重试可直接复用远端产物并断点续传拉回，不必重跑一遍 basebackup
    # （pg_basebackup 对 TB 级实例可能跑数小时）。
    remote_tmp = fixed_remote_path(task, f"{tool}_phys", "d")
    _log = logging.getLogger(__name__)
    ready = remote_artifact_ready(client, remote_tmp, is_dir=True, min_size=1)
    reuse = bool(ready["reusable"])
    if reuse:
        _log.info("[remote_dump] 复用远端已完成 %s 产物 %s（%s），跳过备份执行",
                  tool, remote_tmp, db.human_size(ready["size"]))
    else:
        # pg_basebackup -D 要求目标目录为空；重试时先清掉上次残留
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

    # 密码环境变量：PG 兼容库 V8 用 PGPASSWORD，KingbaseES V9 用 KINGBASE_PASSWORD，
    # 两个都注入避免版本差异导致鉴权失败；openGauss 另需 GAUSSHOME/lib
    env_extra = ""
    if extra_env:
        env_extra = "".join(
            f" export {k}={shlex.quote(str(v))};" for k, v in extra_env.items())
    flags = " ".join(shlex.quote(a) for a in (base_flags or _PG_BASEBACKUP_FLAGS))
    if reuse:
        # 产物已在远端且标记为完成 → 本次只需拉回，跳过 basebackup 执行
        rc, err, duration = 0, "", 0.0
        out_text = (f"复用远端已完成产物 {remote_tmp}"
                    f"（{db.human_size(ready['size'])}），跳过备份执行")
    else:
        inner = (
            f"export PGPASSWORD={shlex.quote(pw)}; "
            f"export KINGBASE_PASSWORD={shlex.quote(pw)};"
            f"{env_extra} "
            f"{resolved} -h {shlex.quote(db_host)} -p {port} -U {shlex.quote(user)} "
            f"-D {remote_tmp} {flags}"
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

    # 写远端完成标记：下一次重试据此复用产物，**不重跑 basebackup**
    if not reuse:
        try:
            done_size = remote_artifact_ready(client, remote_tmp, is_dir=True)["size"]
        except Exception:  # noqa: BLE001
            done_size = -1
        mark_remote_artifact_done(client, remote_tmp, is_dir=True,
                                  size=done_size, note=f"{tool} {hk}")
    return {
        "ok": True, "rc": 0, "stdout": out_text, "stderr": err,
        "remote_dir": remote_tmp, "reused": reuse,
        "message": (f"远端 {tool_label} 物理备份"
                    f"{'复用上次产物（未重跑备份）' if reuse else '执行成功(rc=0)'}"
                    f"，产物在 {remote_tmp}"),
    }


def _pull_remote_tars(client, remote_dir: str, out_dir: str, *,
                      task: dict = None, host_key: str = "",
                      db_type: str = "pg_phys") -> list:
    """从远端目录拉取 *.tar[.gz] 到本地 out_dir，返回 [(local_path, size)]。

    传入 task 时**逐文件断点续传**（pg_basebackup 会产出 base.tar.gz + 若干 WAL tar，
    大实例单包可能数十 GB，断链后不该从头再传）；未传 task 时退回整文件 sftp.get，
    保持对既有调用方的兼容。
    """
    os.makedirs(out_dir, exist_ok=True)
    sftp = client.open_sftp()
    try:
        names = sorted(a.filename for a in sftp.listdir_attr(remote_dir)
                       if a.filename.endswith((".tar.gz", ".tar")))
    finally:
        try:
            sftp.close()
        except Exception:
            pass

    pieces = []
    for fname in names:
        remote_path = f"{remote_dir}/{fname}"
        local_path = os.path.join(out_dir, fname)
        if task:
            res = sftp_pull_resumable(
                client, remote_path, local_path, task=task, key=fname,
                db_type=db_type, host_key=host_key, has_rc=False,
                stable_secs=3, label=fname, min_size=1)
        else:
            sftp2 = client.open_sftp()
            try:
                sftp2.get(remote_path, local_path)
            finally:
                try:
                    sftp2.close()
                except Exception:
                    pass
            res = {"path": local_path, "size": os.path.getsize(local_path)}
        pieces.append((res["path"], res["size"]))
    return pieces


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


def _remote_list_pg_databases(task: dict, ssh_host: dict, db_type: str = "postgresql") -> list:
    """通过 SSH 在 PG/kingbase/openGauss 上跑 SELECT datname FROM pg_database。

    客户端（psql/ksql/gsql）与默认端口按类型解析；openGauss 额外注入
    GAUSSHOME/lib 以便 gsql 正常加载动态库。
    """
    cfg = _PG_FAMILY_TOOLING.get(db_type) or _PG_FAMILY_TOOLING["postgresql"]
    client = _connect(ssh_host)
    user = task.get("username") or cfg["default_user"]
    pw = db.decrypt_secret(task.get("password") or "")
    pg_bin = ""
    for _name in cfg["query_candidates"]:
        pg_bin = _resolve_remote_bin(client, _name) or ""
        if pg_bin:
            break
    pg_bin = pg_bin or cfg["query_candidates"][0]
    port = task.get("port") or cfg["default_port"]
    maint = "postgres"
    env = _pg_family_env_exports(cfg, pw, bin_path=pg_bin)
    shell = (
        f"set -o pipefail; {env} "
        f"{pg_bin} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {maint} -tA -c "
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
    if db_type in ("postgresql", "kingbase", "opengauss"):
        return _remote_list_pg_databases(task, ssh_host, db_type)
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


def _remote_mysql_full_instance_tar(client, mysqldump_bin: str, remote_cnf: str,
                                    port: int, extra: dict,
                                    db_type: str = "") -> bytes:
    """MySQL/MariaDB 全实例：逐库 .sql（--databases 保证含 CREATE DATABASE/USE）
    → tar.gz + manifest。默认排除系统库（SYSTEM_DBS），可含 schema_only/data_only。
    """
    from core.engines.file import _ssh_exec_pipe

    mysql_bin = os.path.join(os.path.dirname(mysqldump_bin), "mysql")
    include_sys = bool(extra.get("include_system_dbs"))
    sys_dbs = SYSTEM_DBS.get("mysql") or ()
    # 基线过滤：information_schema/performance_schema 是虚拟库，mysqldump
    # 无法导出（--all-databases 同样跳过），无论如何排除。
    # include_sys=false 时再排除 mysql/sys 等系统库；grep 空结果返回 1，
    # set -e 下会让整个脚本意外退出，必须 || true 容错。
    virtual_excl = "^(information_schema|performance_schema)$"
    if include_sys:
        enum_filter = f" | grep -vE '{virtual_excl}' || true"
    else:
        excl = "|".join(list(sys_dbs) + ["information_schema",
                                         "performance_schema"])
        enum_filter = f" | grep -vE '^({excl})$' || true"

    dump_flags = ""
    if extra.get("schema_only"):
        dump_flags += " --no-data"
    if extra.get("data_only"):
        dump_flags += " --no-create-info"
    # 默认禁用 GTID_PURGED（与单库逻辑备份对齐），避免恢复到已开 GTID 的
    # 实例时逐库 dump 的 SET @@GLOBAL.GTID_PURGED 触发 1840。
    # 用户可通过 extra_options.gtid_purged=true 显式保留 GTID 信息。
    # MariaDB 的 mysqldump 无 --set-gtid-purged 选项，按 db_type 跳过。
    if not extra.get("gtid_purged") and (db_type != "mariadb"):
        dump_flags += " --set-gtid-purged=OFF"

    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lines = [
        "set -eu",
        f"MYSQL_BIN={shlex.quote(mysql_bin)}",
        f"DUMP_BIN={shlex.quote(mysqldump_bin)}",
        f"CNF={shlex.quote(remote_cnf)}",
        f"PORT={int(port)}",
        'WORK=$(mktemp -d /tmp/bp_mysql_fi.XXXXXX)',
        "trap 'rm -rf \"$WORK\"' EXIT",
        'mkdir -p "$WORK/dbs"',
        f'DBS=$("$MYSQL_BIN" --defaults-file="$CNF" -h 127.0.0.1 -P $PORT '
        f'-N -B -e "SHOW DATABASES"{enum_filter})',
        # 业务库兜底：默认排除系统库后为空（实例只有默认系统库）→ 自动包含
        # mysql/sys 等系统库（虚拟库始终排除），不让任务直接失败
        'AUTO_SYS=0',
        'if [ -z "${DBS:-}" ] && [ ' + ("1" if include_sys else "0") + ' -eq 0 ]; then',
        f'  DBS=$("$MYSQL_BIN" --defaults-file="$CNF" -h 127.0.0.1 -P $PORT '
        f'-N -B -e "SHOW DATABASES" | grep -vE \'{virtual_excl}\' || true)',
        '  [ -z "${DBS:-}" ] || AUTO_SYS=1',
        'fi',
        '[ -n "${DBS:-}" ] || { echo "no backupable databases: instance is empty'
        ' (only information_schema/performance_schema virtual dbs)" >&2; exit 31; }',
        'if [ "$AUTO_SYS" -eq 1 ] || [ ' + ("1" if include_sys else "0")
        + ' -eq 1 ]; then INCSYS=true; else INCSYS=false; fi',
        'for d in $DBS; do',
        '  "$DUMP_BIN" --defaults-file="$CNF" -h 127.0.0.1 -P $PORT '
        '--single-transaction --routines --triggers --events '
        '--default-character-set=utf8mb4' + dump_flags + ' '
        '--databases "$d" > "$WORK/dbs/$d.sql" '
        '|| { echo "dump failed for db $d" >&2; exit 32; }',
        "done",
        'DBJSON=$(printf \'%s\\n\' "$DBS" | awk \'BEGIN{ORS="";first=1} '
        '{if(!first)print ","; printf "\\"%s\\"",$0; first=0}\')',
        f'printf \'{{"format":"multi-db-tar","db_type":"mysql",'
        '"generated_at":"' + ts + '","globals":"na"'
        ',"include_system_dbs":\'$INCSYS\',"auto_include_system":\'$AUTO_SYS\''
        ',"databases":[%s]}\' '
        '"$DBJSON" > "$WORK/manifest.json"',
        'tar -czf - -C "$WORK" manifest.json dbs',
    ]
    script = "\n".join(lines)
    wrapped = _wrap_login(script)
    out, err, rc = _ssh_exec_pipe(client, wrapped, timeout=7200)
    if rc != 0:
        raise RuntimeError(f"远程 MySQL 全实例备份失败(rc={rc}): {err[:800]}")
    if len(out) <= 100:
        raise RuntimeError(
            f"远程 MySQL 全实例备份疑似失败：tar 流仅 {len(out)} 字节"
            f"（stderr: {err[:200]}）")
    return out


def _remote_mysql_restore_tar(task: dict, ssh_host: dict, dump_bytes: bytes) -> None:
    """MySQL 全实例 tar 恢复：SFTP 上传后逐库灌入（dump 内含 CREATE DATABASE/USE）。"""
    import tempfile
    from core.engines.file import _ssh_exec_pipe

    client = _connect(ssh_host)
    mysql_bin = _resolve_remote_bin(client, "mysql")
    if not mysql_bin:
        raise RuntimeError("远端主机未找到 mysql 客户端，无法执行全实例恢复。")
    user = task.get("username") or "root"
    pw = db.decrypt_secret(task.get("password") or "")
    port = int(task.get("port") or 3306)

    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        tmp.write(dump_bytes)
        tmp.close()
        pkg_path = f"/tmp/bp_mysql_restore_{os.getpid()}_{int(time.time())}.tar.gz"
        sftp = client.open_sftp()
        try:
            sftp.put(tmp.name, pkg_path)
        finally:
            sftp.close()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # 密码走临时 cnf，不进 argv/环境
    cnf_local = tempfile.mktemp(suffix=".cnf")
    with open(cnf_local, "wb") as f:
        f.write(f"[client]\nuser={user}\npassword={pw}\n".encode("utf-8"))
    remote_cnf = "/tmp/bp_mysql_restore.cnf"
    sftp = client.open_sftp()
    try:
        sftp.put(cnf_local, remote_cnf)
        try:
            sftp.chmod(remote_cnf, 0o600)
        except Exception:
            pass
    finally:
        os.remove(cnf_local)
        sftp.close()

    script = "\n".join([
        "set -eu",
        f"MYSQL_BIN={shlex.quote(mysql_bin)}",
        f"CNF={shlex.quote(remote_cnf)}",
        f"PKG={shlex.quote(pkg_path)}",
        f"PORT={int(port)}",
        'WORK=$(mktemp -d /tmp/bp_mysql_restore.XXXXXX)',
        'trap \'rm -rf "$WORK" "$PKG" "$CNF"\' EXIT',
        'tar -xzf "$PKG" -C "$WORK"',
        'RESTORED=""',
        'for f in "$WORK"/dbs/*.sql; do',
        '  [ -e "$f" ] || continue',
        '  "$MYSQL_BIN" --defaults-file="$CNF" -h 127.0.0.1 -P $PORT < "$f"'
        ' || { echo "restore failed for $(basename $f)" >&2; exit 52; }',
        '  RESTORED="$RESTORED $(basename $f .sql)"',
        "done",
        'echo "restored:$RESTORED"',
    ])
    _out, err, rc = _ssh_exec_pipe(client, _wrap_login(script), timeout=7200)
    if rc != 0:
        raise RuntimeError(f"远程 MySQL 全实例恢复失败(rc={rc}): {err[:800]}")


def _pg_family_detect_maint_db(client, cfg: dict, query_bin: str,
                               user: str, pw: str, port: int) -> str:
    """探测可连接的维护库（金仓 V9R1 无 postgres 库，需候选尝试）。

    返回第一个可连通的维护库名；全部失败抛 RuntimeError。
    """
    from core.engines.file import _ssh_exec_pipe
    env = _pg_family_env_exports(cfg, pw, bin_path=query_bin)
    catalogs = cfg.get("catalog_candidates") or (cfg["catalog_table"],)
    for mdb in cfg["maint_candidates"]:
        probe = (
            f"{env} {query_bin} -h 127.0.0.1 -p {int(port)} "
            f"-U {shlex.quote(user)} -d {shlex.quote(mdb)} -t -A "
            f"-c 'SELECT 1' 2>/dev/null"
        )
        _out, _err, rc = _ssh_exec_pipe(client, _wrap_login(probe), timeout=30)
        if rc == 0:
            return mdb
    raise RuntimeError(
        f"无法连接实例（尝试维护库: {', '.join(cfg['maint_candidates'])}），"
        "请检查连接信息")


def _remote_pg_restore(task: dict, ssh_host: dict, dump_bytes: bytes,
                       is_custom: bool, db_type: str = "postgresql") -> None:
    """单库 dump 恢复（PG 系通用：postgresql=psql/pg_restore，kingbase=ksql/sys_restore，
    openGauss=gsql/gs_restore）。"""
    cfg = _PG_FAMILY_TOOLING[db_type]
    # 还原工具按类型候选解析（sys_restore/gs_restore/pg_restore 命名不同）
    restore_tool = (cfg.get("restore_candidates") or ("pg_restore",))[0]
    user = task.get("username") or cfg["default_user"]
    pw = db.decrypt_secret(task.get("password") or "")
    db_name = task.get("db_name") or ""
    port = int(task.get("port") or cfg["default_port"])
    # 探测工具路径
    client = _connect(ssh_host)
    from core.engines.file import _ssh_exec_pipe
    # SQL 客户端提前解析：openGauss 需按工具路径反推 GAUSSHOME（LD_LIBRARY_PATH）
    query_bin_probe = _pg_family_resolve_query_bin(client, cfg)
    env = _pg_family_env_exports(cfg, pw, bin_path=query_bin_probe)

    # 0) 远程先 DROP+CREATE 目标库，保证干净恢复。
    #    pg_restore 的 "-C" 在目标库同名已存在时会因 "cannot drop the
    #    currently open database" 失败，导致旧对象残留，这里改为两步建库。
    #    维护库不写死 postgres（金仓 V9R1 无 postgres 库）——候选探测。
    if db_name:
        psql_tool = query_bin_probe or _pg_family_resolve_query_bin(client, cfg)
        if not psql_tool:
            raise RuntimeError(
                f"远端主机未找到 SQL 客户端（{'/'.join(cfg['query_candidates'])}），无法恢复。")
        maint = _pg_family_detect_maint_db(client, cfg, psql_tool, user, pw, port)
        safe_db = db_name.replace('"', '""')
        # DROP ... WITH (FORCE) 需 PG13+/金仓较新版本；openGauss/PG9.2 系不支持，
        # 老版本先杀连接再 DROP（pg_terminate_backend 在 openGauss 同样可用）
        prep = (
            f"set -o pipefail; {env} "
            f"{psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {shlex.quote(maint)} "
            f"-c 'DROP DATABASE IF EXISTS \"{safe_db}\" WITH (FORCE);' "
            f"|| {{ {psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {shlex.quote(maint)} "
            f"-c \"SELECT sys_terminate_backend(pid) FROM sys_stat_activity WHERE datname = '{db_name}';\" "
            f"|| {psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {shlex.quote(maint)} "
            f"-c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}';\" ; "
            f"{psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {shlex.quote(maint)} "
            f"-c 'DROP DATABASE IF EXISTS \"{safe_db}\";' ; }} "
            f"&& {psql_tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} -d {shlex.quote(maint)} "
            f"-c 'CREATE DATABASE \"{safe_db}\";'"
        )
        _out, perr, prc = _ssh_exec_pipe(
            client, _wrap_login(prep), input_data=b"", timeout=300)
        if prc != 0:
            raise RuntimeError(f"远程重建目标库 {db_name} 失败(rc={prc}): {perr[:600]}")

    if is_custom:
        tool = _resolve_remote_bin(client, restore_tool) or restore_tool
    else:
        tool = query_bin_probe or _pg_family_resolve_query_bin(client, cfg)
        if not tool:
            raise RuntimeError(
                f"远端主机未找到 SQL 客户端（{'/'.join(cfg['query_candidates'])}），无法恢复。")
    shell = (
        f"set -o pipefail; {env} "
        f"{tool} -h 127.0.0.1 -p {port} -U {shlex.quote(user)} "
        f"-d {shlex.quote(db_name)}"
    )
    wrapped = _wrap_login(shell)
    _out, err, rc = _ssh_exec_pipe(
        client, wrapped, input_data=dump_bytes, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"远程恢复失败(rc={rc}): {err[:600]}")


def _pg_family_tar_restore_script(cfg: dict, restore_bin: str, query_bin: str,
                                  pkg_path: str, user: str, pw: str, port: int) -> str:
    """构造全实例 tar 包恢复脚本：globals + 逐库（缺失自动建库，-c 清理覆盖）。"""
    env = _pg_family_env_exports(cfg, pw, bin_path=restore_bin)
    # 系统目录表候选探测（V8=sys_database / V9R1=pg_database）
    catalogs = cfg.get("catalog_candidates") or (cfg["catalog_table"],)
    maints = " ".join(cfg["maint_candidates"])
    return "\n".join([
        "set -eu",
        env,
        f"RESTORE_BIN={shlex.quote(restore_bin)}",
        f"QUERY_BIN={shlex.quote(query_bin)}",
        f"PKG={shlex.quote(pkg_path)}",
        f"PORT={int(port)}",
        f"USERQ={shlex.quote(user)}",
        'WORK=$(mktemp -d /tmp/bp_restore.XXXXXX)',
        "trap 'rm -rf \"$WORK\" \"$PKG\"' EXIT",
        'tar -xzf "$PKG" -C "$WORK"',
        # pg_restore/sys_restore 低版本可能无 --if-exists，探测后再用
        'IFEX=""',
        'if "$RESTORE_BIN" --help 2>&1 | grep -q -- "--if-exists"; then IFEX="--if-exists"; fi',
        f'CATALOGS="{(" ".join(catalogs))}"',
        'MAINT=""',
        'DBS_EXIST=""',
        "for MDB in " + maints + "; do",
        '  for CAT in $CATALOGS; do',
        '    if DBS_EXIST=$("$QUERY_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -d "$MDB" -t -A '
        '        -c "SELECT datname FROM $CAT WHERE NOT datistemplate" 2>/dev/null) '
        '&& [ -n "$DBS_EXIST" ]; then MAINT="$MDB"; break 2; fi',
        "  done",
        "done",
        '[ -n "${MAINT:-}" ] || { echo "cannot connect instance to restore" >&2; exit 51; }',
        # 全局对象（角色/表空间）：失败不阻塞（可能已存在）
        'if [ -s "$WORK/globals.sql" ]; then',
        '  "$QUERY_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -d "$MAINT" '
        '-f "$WORK/globals.sql" >/dev/null 2>&1 \\',
        '    || echo "WARN: globals restore failed (may already exist)" >&2',
        "fi",
        'RESTORED=""',
        'for f in "$WORK"/dbs/*.dump; do',
        '  [ -e "$f" ] || continue',
        '  d=$(basename "$f" .dump)',
        '  if ! printf \'%s\\n\' "$DBS_EXIST" | grep -qxF "$d"; then',
        '    "$QUERY_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" -d "$MAINT" '
        '-c "CREATE DATABASE \\"$d\\"" >/dev/null 2>&1 || true',
        "  fi",
        '  "$RESTORE_BIN" -h 127.0.0.1 -p $PORT -U "$USERQ" --dbname "$d" '
        '--clean $IFEX "$f" || { echo "restore failed for db $d" >&2; exit 52; }',
        '  RESTORED="$RESTORED $d"',
        "done",
        'echo "restored:$RESTORED"',
    ])


def _remote_pg_family_restore_tar(task: dict, ssh_host: dict, db_type: str,
                                  dump_bytes: bytes) -> None:
    """全实例 tar 包恢复：SFTP 上传后在远端解包，逐库 restore（含建库/globals）。"""
    import tempfile
    from core.engines.file import _ssh_exec_pipe

    cfg = _PG_FAMILY_TOOLING[db_type]
    client = _connect(ssh_host)
    restore_tool = ""
    restore_bin = ""
    # 还原工具候选（sys_restore/gs_restore/pg_restore），逐个探测取第一个可用
    for _name in (cfg.get("restore_candidates") or ("pg_restore",)):
        restore_bin = _resolve_remote_bin(client, _name) or ""
        if restore_bin:
            restore_tool = _name
            break
    if not restore_bin:
        raise RuntimeError(
            f"远端主机未找到还原工具（{'/'.join(cfg.get('restore_candidates') or ('pg_restore',))}），"
            "无法执行全实例恢复。")
    query_bin = _pg_family_resolve_query_bin(client, cfg)
    if not query_bin:
        raise RuntimeError(
            f"远端主机未找到 SQL 客户端（{'/'.join(cfg['query_candidates'])}），"
            "无法执行全实例恢复。")

    user = task.get("username") or cfg["default_user"]
    pw = db.decrypt_secret(task.get("password") or "")
    port = int(task.get("port") or cfg["default_port"])

    # dump 字节流先落本地临时文件，再 SFTP 推送（tar 需远端随机访问，不能走 stdin）
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        tmp.write(dump_bytes)
        tmp.close()
        pkg_path = f"/tmp/bp_restore_{os.getpid()}_{int(time.time())}.tar.gz"
        sftp = client.open_sftp()
        try:
            sftp.put(tmp.name, pkg_path)
        finally:
            sftp.close()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    script = _pg_family_tar_restore_script(
        cfg, restore_bin, query_bin, pkg_path, user, pw, port)
    out, err, rc = _ssh_exec_pipe(client, _wrap_login(script), timeout=7200)
    if rc != 0:
        raise RuntimeError(f"远程全实例恢复失败(rc={rc}): {err[:800]}")


def _looks_like_full_instance_tar(dump_bytes: bytes) -> bool:
    """识别 multi-db-tar 产物：gzip 魔数 + tar 内含 manifest.json。"""
    if dump_bytes[:2] != b"\x1f\x8b":
        return False
    try:
        import io as _io
        import tarfile
        with tarfile.open(fileobj=_io.BytesIO(dump_bytes), mode="r:gz") as tf:
            names = tf.getnames()
        return "manifest.json" in names
    except Exception:
        return False


def remote_db_restore(task: dict, ssh_host: dict, db_type: str,
                      dump_bytes: bytes, is_custom: bool = False) -> None:
    """统一入口：将本地 dump 字节流经 SSH 灌入数据库服务器。

    自动识别 multi-db-tar（全实例逐库 tar.gz）产物并走整实例恢复分支。
    """
    if db_type in ("postgresql", "kingbase", "opengauss") and _looks_like_full_instance_tar(dump_bytes):
        _remote_pg_family_restore_tar(task, ssh_host, db_type, dump_bytes)
        return
    if db_type in ("mysql", "mariadb") and _looks_like_full_instance_tar(dump_bytes):
        _remote_mysql_restore_tar(task, ssh_host, dump_bytes)
        return
    if db_type == "mysql":
        _remote_mysql_restore(task, ssh_host, dump_bytes)
    elif db_type in ("postgresql", "kingbase", "opengauss"):
        _remote_pg_restore(task, ssh_host, dump_bytes, is_custom, db_type=db_type)
    else:
        raise RuntimeError(f"不支持的远程恢复类型: {db_type}")
