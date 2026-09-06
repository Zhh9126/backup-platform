#!/usr/bin/env python3
"""离线环境自检脚本（部署到离线环境后首先运行）。

检查平台"零安装、离线自足"的各项前提：
1. Python 依赖（随 PyInstaller 打包，应全部内置）
2. JDBC 驱动 jar（drivers/ 目录）
3. JVM（内嵌 jdk/ 目录 或 服务器已有 JRE）—— 达梦/Oracle/金仓连接通道
4. dmPython（可选，需 libdmdpi.so 达梦客户端库）
5. 外部备份工具（可选，服务端插件离线包）

用法: python scripts/check_offline.py
退出码: 0=就绪 1=存在缺失（按报告处置）
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)

OK, WARN, FAIL = "✓", "⚠", "✗"
issues = []


def check(name, ok, msg="", warn_only=False):
    mark = OK if ok else (WARN if warn_only else FAIL)
    print(f"  {mark} {name}: {msg}")
    if not ok and not warn_only:
        issues.append(f"{name}: {msg}")


print("=" * 62)
print("离线环境自检（零安装 · 离线自足）")
print("=" * 62)

# ---- 1. Python 依赖 ----
print("\n[1] Python 依赖（应随平台包内置）")
mods = {
    "pymysql": "MySQL/MariaDB 连接",
    "psycopg2": "PostgreSQL 连接",
    "oracledb": "Oracle 连接（thin 免装客户端）",
    "paramiko": "SSH 通道",
    "jpype": "JDBC 通道",
    "jaydebeapi": "JDBC 通道",
    "flask": "Web 服务",
    "apscheduler": "调度",
    "minio": "MinIO/S3 对象存储",
    "pymongo": "MongoDB 备份恢复",
    "redis": "Redis 备份恢复",
    "pymysqlreplication": "MySQL binlog CDC",
    "zstandard": ".zst 压缩",
    "yaml": "配置",
}
for m, desc in mods.items():
    try:
        __import__(m)
        check(m, True, desc)
    except Exception as e:
        check(m, False, f"缺失（{desc}）— {type(e).__name__}: {e}")

# ---- 2. JDBC 驱动 jar ----
print("\n[2] JDBC 驱动 jar（drivers/ 随包分发）")
drivers = Path(ROOT, "drivers")
if FROZEN:
    drivers = Path(sys._MEIPASS) / "drivers"
need_jars = {
    "mysql-connector-j": "MySQL/MariaDB",
    "postgresql": "PostgreSQL/金仓兜底",
    "ojdbc": "Oracle",
    "DmJdbcDriver": "达梦（核心：dmPython 缺失时的唯一通道）",
}
if drivers.is_dir():
    jars = [p.name for p in drivers.rglob("*.jar")]
    print(f"  共 {len(jars)} 个 jar: {', '.join(jars[:8])}{'...' if len(jars) > 8 else ''}")
    for kw, desc in need_jars.items():
        hit = any(kw.lower() in j.lower() for j in jars)
        check(f"jar:{kw}", hit, desc + ("（就绪）" if hit else "（缺失！）"))
else:
    for kw, desc in need_jars.items():
        check(f"jar:{kw}", False, f"drivers/ 目录不存在（{desc}）")

# ---- 3. JVM ----
print("\n[3] JVM（达梦/Oracle/金仓 JDBC 通道必需）")
jvm = None
# 内嵌 jdk/ 目录（离线包标准布局）
for base in ([Path(sys._MEIPASS)] if FROZEN else [ROOT]) + [Path(ROOT)]:
    for name in ("jdk", "jre", "java", "runtime"):
        cand = base / name
        if (cand / "bin").is_dir() and any(
                (cand / "bin").glob("java*")):
            jvm = cand
            break
    if jvm:
        break
if not jvm:
    for var in ("JAVA_HOME", "JDK_HOME"):
        home = os.environ.get(var)
        if home and Path(home, "bin").is_dir():
            jvm = Path(home)
            break
if not jvm:
    which = shutil.which("java")
    if which:
        jvm = Path(which).parent.parent
msg = str(jvm) if jvm else "未找到！离线包应包含 jdk/ 目录（jlink 裁剪 JRE 或完整 JDK）；否则达梦/Oracle 仅剩 dmPython 通道"
check("JVM", bool(jvm), msg)

# ---- 4. dmPython（可选）----
print("\n[4] dmPython 达梦原生驱动（可选，JDBC 已可兜底）")
try:
    import dmPython  # noqa
    check("dmPython", True, "就绪（达梦原生通道）")
except Exception as e:
    check("dmPython", False,
          f"不可用（{e}）— JDBC 通道可兜底；如需原生通道，"
          f"将达梦客户端 libdmdpi.so 放入平台 drivers/dm/ 并配置 LD_LIBRARY_PATH",
          warn_only=True)

# ---- 5. 外部备份工具 ----
print("\n[5] 外部备份工具（服务端可选插件，离线包安装）")
tools = {"mysqldump": "MySQL 逻辑备份（服务端回退路径）",
         "pg_dump": "PG 逻辑备份（服务端回退路径）"}
for t, desc in tools.items():
    found = shutil.which(t)
    check(t, bool(found), f"{desc} — {'就绪 ' + found if found else '未装（可仅用 SSH 远程模式，数据库自带工具）'}",
          warn_only=True)

print("\n" + "=" * 62)
if issues:
    print(f"结论：存在 {len(issues)} 项缺失，需在打包/部署阶段补齐后重检：")
    for i in issues:
        print(f"  ✗ {i}")
    sys.exit(1)
print("结论：离线自足检查通过（平台零安装原则满足）")
sys.exit(0)
