# -*- coding: utf-8 -*-
"""
直连通道模块（原 JDBC 连接方式模块）

连接测试 / 拉库列表的统一入口，**原生 Python 驱动直连优先，JDBC 为可选兜底**：

1. 原生直连（core/native_conn.py）：pymysql / psycopg2 / oracledb / dmPython，
   纯 Python 或 wheel 分发，**不依赖 Java/JVM** —— Docker 与离线镜像默认即用；
2. JDBC 兜底（本模块 JPype 桥接）：仅当原生驱动缺失或服务端不支持
   （如 Oracle 11g 瘦模式）且本机恰有 JRE + drivers/ 驱动 jar 时启用。

设计原则：
- 「原有连接方式优先」：本模块仅作为连接测试 / 拉库列表的兜底通道与显式入口，
  备份执行仍走原有（SSH / 本机客户端）方式。
- JVM 全局单例：JPype 限制 JVM 只能启动一次，classpath 在首次启动时
  一次性包含 drivers/ 下全部 jar，之后任何 db_type 均可复用。
- 自动探测 JDK：支持 JAVA_HOME / 常见安装路径 / .jdks / PATH 推导。

依赖：原生通道只需 requirements.txt 必选依赖；JDBC 兜底另需
jpype1、jaydebeapi、本机 JDK（JRE 亦可）、drivers/ 下的驱动 jar（均可选）。
"""

from __future__ import annotations

import os
import json
import re
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
        # 官方 kingbase8.jar（Maven Central: cn.com.kingbase）已内置 drivers/，
        # 缺失时自动降级用 PG 驱动
        "jar": "kingbase8-8.6.0.jar",
        "class": "com.kingbase8.Driver",
        "url": "jdbc:kingbase8://{host}:{port}/{db}?connectTimeout=8&socketTimeout=15",
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM sys_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
        "fallback_to_pg": True,
    },
    "opengauss": {
        # openGauss 官方 JDBC 为 PG 分叉：类仍在 org.postgresql 包，
        # URL 前缀用 jdbc:opengauss://（Maven Central: org.opengauss:opengauss-jdbc）
        "jar": "opengauss-jdbc-6.0.6.jar",
        "class": "org.postgresql.Driver",
        "url": "jdbc:opengauss://{host}:{port}/{db}?connectTimeout=8&socketTimeout=15",
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
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
        # DM 为单库实例架构，URL 中的 db 段实为 schema：留空或填登录用户
        # 的 schema 均可（默认表空间下直接连实例），填其他库名会报
        # "Invalid schema name"
        "url": "jdbc:dm://{host}:{port}?connectTimeout=8000&socketTimeout=15000",
        "probe": "SELECT 1",
        "list_sql": ("SELECT username FROM all_users "
                     "ORDER BY 1"),
        # 注意：达梦的 SYSDBA 既是管理员又常被用作业务 schema，不能排除
        "filter": ("SYS", "SYSAUDITOR", "SYSSSO", "SYSDBAOPER", "CTISYS", "CJISYS"),
    },
    # ---------------- 2026-11-05 全库型驱动内置（Maven Central 官方包） ----------------
    "clickhouse": {
        "jar": "clickhouse-jdbc-0.3.2-patch11-all.jar",
        "class": "com.clickhouse.jdbc.ClickHouseDriver",
        "url": "jdbc:clickhouse://{host}:{port}/{db}",
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("system", "INFORMATION_SCHEMA", "information_schema"),
    },
    "db2": {
        "jar": "db2jcc-12.1.5.0.jar",
        "class": "com.ibm.db2.jcc.DB2Driver",
        "url": "jdbc:db2://{host}:{port}/{db}",
        "probe": "SELECT 1 FROM SYSIBM.SYSDUMMY1",
        "list_sql": "SELECT schemaname FROM syscat.schemata WHERE schemaname NOT LIKE 'SYS%' ORDER BY 1",
        "filter": (),
    },
    "tdengine": {
        # RestfulDriver 纯 Java（HTTP 6041），离线环境无需 libtaos.so 原生库
        "jar": "taos-jdbcdriver-3.10.0.jar",
        "class": "com.taosdata.jdbc.rs.RestfulDriver",
        "url": "jdbc:TAOS-RS://{host}:{port}/",
        "probe": "SELECT SERVER_VERSION()",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "performance_schema", "log"),
    },
    "oceanbase": {
        "jar": "oceanbase-client-2.4.19.jar",
        "class": "com.oceanbase.jdbc.Driver",
        "url": "jdbc:oceanbase://{host}:{port}/{db}",
        "probe": "SELECT 1",
        "list_sql": "SHOW DATABASES",
        "filter": ("information_schema", "mysql", "sys", "oceanbase"),
    },
    "highgo": {
        "jar": "HgdbJdbc-6.2.5.jar",
        "class": "com.highgo.jdbc.Driver",
        "url": "jdbc:highgo://{host}:{port}/{db}?connectTimeout=8&socketTimeout=15",
        "probe": "SELECT 1",
        "list_sql": "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1",
        "filter": (),
    },
    "yashandb": {
        "jar": "yashandb-jdbc-1.10.7.jar",
        "class": "com.yashandb.jdbc.YasDriver",
        "url": "jdbc:yasdb://{host}:{port}/{db}",
        "probe": "SELECT 1 FROM DUAL",
        "list_sql": "SELECT username FROM all_users ORDER BY 1",
        "filter": ("SYS", "SYSTEM", "SYSAUDITOR", "SYSSSO"),
    },
}

