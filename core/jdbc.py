# -*- coding: utf-8 -*-
"""
JDBC 连接方式模块

通过 JPype/jaydebeapi 桥接 Java JDBC 驱动，实现不依赖 SSH/本机客户端的
数据库直连能力（连接测试、拉取库列表）。

设计原则：
- 「原有连接方式优先」：本模块仅作为连接测试 / 拉库列表的兜底通道与显式入口，
  备份执行仍走原有（SSH / 本机客户端）方式。
- JVM 全局单例：JPype 限制 JVM 只能启动一次，classpath 在首次启动时
  一次性包含 drivers/ 下全部 jar，之后任何 db_type 均可复用。
- 自动探测 JDK：支持 JAVA_HOME / 常见安装路径 / .jdks / PATH 推导。

依赖：jpype1、jaydebeapi、本机 JDK（JRE 亦可）、drivers/ 下的驱动 jar。
"""

from __future__ import annotations

import os
import time
import threading
from pathlib import Path

# 限制 numpy/OpenBLAS 线程数：平台 JDBC 仅做短查询，多线程徒增内存与开销
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

# ---------------------------------------------------------------------------
# 驱动注册表
# ---------------------------------------------------------------------------

DRIVERS_DIR = Path(__file__).resolve().parent.parent / "drivers"

# db_type -> JDBC 驱动配置（jar / 驱动类 / URL 模板 / 探活 SQL / 拉库 SQL）
DRIVER_CONFIG = {
    "mysql": {
        "jar": "mysql-connector-j-8.4.0.jar",
        "class": "com.mysql.cj.jdbc.Driver",
        "url": "jdbc:mysql://{host}:{port}/{db}?useSSL=false&allowPublicKeyRetrieval=true"
               "&serverTimezone=Asia/Shanghai&useUnicode=true&characterEncoding=utf8"
               "&connectTimeout=8000&socketTimeout=15000",
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "performance_schema", "mysql", "sys"),
    },
    "mariadb": {
        "jar": "mariadb-java-client-3.4.1.jar",
        "class": "org.mariadb.jdbc.Driver",
        "url": "jdbc:mariadb://{host}:{port}/{db}?useSSL=false&useUnicode=true"
               "&characterEncoding=utf8&connectTimeout=8000&socketTimeout=15000",
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "performance_schema", "mysql", "sys"),
    },
    "postgresql": {
        "jar": "postgresql-42.7.5.jar",
        "class": "org.postgresql.Driver",
        "url": "jdbc:postgresql://{host}:{port}/{db}?connectTimeout=8&socketTimeout=15",
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
    },
    "kingbase": {
        # 官方 kingbase8.jar 需从人大金仓官网获取；缺失时自动降级用 PG 驱动
        "jar": "kingbase8-8.6.0.jar",
        "class": "com.kingbase8.Driver",
        "url": "jdbc:kingbase8://{host}:{port}/{db}?connectTimeout=8&socketTimeout=15",
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM sys_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
        "fallback_to_pg": True,
    },
    "oracle": {
        "jar": "ojdbc11-23.4.0.24.05.jar",
        "class": "oracle.jdbc.OracleDriver",
        # db 参数为 service_name（可省略时默认 XE/ORCL，由驱动决定）
        "url": "jdbc:oracle:thin:@//{host}:{port}/{db}",
        "probe": "SELECT 1 FROM DUAL",
        "list_sql": "SELECT username FROM all_users WHERE oracle_maintained = 'N' ORDER BY 1",
        "filter": (),
    },
    "dameng": {
        "jar": "DmJdbcDriver18-8.1.3.62.jar",
        "class": "dm.jdbc.driver.DmDriver",
        "url": "jdbc:dm://{host}:{port}/{db}?connectTimeout=8000&socketTimeout=15000",
        "probe": "SELECT 1",
        "list_sql": "SELECT username FROM all_users ORDER BY 1",
        "filter": ("SYS", "SYSDBA", "SYSAUDITOR", "SYSSSO", "SYSTEM", "CTISYS", "CJISYS"),
    },
}

# 使用 JDBC 的数据库类型（redis / mongodb 走 python 原生驱动，不需要 JDBC）
JDBC_DB_TYPES = tuple(DRIVER_CONFIG.keys())

# 各类型默认端口（与引擎默认值对齐）
DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "kingbase": 54321,
    "oracle": 1521,
    "dameng": 5236,
}


def _norm_port(db_type, port):
    if port in (None, "", 0):
        return DEFAULT_PORTS.get(db_type, 0)
    return int(port)


# ---------------------------------------------------------------------------
# JVM 探测与管理
# ---------------------------------------------------------------------------

