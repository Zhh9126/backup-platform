# -*- coding: utf-8 -*-
"""
数据库连通性探测：在巡检 / 数据同步前，对源/目标库做一次轻量级连通性检查。

零额外依赖，使用各数据库自带命令行客户端（与备份引擎一致）。
- 返回 (True, msg)  表示连接正常
- 返回 (False, msg) 表示连接失败（应触发告警）
- 返回 (None, msg)  表示无法判定（客户端缺失 / 类型未实现探测），视为“未知/警告”
"""
import importlib
import os
import shutil
import subprocess

import core.db as db


def _run(cmd: list, env: dict = None, timeout: int = 12, stdin_text: str = ""):
    try:
        proc = subprocess.run(
            cmd, env=env, input=(stdin_text.encode("utf-8") if stdin_text else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "ignore"),
                proc.stderr.decode("utf-8", "ignore"))
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except FileNotFoundError as e:
        return -2, "", f"命令不存在: {e}"


def _try_import(*names):
    """按序惰性导入首个可用模块（T06：信创驱动全部为可选依赖）。

    Args:
        *names: 候选模块名，按优先级排列。

    Returns:
        ``(module | None, 原因)``。**绝不抛异常**。
    """
    for name in names:
        try:
            return importlib.import_module(name), ""
        except ImportError:
            continue
        except Exception as e:  # pragma: no cover - 驱动内部异常
            return None, f"{name} 导入失败: {e}"
    return None, "未安装 " + " / ".join(names)


def _first_client(*names) -> str:
    """返回 PATH 中首个存在的命令行客户端名；都不存在返回空串。"""
    for name in names:
        if shutil.which(name):
            return name
    return ""


def _probe_via_driver(module, connect, probe_sql: str, ok_msg: str):
    """用 Python 驱动执行一次轻量查询验证连通性。

    Args:
        module: 已导入的驱动模块（仅用于判空）。
        connect: 无参可调用对象，返回连接。
        probe_sql: 探活语句。
        ok_msg: 成功时的中文提示。

    Returns:
        ``(True/False, 消息)``；驱动缺失时返回 ``(None, "")`` 交由调用方回退 CLI。
    """
    if module is None:
        return None, ""
    try:
        conn = connect()
    except Exception as e:
        return False, f"连接失败: {str(e).strip()[:160]}"
    try:
        cur = conn.cursor()
        try:
            cur.execute(probe_sql)
            cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return True, ok_msg
    except Exception as e:
        return False, f"探活查询失败: {str(e).strip()[:160]}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _probe_mysql(h, p, u, pw, db_name, t):
    env = os.environ.copy()
    if pw:
        env["MYSQL_PWD"] = pw
    rc, out, err = _run(
        ["mysql", "-h", h, "-P", str(p), "-u", u, "--connect-timeout",
         str(t), "-e", "SELECT 1"], env, t + 5)
    return (rc == 0, "MySQL 连接正常" if rc == 0 else (err.strip() or "连接失败"))


def _probe_postgresql(h, p, u, pw, db_name, t):
    env = os.environ.copy()
    if pw:
        env["PGPASSWORD"] = pw
    rc, out, err = _run(
        ["pg_isready", "-h", h, "-p", str(p), "-U", u, "-t", str(t)],
        env, t + 5)
    # 0=接受连接 1=拒绝 2=无响应 3=未尝试
    if rc == 0:
        return True, "PostgreSQL 可接受连接"
    return False, (out + err).strip() or "连接被拒绝"


def _probe_redis(h, p, u, pw, db_name, t):
    cmd = ["redis-cli", "-h", h, "-p", str(p)]
    if pw:
        cmd += ["-a", pw]
    cmd += ["PING"]
    rc, out, err = _run(cmd, None, t + 5)
    ok = rc == 0 and "PONG" in out.upper()
    return ok, (out.strip() or err.strip() or "连接失败")


def _probe_mongodb(h, p, u, pw, db_name, t):
    uri = (f"mongodb://{u}:{pw}@{h}:{p}/{db_name or ''}" if pw
           else f"mongodb://{h}:{p}/{db_name or ''}")
    rc, out, err = _run(
        ["mongosh", "--quiet", "--eval", "db.runCommand({ping:1})", uri],
        None, t + 10)
    if rc != 0 and shutil.which("mongo"):
        rc, out, err = _run(
            ["mongo", "--quiet", "--eval", "db.runCommand({ping:1})", uri],
            None, t + 10)
    ok = rc == 0 and "ok" in out.lower()
    return ok, (out.strip() or err.strip() or "连接失败")