# 使用 JDBC 的数据库类型（redis / mongodb 走 python 原生驱动，不需要 JDBC）
JDBC_DB_TYPES = tuple(DRIVER_CONFIG.keys())

# ---------------------------------------------------------------------------
# 页面注册（用户上传 jar 后在页面绑定到库型，持久化到 instance/ 下 JSON）
# ---------------------------------------------------------------------------

# 已知库型的连接模板：注册时提供驱动类/URL 模板/探活 SQL 默认值。
# jar 由注册条目决定，其余字段缺省取这里。
KNOWN_DRIVER_TEMPLATES = {
    "mysql": {"label": "MySQL", "class": "com.mysql.cj.jdbc.Driver",
              "url": DRIVER_CONFIG["mysql"]["url"], "probe": "SELECT 1",
              "list_sql": DRIVER_CONFIG["mysql"]["list_sql"],
              "filter": list(DRIVER_CONFIG["mysql"]["filter"])},
    "mariadb": {"label": "MariaDB", "class": "org.mariadb.jdbc.Driver",
                "url": DRIVER_CONFIG["mariadb"]["url"], "probe": "SELECT 1",
                "list_sql": DRIVER_CONFIG["mariadb"]["list_sql"],
                "filter": list(DRIVER_CONFIG["mariadb"]["filter"])},
    "postgresql": {"label": "PostgreSQL", "class": "org.postgresql.Driver",
                   "url": DRIVER_CONFIG["postgresql"]["url"], "probe": "SELECT 1",
                   "list_sql": DRIVER_CONFIG["postgresql"]["list_sql"],
                   "filter": []},
    "kingbase": {"label": "KingbaseES", "class": "com.kingbase8.Driver",
                 "url": DRIVER_CONFIG["kingbase"]["url"], "probe": "SELECT 1",
                 "list_sql": DRIVER_CONFIG["kingbase"]["list_sql"],
                 "filter": [], "fallback_to_pg": True},
    "opengauss": {"label": "openGauss", "class": "org.postgresql.Driver",
                  "url": DRIVER_CONFIG["opengauss"]["url"], "probe": "SELECT 1",
                  "list_sql": DRIVER_CONFIG["opengauss"]["list_sql"],
                  "filter": []},
    "oracle": {"label": "Oracle", "class": "oracle.jdbc.OracleDriver",
               "url": DRIVER_CONFIG["oracle"]["url"],
               "probe": "SELECT 1 FROM DUAL",
               "list_sql": DRIVER_CONFIG["oracle"]["list_sql"], "filter": []},
    "dameng": {"label": "达梦", "class": "dm.jdbc.driver.DmDriver",
               "url": DRIVER_CONFIG["dameng"]["url"], "probe": "SELECT 1",
               "list_sql": DRIVER_CONFIG["dameng"]["list_sql"],
               "filter": list(DRIVER_CONFIG["dameng"]["filter"])},
    "sqlserver": {"label": "SQL Server",
                  "class": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
                  "url": ("jdbc:sqlserver://{host}:{port};databaseName={db};"
                          "encrypt=false;trustServerCertificate=true;"
                          "loginTimeout=8;socketTimeout=15"),
                  "probe": "SELECT 1",
                  "list_sql": ("SELECT name FROM sys.databases "
                               "WHERE database_id > 4 ORDER BY 1"),
                  "filter": []},
    "clickhouse": {"label": "ClickHouse", "class": "com.clickhouse.jdbc.ClickHouseDriver",
                   "url": DRIVER_CONFIG["clickhouse"]["url"], "probe": "SELECT 1",
                   "list_sql": DRIVER_CONFIG["clickhouse"]["list_sql"],
                   "filter": list(DRIVER_CONFIG["clickhouse"]["filter"])},
    "db2": {"label": "Db2", "class": "com.ibm.db2.jcc.DB2Driver",
            "url": DRIVER_CONFIG["db2"]["url"], "probe": DRIVER_CONFIG["db2"]["probe"],
            "list_sql": DRIVER_CONFIG["db2"]["list_sql"], "filter": []},
    "tdengine": {"label": "TDengine", "class": "com.taosdata.jdbc.rs.RestfulDriver",
                 "url": DRIVER_CONFIG["tdengine"]["url"],
                 "probe": DRIVER_CONFIG["tdengine"]["probe"],
                 "list_sql": DRIVER_CONFIG["tdengine"]["list_sql"], "filter": []},
    "oceanbase": {"label": "OceanBase", "class": "com.oceanbase.jdbc.Driver",
                  "url": DRIVER_CONFIG["oceanbase"]["url"], "probe": "SELECT 1",
                  "list_sql": DRIVER_CONFIG["oceanbase"]["list_sql"],
                  "filter": list(DRIVER_CONFIG["oceanbase"]["filter"])},
    "highgo": {"label": "瀚高 HighGo", "class": "com.highgo.jdbc.Driver",
               "url": DRIVER_CONFIG["highgo"]["url"], "probe": "SELECT 1",
               "list_sql": DRIVER_CONFIG["highgo"]["list_sql"], "filter": []},
    "yashandb": {"label": "崖山 YashanDB", "class": "com.yashandb.jdbc.YasDriver",
                 "url": DRIVER_CONFIG["yashandb"]["url"],
                 "probe": DRIVER_CONFIG["yashandb"]["probe"],
                 "list_sql": DRIVER_CONFIG["yashandb"]["list_sql"],
                 "filter": list(DRIVER_CONFIG["yashandb"]["filter"])},
    # ---------------- 协议复用型（无独立官方 JDBC，用协议兼容驱动） ----------------
    "gbase8a": {"label": "GBase 8a（MySQL 协议）", "class": "com.gbase.jdbc.Driver",
                "url": "jdbc:gbase://{host}:{port}/{db}?useSSL=false&characterEncoding=utf8",
                "probe": "SELECT 1", "list_sql": "SHOW DATABASES",
                "filter": ["information_schema", "performance_schema", "mysql", "sys"]},
    "gbase8s": {"label": "GBase 8s", "class": "com.gbasedbt.jdbc.Driver",
                "url": "jdbc:gbasedbt-sqli://{host}:{port}/{db}:informixserver=gbaseserver",
                "probe": "SELECT 1 FROM systables WHERE tabid = 1",
                "list_sql": "SELECT name FROM sysmaster:sysdatabases",
                "filter": []},
    "greenplum": {"label": "Greenplum（PG 协议）", "class": "org.postgresql.Driver",
                  "url": "jdbc:postgresql://{host}:{port}/{db}", "probe": "SELECT 1",
                  "list_sql": DRIVER_CONFIG["postgresql"]["list_sql"], "filter": []},
    "ivorysql": {"label": "IvorySQL（PG 协议）", "class": "org.postgresql.Driver",
                 "url": "jdbc:postgresql://{host}:{port}/{db}", "probe": "SELECT 1",
                 "list_sql": DRIVER_CONFIG["postgresql"]["list_sql"], "filter": []},
    "tidb": {"label": "TiDB（MySQL 协议）", "class": "com.mysql.cj.jdbc.Driver",
             "url": DRIVER_CONFIG["mysql"]["url"], "probe": "SELECT 1",
             "list_sql": DRIVER_CONFIG["mysql"]["list_sql"],
             "filter": ["information_schema", "performance_schema", "mysql", "sys"]},
    "uxdb": {"label": "优炫 UXDB（PG 协议）", "class": "org.postgresql.Driver",
             "url": "jdbc:postgresql://{host}:{port}/{db}", "probe": "SELECT 1",
             "list_sql": DRIVER_CONFIG["postgresql"]["list_sql"], "filter": []},
}