_jvm_lock = threading.RLock()
_jvm_started = False


def _frozen_jdk_dirs():
    """冻结（PyInstaller）环境下，可执行文件同目录的便携 JDK/JRE 目录。

    离线部署无需在系统安装 Java：将 JDK 目录重命名为 jdk / jre / java / runtime
    并放到可执行文件（dist/backup_platform(.exe)）同目录即可被自动识别。
    """
    if not getattr(os.sys, "frozen", False):
        return
    base = Path(os.sys.executable).resolve().parent
    for name in ("jdk", "jre", "java", "runtime"):
        p = base / name
        if p.is_dir():
            yield p


def _candidate_jvm_dirs():
    """探测候选 JDK/JRE 安装目录（返回 Path 列表）。"""
    dirs = []
    seen = set()

    def _add(p):
        if p and p.is_dir() and str(p) not in seen:
            seen.add(str(p))
            dirs.append(p)

    for p in _frozen_jdk_dirs():
        _add(p)

    for var in ("JAVA_HOME", "JDK_HOME"):
        env = os.environ.get(var)
        if env:
            _add(Path(env))

    if os.name == "nt":
        home = Path.home()
        _add(home / ".jdks")  # IDE 自管理 JDK
        if (home / ".jdks").is_dir():
            for d in (home / ".jdks").iterdir():
                _add(d)
        for base_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(base_name)
            if not base:
                continue
            base = Path(base)
            for pat in ("jdk*", "jre*", "Java*", "*JDK*", "temurin*", "zulu*", "microsoft*"):
                try:
                    for d in base.glob(pat):
                        _add(d)
                except OSError:
                    pass
            # 常见子目录
            for sub in ("Java", "Eclipse Adoptium", "Microsoft", "Amazon Corretto", "Zulu"):
                subp = base / sub
                if subp.is_dir():
                    for d in subp.iterdir():
                        _add(d)
    else:
        for base in (Path("/usr/lib/jvm"), Path("/opt/jdk"), Path("/usr/java")):
            if base.is_dir():
                for d in base.iterdir():
                    _add(d)
                _add(base)
        # PATH 中的 java 推导
    try:
        import shutil
        java_bin = shutil.which("java")
        if java_bin:
            _add(Path(java_bin).resolve().parent.parent)
    except Exception:
        pass

    return dirs


def _find_jvm():
    """定位 JVM 动态库路径（Windows jvm.dll / Linux libjvm.so）。"""
    for jdk in _candidate_jvm_dirs():
        if os.name == "nt":
            for rel in ("bin/server/jvm.dll", "bin/client/jvm.dll", "jre/bin/server/jvm.dll"):
                cand = jdk / rel
                if cand.exists():
                    return str(cand)
        else:
            for rel in ("lib/server/libjvm.so", "jre/lib/server/libjvm.so",
                        "jre/lib/amd64/server/libjvm.so"):
                cand = jdk / rel
                if cand.exists():
                    return str(cand)
    return None


def jvm_info():
    """返回 JVM 探测结果，用于诊断展示。"""
    jvm = _find_jvm()
    return {
        "found": jvm is not None,
        "path": jvm,
        "candidates": [str(p) for p in _candidate_jvm_dirs()],
        "started": _jvm_started,
    }


def _ensure_jvm():
    """确保 JVM 已启动（全局单例，classpath 包含 drivers/ 下全部 jar）。"""
    global _jvm_started
    with _jvm_lock:
        if _jvm_started:
            return
        jvm = _find_jvm()
        if not jvm:
            raise RuntimeError(
                "未找到 JDK/JRE 运行时。请安装 Java（JDK 8+）并设置 JAVA_HOME，"
                "或让平台自动探测（已尝试常见安装路径 / .jdks / PATH）。")
        jars = [str(p) for p in DRIVERS_DIR.glob("*.jar") if p.stat().st_size > 100000]
        if not jars:
            raise RuntimeError(f"drivers/ 目录下未发现 JDBC 驱动 jar：{DRIVERS_DIR}")
        import jpype
        # 低内存参数：平台仅用 JDBC 做短连接查询，SerialGC + 小堆降低资源占用，
        # 避免服务器内存/页面文件紧张时 JVM 启动失败（errno=1455 等）。
        # 注意：jpype 1.x 中 JVM 参数为位置参数（*jvmargs）。
        jpype.startJVM(
            "-Xmx256m", "-Xms32m", "-XX:MaxMetaspaceSize=128m",
            "-XX:+UseSerialGC", "-Djava.awt.headless=true",
            jvmpath=jvm,
            classpath=jars,
            convertStrings=True,
        )
        _jvm_started = True


