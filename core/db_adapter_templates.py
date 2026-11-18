# -*- coding: utf-8 -*-
"""可插拔数据库类型 —— 模板目录（Catalog）。

设计目标：让「数据库类型（可插拔适配器）」页面能一次性覆盖业界主流数据库，
而不是靠用户从零写脚本。

覆盖分层（与 docs/db_type_coverage_20261113.md 对齐）：

1. **内置引擎**（21 种，代码静态注册，见 core/engines/__init__.py）：
   MySQL / MariaDB / PostgreSQL / Oracle / SQL Server / 达梦 / 金仓 / openGauss /
   磐维 / 瀚高 / 优炫 / TiDB / OceanBase / Redis / MongoDB / Neo4j / 向量库 /
   对象存储 / NAS / 文件。
2. **协议兼容模板**（本文件的 pg / mysql / oracle 三族）：与已验证引擎同族工具
   （pg_dump、mysqldump、exp/imp），只是换个发行版/云原生版本，工具参数基本一致
   → 标记 ``verification = "protocol"``。
3. **专有工具模板**（olap / ts / nosql / graph / other）：各厂商自带工具或
   API/SQL 导出，命令骨架按官方文档给出 → 标记 ``verification = "template"``，
   首次使用请点「测试」按钮核对工具路径与参数。

诚实边界：``verification = "template"`` 的条目**未经本平台真机验证**，仅保证
脚本骨架可执行、变量注入完整；厂商工具名/参数随版本变化，需现场核对。

脚本环境变量由平台注入（core/engines/base.py）：
    PLATFORM_DB_HOST / PLATFORM_DB_PORT / PLATFORM_DB_USER / PLATFORM_DB_PASSWORD /
    PLATFORM_DB_NAME / PLATFORM_DB_TYPE / PLATFORM_BACKUP_TYPE / PLATFORM_BACKUP_LEVEL /
    PLATFORM_BACKUP_SCOPE / PLATFORM_TABLES / PLATFORM_BACKUP_DIR / PLATFORM_TASK_ID /
    PLATFORM_TASK_NAME
恢复额外注入：
    PLATFORM_BACKUP_FILE（推送到服务器上的备份文件路径）、PLATFORM_RESTORE_DB
适配器 ``params`` 里的键注入为 ``PLATFORM_PARAM_<KEY>``（见 params_hint）。
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------- 通用工具片段

_PATH_PREP = (
    'if [ -n "${PLATFORM_PARAM_TOOL_BIN:-}" ]; then export PATH="${PLATFORM_PARAM_TOOL_BIN}:$PATH"; fi\n'
)


def _pg_scripts(pw_style: str = "env", maint_db: str = "postgres") -> dict:
    """PostgreSQL 协议族脚本文本（pg_dump/pg_restore 或等价工具）。

    pw_style:
        env → 用 PGPASSWORD 环境变量（PostgreSQL / 瀚高 / 优炫 / 金仓）
        w   → 用 -W 显式传密码（openGauss / GaussDB / Vastbase 的 gs_dump 系列）
    """
    if pw_style == "w":
        pw_lines = 'PW=(-W "$PLATFORM_DB_PASSWORD")\n'
        pw_env = ""
    else:
        pw_env = 'export PGPASSWORD="$PLATFORM_DB_PASSWORD"\n'
        pw_lines = "PW=()\n"
    backup = (
        "set -e\n"
        'mkdir -p "$PLATFORM_BACKUP_DIR"\n'
        + pw_env
        + _PATH_PREP
        + pw_lines
        + 'DUMP="${PLATFORM_PARAM_DUMP_BIN:-pg_dump}"\n'
        + '"$DUMP" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" '
        + '"${PW[@]}" -Fc -f "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.dump" "$PLATFORM_DB_NAME"\n'
        + 'echo "BACKUP_OK size=$(du -h "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.dump" | cut -f1)"\n'
    )
    restore = (
        "set -e\n"
        + pw_env
        + _PATH_PREP
        + pw_lines
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-psql}"\n'
        + 'REST="${PLATFORM_PARAM_RESTORE_BIN:-pg_restore}"\n'
        + 'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
        + '"$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" "${PW[@]}" '
        + f'-d {maint_db} -c "CREATE DATABASE \\"$TDB\\"" >/dev/null 2>&1 || true\n'
        + '"$REST" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" "${PW[@]}" '
        + '-d "$TDB" --no-owner --no-privileges "$PLATFORM_BACKUP_FILE"\n'
        + 'echo "RESTORE_OK db=$TDB"\n'
    )
    test_conn = (
        "set -e\n" + pw_env + _PATH_PREP + pw_lines
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-psql}"\n'
        + '"$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" "${PW[@]}" '
        + f'-d {maint_db} -t -c "SELECT version()" | head -1\n'
        + 'echo CONN_OK\n'
    )
    list_dbs = (
        pw_env + _PATH_PREP + pw_lines
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-psql}"\n'
        + '"$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" "${PW[@]}" '
        + f'-d {maint_db} -t -A -c "SELECT datname FROM pg_database WHERE datistemplate=false"\n'
    )
    return {
        "script_backup_full": backup,
        "script_restore": restore,
        "script_test_conn": test_conn,
        "script_list_dbs": list_dbs,
    }


def _mysql_scripts(gtid_flag: str = " --set-gtid-purged=OFF", extra_dump: str = "") -> dict:
    """MySQL 协议族脚本文本（mysqldump / mysql）。"""
    backup = (
        "set -e\n"
        'mkdir -p "$PLATFORM_BACKUP_DIR"\n'
        'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
        + _PATH_PREP
        + 'DUMP="${PLATFORM_PARAM_DUMP_BIN:-mysqldump}"\n'
        + '"$DUMP" --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
        + "--single-transaction --quick --routines --triggers --events" + gtid_flag + extra_dump
        + ' "$PLATFORM_DB_NAME" | gzip -c > "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.sql.gz"\n'
        + 'echo "BACKUP_OK size=$(du -h "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.sql.gz" | cut -f1)"\n'
    )
    restore = (
        "set -e\n"
        'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
        + _PATH_PREP
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
        + 'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
        + '"$CLI" --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
        + '-e "CREATE DATABASE IF NOT EXISTS \\`$TDB\\` DEFAULT CHARACTER SET utf8mb4"\n'
        + 'gunzip -c "$PLATFORM_BACKUP_FILE" | "$CLI" --no-defaults -h "$PLATFORM_DB_HOST" '
        + '-P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" "$TDB"\n'
        + 'echo "RESTORE_OK db=$TDB"\n'
    )
    test_conn = (
        "set -e\n"
        'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
        + _PATH_PREP
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
        + '"$CLI" --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
        + '-e "SELECT version()"\n'
        + 'echo CONN_OK\n'
    )
    list_dbs = (
        'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
        + _PATH_PREP
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
        + '"$CLI" --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
        + '-N -e "SHOW DATABASES" | grep -v -E '
        + "'^(information_schema|performance_schema|mysql|sys|__recyclebin__)$'\n"
    )
    return {
        "script_backup_full": backup,
        "script_restore": restore,
        "script_test_conn": test_conn,
        "script_list_dbs": list_dbs,
    }


def _exp_imp_scripts(exp_default: str = "exp", imp_default: str = "imp",
                     owner_clause: str = "owner") -> dict:
    """Oracle 兼容族（客户端 exp/imp 直连）脚本文本。"""
    tns = '"$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@//$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME"'
    backup = (
        "set -e\n"
        'mkdir -p "$PLATFORM_BACKUP_DIR"\n'
        + _PATH_PREP
        + 'EXP="${PLATFORM_PARAM_EXP_BIN:-' + exp_default + '}"\n'
        + '"$EXP" ' + tns + " " + owner_clause + '="$PLATFORM_DB_USER" '
        + 'file="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.dmp" '
        + 'log="$PLATFORM_BACKUP_DIR/exp.log" consistent=y statistics=none\n'
        + 'echo "BACKUP_OK size=$(du -h "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.dmp" | cut -f1)"\n'
    )
    restore = (
        "set -e\n"
        + _PATH_PREP
        + 'IMP="${PLATFORM_PARAM_IMP_BIN:-' + imp_default + '}"\n'
        + 'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
        + '"$IMP" ' + tns + ' file="$PLATFORM_BACKUP_FILE" '
        + 'fromuser="$PLATFORM_DB_USER" touser="$TDB" ignore=y commit=y '
        + 'log="$PLATFORM_BACKUP_DIR/imp.log"\n'
        + 'echo "RESTORE_OK db=$TDB"\n'
    )
    test_conn = (
        "set -e\n"
        + _PATH_PREP
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-sqlplus}"\n'
        + '"$CLI" -L "//$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME" <<SQL\n'
        + '$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD\n'
        + 'SELECT 1 FROM dual;\nEXIT;\nSQL\n'
        + 'echo CONN_OK\n'
    )
    list_dbs = (
        _PATH_PREP
        + 'CLI="${PLATFORM_PARAM_CLI_BIN:-sqlplus}"\n'
        + 'echo "select username from dba_users" | "$CLI" -L "//$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME"\n'
    )
    return {
        "script_backup_full": backup,
        "script_restore": restore,
        "script_test_conn": test_conn,
        "script_list_dbs": list_dbs,
    }


def _snapshot_scripts(data_dir_hint: str, stop_hint: str = "") -> dict:
    """数据目录快照型（无逻辑导出工具时的兜底）：打包数据目录。"""
    hint = f"（{stop_hint}）" if stop_hint else ""
    backup = (
        "set -e\n"
        'mkdir -p "$PLATFORM_BACKUP_DIR"\n'
        f'SRC="${{PLATFORM_PARAM_DATA_DIR:?请在适配器 params 填写 data_dir{hint}}}"\n'
        'NAME="${PLATFORM_DB_NAME:-instance}"\n'
        'tar czf "$PLATFORM_BACKUP_DIR/${NAME}_datadir.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")"\n'
        'echo "BACKUP_OK size=$(du -h "$PLATFORM_BACKUP_DIR/${NAME}_datadir.tar.gz" | cut -f1)"\n'
    )
    restore = (
        "set -e\n"
        'DST="${PLATFORM_PARAM_DATA_DIR:?请在适配器 params 填写 data_dir}"\n'
        'tar xzf "$PLATFORM_BACKUP_FILE" -C "$(dirname "$DST")"\n'
        'echo "RESTORE_OK dir=$DST（请按厂商文档重启服务后核对数据）"\n'
    )
    return {
        "script_backup_full": backup,
        "script_restore": restore,
        "script_test_conn": (
            "set -e\n"
            '"${PLATFORM_PARAM_CLI_BIN:-bash}" -c "echo CONN_OK" 2>/dev/null || true\n'
            'test -d "${PLATFORM_PARAM_DATA_DIR:?params.data_dir}" && echo "DATA_DIR_OK"\n'
        ),
    }


def _raw_scripts(backup: str, restore: str, test_conn: str = "",
                 list_dbs: str = "") -> dict:
    return {
        "script_backup_full": backup,
        "script_restore": restore,
        "script_test_conn": test_conn or ("set -e\necho CONN_OK\n"),
        "script_list_dbs": list_dbs or "",
    }


# ---------------------------------------------------------------- 目录条目

CATALOG: list[dict] = []


def _add(**kw) -> None:
    kw.setdefault("group", "other")
    kw.setdefault("category", "relational")
    kw.setdefault("icon", "bi-database-fill")
    kw.setdefault("backup_modes", ["logical"])
    kw.setdefault("supports_incremental", False)
    kw.setdefault("supports_full_instance", True)
    kw.setdefault("supports_sync", False)
    kw.setdefault("verification", "template")
    kw.setdefault("client_tools", [])
    kw.setdefault("params_hint", {})
    CATALOG.append(kw)


# ---- 1) PostgreSQL 协议族 -------------------------------------------------
for _e in (
    ("gaussdb", "GaussDB / openGauss 系", "华为 GaussDB（集中式）与 openGauss 发行版，"
     "gs_dump/gs_restore/gsql；密码用 -W 显式传递", 8000, "w", ["gs_dump", "gs_restore", "gsql"]),
    ("vastbase", "海量 Vastbase G100", "海量数据 Vastbase（openGauss 系），"
     "vs_dump/vs_restore 或 gs_dump 系列，按现场发行版填 dump_bin", 5432, "w", ["vs_dump", "vsql"]),
    ("mogdb", "云和恩墨 MogDB", "MogDB（openGauss 系），gs_dump/gs_restore", 5432, "w",
     ["gs_dump", "gs_restore"]),
    ("ivorysql", "瀚高 IvorySQL", "IvorySQL（PostgreSQL 开源分支，Oracle 兼容），pg_dump/pg_restore",
     5432, "env", ["pg_dump", "pg_restore", "psql"]),
    ("polardb_pg", "PolarDB PostgreSQL 版", "阿里云 PolarDB for PostgreSQL，pg_dump 直连集群地址",
     1921, "env", ["pg_dump", "pg_restore", "psql"]),
    ("greenplum", "Greenplum", "Greenplum MPP，pg_dump 按单库导出（gp_dump 已废弃）",
     5432, "env", ["pg_dump", "pg_restore", "psql"]),
    ("yugabyte", "YugabyteDB (YSQL)", "YugabyteDB YSQL 兼容 PostgreSQL，ysql_dump 或 pg_dump",
     5433, "env", ["ysql_dump", "ysqlsh", "pg_dump"]),
    ("cockroach", "CockroachDB", "CockroachDB（PG 线协议），pg_dump 单库导出（cockroach dump 已移除）",
     26257, "env", ["pg_dump", "cockroach"]),
    ("seaboxsql", "海盒 SeaboxSQL", "东方金信 SeaboxSQL（PG 系，国测在列），pg_dump/pg_restore",
     5432, "env", ["pg_dump", "pg_restore"]),
    ("timescaledb", "TimescaleDB", "TimescaleDB（PG 扩展），pg_dump 全库导出（含 hypertable）",
     5432, "env", ["pg_dump", "pg_restore"]),
):
    _key, _label, _desc, _port, _pw, _tools = _e
    _add(key=_key, db_type_hint=_key, label=_label, display_name=_label.split("（")[0].split(" (")[0],
         group="pg", category="relational", default_port=_port,
         icon="bi-database-fill-gear", client_tools=_tools,
         description=_desc, verification="protocol",
         params_hint={"dump_bin": _tools[0], "restore_bin": _tools[1],
                      "cli_bin": _tools[2] if len(_tools) > 2 else "psql"},
         scripts=_pg_scripts(pw_style=_pw))


# ---- 2) MySQL 协议族 -----------------------------------------------------
for _e in (
    ("goldendb", "中兴 GoldenDB", "中兴 GoldenDB（MySQL 系分布式），mysqldump 按分片库导出",
     3306, " --set-gtid-purged=OFF", "", ["mysqldump", "mysql"]),
    ("tdsql", "腾讯 TDSQL MySQL 版", "腾讯 TDSQL（MySQL 协议），mysqldump 单库导出",
     3306, " --set-gtid-purged=OFF", "", ["mysqldump", "mysql"]),
    ("polardbx", "PolarDB-X", "阿里 PolarDB-X（MySQL 协议），mysqldump 单库；大库建议 mydumper",
     3306, " --set-gtid-purged=OFF", "", ["mysqldump", "mysql", "mydumper"]),
    ("greatdb", "万里开源 GreatDB", "万里开源 GreatDB（MySQL 系），mysqldump",
     3306, " --set-gtid-purged=OFF", "", ["mysqldump", "mysql"]),
    ("he3db", "中移 He3DB", "中移大云海山 He3DB（MySQL 系），mysqldump",
     3306, " --set-gtid-purged=OFF", "", ["mysqldump", "mysql"]),
    ("gbase8a", "南大通用 GBase 8a", "GBase 8a 列存 MPP（MySQL 协议），推荐 gcdump；"
     "工具名按现场版本填 dump_bin", 5258, "", "", ["gcdump", "gccli"]),
):
    _key, _label, _desc, _port, _gtid, _extra, _tools = _e
    _add(key=_key, db_type_hint=_key, label=_label, display_name=_label.split("（")[0],
         group="mysql", category="relational", default_port=_port,
         icon="bi-database-fill-gear", client_tools=_tools,
         description=_desc, verification="protocol",
         params_hint={"dump_bin": _tools[0], "cli_bin": _tools[1]},
         scripts=_mysql_scripts(gtid_flag=_gtid, extra_dump=_extra))


# ---- 3) Oracle 兼容族 -----------------------------------------------------
for _e in (
    ("oscar", "神通数据库 OSCAR", "神舟通用神通（Oracle 兼容），exp/imp 或 dmp 工具；"
     "工具名按现场填 exp_bin/imp_bin", 2003, "exp", "imp"),
    ("yashandb", "崖山 YashanDB", "深圳计算科学研究院崖山（Oracle 兼容），exp/imp 兼容用法；"
     "也可改用 yas_dump/yas_load", 1688, "exp", "imp"),
    ("polardb_o", "PolarDB Oracle 版", "阿里云 PolarDB-O（Oracle 兼容），exp/imp 客户端直连",
     1521, "exp", "imp"),
):
    _key, _label, _desc, _port, _exp, _imp = _e
    _add(key=_key, db_type_hint=_key, label=_label, display_name=_label.split("（")[0].split(" ")[0],
         group="oracle", category="relational", default_port=_port,
         icon="bi-database-fill-exclamation", client_tools=[_exp, _imp, "sqlplus"],
         description=_desc, verification="template",
         params_hint={"exp_bin": _exp, "imp_bin": _imp, "cli_bin": "sqlplus"},
         scripts=_exp_imp_scripts(_exp, _imp))


# ---- 4) 分析型 / MPP / 数仓 ---------------------------------------------
_add(
    key="clickhouse", db_type_hint="clickhouse", label="ClickHouse", display_name="ClickHouse",
    group="olap", category="relational", default_port=9000, icon="bi-bar-chart-fill",
    client_tools=["clickhouse-client"], backup_modes=["logical"], supports_full_instance=True,
    description="ClickHouse 原生 BACKUP/RESTORE（22.8+ 支持 File 目标，产物落在服务器本地）",
    params_hint={"cli_bin": "clickhouse-client", "native_port": "9000"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-clickhouse-client}"\n'
            '"$CLI" --host "$PLATFORM_DB_HOST" --port "${PLATFORM_PARAM_NATIVE_PORT:-9000}" '
            '--user "$PLATFORM_DB_USER" --password "$PLATFORM_DB_PASSWORD" \\\n'
            '  --query "BACKUP DATABASE `$PLATFORM_DB_NAME` TO File(\'$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.zip\')"\n'
            'echo "BACKUP_OK size=$(du -h "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.zip" | cut -f1)"\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-clickhouse-client}"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            '"$CLI" --host "$PLATFORM_DB_HOST" --port "${PLATFORM_PARAM_NATIVE_PORT:-9000}" '
            '--user "$PLATFORM_DB_USER" --password "$PLATFORM_DB_PASSWORD" \\\n'
            '  --query "RESTORE DATABASE `$PLATFORM_DB_NAME` AS `$TDB` FROM File(\'$PLATFORM_BACKUP_FILE\')"\n'
            'echo "RESTORE_OK db=$TDB"\n'),
        test_conn=(
            "set -e\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-clickhouse-client}"\n'
            '"$CLI" --host "$PLATFORM_DB_HOST" --port "${PLATFORM_PARAM_NATIVE_PORT:-9000}" '
            '--user "$PLATFORM_DB_USER" --password "$PLATFORM_DB_PASSWORD" --query "SELECT version()"\n'
            'echo CONN_OK\n'),
        list_dbs=(
            _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-clickhouse-client}"\n'
            '"$CLI" --host "$PLATFORM_DB_HOST" --port "${PLATFORM_PARAM_NATIVE_PORT:-9000}" '
            '--user "$PLATFORM_DB_USER" --password "$PLATFORM_DB_PASSWORD" --query "SHOW DATABASES"\n'),
    ),
)

for _e in (
    ("starrocks", "StarRocks", "StarRocks 3.x BACKUP SNAPSHOT 到 BACKUP 仓库；"
     "需先建仓库并把仓库目录指向 params.repo_dir", 9030, "mysql"),
    ("doris", "Apache Doris", "Doris BACKUP SNAPSHOT 到 BACKUP 仓库；需先建仓库并填 params.repo_dir",
     9030, "mysql"),
):
    _key, _label, _desc, _port, _cli = _e
    _add(
        key=_key, db_type_hint=_key, label=_label, display_name=_label.split("（")[0],
        group="olap", category="relational", default_port=_port,
        icon="bi-bar-chart-fill", client_tools=[_cli],
        description=_desc + "（快照目录打包进产物目录，恢复前解包回仓库目录）",
        params_hint={"cli_bin": _cli, "repo": "aidbm_repo", "repo_dir": "/data/aidbm_repo",
                     "snapshot": ""},
        scripts=_raw_scripts(
            backup=(
                "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
                'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
                'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
                'REPO="${PLATFORM_PARAM_REPO:-aidbm_repo}"\n'
                'SNAP="${PLATFORM_PARAM_SNAPSHOT:-${PLATFORM_DB_NAME}_$(date +%Y%m%d%H%M%S)}"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" -e \\\n'
                '  "BACKUP SNAPSHOT `$PLATFORM_DB_NAME`.`$SNAP` TO `$REPO` ON (`$PLATFORM_DB_NAME`) '
                'PROPERTIES(\'type\'=\'full\')"\n'
                'RD="${PLATFORM_PARAM_REPO_DIR:?请在 params 填写 repo_dir（仓库在服务器上的本地目录）}"\n'
                'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_${SNAP}.tar.gz" -C "$RD" "$SNAP" 2>/dev/null \\\n'
                '  || tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_${SNAP}.tar.gz" -C "$RD" .\n'
                'echo "BACKUP_OK snapshot=$SNAP"\n'),
            restore=(
                "set -e\n" + _PATH_PREP +
                'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
                'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
                'REPO="${PLATFORM_PARAM_REPO:-aidbm_repo}"\n'
                'SNAP="${PLATFORM_PARAM_SNAPSHOT:?请在 params 填写 snapshot（备份时的快照名）}"\n'
                'RD="${PLATFORM_PARAM_REPO_DIR:?请在 params 填写 repo_dir}"\n'
                'mkdir -p "$RD" && tar xzf "$PLATFORM_BACKUP_FILE" -C "$RD"\n'
                'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" -e \\\n'
                '  "RESTORE SNAPSHOT `$PLATFORM_DB_NAME`.`$SNAP` FROM `$REPO` ON (`$TDB`) '
                'PROPERTIES(\'backup_timestamp\'=\'${PLATFORM_PARAM_BACKUP_TIMESTAMP:?请填 backup_timestamp}\','
                '\'replication_num\'=\'1\')"\n'
                'echo "RESTORE_OK db=$TDB"\n'),
            test_conn=(
                "set -e\n" + _PATH_PREP + 'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
                'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
                '-e "SHOW DATABASES"\n'),
            list_dbs=(
                _PATH_PREP + 'export MYSQL_PWD="$PLATFORM_DB_PASSWORD"\n'
                'CLI="${PLATFORM_PARAM_CLI_BIN:-mysql}"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
                '-N -e "SHOW DATABASES"\n'),
        ),
    )

_add(
    key="hive", db_type_hint="hive", label="Apache Hive", display_name="Hive",
    group="olap", category="relational", default_port=10000, icon="bi-diagram-2-fill",
    client_tools=["hive", "hdfs"],
    description="Hive 使用 EXPORT TABLE / IMPORT TABLE；表级导出（全库循环需按 tables 逐个执行）",
    params_hint={"cli_bin": "hive", "table": ""},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'TBL="${PLATFORM_PARAM_TABLE:?请在 params 填写 table（Hive 按表导出）}"\n'
            'CLI="${PLATFORM_PARAM_CLI_BIN:-hive}"\n'
            '"$CLI" -e "EXPORT TABLE $PLATFORM_DB_NAME.$TBL TO \'$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_${TBL}\'"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'TBL="${PLATFORM_PARAM_TABLE:?请在 params 填写 table}"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            'CLI="${PLATFORM_PARAM_CLI_BIN:-hive}"\n'
            '"$CLI" -e "IMPORT TABLE $TDB.${TBL} FROM \'$PLATFORM_BACKUP_FILE\'"\n'
            'echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + 'CLI="${PLATFORM_PARAM_CLI_BIN:-hive}"\n'
                   '"$CLI" -e "SHOW DATABASES"\n'),
    ),
)

_add(
    key="impala", db_type_hint="impala", label="Apache Impala", display_name="Impala",
    group="olap", category="relational", default_port=21050, icon="bi-diagram-2-fill",
    client_tools=["impala-shell", "hdfs"],
    description="Impala 依赖 Hive Metastore + HDFS，备份 = HDFS 仓库目录拷贝（params.warehouse）",
    params_hint={"cli_bin": "hdfs", "warehouse": "/user/hive/warehouse"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'HDFS="${PLATFORM_PARAM_CLI_BIN:-hdfs}"\n'
            'WH="${PLATFORM_PARAM_WAREHOUSE:-/user/hive/warehouse}"\n'
            '"$HDFS" dfs -test -e "$WH/$PLATFORM_DB_NAME.db" || { echo "仓库目录不存在: $WH/$PLATFORM_DB_NAME.db"; exit 3; }\n'
            '"$HDFS" dfs -cp -f "$WH/$PLATFORM_DB_NAME.db" "$WH/.aidbm_bak/$PLATFORM_DB_NAME.db" 2>/dev/null || true\n'
            '"$HDFS" dfs -get "$WH/$PLATFORM_DB_NAME.db" "$PLATFORM_BACKUP_DIR/"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.tar.gz" -C "$PLATFORM_BACKUP_DIR" "$PLATFORM_DB_NAME.db"\n'
            'rm -rf "$PLATFORM_BACKUP_DIR/$PLATFORM_DB_NAME.db"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'HDFS="${PLATFORM_PARAM_CLI_BIN:-hdfs}"\n'
            'WH="${PLATFORM_PARAM_WAREHOUSE:-/user/hive/warehouse}"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}.db"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            'DIR=$(ls -d "$TMP"/*.db | head -1)\n'
            '"$HDFS" dfs -put -f "$DIR" "$WH/$TDB"\n'
            'rm -rf "$TMP"; echo "RESTORE_OK hdfs=$WH/$TDB（需在 Hive 侧 MSCK REPAIR TABLE）"\n'),
        test_conn=("set -e\n" + _PATH_PREP + 'CLI="${PLATFORM_PARAM_CLI_BIN:-hdfs}"\n'
                   '"$CLI" dfs -ls "${PLATFORM_PARAM_WAREHOUSE:-/user/hive/warehouse}" | head -5\n'),
    ),
)

_add(
    key="vertica", db_type_hint="vertica", label="Vertica", display_name="Vertica",
    group="olap", category="relational", default_port=5433, icon="bi-bar-chart-fill",
    client_tools=["vsql", "vbr"],
    description="Vertica 集群级备份用 vbr（需 vbr.ini），单库导出可用 vsql EXPORT TO PARQUET",
    params_hint={"cli_bin": "vsql", "vbr_ini": "/opt/vertica/config/vbr.ini"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'VSQL="${PLATFORM_PARAM_CLI_BIN:-vsql}"\n'
            '"$VSQL" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" '
            '-w "$PLATFORM_DB_PASSWORD" -c \\\n'
            '  "EXPORT TO PARQUET(directory=\'$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}\') AS SELECT * FROM tables"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.tar.gz" -C "$PLATFORM_BACKUP_DIR" "$PLATFORM_DB_NAME"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'VSQL="${PLATFORM_PARAM_CLI_BIN:-vsql}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$VSQL" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" '
            '-w "$PLATFORM_DB_PASSWORD" -c "COPY ... FROM \'$TMP\'" || '
            '{ echo "请用 vbr 或 COPY 逐表导入（参数随版本差异大）"; exit 4; }\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + 'VSQL="${PLATFORM_PARAM_CLI_BIN:-vsql}"\n'
                   '"$VSQL" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" '
                   '-w "$PLATFORM_DB_PASSWORD" -c "SELECT version()"\n'),
    ),
)

_add(
    key="teradata", db_type_hint="teradata", label="Teradata", display_name="Teradata",
    group="olap", category="relational", default_port=1025, icon="bi-bar-chart-fill",
    client_tools=["bteq", "tdatdump", "arccmd"],
    description="Teradata 用 ARC（arccmd）做全库归档，或用 tdatdump/FastExport 逐表导出；"
                "命令随 Teradata Tools and Utilities 版本差异较大",
    params_hint={"cli_bin": "bteq", "arc_cmd": "arccmd"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n"
            'cat > /tmp/aidbm_arc_$$.txt <<ARC\n'
            'ARCHIVE DATA TABLES (ALL) FROM DATABASE $PLATFORM_DB_NAME, '
            'RELEASE LOCK, FILE = $PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.arc;\nARC\n'
            '"${PLATFORM_PARAM_ARC_CMD:-arccmd}" < /tmp/aidbm_arc_$$.txt\n'
            'rm -f /tmp/aidbm_arc_$$.txt; echo BACKUP_OK\n'),
        restore=(
            "set -e\n"
            'cat > /tmp/aidbm_arcr_$$.txt <<ARC\n'
            'RESTORE DATA TABLES (ALL) FROM DATABASE $PLATFORM_DB_NAME, '
            'FILE = $PLATFORM_BACKUP_FILE;\nARC\n'
            '"${PLATFORM_PARAM_ARC_CMD:-arccmd}" < /tmp/aidbm_arcr_$$.txt\n'
            'rm -f /tmp/aidbm_arcr_$$.txt; echo RESTORE_OK\n'),
        test_conn=("set -e\necho \"SELECT 1;\" | \"${PLATFORM_PARAM_CLI_BIN:-bteq}\" "
                   "-h \"$PLATFORM_DB_HOST\" -u \"$PLATFORM_DB_USER\" -p \"$PLATFORM_DB_PASSWORD\"\n"),
    ),
)


# ---- 5) 时序数据库 -------------------------------------------------------
_add(
    key="influxdb", db_type_hint="influxdb", label="InfluxDB 1.x", display_name="InfluxDB 1.x",
    group="ts", category="timeseries", default_port=8088, icon="bi-graph-up",
    client_tools=["influxd", "influx"],
    description="InfluxDB 1.x 用 influxd backup（RPC 端口 8088）导出，restore -portable 恢复",
    params_hint={"cli_bin": "influxd", "rpc_port": "8088"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'BK="${PLATFORM_PARAM_CLI_BIN:-influxd}"\n'
            'D="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_influx"\n'
            '"$BK" backup -host "${PLATFORM_DB_HOST}:${PLATFORM_PARAM_RPC_PORT:-8088}" '
            '-database "$PLATFORM_DB_NAME" "$D"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_influx.tar.gz" -C "$D" .\n'
            'rm -rf "$D"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'BK="${PLATFORM_PARAM_CLI_BIN:-influxd}"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$BK" restore -portable -host "${PLATFORM_DB_HOST}:${PLATFORM_PARAM_RPC_PORT:-8088}" '
            '-db "$PLATFORM_DB_NAME" -newdb "$TDB" "$TMP"\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   'CLI="${PLATFORM_PARAM_CLI_BIN:-influxd}"\n'
                   '"$CLI" version\n'),
    ),
)

_add(
    key="influxdb2", db_type_hint="influxdb2", label="InfluxDB 2.x", display_name="InfluxDB 2.x",
    group="ts", category="timeseries", default_port=8086, icon="bi-graph-up",
    client_tools=["influx"],
    description="InfluxDB 2.x 用 influx backup/restore（需 API Token，填 params.token）",
    params_hint={"cli_bin": "influx", "token": ""},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-influx}"\n'
            'D="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_v2"\n'
            '"$CLI" backup "$D" --host "http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '--token "${PLATFORM_PARAM_TOKEN:?请在 params 填写 token}"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_v2.tar.gz" -C "$D" .\n'
            'rm -rf "$D"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-influx}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$CLI" restore "$TMP" --host "http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '--token "${PLATFORM_PARAM_TOKEN:?请填写 token}"\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-influx}" version\n'),
    ),
)

_add(
    key="tdengine", db_type_hint="tdengine", label="TDengine", display_name="TDengine",
    group="ts", category="timeseries", default_port=6030, icon="bi-graph-up",
    client_tools=["taosdump", "taos"],
    description="TDengine 用 taosdump 导出（-D 指定库，-o 输出目录），taosdump -i 恢复",
    params_hint={"dump_bin": "taosdump", "cli_bin": "taos"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'DUMP="${PLATFORM_PARAM_DUMP_BIN:-taosdump}"\n'
            'D="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tdengine"\n'
            '"$DUMP" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" '
            '-p "$PLATFORM_DB_PASSWORD" -D "$PLATFORM_DB_NAME" -o "$D"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tdengine.tar.gz" -C "$D" .\n'
            'rm -rf "$D"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'DUMP="${PLATFORM_PARAM_DUMP_BIN:-taosdump}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$DUMP" -i "$TMP" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" '
            '-u "$PLATFORM_DB_USER" -p "$PLATFORM_DB_PASSWORD"\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-taos}" -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" '
                   '-u "$PLATFORM_DB_USER" -p "$PLATFORM_DB_PASSWORD" -s "SHOW DATABASES"\n'),
    ),
)

for _e in (
    ("iotdb", "Apache IoTDB", "IoTDB 无官方在线全库导出，备份 = 数据目录快照（建议 flush 后执行）",
     6667, "/data/iotdb/data"),
    ("questdb", "QuestDB", "QuestDB 备份 = 数据目录快照（目录内含 db 子目录）；建议在低峰期执行",
     8812, "/var/lib/questdb/db"),
):
    _key, _label, _desc, _port, _dir = _e
    _add(key=_key, db_type_hint=_key, label=_label, display_name=_label.split("（")[0],
         group="ts", category="timeseries", default_port=_port, icon="bi-graph-up",
         client_tools=["tar"], description=_desc, verification="template",
         params_hint={"data_dir": _dir},
         scripts=_snapshot_scripts(_dir, "建议先停写或执行 flush"))


# ---- 6) NoSQL / KV / 文档 / 搜索 -----------------------------------------
_add(
    key="cassandra", db_type_hint="cassandra", label="Apache Cassandra", display_name="Cassandra",
    group="nosql", category="nosql", default_port=9042, icon="bi-hdd-stack",
    client_tools=["nodetool"], description="Cassandra 用 nodetool snapshot 打快照后打包 snapshots 目录，"
    "结束后 clearsnapshot 清理远端残留",
    params_hint={"cli_bin": "nodetool", "data_dir": "/var/lib/cassandra/data"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'NT="${PLATFORM_PARAM_CLI_BIN:-nodetool}"\n'
            'DD="${PLATFORM_PARAM_DATA_DIR:?请在 params 填写 data_dir}"\n'
            'TAG="${PLATFORM_DB_NAME}_$(date +%Y%m%d%H%M%S)"\n'
            '"$NT" snapshot -t "$TAG" "$PLATFORM_DB_NAME"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${TAG}.tar.gz" -C "$DD" --wildcards "*/snapshots/$TAG" '
            '"$PLATFORM_DB_NAME"/*/snapshots/"$TAG" 2>/dev/null || \\\n'
            '  tar czf "$PLATFORM_BACKUP_DIR/${TAG}.tar.gz" -C "$DD" .\n'
            '"$NT" clearsnapshot -t "$TAG" "$PLATFORM_DB_NAME" || true\n'
            'echo "BACKUP_OK tag=$TAG"\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'DD="${PLATFORM_PARAM_DATA_DIR:?请填写 data_dir}"\n'
            'tar xzf "$PLATFORM_BACKUP_FILE" -C "$DD"\n'
            'echo "RESTORE_OK dir=$DD（需停节点后替换 sstable，再 nodetool refresh）"\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-nodetool}" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" status\n'),
    ),
)

_add(
    key="scylladb", db_type_hint="scylladb", label="ScyllaDB", display_name="ScyllaDB",
    group="nosql", category="nosql", default_port=9042, icon="bi-hdd-stack",
    client_tools=["nodetool"], description="ScyllaDB 与 Cassandra 同源的 nodetool snapshot 通道",
    params_hint={"cli_bin": "nodetool", "data_dir": "/var/lib/scylla/data"},
    scripts=_snapshot_scripts("/var/lib/scylla/data", "建议先 nodetool snapshot 再打包目录"))

_add(
    key="hbase", db_type_hint="hbase", label="Apache HBase", display_name="HBase",
    group="nosql", category="nosql", default_port=16000, icon="bi-hdd-stack",
    client_tools=["hbase", "hdfs"],
    description="HBase 用 hbase shell create_snapshot 打快照，再用 hdfs dfs -get 把快照目录拉回",
    params_hint={"cli_bin": "hbase", "hdfs_bin": "hdfs", "snapshot_root": "/hbase/.hbase-snapshot"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'HB="${PLATFORM_PARAM_CLI_BIN:-hbase}"\n'
            'HDFS="${PLATFORM_PARAM_HDFS_BIN:-hdfs}"\n'
            'ROOT="${PLATFORM_PARAM_SNAPSHOT_ROOT:-/hbase/.hbase-snapshot}"\n'
            'SNAP="${PLATFORM_PARAM_SNAPSHOT:-${PLATFORM_DB_NAME}_$(date +%Y%m%d%H%M%S)}"\n'
            'echo "snapshot \'$PLATFORM_DB_NAME\', \'$SNAP\', {SKIP_FLUSH => false}" | "$HB" shell\n'
            '"$HDFS" dfs -get "$ROOT/$SNAP" "$PLATFORM_BACKUP_DIR/"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${SNAP}.tar.gz" -C "$PLATFORM_BACKUP_DIR" "$SNAP"\n'
            'rm -rf "$PLATFORM_BACKUP_DIR/$SNAP"\n'
            'echo "BACKUP_OK snapshot=$SNAP"\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'HB="${PLATFORM_PARAM_CLI_BIN:-hbase}"\n'
            'HDFS="${PLATFORM_PARAM_HDFS_BIN:-hdfs}"\n'
            'ROOT="${PLATFORM_PARAM_SNAPSHOT_ROOT:-/hbase/.hbase-snapshot}"\n'
            'SNAP="${PLATFORM_PARAM_SNAPSHOT:?请在 params 填写 snapshot 名}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$HDFS" dfs -put -f "$TMP/$SNAP" "$ROOT/$SNAP"\n'
            'echo "restore_snapshot \'$SNAP\'" | "$HB" shell\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + 'echo "status" | "${PLATFORM_PARAM_CLI_BIN:-hbase}" shell\n'),
    ),
)

_add(
    key="couchdb", db_type_hint="couchdb", label="Apache CouchDB", display_name="CouchDB",
    group="nosql", category="nosql", default_port=5984, icon="bi-file-earmark-zip",
    client_tools=["couchbackup", "couchrestore"],
    description="CouchDB 用 couchbackup/couchrestore（HTTP API 逐文档导出，JSONL 文本）",
    params_hint={"backup_bin": "couchbackup", "restore_bin": "couchrestore"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'URL="http://$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT"\n'
            '"${PLATFORM_PARAM_BACKUP_BIN:-couchbackup}" --url "$URL" --db "$PLATFORM_DB_NAME" '
            '> "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.jsonl"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'URL="http://$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            '"${PLATFORM_PARAM_RESTORE_BIN:-couchrestore}" --url "$URL" --db "$TDB" '
            '< "$PLATFORM_BACKUP_FILE"\n'
            'echo RESTORE_OK\n'),
        test_conn=("set -e\ncurl -sS -f -u \"$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD\" "
                   "\"http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/_all_dbs\"\n"),
    ),
)

for _e in (
    ("elasticsearch", "Elasticsearch", "Elasticsearch 用 _snapshot API（需先注册 fs 仓库并配置 path.repo），"
     "快照目录由 params.repo_dir 打包", 9200),
    ("opensearch", "OpenSearch", "OpenSearch 同 Elasticsearch 的 _snapshot API 通道", 9200),
):
    _key, _label, _desc, _port = _e
    _add(
        key=_key, db_type_hint=_key, label=_label, display_name=_label,
        group="nosql", category="nosql", default_port=_port, icon="bi-search",
        client_tools=["curl"], description=_desc + "；恢复用 _restore 并支持 rename_pattern",
        params_hint={"repo": "aidbm_repo", "repo_dir": "/data/es_repo", "snapshot": ""},
        scripts=_raw_scripts(
            backup=(
                "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n"
                'REPO="${PLATFORM_PARAM_REPO:-aidbm_repo}"\n'
                'SNAP="${PLATFORM_PARAM_SNAPSHOT:-${PLATFORM_DB_NAME}_$(date +%Y%m%d%H%M%S)}"\n'
                'curl -sS -k -u "$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD" -X PUT \\\n'
                '  "http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/_snapshot/$REPO/$SNAP?wait_for_completion=true"\n'
                'RD="${PLATFORM_PARAM_REPO_DIR:?请在 params 填写 repo_dir}"\n'
                'tar czf "$PLATFORM_BACKUP_DIR/${SNAP}.tar.gz" -C "$RD" "$SNAP" 2>/dev/null || '
                'tar czf "$PLATFORM_BACKUP_DIR/${SNAP}.tar.gz" -C "$RD" .\n'
                'echo "BACKUP_OK snapshot=$SNAP"\n'),
            restore=(
                "set -e\n"
                'REPO="${PLATFORM_PARAM_REPO:-aidbm_repo}"\n'
                'SNAP="${PLATFORM_PARAM_SNAPSHOT:?请在 params 填写 snapshot}"\n'
                'RD="${PLATFORM_PARAM_REPO_DIR:?请填写 repo_dir}"\n'
                'mkdir -p "$RD" && tar xzf "$PLATFORM_BACKUP_FILE" -C "$RD"\n'
                'curl -sS -k -u "$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD" -X POST \\\n'
                '  "http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/_snapshot/$REPO/$SNAP/_restore?wait_for_completion=true" \\\n'
                '  -H "Content-Type: application/json" -d \'{"include_global_state":false}\'\n'
                'echo RESTORE_OK\n'),
            test_conn=(
                "set -e\ncurl -sS -k -u \"$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD\" "
                "\"http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/_cluster/health\"\n"),
        ),
    )

for _e in (
    ("keydb", "KeyDB", "KeyDB（Redis 协议多线程分支）用 redis-cli --rdb 拉 RDB 快照", 6379),
    ("dragonfly", "Dragonfly", "Dragonfly（Redis 协议）用 redis-cli --rdb 拉快照", 6379),
):
    _key, _label, _desc, _port = _e
    _add(
        key=_key, db_type_hint=_key, label=_label, display_name=_label,
        group="nosql", category="nosql", default_port=_port, icon="bi-lightning-charge",
        client_tools=["redis-cli"], description=_desc,
        params_hint={"cli_bin": "redis-cli"},
        scripts=_raw_scripts(
            backup=(
                "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
                'CLI="${PLATFORM_PARAM_CLI_BIN:-redis-cli}"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -a "$PLATFORM_DB_PASSWORD" '
                '--no-auth-warning --rdb "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.rdb"\n'
                'echo BACKUP_OK\n'),
            restore=(
                "set -e\n" + _PATH_PREP +
                'CLI="${PLATFORM_PARAM_CLI_BIN:-redis-cli}"\n'
                'DIR=$("$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -a "$PLATFORM_DB_PASSWORD" '
                '--no-auth-warning CONFIG GET dir | tail -1)\n'
                'cp -f "$PLATFORM_BACKUP_FILE" "$DIR/dump.rdb"\n'
                '"$CLI" -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -a "$PLATFORM_DB_PASSWORD" '
                '--no-auth-warning SHUTDOWN NOSAVE || true\n'
                'echo "RESTORE_OK dir=$DIR（请启动服务后核对 dbsize）"\n'),
            test_conn=("set -e\n" + _PATH_PREP +
                       '"${PLATFORM_PARAM_CLI_BIN:-redis-cli}" -h "$PLATFORM_DB_HOST" '
                       '-p "$PLATFORM_DB_PORT" -a "$PLATFORM_DB_PASSWORD" --no-auth-warning PING\n'),
        ),
    )

_add(
    key="etcd", db_type_hint="etcd", label="etcd", display_name="etcd",
    group="nosql", category="nosql", default_port=2379, icon="bi-diagram-3",
    client_tools=["etcdctl"], description="etcd 用 etcdctl snapshot save / snapshot restore（恢复需停 etcd）",
    params_hint={"cli_bin": "etcdctl", "data_dir": "/var/lib/etcd-restore"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'export ETCDCTL_API=3\n'
            'CTL="${PLATFORM_PARAM_CLI_BIN:-etcdctl}"\n'
            '"$CTL" --endpoints="http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" snapshot save '
            '"$PLATFORM_BACKUP_DIR/etcd_$(date +%Y%m%d%H%M%S).db"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'export ETCDCTL_API=3\n'
            'CTL="${PLATFORM_PARAM_CLI_BIN:-etcdctl}"\n'
            'DD="${PLATFORM_PARAM_DATA_DIR:-/var/lib/etcd-restore}"\n'
            '"$CTL" snapshot restore "$PLATFORM_BACKUP_FILE" --data-dir="$DD"\n'
            'echo "RESTORE_OK data_dir=$DD（请用该目录重启 etcd 成员）"\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   'export ETCDCTL_API=3\n'
                   '"${PLATFORM_PARAM_CLI_BIN:-etcdctl}" --endpoints="http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
                   'endpoint health\n'),
    ),
)

_add(
    key="consul", db_type_hint="consul", label="HashiCorp Consul", display_name="Consul",
    group="nosql", category="nosql", default_port=8500, icon="bi-diagram-3",
    client_tools=["consul"], description="Consul 用 consul snapshot save / restore（KV + 目录数据）",
    params_hint={"cli_bin": "consul"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            '"${PLATFORM_PARAM_CLI_BIN:-consul}" snapshot save '
            '"$PLATFORM_BACKUP_DIR/consul_$(date +%Y%m%d%H%M%S).snap" '
            '-http-addr="http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            '"${PLATFORM_PARAM_CLI_BIN:-consul}" snapshot restore "$PLATFORM_BACKUP_FILE" '
            '-http-addr="http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT"\n'
            'echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-consul}" members '
                   '-http-addr="http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT"\n'),
    ),
)


# ---- 7) 图数据库 ---------------------------------------------------------
_add(
    key="nebula", db_type_hint="nebula", label="NebulaGraph", display_name="NebulaGraph",
    group="graph", category="graph", default_port=9669, icon="bi-diagram-3",
    client_tools=["br"], description="NebulaGraph 用 nebula-br 全量备份（local:// 存储到本机目录）",
    params_hint={"cli_bin": "br", "meta_addr": "", "storage": "local:///data/nebula_backup"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'BR="${PLATFORM_PARAM_CLI_BIN:-br}"\n'
            'META="${PLATFORM_PARAM_META_ADDR:-$PLATFORM_DB_HOST:$PLATFORM_DB_PORT}"\n'
            'ST="${PLATFORM_PARAM_STORAGE:-local:///data/nebula_backup}"\n'
            '"$BR" backup full --meta "$META" --storage "$ST"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/nebula_$(date +%Y%m%d%H%M%S).tar.gz" -C "${ST#local://}" .\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'BR="${PLATFORM_PARAM_CLI_BIN:-br}"\n'
            'META="${PLATFORM_PARAM_META_ADDR:-$PLATFORM_DB_HOST:$PLATFORM_DB_PORT}"\n'
            'ST="${PLATFORM_PARAM_STORAGE:-local:///data/nebula_backup}"\n'
            'tar xzf "$PLATFORM_BACKUP_FILE" -C "${ST#local://}"\n'
            '"$BR" restore full --meta "$META" --storage "$ST" '
            '--name "${PLATFORM_PARAM_BACKUP_NAME:?请在 params 填写 backup_name}"\n'
            'echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + '"${PLATFORM_PARAM_CLI_BIN:-br}" version\n'),
    ),
)

_add(
    key="arangodb", db_type_hint="arangodb", label="ArangoDB", display_name="ArangoDB",
    group="graph", category="graph", default_port=8529, icon="bi-diagram-3",
    client_tools=["arangodump", "arangorestore"],
    description="ArangoDB 用 arangodump/arangorestore（JSON 逐集合导出）",
    params_hint={"dump_bin": "arangodump", "restore_bin": "arangorestore"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'D="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_arango"\n'
            '"${PLATFORM_PARAM_DUMP_BIN:-arangodump}" --server.endpoint '
            '"tcp://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" --server.username "$PLATFORM_DB_USER" '
            '--server.password "$PLATFORM_DB_PASSWORD" --server.database "$PLATFORM_DB_NAME" '
            '--output-directory "$D" --overwrite true\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_arango.tar.gz" -C "$D" .\n'
            'rm -rf "$D"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            '"${PLATFORM_PARAM_RESTORE_BIN:-arangorestore}" --server.endpoint '
            '"tcp://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" --server.username "$PLATFORM_DB_USER" '
            '--server.password "$PLATFORM_DB_PASSWORD" --server.database "$TDB" '
            '--input-directory "$TMP" --create-database true\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\ncurl -sS -f -u \"$PLATFORM_DB_USER:$PLATFORM_DB_PASSWORD\" "
                   "\"http://$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/_api/version\"\n"),
    ),
)

_add(
    key="hugegraph", db_type_hint="hugegraph", label="HugeGraph", display_name="HugeGraph",
    group="graph", category="graph", default_port=8080, icon="bi-diagram-3",
    client_tools=["hugegraph-server"], description="HugeGraph 备份 = 后端存储快照"
    "（RocksDB 目录 / HBase 快照）；参数 data_dir 指向后端数据目录",
    params_hint={"data_dir": "/hugegraph/data"},
    scripts=_snapshot_scripts("/hugegraph/data", "建议先停写或按后端类型打快照"))

_add(
    key="janusgraph", db_type_hint="janusgraph", label="JanusGraph", display_name="JanusGraph",
    group="graph", category="graph", default_port=8182, icon="bi-diagram-3",
    client_tools=["gremlin-console"],
    description="JanusGraph 无自带备份，数据在存储后端（Cassandra/HBase/BerkeleyDB），"
    "按后端选择 Cassandra/HBase 模板或本模板的目录快照",
    params_hint={"data_dir": "/var/lib/janusgraph"},
    scripts=_snapshot_scripts("/var/lib/janusgraph", "按后端存储类型选择备份通道"))


# ---- 8) 其他（嵌入式 / 商用 / 文件型） ------------------------------------
_add(
    key="sqlite", db_type_hint="sqlite", label="SQLite", display_name="SQLite",
    group="other", category="file", default_port=None, icon="bi-file-earmark-binary",
    client_tools=["sqlite3"], backup_modes=["logical"], supports_full_instance=False,
    description="SQLite 用 sqlite3 .backup（在线一致快照，比直接 cp 安全）；params.db_file 填库文件绝对路径",
    params_hint={"cli_bin": "sqlite3", "db_file": "/data/app.db"},
    verification="template",
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'DBF="${PLATFORM_PARAM_DB_FILE:?请在 params 填写 db_file（sqlite 文件绝对路径）}"\n'
            # sqlite3 对不存在的路径会**新建空库**，若无此检查会静默产出空备份
            '[ -f "$DBF" ] || { echo "db_file 不存在：$DBF"; exit 3; }\n'
            '"${PLATFORM_PARAM_CLI_BIN:-sqlite3}" "$DBF" ".backup \'$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.db\'"\n'
            '[ -s "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.db" ] || { echo "备份产物为空"; exit 4; }\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            # 恢复到新文件（restore_file）以便与源库对比；未指定则覆盖原库文件
            'DBF="${PLATFORM_PARAM_RESTORE_FILE:-${PLATFORM_PARAM_DB_FILE:?请填写 db_file/restore_file}}"\n'
            'mkdir -p "$(dirname "$DBF")"\n'
            'cp -f "$PLATFORM_BACKUP_FILE" "$DBF"\n'
            'echo "RESTORE_OK file=$DBF"\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-sqlite3}" "${PLATFORM_PARAM_DB_FILE:?请填写 db_file}" '
                   '"SELECT sqlite_version()"\n'),
    ),
)

_add(
    key="duckdb", db_type_hint="duckdb", label="DuckDB", display_name="DuckDB",
    group="other", category="file", default_port=None, icon="bi-file-earmark-binary",
    client_tools=["duckdb"], backup_modes=["logical"], supports_full_instance=False,
    description="DuckDB 用 EXPORT DATABASE 导出 Parquet + schema.sql；params.db_file 填库文件路径",
    params_hint={"cli_bin": "duckdb", "db_file": "/data/app.duckdb"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-duckdb}"\n'
            'DBF="${PLATFORM_PARAM_DB_FILE:?请在 params 填写 db_file}"\n'
            'D="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_duckdb"\n'
            '"$CLI" "$DBF" -c "EXPORT DATABASE \'$D\' (FORMAT PARQUET)"\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_duckdb.tar.gz" -C "$D" .\n'
            'rm -rf "$D"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-duckdb}"\n'
            'DBF="${PLATFORM_PARAM_DB_FILE:?请填写 db_file}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"$CLI" "$DBF" -c "IMPORT DATABASE \'$TMP\'"\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + '"${PLATFORM_PARAM_CLI_BIN:-duckdb}" -c "SELECT version()"\n'),
    ),
)

_add(
    key="firebird", db_type_hint="firebird", label="Firebird", display_name="Firebird",
    group="other", category="relational", default_port=3050, icon="bi-database",
    client_tools=["gbak"], description="Firebird 用 gbak -b 逻辑备份、gbak -c 创建新库（-user/-pass 认证）",
    params_hint={"backup_bin": "gbak"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            '"${PLATFORM_PARAM_BACKUP_BIN:-gbak}" -b -v -user "$PLATFORM_DB_USER" '
            '-pass "$PLATFORM_DB_PASSWORD" "$PLATFORM_DB_HOST:$PLATFORM_DB_NAME" '
            '"$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.fbk"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'TDB="${PLATFORM_RESTORE_DB:-$PLATFORM_DB_NAME}"\n'
            '"${PLATFORM_PARAM_BACKUP_BIN:-gbak}" -c -v -user "$PLATFORM_DB_USER" '
            '-pass "$PLATFORM_DB_PASSWORD" "$PLATFORM_BACKUP_FILE" '
            '"$PLATFORM_DB_HOST:$TDB"\n'
            'echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-isql-fb}" -user "$PLATFORM_DB_USER" '
                   '-password "$PLATFORM_DB_PASSWORD" "$PLATFORM_DB_HOST:$PLATFORM_DB_NAME" '
                   '-q <<< "SELECT 1 FROM RDB\\$DATABASE;"\n'),
    ),
)

_add(
    key="db2", db_type_hint="db2", label="IBM Db2 LUW", display_name="Db2",
    group="other", category="relational", default_port=50000, icon="bi-database",
    client_tools=["db2"], description="Db2 LUW 用 db2 backup db ... to <dir>（需实例用户或 db2 命令行环境）",
    params_hint={"cli_bin": "db2"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'CLI="${PLATFORM_PARAM_CLI_BIN:-db2}"\n'
            '[ -n "${PLATFORM_PARAM_INSTANCE_USER:-}" ] && { su - "${PLATFORM_PARAM_INSTANCE_USER}" -c "'
            '$CLI connect to $PLATFORM_DB_NAME user $PLATFORM_DB_USER using $PLATFORM_DB_PASSWORD; '
            "$CLI backup db $PLATFORM_DB_NAME to \\\"$PLATFORM_BACKUP_DIR\\\" compress\"; } || \\\n"
            '  { "$CLI" connect to "$PLATFORM_DB_NAME" user "$PLATFORM_DB_USER" using "$PLATFORM_DB_PASSWORD"; '
            '"$CLI" backup db "$PLATFORM_DB_NAME" to "$PLATFORM_BACKUP_DIR" compress; }\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'TDB="${PLATFORM_RESTORE_DB:-${PLATFORM_DB_NAME}_restore}"\n'
            'CLI="${PLATFORM_PARAM_CLI_BIN:-db2}"\n'
            'TMP=$(mktemp -d); cp -f "$PLATFORM_BACKUP_FILE" "$TMP/"\n'
            '"$CLI" restore db "$PLATFORM_DB_NAME" from "$TMP" taken at '
            '"${PLATFORM_PARAM_TIMESTAMP:?请在 params 填写备份时间戳}" into "$TDB" '
            'redirect generate script /tmp/aidbm_rst.sql || true\n'
            '"$CLI" -tvf /tmp/aidbm_rst.sql\n'
            'rm -rf "$TMP"; echo "RESTORE_OK db=$TDB"\n'),
        test_conn=("set -e\n" + _PATH_PREP + '"${PLATFORM_PARAM_CLI_BIN:-db2}" connect to '
                   '"$PLATFORM_DB_NAME" user "$PLATFORM_DB_USER" using "$PLATFORM_DB_PASSWORD"\n'),
    ),
)

_add(
    key="informix", db_type_hint="informix", label="IBM Informix", display_name="Informix",
    group="other", category="relational", default_port=9088, icon="bi-database",
    client_tools=["dbexport", "dbimport"],
    description="Informix 用 dbexport 导出 ASCII 目录、dbimport 建库（需 INFORMIXSERVER/INFORMIXDIR 环境）",
    params_hint={"export_bin": "dbexport", "import_bin": "dbimport", "informixdir": "/opt/IBM/informix"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'export INFORMIXDIR="${PLATFORM_PARAM_INFORMIXDIR:-/opt/IBM/informix}"\n'
            '"${PLATFORM_PARAM_EXPORT_BIN:-dbexport}" "$PLATFORM_DB_NAME" '
            '-o "$PLATFORM_BACKUP_DIR" -q\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.exp.tar.gz" -C "$PLATFORM_BACKUP_DIR" '
            '"${PLATFORM_DB_NAME}.exp"\n'
            'rm -rf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.exp"; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'export INFORMIXDIR="${PLATFORM_PARAM_INFORMIXDIR:-/opt/IBM/informix}"\n'
            'TDB="${PLATFORM_RESTORE_DB:-${PLATFORM_DB_NAME}_restore}"\n'
            'TMP=$(mktemp -d); tar xzf "$PLATFORM_BACKUP_FILE" -C "$TMP"\n'
            '"${PLATFORM_PARAM_IMPORT_BIN:-dbimport}" "$TMP"/*.exp -i -d "$TDB"\n'
            'rm -rf "$TMP"; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   'export INFORMIXDIR="${PLATFORM_PARAM_INFORMIXDIR:-/opt/IBM/informix}"\n'
                   'echo "SELECT FIRST 1 CURRENT FROM systables;" | '
                   '"${PLATFORM_PARAM_CLI_BIN:-dbaccess}" "$PLATFORM_DB_NAME" -\n'),
    ),
)

_add(
    key="sybase_ase", db_type_hint="sybase_ase", label="SAP ASE (Sybase)", display_name="SAP ASE",
    group="other", category="relational", default_port=5000, icon="bi-database",
    client_tools=["isql"], description="SAP ASE 用 dump database / load database（isql 执行，产物在服务器本地设备路径）",
    params_hint={"cli_bin": "isql"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'cat > /tmp/aidbm_ase_$$.sql <<SQL\n'
            'dump database $PLATFORM_DB_NAME to "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}.dmp"\ngo\nSQL\n'
            '"${PLATFORM_PARAM_CLI_BIN:-isql}" -S "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '-U "$PLATFORM_DB_USER" -P "$PLATFORM_DB_PASSWORD" -i /tmp/aidbm_ase_$$.sql\n'
            'rm -f /tmp/aidbm_ase_$$.sql; echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            'cat > /tmp/aidbm_asel_$$.sql <<SQL\n'
            'load database $PLATFORM_DB_NAME from "$PLATFORM_BACKUP_FILE"\ngo\nSQL\n'
            '"${PLATFORM_PARAM_CLI_BIN:-isql}" -S "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '-U "$PLATFORM_DB_USER" -P "$PLATFORM_DB_PASSWORD" -i /tmp/aidbm_asel_$$.sql\n'
            'rm -f /tmp/aidbm_asel_$$.sql; echo RESTORE_OK\n'),
        test_conn=("set -e\n" + _PATH_PREP + 'echo "select @@version" | "${PLATFORM_PARAM_CLI_BIN:-isql}" '
                   '-S "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" '
                   '-P "$PLATFORM_DB_PASSWORD"\n'),
    ),
)

_add(
    key="sap_hana", db_type_hint="sap_hana", label="SAP HANA", display_name="SAP HANA",
    group="other", category="relational", default_port=30015, icon="bi-database",
    client_tools=["hdbsql"], description="SAP HANA 用 hdbsql 执行 BACKUP DATA ... USING FILE（服务端路径，"
    "恢复用 RECOVER DATABASE）",
    params_hint={"cli_bin": "hdbsql"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            '"${PLATFORM_PARAM_CLI_BIN:-hdbsql}" -n "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '-u "$PLATFORM_DB_USER" -p "$PLATFORM_DB_PASSWORD" -x -a \\\n'
            '  "BACKUP DATA FOR $PLATFORM_DB_NAME USING FILE '
            '(\'$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_databackup\')"\n'
            'echo BACKUP_OK\n'),
        restore=(
            "set -e\n" + _PATH_PREP +
            '"${PLATFORM_PARAM_CLI_BIN:-hdbsql}" -n "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
            '-u "$PLATFORM_DB_USER" -p "$PLATFORM_DB_PASSWORD" -x -a \\\n'
            '  "RECOVER DATABASE FOR $PLATFORM_DB_NAME UNTIL TIMESTAMP '
            '\'${PLATFORM_PARAM_UNTIL_TS:?请在 params 填写 until_ts}\' CLEAR LOG"\n'
            'echo "RESTORE_OK（HANA 恢复会重启租户库，请核对）"\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   'echo "SELECT DATABASE_NAME FROM SYS_DATABASES.M_SERVICES" | '
                   '"${PLATFORM_PARAM_CLI_BIN:-hdbsql}" -n "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" '
                   '-u "$PLATFORM_DB_USER" -p "$PLATFORM_DB_PASSWORD" -x\n'),
    ),
)

_add(
    key="access", db_type_hint="access", label="Microsoft Access", display_name="Access",
    group="other", category="file", default_port=None, icon="bi-file-earmark-binary",
    client_tools=["mdb-export"], backup_modes=["logical"], supports_full_instance=False,
    description="Access 用 mdb-tools 的 mdb-export 逐表导出 CSV（params.db_file 填 .mdb/.accdb 路径）",
    params_hint={"cli_bin": "mdb-export", "db_file": "/data/app.mdb"},
    scripts=_raw_scripts(
        backup=(
            "set -e\nmkdir -p \"$PLATFORM_BACKUP_DIR\"\n" + _PATH_PREP +
            'DBF="${PLATFORM_PARAM_DB_FILE:?请在 params 填写 db_file}"\n'
            'OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_access"; mkdir -p "$OUT"\n'
            '"${PLATFORM_PARAM_CLI_BIN:-mdb-tables}" -1 "$DBF" | while read -r t; do\n'
            '  [ -n "$t" ] && "${PLATFORM_PARAM_EXPORT_BIN:-mdb-export}" "$DBF" "$t" > "$OUT/$t.csv"\n'
            'done\n'
            'tar czf "$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_access.tar.gz" -C "$OUT" .\n'
            'rm -rf "$OUT"; echo BACKUP_OK\n'),
        restore=(
            "set -e\necho \"Access 恢复需人工处理：解包 CSV 后用 Access/ODBC 导入\"; "
            'echo "备份文件: $PLATFORM_BACKUP_FILE"\n'),
        test_conn=("set -e\n" + _PATH_PREP +
                   '"${PLATFORM_PARAM_CLI_BIN:-mdb-tables}" -1 "${PLATFORM_PARAM_DB_FILE:?请填写 db_file}" '
                   '| head -5\n'),
    ),
)


# ---------------------------------------------------------------- 对外接口
GROUP_LABELS = {
    "pg": "PostgreSQL 协议族（国产 / 云原生 / 分布式）",
    "mysql": "MySQL 协议族（国产 / 云原生 / 分布式）",
    "oracle": "Oracle 兼容族（国产）",
    "olap": "分析型 / MPP / 数仓",
    "ts": "时序数据库",
    "nosql": "NoSQL / KV / 文档 / 搜索",
    "graph": "图数据库",
    "other": "其他（嵌入式 / 商用 / 文件型）",
}

GROUP_ORDER = ["pg", "mysql", "oracle", "olap", "ts", "nosql", "graph", "other"]


def _brief(t: dict) -> dict:
    """转成 API 返回结构（key + 全部可编辑字段）。"""
    out = {k: v for k, v in t.items() if k != "scripts"}
    out.update(t.get("scripts") or {})
    out["key"] = t["key"]
    out["label"] = t["label"]
    out["verification"] = t.get("verification", "template")
    return out


def all_templates() -> list[dict]:
    return [_brief(t) for t in CATALOG]


def by_key(key: str) -> Optional[dict]:
    for t in CATALOG:
        if t["key"] == key:
            return _brief(t)
    return None


def grouped() -> list[dict]:
    """按族分组返回（前端「从模板复制」弹窗用）。"""
    out = []
    for g in GROUP_ORDER:
        items = [_brief(t) for t in CATALOG if t.get("group") == g]
        if items:
            out.append({"group": g, "label": GROUP_LABELS.get(g, g), "templates": items})
    return out


def stats() -> dict:
    total = len(CATALOG)
    by_ver = {}
    by_group = {}
    for t in CATALOG:
        by_ver[t.get("verification", "template")] = by_ver.get(t.get("verification", "template"), 0) + 1
        by_group[t.get("group", "other")] = by_group.get(t.get("group", "other"), 0) + 1
    return {"total": total, "by_verification": by_ver, "by_group": by_group}
