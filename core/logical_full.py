# -*- coding: utf-8 -*-
"""全实例逻辑备份/恢复 —— 本机实现（PG 系 / MySQL 系通用）。

与 core/remote_dump.py 的远端（SSH）实现语义一致：
- 全实例备份 = 枚举库（默认排除系统库）→ 逐库 dump（每库一个文件，
  各自一致性快照）+ 全局对象（PG 系 dumpall -g）→ tar.gz + manifest.json；
  pg_dump/sys_dump 不存在 --all-databases；MySQL 虽有该参数，但为统一
  「逐库文件 + 可排除系统库」语义，同样走逐库打包。
- 全实例恢复 = 解包 → 全局对象 → 缺失库自动 CREATE → 逐库恢复。

产物格式（manifest.json）：
  {"format": "multi-db-tar", "db_type": "...", "generated_at": "...",
   "globals": "yes|no|failed|na", "include_system_dbs": false,
   "databases": ["db1", "db2"]}

系统库清单（默认排除，include_system_dbs=true 时包含）见 SYSTEM_DBS。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time

import config

logger = logging.getLogger(__name__)

# PG 协议系（逐库 dump + globals 语义一致），新增同源库时加入此元组即可
PG_FAMILY = ("postgresql", "kingbase", "opengauss")

# 各库类型的系统库清单（全实例默认排除，业务库优先）
SYSTEM_DBS = {
    "mysql": ("information_schema", "performance_schema", "mysql", "sys"),
    "mariadb": ("information_schema", "performance_schema", "mysql", "sys"),
    "postgresql": ("postgres", "template0", "template1"),
    "kingbase": ("template0", "template1", "template2", "security", "test"),
    # openGauss 默认库：postgres（维护库）+ 两个模板库
    "opengauss": ("postgres", "template0", "template1"),
}

# 各库类型差异点
TOOLING = {
    "postgresql": {
        "catalog_sqls": (
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("postgres", "template1"),
        "env_exports": ("PGPASSWORD",),
        "default_query": "psql",
        "default_dumpall": "pg_dumpall",
    },
    "kingbase": {
        # V8/V009R003=sys_database、V9R1=pg_database —— 运行时候选探测
        "catalog_sqls": (
            "SELECT datname FROM sys_database WHERE NOT datistemplate ORDER BY 1",
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("test", "postgres", "security", "template1"),
        "env_exports": ("KINGBASE_PASSWORD", "PGPASSWORD"),
        "default_query": "ksql",
        "default_dumpall": "sys_dumpall",
    },
    # openGauss：gsql/gs_dump/gs_dumpall（工具随 GAUSSHOME/bin 提供）
    "opengauss": {
        "catalog_sqls": (
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("postgres", "template1"),
        "env_exports": ("PGPASSWORD",),
        "default_query": "gsql",
        "default_dumpall": "gs_dumpall",
    },
    "mysql": {
        "catalog_sqls": ("SHOW DATABASES",),
        "maint_candidates": ("mysql",),
        "env_exports": ("MYSQL_PWD",),
        "default_query": "mysql",
    },
    "mariadb": {
        "catalog_sqls": ("SHOW DATABASES",),
        "maint_candidates": ("mysql",),
        "env_exports": ("MYSQL_PWD",),
        "default_query": "mysql",
    },
}


def _build_env(db_type: str, password: str) -> dict:
    env = os.environ.copy()
    if password:
        for name in TOOLING[db_type]["env_exports"]:
            env[name] = password
    return env


def _run(cmd: list, env: dict, timeout: int = 3600, stdin_file=None):
    kwargs = {"capture_output": True, "text": True, "timeout": timeout, "env": env}
    if stdin_file:
        with open(stdin_file, "rb") as f:
            p = subprocess.run(cmd, stdin=f, **kwargs)
    else:
        p = subprocess.run(cmd, **kwargs)
    return p.returncode, p.stdout, p.stderr


def _which_any(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


# --------------------------------------------------------------------------- #
# 临时工作目录与磁盘空间
#
# 全实例备份要先把每个库 dump 成本地文件、再打成 tar.gz。工作目录原先固定为
# tempfile 默认的 /tmp，而 /tmp 常常是小分区或 tmpfs：大实例写满后 mysqldump
# 立刻以 `Got errno 28 on write`（ENOSPC = 设备已满）失败，用户看到的却只是
# 一句"dump 失败(rc=5)"，根本不知道写哪个路径满了、还差多少空间。
# 现在：工作目录默认与产物同盘，dump 之前先做真实的容量预检查，
# 失败时给出真实数字（可用空间/预计占用）与可执行的处置建议。
# --------------------------------------------------------------------------- #

def human_size(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "未知"
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
    return "%.1f TB" % n


def _work_root(prefer_path: str = "") -> str:
    """选择并**实测可写**的临时工作目录（优先与产物同盘，避免 /tmp 写满）。"""
    cands = []
    cfg_dir = (getattr(config, "FULL_INSTANCE_WORK_DIR", "")
               or os.environ.get("BP_WORK_DIR", "") or "").strip()
    if cfg_dir:
        cands.append(cfg_dir)
    if prefer_path:
        base = prefer_path if os.path.isdir(prefer_path) else (
            os.path.dirname(prefer_path) or "/")
        cands.append(os.path.join(base, ".bp_work"))
    cands.append(tempfile.gettempdir())
    errs = []
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".bp_write_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
            return c
        except Exception as e:
            errs.append("%s（%s）" % (c, str(e) or type(e).__name__))
    raise RuntimeError("没有可用的临时工作目录：%s" % "；".join(errs))


def disk_free(path: str) -> int:
    """path 所在分区的可用字节数（真实 statvfs 数据；取不到返回 -1）。"""
    d = path
    while d and not os.path.exists(d):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    try:
        return shutil.disk_usage(d or ".").free
    except OSError:
        return -1


_ENOSPC_MARKS = ("errno 28", "no space left", "disk full", "enospc",
                 "quota exceeded", "not enough space")


def is_enospc(text) -> bool:
    """识别"磁盘写满"类错误（不同工具措辞不同，统一判定）。"""
    t = str(text or "").lower()
    return any(m in t for m in _ENOSPC_MARKS)


def _key_line(err) -> str:
    """从 stderr 里挑出**真正说明问题**的那一行。

    mysqldump 会先打一堆无关警告（如 "column statistics not supported"），
    直接截断前 200 字符会把警告当原因展示，反而盖住关键信息。
    """
    lines = [ln.strip() for ln in str(err or "").splitlines() if ln.strip()]
    for ln in lines:
        if is_enospc(ln):
            return ln
    return lines[-1] if lines else ""


def _space_hint(err, *paths) -> str:
    """ENOSPC 的真实诊断：哪个路径、各自可用空间、关键错误、怎么处置。"""
    ps = [p for p in paths if p]
    where = "；".join("%s 当前可用 %s" % (p, human_size(disk_free(p))) for p in ps)
    return ("磁盘空间不足（ENOSPC：写入时设备已满）%s。关键错误: %s。"
            "处置：清理对应分区 / 把任务「产物根目录」改到空间充足的分区 / "
            "用环境变量 BP_WORK_DIR 指定更大的临时工作目录"
            % (("；" + where) if where else "",
               _key_line(err) or "命令未输出 stderr"))


# InnoDB **物理文件大小**查询：按版本兼容顺序尝试（查不到就跳过，不影响主流程）。
# 为什么需要它：information_schema.tables 的 DATA_LENGTH 是 InnoDB 的**采样统计**，
# 新建/大量写入后未必刷新（实测刚灌 4MB 的表只报 16KB），据此做空间预检会严重
# 低估，等于让"备份前拦截"形同虚设。表空间文件大小是磁盘上的真实占用，可信得多。
# MySQL 5.6/5.7/MariaDB 10.x 用 INNODB_SYS_TABLESPACES，MySQL 8.0+ 改名为
# INNODB_TABLESPACES（旧名已移除），两个都试，谁返回结果用谁。
_PHYSICAL_SIZE_SQLS = (
    "SELECT LEFT(NAME, LOCATE('/', NAME) - 1), SUM(FILE_SIZE) "
    "FROM information_schema.innodb_tablespaces "
    "WHERE LOCATE('/', NAME) > 1 GROUP BY 1",
    "SELECT LEFT(NAME, LOCATE('/', NAME) - 1), SUM(FILE_SIZE) "
    "FROM information_schema.innodb_sys_tablespaces "
    "WHERE LOCATE('/', NAME) > 1 GROUP BY 1",
)


def _estimate_physical(db_type, *, query_tool, host, port, user, env) -> dict:
    """返回 {schema: 物理占用字节}（MySQL 系专用，取不到返回 {}）。"""
    if db_type in PG_FAMILY:
        return {}
    for sql in _PHYSICAL_SIZE_SQLS:
        cmd = [query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
               "-u", user, "-N", "-B", "-e", sql]
        rc, out, _err = _run(cmd, env, timeout=120)
        if rc != 0 or not out or not out.strip():
            continue
        got = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                got[parts[0].strip()] = max(
                    got.get(parts[0].strip(), 0), int(float(parts[1] or 0)))
            except (ValueError, TypeError):
                continue
        if got:
            return got
    return {}


def estimate_instance_bytes(db_type: str, *, query_tool, host, port, user, env,
                            maint: str, dbs) -> int:
    """估算本次要落盘的备份数据量（查询数据库自身，失败返回 -1）。

    取值口径：**逻辑统计与物理占用逐库取大者**再求和。
    - MySQL 系：information_schema.tables 的 DATA_LENGTH+INDEX_LENGTH（所有版本
      都有，但是采样值可能滞后）VS InnoDB 表空间 FILE_SIZE（真实磁盘占用，
      8.0 用 innodb_tablespaces、5.6/5.7 用 innodb_sys_tablespaces）；
    - PG 系：pg_database_size()，本身就是磁盘真实大小。

    估算值只用于**提前发现空间根本不够**，不当作精确值。
    """
    is_pg = db_type in PG_FAMILY
    want = set(dbs or [])
    if is_pg:
        if not maint:
            return -1
        sql = ("SELECT datname, pg_database_size(datname) FROM pg_database "
               "WHERE NOT datistemplate")
        cmd = [query_tool, "-h", str(host), "-p", str(port), "-U", user,
               "-d", maint, "-t", "-A", "-c", sql]
    else:
        sql = ("SELECT table_schema, SUM(data_length + index_length) "
               "FROM information_schema.tables GROUP BY table_schema")
        cmd = [query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
               "-u", user, "-N", "-B", "-e", sql]
    rc, out, _err = _run(cmd, env, timeout=120)
    if rc != 0 or not out:
        return -1
    logical = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|") if is_pg else line.split("\t")
        if len(parts) < 2:
            continue
        name, size = parts[0].strip(), parts[1].strip()
        try:
            n = int(float(size or 0))
        except (ValueError, TypeError):
            continue
        logical[name] = max(logical.get(name, 0), n)

    total = 0
    physical = _estimate_physical(db_type, query_tool=query_tool, host=host,
                                  port=port, user=user, env=env)
    for name, n in logical.items():
        if want and name not in want:
            continue
        total += max(n, physical.get(name, 0))
    # 只存在于物理统计里的库（如逻辑统计尚未产生行时）也要计入
    for name, n in physical.items():
        if want and name not in want:
            continue
        if name not in logical:
            total += n
    return total


def enumerate_databases(db_type: str, query_tool: str, host: str, port,
                        user: str, env: dict, include_system_dbs: bool = False):
    """枚举库清单，默认过滤系统库。返回 (命中的维护库, 库名列表, 最近错误)。

    PG 系用 SQL 目录表查询（需先连到某个维护库）；MySQL 系 SHOW DATABASES
    无需维护库。全部候选失败返回 ("", [], 最后一条 stderr)——把 stderr 带回去，
    是为了让"连不上/认证失败"这类真实原因能出现在用户看到的失败消息里。
    """
    cfg = TOOLING[db_type]
    is_pg = db_type in PG_FAMILY
    sys_set = set() if include_system_dbs else set(SYSTEM_DBS.get(db_type) or ())
    last_err = ""
    for mdb in cfg["maint_candidates"]:
        for catalog_sql in cfg["catalog_sqls"]:
            if is_pg:
                cmd = [query_tool, "-h", str(host), "-p", str(port), "-U", user,
                       "-d", mdb, "-t", "-A", "-c", catalog_sql]
            else:
                # 注意 MySQL 客户端 -P(大写)=端口、-p(小写)=密码
                # --no-defaults 必须在首位：否则平台机的 /root/.my.cnf 会**覆盖**
                # 任务里配置的用户与密码（MYSQL_PWD 环境变量优先级低于配置文件），
                # 表现为密码填错也能"备份成功"，实际连的是 my.cnf 里的实例/账号。
                cmd = [query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                       "-u", user, "-N", "-B", "-e", catalog_sql]
            rc, out, err = _run(cmd, env)
            if rc == 0 and out.strip():
                dbs = [ln.strip().split("\t")[0] for ln in out.splitlines() if ln.strip()]
                dbs = [d for d in dbs if d and d not in sys_set]
                return mdb, dbs, ""
            if not last_err and err and err.strip():
                last_err = err.strip()
    return "", [], last_err


def _mysqldump_to_file(cmd: list, env: dict, fpath: str):
    """mysqldump 输出**流式**落盘（stdout 直连文件，二进制保真）。

    绝不能把产物读进内存：全实例/大库的 dump 动辄数 GB，而平台常与数据库
    部署在同一台机器上（内存被 DB 占用），一旦用 ``capture_output=True`` 整体
    缓存，Python 会抛 ``MemoryError``——它恰好 **``str()`` 为空字符串**，最终
    上报成「MySQL 全实例备份失败: 」这种看不到任何原因的消息。用户侧表现就是
    「明明备份跑了 5 分多钟，最后突然失败」，极难排查。

    改为 stdout 直连文件后，本机备份内存占用恒定为管道缓冲区大小（与产物大小无关）。
    """
    with open(fpath, "wb") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE,
                           timeout=7200, env=env)
    err = (p.stderr or b"").decode("utf-8", "replace")[:300]
    return p.returncode, "", err


def backup_full_instance(db_type: str, *, host, port, user, password,
                         dump_tool: str, out_path: str,
                         query_tool: str = "", dumpall_tool: str = "",
                         include_system_dbs: bool = False) -> dict:
    """本机全实例备份：逐库一个文件 + globals（PG 系）→ tar.gz。返回 manifest。"""
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    is_pg = db_type in PG_FAMILY
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法枚举数据库执行全实例备份")
    if is_pg:
        dumpall_tool = dumpall_tool or _which_any(cfg["default_dumpall"])

    maint, dbs, enum_err = enumerate_databases(
        db_type, query_tool, host, port, user, env, include_system_dbs)
    include_system_dbs_orig = bool(include_system_dbs)
    if not is_pg:
        # mysqldump 无法导出虚拟库（--all-databases 同样跳过），勾选包含也排除
        dbs = [d for d in dbs
               if d not in ("information_schema", "performance_schema")]
    if not dbs:
        # 枚举成功但过滤系统库后为空（典型：实例里只有默认系统库）→
        # 自动兜底包含系统库（information_schema/performance_schema 为虚拟库
        # mysqldump 无法导出，仍排除），而不是直接失败让用户摸不着头脑。
        if not enum_err and not include_system_dbs:
            maint2, dbs2, _ = enumerate_databases(
                db_type, query_tool, host, port, user, env,
                include_system_dbs=True)
            if not is_pg:
                dbs2 = [d for d in dbs2
                        if d not in ("information_schema", "performance_schema")]
            if dbs2:
                include_system_dbs = True
                dbs = dbs2
                maint = maint2 or maint
                logger.warning("[全实例] %s:%s 无业务库，已自动包含系统库: %s",
                               host, port, ", ".join(dbs))
            else:
                raise RuntimeError(
                    f"无法在 {host}:{port} 枚举到可备份的数据库——"
                    f"实例为空（仅含 information_schema/performance_schema 虚拟库），"
                    f"请先创建业务库后再配置备份")
        if not dbs:
            if enum_err:
                # 连不上/认证失败才是真原因，比"没有可备份的库"有用得多
                hint = (f"连接或认证失败: {enum_err[:200]}。"
                        f"请核对任务里配置的地址、端口、用户与密码")
            elif not include_system_dbs:
                hint = "排除系统库后没有可备份的库，可勾选「包含系统库」"
            else:
                hint = (f"候选维护库: {', '.join(cfg['maint_candidates'])}，"
                        f"请检查连接信息")
            raise RuntimeError(f"无法在 {host}:{port} 枚举到可备份的数据库——{hint}")

    # 工作目录默认与产物同盘：tempfile 默认的 /tmp 常是小分区/tmpfs，
    # 逐库 dump 会把它写满（表现为 mysqldump "Got errno 28 on write"）。
    work = tempfile.mkdtemp(prefix="bp_fullinst_", dir=_work_root(out_path))
    try:
        dbs_dir = os.path.join(work, "dbs")
        os.makedirs(dbs_dir)

        # 空间预检查：用数据库自身统计估算本次落盘量（SQL 文本通常 ≥ 数据大小，
        # 按 1.2 倍留余量），根本不够就**在备份前**失败——而不是跑到一半让
        # mysqldump 扔出一个看不懂的 errno 28。
        est = estimate_instance_bytes(
            db_type, query_tool=query_tool, host=host, port=port, user=user,
            env=env, maint=maint, dbs=dbs)
        free = disk_free(work)
        if est > 0 and free >= 0:
            need = int(est * 1.2)
            note = ("预计占用约 %s（数据库统计 %s × 1.2），临时工作目录 %s 可用 %s"
                    % (human_size(need), human_size(est), work, human_size(free)))
            if free < need:
                raise RuntimeError(
                    "磁盘空间不足，已在备份前终止：%s。请清理该分区，或把任务"
                    "「产物根目录」改到空间充足的分区，也可用环境变量 BP_WORK_DIR "
                    "指定更大的临时工作目录" % note)
            logger.info("[full-instance] %s，空间预检查通过", note)
        else:
            logger.warning(
                "[full-instance] 无法估算数据量（数据库统计查询未返回），跳过空间"
                "预检查；临时工作目录 %s 当前可用 %s",
                work, human_size(free) if free >= 0 else "未知")
        # 第二道空间防线：估算可能得不到（统计查不到/ PG 某些版本），此时靠
        # "每 dump 完一个库就看一眼余量"兜底——下一个库大概率还需要同样量级的
        # 空间，若连上一个库的两倍都放不下，就在**这里**停，而不是跑到最后
        # 一个库写满设备才失败（用户现场就是跑了几分钟才炸）。
        prev_size = 0
        for idx, d in enumerate(dbs, 1):
            if is_pg:
                rc, _o, err = _run([
                    dump_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "-Fc", "-f", os.path.join(dbs_dir, f"{d}.dump"), d,
                ], env, timeout=7200)
            else:
                # MySQL 系：逐库 .sql（--databases 保证含 CREATE DATABASE/USE）
                # --no-defaults 见 enumerate_databases 处说明：必须用任务配置的凭据
                cmd = [dump_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                       "-u", user, "--single-transaction", "--routines",
                       "--triggers", "--events", "--default-character-set=utf8mb4",
                       "--databases", d]
                rc, _o, err = _mysqldump_to_file(
                    cmd, env, os.path.join(dbs_dir, f"{d}.sql"))
            if rc != 0:
                # errno 28 = ENOSPC：给出"写哪个路径满了、还剩多少"的真实诊断，
                # 而不是把裸错误原样丢给用户
                if is_enospc(err):
                    raise RuntimeError(
                        "库 %s dump 失败：%s" % (d, _space_hint(err, work, out_path)))
                raise RuntimeError(
                    "库 %s dump 失败(rc=%s): %s"
                    % (d, rc, (err or "").strip()[:300] or "命令未输出 stderr（请检查该库的读取权限与磁盘）"))

            try:
                size = os.path.getsize(os.path.join(
                    dbs_dir, f"{d}.dump" if is_pg else f"{d}.sql"))
            except OSError:
                size = 0
            free_now = disk_free(work)
            floor = max(prev_size, size) * 2
            if prev_size and size and free_now >= 0 and free_now < floor:
                raise RuntimeError(
                    "磁盘空间不足，已在第 %d/%d 个库（%s）后提前终止，避免产出残缺备份："
                    "已完成 %s；临时工作目录 %s 剩余 %s，不足以容纳下一个库"
                    "（上一个库 dump %s，按 2 倍预留需 %s）。"
                    "处置：清理该分区 / 把任务「产物根目录」改到空间充足的分区 / "
                    "用环境变量 BP_WORK_DIR 指定更大的临时工作目录"
                    % (idx, len(dbs), d, "、".join(dbs[:idx]), work,
                       human_size(free_now), human_size(max(prev_size, size)),
                       human_size(floor)))
            prev_size = max(prev_size, size)

        globals_status = "na"
        globals_path = ""
        if is_pg:
            if dumpall_tool:
                rc, out, _err = _run([
                    dumpall_tool, "-h", str(host), "-p", str(port), "-U", user, "-g",
                ], env, timeout=1800)
                if rc == 0 and out.strip():
                    globals_path = os.path.join(work, "globals.sql")
                    with open(globals_path, "w", encoding="utf-8") as f:
                        f.write(out)
                    globals_status = "yes"
                else:
                    globals_status = "failed"
            else:
                globals_status = "no"

        manifest = {
            "format": "multi-db-tar",
            "db_type": db_type,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "globals": globals_status,
            "include_system_dbs": bool(include_system_dbs),
            # 兜底标记：实例无业务库时自动包含了系统库（审计/恢复时区分）
            "auto_include_system": bool(
                not include_system_dbs_orig and include_system_dbs),
            "databases": dbs,
        }
        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(os.path.join(work, "manifest.json"), arcname="manifest.json")
            tf.add(dbs_dir, arcname="dbs")
            if globals_path and os.path.exists(globals_path):
                tf.add(globals_path, arcname="globals.sql")
        return manifest
    except OSError as e:
        # 打 tar.gz 阶段同样会被写满（产物目录分区不足），单独识别并说清楚
        if is_enospc(e):
            raise RuntimeError("打包产物失败：%s"
                               % _space_hint(e, os.path.dirname(out_path) or ".", work))
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_full_instance(db_type: str, *, host, port, user, password,
                          backup_path: str, restore_tool: str,
                          query_tool: str = "") -> dict:
    """本机全实例恢复：解包 → globals（PG 系）→ 缺失库自动建库 → 逐库恢复。

    返回 {"restored": [库名...], "globals": bool}；任一库恢复失败即抛异常。
    """
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    is_pg = db_type in PG_FAMILY
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法执行全实例恢复")

    # 解包也要占用与归档相当的磁盘，同样放到归档所在分区（避免 /tmp 写满）
    work = tempfile.mkdtemp(prefix="bp_restore_",
                            dir=_work_root(os.path.dirname(backup_path) or ""))
    try:
        with tarfile.open(backup_path, "r:gz") as tf:
            tf.extractall(work)
        mpath = os.path.join(work, "manifest.json")
        if not os.path.exists(mpath):
            raise RuntimeError("tar 包内缺少 manifest.json，不是全实例备份产物")
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        dbs = manifest.get("databases") or []

        # 恢复端枚举不做系统库过滤（include_system_dbs 备份可能含系统库）
        maint, existing, _enum_err = enumerate_databases(
            db_type, query_tool, host, port, user, env, include_system_dbs=True)

        globals_ok = False
        gpath = os.path.join(work, "globals.sql")
        if is_pg:
            if not maint:
                raise RuntimeError(
                    f"无法连接 {host}:{port}（尝试维护库: "
                    f"{', '.join(cfg['maint_candidates'])}），请检查目标实例连接信息")
            if os.path.exists(gpath) and os.path.getsize(gpath) > 0:
                rc, _o, _e = _run([
                    query_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "-d", maint, "-f", gpath,
                ], env, timeout=1800)
                globals_ok = rc == 0  # 失败多为对象已存在，不阻塞逐库恢复
            # pg_restore/sys_restore 低版本可能无 --if-exists，探测后再用
            rc, out, _e = _run([restore_tool, "--help"], env)
            ifex = ["--if-exists"] if "--if-exists" in (out or "") else []

        restored = []
        for d in dbs:
            if is_pg:
                if d not in existing:
                    _run([
                        query_tool, "-h", str(host), "-p", str(port), "-U", user,
                        "-d", maint, "-c", f'CREATE DATABASE "{d}"',
                    ], env)  # 已存在/权限不足等忽略，交由 restore 报真实错误
                rc, _o, err = _run([
                    restore_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "--dbname", d, "--clean", *ifex,
                    os.path.join(work, "dbs", f"{d}.dump"),
                ], env, timeout=7200)
            else:
                # MySQL 系：dump 内含 CREATE DATABASE/USE，直接灌入即可
                src = os.path.join(work, "dbs", f"{d}.sql")
                if not os.path.exists(src):
                    src = os.path.join(work, "dbs", f"{d}.dump")
                rc, _o, err = _run([
                    query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                    "-u", user,
                ], env, timeout=7200, stdin_file=src)
            if rc != 0:
                raise RuntimeError(f"库 {d} 恢复失败(rc={rc}): {err[:300]}")
            restored.append(d)
        return {"restored": restored, "globals": globals_ok,
                "declared": dbs}
    finally:
        shutil.rmtree(work, ignore_errors=True)
