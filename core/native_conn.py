# -*- coding: utf-8 -*-
"""原生 Python 驱动直连层（Native Direct Connect）。

通过纯 Python / wheel 分发的 DB-API 驱动直连数据库，**完全不依赖 Java/JVM**，
用于连接测试、拉取库列表与数据对比/同步等直连数据通道：

- mysql / mariadb  → pymysql（纯 Python，无编译依赖）
- postgresql       → psycopg2（官方 wheel）
- kingbase         → psycopg2（KingbaseES 兼容 PG 协议，实测可用）
- oracle           → oracledb 瘦客户端（纯 Python，免装 Instant Client；
                     12c+ 可直连，11g 服务端不支持瘦模式，自动回退 JDBC/报明确提示）
- dameng           → dmPython（随达梦客户端提供，非 PyPI；缺失时给出安装指引）

与 core/jdbc.py 的关系：
- 直连是首选通道（无 Java 依赖，Docker/离线镜像默认即用）；
- jdbc.py 在原生驱动缺失或服务端不支持（如 Oracle 11g 瘦模式）时
  作为可选兜底（需本机 JRE + drivers/ 驱动 jar）。

接口与 core/jdbc.py 对齐：test_connection / list_databases / build_url 语义一致，
调用方（api/jdbc.py、core/data_compare.py）可透明切换。
"""

from __future__ import annotations

import time
from typing import Tuple

# ---------------------------------------------------------------------------
# 驱动注册表（与 core/jdbc.py 的探活/拉库 SQL 保持一致）
# ---------------------------------------------------------------------------

DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "kingbase": 54321,
    "opengauss": 26000,
    "panwei": 12800,
    "oracle": 1521,
    "dameng": 5236,
}

# db_type -> (探活 SQL, 拉库 SQL, 需过滤的系统库, 直连时默认连接的库名)
NATIVE_CONFIG = {
    "mysql": {
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "performance_schema", "mysql", "sys"),
        "default_db": None,
    },
    "mariadb": {
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "performance_schema", "mysql", "sys"),
        "default_db": None,
    },
    "postgresql": {
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
        "default_db": "postgres",
    },
    "kingbase": {
        "probe": "SELECT 1",
        "list_sql": ("SELECT datname FROM sys_database WHERE datistemplate = false ORDER BY 1"),
        "filter": (),
        "default_db": "test",
    },
    "opengauss": {
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
        "default_db": "postgres",
    },
    "panwei": {
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
        "default_db": "postgres",
    },
    "oracle": {
        "probe": "SELECT 1 FROM DUAL",
        "list_sql": "SELECT username FROM all_users WHERE oracle_maintained = 'N' ORDER BY 1",
        "filter": (),
        "default_db": None,
    },
    "dameng": {
        "probe": "SELECT 1",
        "list_sql": "SELECT username FROM all_users ORDER BY 1",
        # 达梦的 SYSDBA 既是管理员又常被用作业务 schema，不能排除
        "filter": ("SYS", "SYSAUDITOR", "SYSSSO", "SYSDBAOPER", "CTISYS", "CJISYS"),
        "default_db": None,
    },
}

NATIVE_DB_TYPES = tuple(NATIVE_CONFIG.keys())


class DriverUnavailable(ConnectionError):
    """原生驱动缺失/无法加载（区别于连接失败：调用方可据此走 JDBC 兜底）。

    也用于「通道不适用」：例如 python-oracledb 的 thin 模式不支持 Oracle
    11.2 及更早版本（DPY-3010）。这类错误不是账号/网络问题，必须让调用方
    切到 JDBC / sqlplus 通道，而不是把它当成连接失败报给用户。
    """


# python-oracledb thin 模式的服务端版本上限报错标识
_THIN_BLOCK_TOKENS = ("DPY-3010", "DPY-3019",
                      "not supported by python-oracledb in thin mode")


def thin_version_blocked(msg) -> bool:
    """判断报错是否为「thin 模式不支持该服务端版本」（Oracle 11.x 及更早）。

    与真实连接失败（ORA-01017 密码错、ORA-12514 服务名错等）区分开：
    前者应切换通道，后者的报错才是用户要看的真原因。
    """
    text = str(msg or "")
    low = text.lower()
    return any(tok.lower() in low for tok in _THIN_BLOCK_TOKENS)


def oracle_thin_unsupported_hint() -> str:
    """11g 直连受限时的统一中文提示（各功能 catch 处复用）。"""
    return ("Oracle 11.2 及更早版本不支持 python-oracledb thin 模式（DPY-3010）："
            "该功能需要 Oracle 12.1+ 直连；备份/恢复不受影响（平台经 SSH 调用"
            "数据库自带的 expdp/impdp/rman 执行），「测试连接」会自动改走 "
            "JDBC 通道")


