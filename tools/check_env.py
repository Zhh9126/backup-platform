# -*- coding: utf-8 -*-
"""服务端环境自检：一键核对所有数据库驱动/JDBC 包是否就绪。

用法：python tools/check_env.py
按平台"服务端集中安装、客户端零安装"的设计，部署后先跑本脚本，
输出缺失项与安装指引；离线环境可提前在有网机器 pip download 离线包。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (模块名, 用途, pip 包名)
PY_DEPS = [
    ("pymysql", "MySQL / MariaDB 直连（备份/恢复/同步/对比）", "pymysql"),
    ("psycopg2", "PostgreSQL / 金仓 KingbaseES 直连（协议兼容）", "psycopg2-binary"),
    ("oracledb", "Oracle 瘦客户端直连（12.1+；免 Instant Client）", "oracledb"),
    ("paramiko", "SSH/SFTP 远程通道（远端备份/恢复/克隆）", "paramiko"),
    ("jaydebeapi", "JDBC 兜底通道（达梦/异构链路驱动缺失时）", "JayDeBeApi"),
    ("jpype", "JVM 内嵌（JayDeBeApi 依赖，需 JRE 11+）", "JPype1"),
    ("pymysqlreplication", "MySQL binlog 解析（准 CDP 实时备份）", "mysql-replication"),
    ("redis", "Redis 备份/巡检", "redis"),
    ("watchdog", "文件事件加速（可选，缺失自动降级轮询）", "watchdog"),
]

# 数据库类型 → 原生驱动 + JDBC jar
DB_MATRIX = [
    ("mysql", "pymysql", "mysql-connector-j-8.4.0.jar"),
    ("mariadb", "pymysql", "mariadb-java-client-3.4.1.jar"),
    ("postgresql", "psycopg2", "postgresql-42.7.5.jar"),
    ("kingbase", "psycopg2（协议兼容）", "kingbase（JDBC 兜底通道可用）"),
    ("oracle", "oracledb", "ojdbc11-23.4.0.24.05.jar"),
    ("dameng", "JDBC 兜底（dmPython 需随达梦安装介质编译）", "DmJdbcDriver18.jar"),
]


def main() -> int:
    print("=" * 64)
    print("服务端环境自检（数据库驱动 / JDBC 驱动包）")
    print("=" * 64)

    # 1) Python 驱动
    print("\n[1] Python 驱动模块")
    missing = []
    for mod, usage, pkg in PY_DEPS:
        try:
            __import__(mod)
            print(f"  ✓ {mod:<18} {usage}")
        except ImportError:
            print(f"  ✗ {mod:<18} 缺失 → pip install {pkg}")
            missing.append(pkg)

    # 2) JDBC 驱动包
    print("\n[2] JDBC 驱动包（drivers/ 目录，JDBC 兜底通道使用）")
    drivers_dir = os.path.join(BASE, "drivers")
    for db, native, jar in DB_MATRIX:
        jar_path = os.path.join(drivers_dir, jar)
        has_jar = os.path.isfile(jar_path)
        mark = "✓" if has_jar else "○"
        print(f"  {mark} {db:<12} 原生: {native}")
        if has_jar:
            print(f"               JDBC: {jar}")

    # 3) JRE（JDBC 兜底需要）
    print("\n[3] Java 运行时（JDBC 兜底通道需要 JRE 11+）")
    java_home = os.environ.get("JAVA_HOME")
    java_bin = None
    for p in ([os.path.join(java_home, "bin", "java")] if java_home else []) + \
             ["/usr/bin/java", "/usr/local/bin/java"]:
        if os.path.exists(p):
            java_bin = p
            break
    print(f"  {'✓' if java_bin else '✗'} java: {java_bin or '未找到（JDBC 兜底通道不可用，原生驱动不受影响）'}")

    # 4) 结论
    print("\n结论：", end="")
    if missing:
        print(f"缺失 {len(missing)} 个 Python 依赖：{'、'.join(missing)}")
        print("在线环境：pip install " + " ".join(missing))
        print("离线环境：在有网机器 pip download -d offline_pkgs " +
              " ".join(missing) + " 后，目标机 pip install --no-index "
              "--find-links=offline_pkgs <包名>")
        return 1
    print("全部就绪 ✓（客户端零安装，所有驱动集中在服务端）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
