"""备份产物导出格式（dump_format）统一定义与解析。

背景
----
此前各引擎把"导出格式"写死在代码里（PG/金仓固定 -Fc 自定义格式、MySQL 固定
mysqldump 标准 SQL、Oracle 固定 expdp .dmp 等），用户在新建任务时无法选择。
本模块把"每种数据库支持哪些导出格式、对应什么命令行开关、落盘什么后缀、用什么
工具恢复"集中成一张表，供：

  * 引擎备份时决定 CLI 开关与产物后缀（core/engines/*.py、core/remote_dump.py）
  * 引擎恢复时决定用哪个客户端回放（pg_restore / psql / mysql / mongorestore）
  * 前端「备份文件格式」下拉动态渲染（经 API /api/meta 或前端常量同步）

存储：任务 extra_options.dump_format（字符串，取值见 DUMP_FORMATS 的 value）。
未配置或取值非法 → 回退到该类型的默认行为（PG 系为 auto：跟随任务压缩开关），
保证存量任务行为完全不变。

设计原则
--------
1. 只列出数据库原生工具**真实支持**的格式，不提供无法落地的假选项；
   若某种数据库只有一种原生格式（Oracle dmp / SQL Server bak / Redis rdb），
   明确标注 native_only，前端展示为不可改。
2. 恢复通道必须真实可用：凡是平台无法自动回放的格式（如 MySQL --xml），
   在 restore 字段标注 "unsupported" 并在 UI 明确提示，不允许"假成功"。
"""