USER_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "instance" / "jdbc_user_registry.json"


def _load_user_registry() -> dict:
    """读取用户页面注册的驱动绑定：{db_type: {jar/class/url/probe/list_sql/filter}}。"""
    try:
        if USER_REGISTRY_PATH.exists():
            data = json.loads(USER_REGISTRY_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_user_registry(reg: dict) -> None:
    USER_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_REGISTRY_PATH.write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def effective_config(db_type: str) -> dict:
    """合并视图：用户页面注册覆盖内置 DRIVER_CONFIG。"""
    reg = _load_user_registry().get(db_type)
    if reg:
        return dict(reg)
    cfg = DRIVER_CONFIG.get(db_type)
    return dict(cfg) if cfg else {}


def effective_db_types() -> tuple:
    """当前可用的 JDBC 库型（内置 ∪ 用户注册），保持内置顺序在前。"""
    merged = list(DRIVER_CONFIG.keys())
    for k in _load_user_registry().keys():
        if k not in merged:
            merged.append(k)
    return tuple(merged)


def register_driver(jar_name: str, db_type: str, driver_class: str = None,
                    url: str = None) -> dict:
    """把一个 jar 绑定到库型（页面注册）：覆盖该库型现有绑定（内置或用户）。

    db_type 必须在 KNOWN_DRIVER_TEMPLATES 内（提供驱动类/URL 默认值）；
    driver_class/url 可显式覆盖模板。
    """
    jar_name = _check_jar_name(jar_name)
    db_type = (db_type or "").strip().lower()
    tpl = KNOWN_DRIVER_TEMPLATES.get(db_type)
    if not tpl:
        raise ValueError(
            f"不支持的库型: {db_type}（可注册: "
            f"{', '.join(sorted(KNOWN_DRIVER_TEMPLATES))}）")
    p = DRIVERS_DIR / jar_name
    if not (p.exists() and p.stat().st_size > 100000):
        raise ValueError(f"驱动文件不存在: {jar_name}")
    reg = _load_user_registry()
    entry = {
        "jar": jar_name,
        "class": (driver_class or "").strip() or tpl["class"],
        "url": (url or "").strip() or tpl["url"],
        "probe": tpl["probe"],
        "list_sql": tpl["list_sql"],
        "filter": tpl.get("filter") or [],
        "label": tpl.get("label") or db_type,
        "user_registered": True,
        "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    reg[db_type] = entry
    _save_user_registry(reg)
    return {"db_type": db_type, **entry}


def unregister_driver(db_type: str) -> dict:
    """解除库型的用户注册绑定（回退内置 DRIVER_CONFIG；内置库型回退后仍注册）。"""
    db_type = (db_type or "").strip().lower()
    reg = _load_user_registry()
    if db_type not in reg:
        raise ValueError(f"库型 {db_type} 没有用户注册绑定")
    del reg[db_type]
    _save_user_registry(reg)
    return {"db_type": db_type, "unregistered": True,
            "fallback_builtin": db_type in DRIVER_CONFIG}


def jar_bound_db_types() -> dict:
    """jar 文件名 → 绑定的库型列表（含内置与用户注册，供列表页展示）。"""
    out: dict = {}
    for db_type, cfg in DRIVER_CONFIG.items():
        if cfg.get("jar"):
            out.setdefault(cfg["jar"], []).append(db_type)
    for db_type, cfg in _load_user_registry().items():
        if cfg.get("jar"):
            out.setdefault(cfg["jar"], []).append(db_type)
    return out

# 各类型默认端口（与引擎默认值对齐）
DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "kingbase": 54321,
    "opengauss": 26000,
    "oracle": 1521,
    "dameng": 5236,
    "sqlserver": 1433,
    "clickhouse": 8123,
    "db2": 50000,
    "tdengine": 6041,
    "oceanbase": 2881,
    "highgo": 5866,
    "yashandb": 1688,
    "gbase8a": 5258,
    "gbase8s": 9088,
    "greenplum": 5432,
    "ivorysql": 5432,
    "tidb": 4000,
    "uxdb": 5432,
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


_last_jvm_major = 0


def _jvm_major_version(jdk_dir: Path) -> int:
    """解析 JDK 根目录的 Java 主版本（release 文件优先，目录名兜底）。"""
    try:
        rel = jdk_dir / "release"
        if rel.is_file():
            for line in rel.read_text(errors="ignore").splitlines():
                if line.startswith("JAVA_VERSION"):
                    val = line.split("=", 1)[1].strip().strip('"')
                    major = val.split(".")[0]
                    if major.isdigit():
                        return int(major)
    except Exception:
        pass
    name = jdk_dir.name
    m = re.search(r"java-(\d+)", name)
    if m:
        n = int(m.group(1))
        return 8 if n == 1 else n
    m = re.search(r"1\.(\d+)\.0", name)
    if m:
        return 8 if m.group(1) == "8" else int(m.group(1))
    return 0


def _find_jvm():
    """定位 JVM 动态库路径（Windows jvm.dll / Linux libjvm.so）。

    JPype 1.6 要求 Java 11+（JDK 8 会报误导性的
    "Can't find org.jpype.jar support library"），因此候选按
    Java 主版本降序选择；仅存在 JDK 8 时回退使用。
    """
    global _last_jvm_major
    candidates = []  # (major, jvm_path)
    for jdk in _candidate_jvm_dirs():
        if os.name == "nt":
            rels = ("bin/server/jvm.dll", "bin/client/jvm.dll", "jre/bin/server/jvm.dll")
        else:
            rels = ("lib/server/libjvm.so", "jre/lib/server/libjvm.so",
                    "jre/lib/amd64/server/libjvm.so")
        for rel in rels:
            cand = jdk / rel
            if cand.exists():
                candidates.append((_jvm_major_version(jdk), str(cand)))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    _last_jvm_major = candidates[0][0]
    return candidates[0][1]


def jvm_info():
    """返回 JVM 探测结果，用于诊断展示。"""
    jvm = _find_jvm()
    info = {
        "found": jvm is not None,
        "path": jvm,
        "java_major": _last_jvm_major,
        "candidates": [str(p) for p in _candidate_jvm_dirs()],
        "started": _jvm_started,
    }
    if jvm and _last_jvm_major and _last_jvm_major < 11:
        info["warning"] = ("当前 JVM 为 Java %d；JPype 1.6 需要 Java 11+，"
                           "JDK 8 下启动会失败（建议安装 JDK 11+）" % _last_jvm_major)
    return info


def _ensure_jvm():
    """确保 JVM 已启动（全局单例，classpath 包含 drivers/ 下全部 jar）。"""
    global _jvm_started
    with _jvm_lock:
        import jpype
        # 守卫：JVM 可能已被其他路径（如 jaydebeapi 自动启动）拉起，
        # 重复 startJVM 会抛 "JVM is already started"
        try:
            if _jvm_started or jpype.isJVMStarted():
                _jvm_started = True
                return
        except Exception:
            pass
        jvm = _find_jvm()
        if not jvm:
            raise RuntimeError(
                "未找到 JDK/JRE 运行时。请安装 Java（JDK 8+）并设置 JAVA_HOME，"
                "或让平台自动探测（已尝试常见安装路径 / .jdks / PATH）。")
        # 顶层 = 数据库驱动 jar（>100KB 防损坏/错误页）；drivers/jdbc/ = 依赖库
        # （jackson/httpclient/slf4j 等，小文件用 >10KB 防损坏），随 classpath 一起加载，
        # 且不进驱动管理页列表（页面只展示数据库驱动，避免噪音行）。
        jars = [str(p) for p in DRIVERS_DIR.glob("*.jar") if p.stat().st_size > 100000]
        jars += [str(p) for p in (DRIVERS_DIR / "jdbc").glob("*.jar")
                 if p.stat().st_size > 10000]
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
    """返回 (jar 路径, 驱动类, url 模板, probe_sql, list_sql, filter)。带 kingbase 降级。

    走合并视图（用户页面注册覆盖内置 DRIVER_CONFIG）。
    """
    cfg = effective_config(db_type)
    if not cfg:
        raise ValueError(f"暂不支持 JDBC 连接类型: {db_type}（支持: {', '.join(effective_db_types())}）")

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
    """测试直连：原生驱动优先，JDBC 兜底，返回 (ok, message, info)。"""
    # 1) 原生直连优先（无 Java 依赖）；驱动缺失抛 DriverUnavailable
    from core import native_conn
    try:
        ok, msg, info = native_conn.test_connection(
            db_type, host, port, db, user, password, timeout=timeout)
    except native_conn.DriverUnavailable as e:
        ok, msg, info = False, str(e), None
    if ok:
        return ok, msg, info
    native_err = msg

    # 2) JDBC 兜底（原生驱动缺失 / Oracle 11g 瘦模式不支持 / 达梦无 dmPython 等）
    t0 = time.monotonic()
    try:
        conn = connect(db_type, host, port, db, user, password, timeout=timeout)
        cfg = effective_config(db_type)
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
            "mode": "jdbc",
            "driver_class": cfg.get("class"),
            "latency_ms": ms,
            "probe_result": str(_to_py(row[0])) if row else "",
        }
        return True, f"JDBC 连接成功（{ms} ms），驱动: {info['driver_class']}", info
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        # 两者都失败时优先返回原生通道的错误（更贴近真实原因）
        return False, f"连接失败（{ms} ms）: {native_err or e}", None


def list_databases(db_type, host, port, db, user, password, timeout=30):
    """拉取数据库/schema 列表：原生驱动优先，JDBC 兜底。"""
    # 原生驱动缺失时走 JDBC 兜底；原生连接失败则错误信息更真实，直接抛出
    from core import native_conn
    try:
        return native_conn.list_databases(
            db_type, host, port, db, user, password, timeout=timeout)
    except native_conn.DriverUnavailable:
        pass
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
    """返回各 db_type 的 JDBC 驱动就绪状态（含用户页面注册的库型）。"""
    result = {}
    for db_type in effective_db_types():
        cfg = effective_config(db_type)
        jar = DRIVERS_DIR / cfg["jar"]
        ready = jar.exists() and jar.stat().st_size > 100000
        if not ready and cfg.get("fallback_to_pg") and db_type == "kingbase":
            pg = effective_config("postgresql")
            ready = (DRIVERS_DIR / pg["jar"]).exists()
        result[db_type] = {
            "available": ready,
            "jar": cfg["jar"],
            "driver_class": cfg.get("class"),
            "fallback": bool(cfg.get("fallback_to_pg")),
            "user_registered": bool(cfg.get("user_registered")),
            "label": cfg.get("label") or config_label(db_type),
        }
    return result


def config_label(db_type: str) -> str:
    return KNOWN_DRIVER_TEMPLATES.get(db_type, {}).get("label") or db_type


# ---------------------------------------------------------------------------
# 驱动文件管理（用于插件页面上传 / 下载 / 删除 jar 包）
# ---------------------------------------------------------------------------

import re as _re
from typing import Iterable as _Iterable


# 合法 jar 文件名：只允许 ASCII 字母数字、点、下划线、横线
_JAR_NAME_RE = _re.compile(r"^[A-Za-z0-9._\-]{1,200}\.jar$")


def _registered_jar_names() -> set:
    """返回已注册（内置 + 用户页面注册）引用的 jar 文件名集合。"""
    names = set()
    for cfg in DRIVER_CONFIG.values():
        if cfg.get("jar"):
            names.add(cfg["jar"])
    for cfg in _load_user_registry().values():
        if cfg.get("jar"):
            names.add(cfg["jar"])
    return names


def list_driver_files() -> list:
    """列出 drivers/ 目录下所有 jar 文件及其元信息。

    返回: [{"name", "size", "mtime", "registered", "mtime_h", "bound_db_types"}, ...]
    """
    registered = _registered_jar_names()
    bound = jar_bound_db_types()
    items: list = []
    if DRIVERS_DIR.exists():
        for jar in sorted(DRIVERS_DIR.glob("*.jar")):
            st = jar.stat()
            items.append({
                "name": jar.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "mtime_h": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(st.st_mtime)),
                "registered": jar.name in registered,
                "bound_db_types": bound.get(jar.name, []),
            })
    return items