def compat_error(db_type, exc) -> str:
    """把「通道不适用」类异常翻译成可操作提示，其余原样返回。

    供数据对比/克隆/迁移/同步等直连功能复用，避免用户只看到裸 DPY-3010
    而误以为平台不支持该数据库。
    """
    if (db_type or "").lower() == "oracle" and thin_version_blocked(exc):
        return oracle_thin_unsupported_hint()
    msg = str(exc).strip()
    return msg or type(exc).__name__


def _norm_port(db_type, port):
    if port in (None, "", 0):
        return DEFAULT_PORTS.get(db_type, 0)
    return int(port)


# ---------------------------------------------------------------------------
# 驱动就绪状态（自检面板用）
# ---------------------------------------------------------------------------

def driver_status() -> dict:
    """返回各 db_type 原生驱动的可用性（不真正连接）。"""
    result = {}
    for db_type in NATIVE_DB_TYPES:
        available, driver, reason = _probe_driver(db_type)
        result[db_type] = {
            "available": available,
            "driver": driver,
            "reason": reason,
        }
    return result


def _probe_driver(db_type: str) -> Tuple[bool, str, str]:
    """探测某类型原生驱动是否可导入。返回 (available, driver_name, reason)。"""
    try:
        mod = _import_driver(db_type)
        return True, getattr(mod, "__name__", "?"), ""
    except Exception as e:  # ImportError 或达梦动态库缺失等
        return False, "", str(e)


def _import_driver(db_type: str):
    """按 db_type 惰性导入原生驱动模块。"""
    if db_type in ("mysql", "mariadb"):
        import pymysql
        return pymysql
    if db_type == "postgresql":
        import psycopg2
        return psycopg2
    if db_type == "kingbase":
        # 金仓协议兼容 PG；优先 ksycopg2，回退 psycopg2
        try:
            import ksycopg2  # type: ignore
            return ksycopg2
        except ImportError:
            import psycopg2
            return psycopg2
    if db_type in ("opengauss", "panwei"):
        # openGauss/磐维协议兼容 PG，psycopg2 直连（部分版本服务端需关闭 SSL 强制）
        import psycopg2
        return psycopg2
    if db_type == "oracle":
        errs = []
        for name in ("oracledb", "cx_Oracle"):
            try:
                return __import__(name)
            except ImportError as e:
                errs.append(str(e))
        raise ImportError("; ".join(errs))
    if db_type == "dameng":
        import dmPython  # type: ignore
        return dmPython
    raise ValueError(f"暂不支持直连类型: {db_type}（支持: {', '.join(NATIVE_DB_TYPES)}）")


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def connect(db_type, host, port, db, user, password, timeout=10):
    """建立原生直连并返回 DB-API connection（调用方负责 close）。

    Raises:
        ConnectionError: 驱动缺失或连接失败（信息含安装指引）。
    """
    db_type = (db_type or "").lower()
    if db_type not in NATIVE_CONFIG:
        raise ValueError(f"暂不支持直连类型: {db_type}（支持: {', '.join(NATIVE_DB_TYPES)}）")
    port = _norm_port(db_type, port)
    last_err = None

    try:
        mod = _import_driver(db_type)
    except Exception as e:
        raise DriverUnavailable(
            f"{db_type} 原生驱动不可用: {_driver_hint(db_type, e)}") from e

    if db_type in ("mysql", "mariadb"):
        try:
            return mod.connect(
                host=host, port=port or 3306, user=user or "", password=password or "",
                database=db or None, charset="utf8mb4",
                connect_timeout=timeout, read_timeout=120, write_timeout=120)
        except Exception as e:
            last_err = e

    elif db_type in ("postgresql", "kingbase", "opengauss", "panwei"):
        dbname = db or NATIVE_CONFIG[db_type]["default_db"] or "postgres"
        try:
            conn = mod.connect(
                host=host, port=port or 5432, user=user or "", password=password or "",
                dbname=dbname, connect_timeout=timeout)
            conn.autocommit = True
            return conn
        except Exception as e:
            last_err = e

    elif db_type == "oracle":
        service = db or "ORCL"
        dsn = mod.makedsn(host, port or 1521, service_name=service)
        # 跨虚拟机 1521 偶发被网络重置（DPY-6005/12514，实测 129 环境）：
        # 服务器侧实例正常时重试即可恢复 → 自动重试 3 次
        last_exc = None
        for attempt in range(3):
            try:
                return mod.connect(user=user or "", password=password or "",
                                   dsn=dsn)
            except Exception as e:
                last_exc = e
                msg = str(e)
                # 通道不适用 ≠ 连接失败：thin 模式连 11.2 及更早版本会直接报
                # DPY-3010。按 DriverUnavailable 抛出，上层才会自动改用 JDBC
                # 通道；否则这个「不支持」会掩盖 JDBC 的真实报错（历史缺陷：
                # 11g 密码填错时界面显示「不支持 11g」，用户误判平台无能力）。
                if thin_version_blocked(msg):
                    raise DriverUnavailable(
                        "python-oracledb thin 模式不支持 Oracle 11.2 及更早版本"
                        "（DPY-3010），平台将自动改用 JDBC 通道；原始报错："
                        + msg) from e
                if not any(k in msg for k in ("DPY-6005", "DPY-4011",
                                              "12514", "12518", "12170")):
                    break
                time.sleep(2 * (attempt + 1))
        last_err = last_exc

    elif db_type == "dameng":
        try:
            return mod.connect(
                server=host, port=port or 5236, user=user or "", password=password or "")
        except Exception as e:
            last_err = e

    raise ConnectionError(f"{db_type} 直连 {host}:{port}/{db or ''} 失败: {last_err}")