# 每个格式项的字段：
#   value    : 存 extra_options.dump_format 的取值
#   label    : 前端展示文案
#   flag     : 传给 dump 工具的命令行开关（字符串或 None）
#   ext      : 落盘文件后缀（目录型格式 ext 为空，由 dir_ext 决定打包后缀）
#   restore  : 恢复方式（pg_restore / psql / mysql / mongorestore / unsupported）
#   note     : 前端与文档使用的说明
DUMP_FORMATS = {
    "mysql": [
        {
            "value": "sql", "label": "SQL 文本（.sql）", "default": True,
            "flag": None, "ext": ".sql", "restore": "mysql",
            "note": "mysqldump 标准输出：建表 + INSERT 语句，可直接用 mysql 客户端回放，"
                    "兼容性最好、可人工审阅，推荐作为默认。",
        },
        {
            "value": "xml", "label": "XML（.xml，仅导出/交换，不可自动恢复）",
            "flag": "--xml", "ext": ".xml", "restore": "unsupported",
            "note": "mysqldump --xml：把库表数据导成 XML，用于数据交换、人工审阅或外部系统对接；"
                    "MySQL 客户端无法直接回放 XML，平台不提供自动恢复（恢复页会明确拒绝）。",
        },
    ],
    "mariadb": [
        {
            "value": "sql", "label": "SQL 文本（.sql）", "default": True,
            "flag": None, "ext": ".sql", "restore": "mysql",
            "note": "mariadb-dump/mysqldump 标准输出，可直接用 mysql 客户端回放，推荐默认。",
        },
        {
            "value": "xml", "label": "XML（.xml，仅导出/交换，不可自动恢复）",
            "flag": "--xml", "ext": ".xml", "restore": "unsupported",
            "note": "XML 形态导出，用于交换/审阅；平台不提供自动恢复。",
        },
    ],
    "postgresql": [
        {
            "value": "auto", "label": "跟随压缩设置（默认）", "default": True,
            "flag": None, "ext": "", "restore": "auto",
            "note": "保持平台默认行为：任务开启压缩 → -Fc 自定义格式（.dump，自带 zlib 压缩，"
                    "支持 pg_restore 选择性恢复与并行回放）；未开压缩 → -Fp 纯文本（.sql）。",
        },
        {
            "value": "custom", "label": "自定义格式 -Fc（.dump）",
            "flag": "-Fc", "ext": ".dump", "restore": "pg_restore",
            "note": "pg_dump -Fc：二进制归档，自带压缩、体积最小；支持 pg_restore 按表/按 schema "
                    "选择性恢复与 -j 并行回放，恢复速度最快，推荐生产使用。",
        },
        {
            "value": "plain", "label": "纯文本 SQL -Fp（.sql）",
            "flag": "-Fp", "ext": ".sql", "restore": "psql",
            "note": "pg_dump -Fp：可读的 SQL 脚本，可人工审阅/改造/版本比对，可用 psql 直接回放；"
                    "体积较大、不支持选择性恢复。",
        },
        {
            "value": "tar", "label": "tar 归档 -Ft（.tar）",
            "flag": "-Ft", "ext": ".tar", "restore": "pg_restore",
            "note": "pg_dump -Ft：tar 归档（每表一个成员），可用 pg_restore 选择性恢复，"
                    "但不支持压缩且单表有 8GB 限制。",
        },
        {
            "value": "directory", "label": "目录格式 -Fd（打包 .tar.gz）",
            "flag": "-Fd", "ext": ".tar.gz", "dir": True, "restore": "pg_restore_dir",
            "note": "pg_dump -Fd：目录归档，天然支持并行 dump 与并行恢复（-j），最适合超大库；"
                    "平台在数据库服务器侧打包为 tar.gz 拉回，恢复时自动解开再用 pg_restore 回放。",
        },
    ],
    "kingbase": [
        {
            "value": "auto", "label": "跟随压缩设置（默认）", "default": True,
            "flag": None, "ext": "", "restore": "auto",
            "note": "默认行为：开压缩 → sys_dump -Fc（.dump）；未开压缩 → -Fp（.sql）。",
        },
        {
            "value": "custom", "label": "自定义格式 -Fc（.dump）",
            "flag": "-Fc", "ext": ".dump", "restore": "pg_restore",
            "note": "sys_dump -Fc：自带压缩、体积最小，可用 sys_restore 选择性/并行恢复，推荐。",
        },
        {
            "value": "plain", "label": "纯文本 SQL -Fp（.sql）",
            "flag": "-Fp", "ext": ".sql", "restore": "psql",
            "note": "sys_dump -Fp：可读 SQL 脚本，用 ksql 回放；体积较大。",
        },
        {
            "value": "tar", "label": "tar 归档 -Ft（.tar）",
            "flag": "-Ft", "ext": ".tar", "restore": "pg_restore",
            "note": "sys_dump -Ft：tar 归档，可用 sys_restore 选择性恢复。",
        },
        {
            "value": "directory", "label": "目录格式 -Fd（打包 .tar.gz）",
            "flag": "-Fd", "ext": ".tar.gz", "dir": True, "restore": "pg_restore_dir",
            "note": "sys_dump -Fd：目录归档，支持并行；平台打包 tar.gz 拉回，恢复时解开后回放。",
        },
    ],
    # openGauss（gs_dump 与 pg_dump 同源，格式开关一致；恢复分别用 gs_restore/gsql）
    "opengauss": [
        {
            "value": "auto", "label": "跟随压缩设置（默认）", "default": True,
            "flag": None, "ext": "", "restore": "auto",
            "note": "保持平台默认行为：任务开启压缩 → gs_dump -Fc（.dump，自带压缩，支持 gs_restore "
                    "选择性恢复）；未开压缩 → gs_dump -Fp 纯文本（.sql，用 gsql 回放）。",
        },
        {
            "value": "custom", "label": "自定义格式 -Fc（.dump）",
            "flag": "-Fc", "ext": ".dump", "restore": "pg_restore",
            "note": "gs_dump -Fc：二进制归档，自带压缩、体积最小；支持 gs_restore 按表/按 schema "
                    "选择性恢复，推荐生产使用。",
        },
        {
            "value": "plain", "label": "纯文本 SQL -Fp（.sql）",
            "flag": "-Fp", "ext": ".sql", "restore": "psql",
            "note": "gs_dump -Fp：可读 SQL 脚本，可用 gsql 直接回放（openGauss 的 gsql 语言与 PG 高度兼容）；"
                    "体积较大、不支持选择性恢复。",
        },
        {
            "value": "tar", "label": "tar 归档 -Ft（.tar）",
            "flag": "-Ft", "ext": ".tar", "restore": "pg_restore",
            "note": "gs_dump -Ft：tar 归档（每表一个成员），可用 gs_restore 选择性恢复，"
                    "但不支持压缩且单表有 8GB 限制。",
        },
        {
            "value": "directory", "label": "目录格式 -Fd（打包 .tar.gz）",
            "flag": "-Fd", "ext": ".tar.gz", "dir": True, "restore": "pg_restore_dir",
            "note": "gs_dump -Fd：目录归档，支持并行 dump 与并行恢复；平台在数据库服务器侧打包为 "
                    "tar.gz 拉回，恢复时自动解开再用 gs_restore 回放。",
        },
    ],
    "mongodb": [
        {
            "value": "archive", "label": "单文件归档 --archive（.archive）", "default": True,
            "flag": "--archive", "ext": ".archive", "restore": "mongorestore",
            "note": "mongodump --archive：单文件归档流，便于落地存储与跨主机传输，"
                    "恢复用 mongorestore --archive。",
        },
        {
            "value": "gzip", "label": "压缩归档 --archive --gzip（.archive.gz）",
            "flag": "--gzip", "ext": ".archive.gz", "restore": "mongorestore_gzip",
            "note": "在归档基础上由 mongodump 自带 gzip 压缩（体积最小），"
                    "恢复需 mongorestore --gzip --archive。",
        },
        {
            "value": "directory", "label": "目录导出 --out（.tar.gz）",
            "flag": "--out", "ext": ".tar.gz", "dir": True, "restore": "mongorestore_dir",
            "note": "mongodump --out：每库每集合一个文件（BSON），便于单集合恢复与人工检查；"
                    "平台在服务器侧打包 tar.gz 拉回，恢复时解开后 mongorestore。",
        },
    ],
    # 以下类型由数据库原生工具决定产物格式，平台不提供可选格式（诚实标注）
    "oracle": [
        {
            "value": "dmp", "label": "Data Pump 转储文件（.dmp，原生唯一）", "default": True,
            "flag": None, "ext": ".dmp", "restore": "impdp", "native_only": True,
            "note": "Oracle 逻辑备份走 expdp，产物固定为 Data Pump 转储集 .dmp，"
                    "恢复用 impdp；如需别的产物形态请用物理备份（RMAN）或自定义脚本模式。",
        },
    ],
    "dameng": [
        {
            "value": "dmp", "label": "dexp 转储文件（.dmp，原生唯一）", "default": True,
            "flag": None, "ext": ".dmp", "restore": "dimp", "native_only": True,
            "note": "达梦逻辑备份走 dexp，产物固定 .dmp，恢复用 dimp。",
        },
    ],
    "sqlserver": [
        {
            "value": "bak", "label": "SQL Server 备份集（.bak/.diff/.trn）", "default": True,
            "flag": None, "ext": ".bak", "restore": "restore_db", "native_only": True,
            "note": "SQL Server 走 BACKUP DATABASE/LOG TO DISK，产物由备份类型决定："
                    "全量 .bak、差异 .diff、日志 .trn；恢复用 RESTORE DATABASE。",
        },
    ],
    "redis": [
        {
            "value": "rdb", "label": "RDB 快照（.rdb，原生唯一）", "default": True,
            "flag": None, "ext": ".rdb", "restore": "rdb", "native_only": True,
            "note": "Redis 备份走 redis-cli --rdb（或复制线上 dump.rdb），产物固定为 RDB 快照。",
        },
    ],
}