def _check_jar_name(name: str) -> str:
    """校验并返回清理后的 jar 文件名；非法时抛 ValueError。"""
    if not isinstance(name, str):
        raise ValueError("name must be str")
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("invalid path separator")
    if not _JAR_NAME_RE.match(name):
        raise ValueError(
            "filename must be ASCII alnum/_/- and end with .jar (max 200)"
        )
    return name


def save_driver_file(name: str, data: bytes) -> dict:
    """把上传的 jar 内容写入 drivers/。不允许覆盖已存在文件。"""
    name = _check_jar_name(name)
    if not isinstance(data, (bytes, bytearray)) or len(data) < 100:
        raise ValueError("jar too small (<100B)")
    if len(data) > 200 * 1024 * 1024:
        raise ValueError("jar too large (>200MB)")
    # 简单魔数校验：jar/zip 都是 PK\x03\x04
    if not data.startswith(b"PK\x03\x04"):
        raise ValueError("not a valid jar/zip file (missing PK magic)")
    DRIVERS_DIR.mkdir(parents=True, exist_ok=True)
    dst = DRIVERS_DIR / name
    if dst.exists():
        raise ValueError(f"file exists: {name}")
    dst.write_bytes(data)
    st = dst.stat()
    return {
        "name": name,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "registered": name in _registered_jar_names(),
    }


def delete_driver_file(name: str) -> dict:
    """删除一个 jar：被 DRIVER_CONFIG 引用的不能删；其他允许删除。"""
    name = _check_jar_name(name)
    if name in _registered_jar_names():
        raise ValueError(
            f"jar '{name}' is registered in DRIVER_CONFIG; "
            "edit core/jdbc.py to remove the reference first"
        )
    p = DRIVERS_DIR / name
    if not p.exists() or not p.is_file():
        raise ValueError(f"file not found: {name}")
    p.unlink()
    return {"name": name, "deleted": True}


def read_driver_file(name: str) -> bytes:
    """读取一个 jar 的原始字节（供下载接口使用）。"""
    name = _check_jar_name(name)
    p = DRIVERS_DIR / name
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(name)
    return p.read_bytes()