# ---------------------------------------------------------------------------
# 连接与查询
# ---------------------------------------------------------------------------

def _resolve_config(db_type):
    """返回 (jar 路径, 驱动类, url 模板, probe_sql, list_sql, filter)。带 kingbase 降级。"""
    cfg = DRIVER_CONFIG.get(db_type)
    if not cfg:
        raise ValueError(f"暂不支持 JDBC 连接类型: {db_type}（支持: {', '.join(JDBC_DB_TYPES)}）")

    jar = DRIVERS_DIR / cfg["jar"]
    if not (jar.exists() and jar.stat().st_size > 100000):
        # kingbase 官方 jar 缺失时降级 PG 驱动
        if cfg.get("fallback_to_pg") and db_type == "kingbase":
            pg = DRIVER_CONFIG["postgresql"]
            return (DRIVERS_DIR / pg["jar"], pg["class"], pg["url"],
                    pg["probe"], cfg["list_sql"], cfg["filter"])
        raise FileNotFoundError(
            f"缺少 JDBC 驱动 jar: {cfg['jar']}（db_type={db_type}）。"
            f"请下载对应驱动放入 drivers/ 目录：{DRIVERS_DIR}")
    return jar, cfg["class"], cfg["url"], cfg["probe"], cfg["list_sql"], cfg["filter"]


def build_url(db_type, host, port, db):
    _, _, url_tpl, _, _, _ = _resolve_config(db_type)
    return url_tpl.format(host=host, port=_norm_port(db_type, port), db=(db or ""))


def connect(db_type, host, port, db, user, password, timeout=None):
    """建立 JDBC 连接并返回 connection（调用方负责 close）。"""
    jar, driver_class, url_tpl, _, _, _ = _resolve_config(db_type)
    _ensure_jvm()
    url = url_tpl.format(host=host, port=_norm_port(db_type, port), db=(db or ""))
    import jaydebeapi
    return jaydebeapi.connect(driver_class, url, [user or "", password or ""], jars=str(jar))


def _to_py(value):
    """将 JPype 返回值转换为 Python 原生类型。"""
    if value is None:
        return None
    try:
        import jpype
        if jpype.isJVMStarted() and isinstance(value, jpype.JObject):
            return str(value)
    except Exception:
        pass
    return value


def test_connection(db_type, host, port, db, user, password, timeout=15):
    """测试 JDBC 连接，返回 (ok, message, info)。"""
    t0 = time.monotonic()
    try:
        conn = connect(db_type, host, port, db, user, password, timeout=timeout)
        cfg = DRIVER_CONFIG.get(db_type, {})
        probe = cfg.get("probe", "SELECT 1")
        cur = conn.cursor()
        cur.execute(probe)
        row = cur.fetchone()
        cur.close()
        conn.close()
        ms = int((time.monotonic() - t0) * 1000)
        info = {
            "db_type": db_type,
            "host": host,
            "port": port,
            "db": db,
            "user": user,
            "driver_class": DRIVER_CONFIG.get(db_type, {}).get("class"),
            "latency_ms": ms,
            "probe_result": str(_to_py(row[0])) if row else "",
        }
        return True, f"JDBC 连接成功（{ms} ms），驱动: {info['driver_class']}", info
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return False, f"JDBC 连接失败（{ms} ms）: {e}", None


def list_databases(db_type, host, port, db, user, password, timeout=30):
    """通过 JDBC 拉取数据库/schema 列表。"""
    _, _, _, _, list_sql, filters = _resolve_config(db_type)
    conn = connect(db_type, host, port, db, user, password, timeout=timeout)
    try:
        cur = conn.cursor()
        cur.execute(list_sql)
        rows = [str(_to_py(r[0])) for r in cur.fetchall()]
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if filters:
        rows = [r for r in rows if r not in filters]
    return rows


def available_drivers():
    """返回各 db_type 的 JDBC 驱动就绪状态。"""
    result = {}
    for db_type, cfg in DRIVER_CONFIG.items():
        jar = DRIVERS_DIR / cfg["jar"]
        ready = jar.exists() and jar.stat().st_size > 100000
        if not ready and cfg.get("fallback_to_pg") and db_type == "kingbase":
            pg = DRIVER_CONFIG["postgresql"]
            ready = (DRIVERS_DIR / pg["jar"]).exists()
        result[db_type] = {
            "available": ready,
            "jar": cfg["jar"],
            "driver_class": cfg["class"],
            "fallback": bool(cfg.get("fallback_to_pg")),
        }
    return result