# PG 系（postgresql / kingbase / opengauss）在 dump_format=auto 时的行为：按压缩开关决定
_PG_AUTO = {True: "custom", False: "plain"}
_PG_FAMILY = ("postgresql", "kingbase", "opengauss")


def supported_formats(db_type: str) -> list:
    """返回该数据库类型支持的格式列表（每项为 dict 副本）。"""
    return [dict(x) for x in (DUMP_FORMATS.get(db_type) or [])]


def _default_item(items: list) -> dict:
    for it in items:
        if it.get("default"):
            return it
    return items[0] if items else {}


def resolve(db_type: str, extra: dict = None, compress: bool = True) -> dict:
    """解析出该任务实际使用的导出格式。

    Args:
        db_type: 数据库类型（mysql/postgresql/kingbase/...）。
        extra:   已解析的 extra_options 字典（可为空）。
        compress: 任务是否开启压缩（PG 系 auto 时据此二选一）。

    Returns:
        格式项 dict（副本）。至少包含 value / label / flag / ext / restore。
        未知类型返回 {"value": "native", ...}（不改变任何命令行行为）。
    """
    items = DUMP_FORMATS.get(db_type) or []
    if not items:
        return {"value": "native", "label": "原生格式", "flag": None,
                "ext": "", "restore": "auto", "note": "该类型沿用引擎原生产物格式。"}

    raw = ""
    try:
        if not isinstance(extra, dict):
            extra = {}
        raw = str(extra.get("dump_format") or "").strip().lower()
    except Exception:
        raw = ""

    chosen = None
    if raw:
        for it in items:
            if it["value"] == raw:
                chosen = it
                break
    if chosen is None:
        chosen = _default_item(items)

    out = dict(chosen)
    # PG 系 auto：按压缩开关落到 custom / plain（保持历史默认行为）
    if out.get("value") == "auto" and db_type in _PG_FAMILY:
        target = _PG_AUTO[bool(compress)]
        for it in items:
            if it["value"] == target:
                out = dict(it)
                out["auto_resolved"] = True
                break
    # auto 未解析出具体 flag 时，按类型兜底
    if not out.get("flag") and out.get("value") == "auto":
        out["ext"] = ""
    return out


def is_native_only(db_type: str) -> bool:
    """该类型是否只有数据库原生唯一格式（前端应置灰并提示）。"""
    items = DUMP_FORMATS.get(db_type) or []
    if not items:
        return True
    return len(items) == 1 and bool(items[0].get("native_only"))