def _driver_hint(db_type: str, exc: Exception) -> str:
    """驱动缺失/加载失败时的安装指引。"""
    hint = {
        "mysql": "pip install pymysql",
        "mariadb": "pip install pymysql",
        "postgresql": "pip install psycopg2-binary",
        "kingbase": "pip install psycopg2-binary（金仓协议兼容 PG）",
        "oracle": "pip install oracledb（瘦客户端，免装 Instant Client）",
        "dameng": ("dmPython 随达梦客户端 drivers/python 目录提供："
                   "cd <DM_HOME>/drivers/python/dmPython && python setup.py install；"
                   "缺失时也可（装 Java 后）走 JDBC 兜底通道"),
    }.get(db_type, "")
    return f"{exc}。{hint}" if hint else str(exc)


# ---------------------------------------------------------------------------
# 连接测试 / 拉库列表（与 core/jdbc.py 接口对齐）
# ---------------------------------------------------------------------------

def test_connection(db_type, host, port, db, user, password, timeout=15):
    """测试原生直连，返回 (ok, message, info)。

    驱动缺失时抛 :class:`DriverUnavailable`（调用方如 jdbc.test_connection
    可据此走 JDBC 兜底）；驱动存在但连接失败时返回 (False, 原因, None)。
    """
    t0 = time.monotonic()
    # 端口预检：TCP 不通时给出明确提示（与 jdbc.test_connection 行为一致）
    try:
        import socket
        with socket.create_connection((host, int(_norm_port(db_type, port))), timeout=4):
            pass
    except Exception as e:
        return False, (
            f"目标 {host}:{port} 端口不可达（{type(e).__name__}）——"
            "请确认数据库实例已启动、监听端口填写正确，且防火墙已放行该端口"), None
    try:
        conn = connect(db_type, host, port, db, user, password, timeout=timeout)
        cfg = NATIVE_CONFIG.get((db_type or "").lower(), {})
        cur = conn.cursor()
        cur.execute(cfg.get("probe", "SELECT 1"))
        row = cur.fetchone()
        cur.close()
        conn.close()
        ms = int((time.monotonic() - t0) * 1000)
        driver_name = getattr(_import_driver(db_type), "__name__", "?")
        info = {
            "db_type": db_type,
            "host": host,
            "port": _norm_port(db_type, port),
            "db": db,
            "user": user,
            "mode": "native",
            "driver": driver_name,
            "latency_ms": ms,
            "probe_result": str(row[0]) if row and row[0] is not None else "",
        }
        return True, f"直连成功（{ms} ms），驱动: {driver_name}", info
    except DriverUnavailable:
        raise
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return False, f"直连失败（{ms} ms）: {e}", None


def list_databases(db_type, host, port, db, user, password, timeout=30):
    """通过原生直连拉取数据库/schema 列表。"""
    db_type = (db_type or "").lower()
    cfg = NATIVE_CONFIG.get(db_type)
    if not cfg:
        raise ValueError(f"暂不支持直连类型: {db_type}")
    conn = connect(db_type, host, port, db, user, password, timeout=timeout)
    try:
        cur = conn.cursor()
        cur.execute(cfg["list_sql"])
        rows = [str(r[0]) for r in cur.fetchall()]
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    filters = cfg.get("filter") or ()
    if filters:
        rows = [r for r in rows if r not in filters]
    return rows