def _probe_oracle(h, p, u, pw, db_name, t):
    """Oracle 连通性（T06）：oracledb / cx_Oracle 驱动优先，回退 sqlplus。

    驱动与客户端都缺失时返回 ``(None, ...)``——「未知」而非「失败」，不误报警。
    """
    service = db_name or "ORCL"
    dsn = f"{h}:{p or 1521}/{service}"
    module, reason = _try_import("oracledb", "cx_Oracle")
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(user=u, password=pw, dsn=dsn),
            "SELECT 1 FROM DUAL",
            f"Oracle 连接正常（{module.__name__}）")
        if ok is not None:
            return ok, msg

    client = _first_client("sqlplus")
    if not client:
        return None, f"缺少 oracledb/cx_Oracle 驱动与 sqlplus 客户端（{reason}），无法验证连接"
    # 密码经 stdin 传入，绝不出现在命令行（共享知识 #16）
    script = f"connect {u}/{pw}@{dsn}\nselect 1 from dual;\nexit\n"
    rc, out, err = _run([client, "-S", "-L", "/nolog"], None, t + 10, script)
    text = (out + err)
    if rc == 0 and "ORA-" not in text.upper():
        return True, "Oracle 连接正常（sqlplus）"
    return False, (text.strip()[:200] or "连接失败")


def _probe_kingbase(h, p, u, pw, db_name, t):
    """KingbaseES 连通性（T06）：ksycopg2 / psycopg2 驱动优先，回退 ksql。"""
    module, reason = _try_import("ksycopg2", "psycopg2")
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(host=h, port=p or 54321, user=u,
                                   password=pw, dbname=db_name or "test",
                                   connect_timeout=max(3, int(t))),
            "SELECT 1",
            f"KingbaseES 连接正常（{module.__name__}）")
        if ok is not None:
            return ok, msg

    client = _first_client("ksql", "sys_psql", "psql")
    if not client:
        return None, f"缺少 ksycopg2/psycopg2 驱动与 ksql 客户端（{reason}），无法验证连接"
    env = os.environ.copy()
    if pw:
        env["KINGBASE_PASSWORD"] = pw
        env["PGPASSWORD"] = pw
    rc, out, err = _run(
        [client, "-h", h, "-p", str(p or 54321), "-U", u,
         "-d", db_name or "test", "-tAc", "SELECT 1"], env, t + 5)
    if rc == 0 and "1" in (out or ""):
        return True, f"KingbaseES 连接正常（{client}）"
    return False, ((out + err).strip()[:200] or "连接失败")


def _probe_dameng(h, p, u, pw, db_name, t):
    """达梦 连通性（T06）：dmPython 驱动优先，回退 disql。"""
    port = p or 5236
    module, reason = _try_import("dmPython", "dmpython")
    if module is not None:
        def _connect():
            try:
                return module.connect(user=u, password=pw, server=str(h),
                                      port=int(port))
            except TypeError:
                return module.connect(u, pw, f"{h}:{port}")
        ok, msg = _probe_via_driver(
            module, _connect, "SELECT 1", f"达梦 连接正常（{module.__name__}）")
        if ok is not None:
            return ok, msg

    client = _first_client("disql")
    if not client:
        return None, f"缺少 dmPython 驱动与 disql 客户端（{reason}），无法验证连接"
    # 密码经 stdin 传入，不进命令行
    script = f"conn {u}/\"{pw}\"@{h}:{port}\nselect 1;\nexit\n"
    rc, out, err = _run([client, "/nolog"], None, t + 10, script)
    text = (out + err)
    if rc == 0 and "-" not in text.split("\n")[0][:1] and "错误" not in text:
        return True, "达梦 连接正常（disql）"
    return False, (text.strip()[:200] or "连接失败")


# 未实现客户端级探测的库：判定为“未知”，不误报警
def _probe_unimplemented(_h, _p, _u, _pw, _db, _t):
    return None, "该类型未实现客户端探测，跳过"


# 值为 (必需客户端列表, 探测函数)。
# 客户端列表为空表示该探测函数自带「驱动优先 + CLI 回退 + 缺失即未知」逻辑（T06）。
_PROBES = {
    "mysql": (["mysql"], _probe_mysql),
    "postgresql": (["psql", "pg_isready"], _probe_postgresql),
    "redis": (["redis-cli"], _probe_redis),
    "mongodb": (["mongosh", "mongo"], _probe_mongodb),
    "oracle": ([], _probe_oracle),
    "kingbase": ([], _probe_kingbase),
    "dameng": ([], _probe_dameng),
}


def probe_db_connection(db_type: str, host: str, port, username: str,
                         password: str, db_name: str = "", timeout: int = 10):
    """返回 (ok: bool|None, message: str)。ok=None 表示未知。"""
    if not host:
        return False, "未配置主机地址"
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    entry = _PROBES.get(db_type)
    if not entry:
        return None, f"未知数据库类型: {db_type}"
    clients, fn = entry
    missing = [c for c in clients if not shutil.which(c)]
    if missing:
        return None, "客户端缺失(" + ",".join(missing) + ")，无法验证连接"
    try:
        return fn(host, port, username, password, db_name, timeout)
    except Exception as e:  # 探测本身异常，按未知处理，不误报失败
        return None, f"探测异常: {e}"
