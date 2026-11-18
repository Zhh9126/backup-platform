# -*- coding: utf-8 -*-
"""无备份目录时的底层磁盘 / 文件系统扫描恢复（零安装、纯 SSH 只读）。

对标 zBackup「scan_fs 裸盘恢复 —— 无备份目录也能从底层磁盘恢复数据」，
但实现严格遵循 AIDBM 的硬性约束，差异如实说明：

* zBackup 的 scan_fs 是**外部二进制**（`scan_fs_v3/v4.*_linux`，从
  `/opt/zbackup/bin/`、`/opt/scan_fs/`、`/logmnr/` 查找后 nohup 启动），
  用户提供的 4 个可执行文件里并不包含它，无法移植其内部实现；
* 本模块只通过 **SSH + paramiko 执行远端系统自带的只读命令**
  （`lsblk`/`blkid`/`find`/`stat`/`sha256sum`），不在被管服务器安装任何工具，
  不读裸块设备、不做块级数据雕刻；
* 产出「可恢复对象清单」（卷 + 文件 + Oracle 数据文件/归档识别），
  再按清单经 SFTP 拉回平台暂存目录，交给既有恢复通道处理。

因此本能力覆盖「备份目录丢失但仍能从文件系统/数据文件层找回数据」，
不承诺「已覆盖磁盘块的扇区级雕刻恢复」。
"""
from __future__ import annotations

import json
import os
import re

from core import remote_dump

ORACLE_DATA_RE = re.compile(
    r"(?i)(^|/)(spfile[^/]*|control[^/]*|\w+\.dbf|\w+\.arc|"
    r"archivelog/|flash_recovery_area/|fast_recovery_area/)")
ORACLE_SUFFIX_RE = re.compile(r"(?i)\.(dbf|arc|log|ctl|ora)$")

# 扫描根目录默认值：覆盖主流数据库的常见数据目录（按库型分组，缺目录自动跳过）
DEFAULT_ROOTS = (
    # Oracle
    "/u01", "/opt/oracle", "/oradata",
    # MySQL / MariaDB
    "/var/lib/mysql", "/data/mysql", "/usr/local/mysql/data",
    # PostgreSQL / 金仓 / openGauss
    "/data/postgresql", "/var/lib/postgresql", "/pgdb/data",
    "/opt/Kingbase", "/kes/data", "/data/gaussdb5",
    # 达梦
    "/dm/data", "/dm8/data",
    # SQL Server
    "/var/opt/mssql",
    # Redis / MongoDB
    "/var/lib/redis", "/var/lib/mongo",
)

# 库型探测顺序：(db_type, 路径/文件名正则)。注意 Oracle 与达梦都用 .dbf，
# 因此带 .dbf 的情况先看路径上下文（见 _db_type 内的专门分支）。
_DB_PATH_RULES = (
    ("dameng", r"(?i)/dameng|/dm/data|/dm8/|/dm/"),
    ("oracle", r"(?i)/oradata|/u01/app/oracle|/opt/oracle|/orcl|"
               r"/flash_recovery_area|/fast_recovery_area"),
    ("mysql", r"(?i)/var/lib/mysql|/data/mysql|/usr/local/mysql/data|/mysql/"),
    ("postgresql", r"(?i)/data/postgresql|/var/lib/postgres|/pgdb/data|/pgsql/data"),
    ("kingbase", r"(?i)/kingbase|/kes/"),
    ("opengauss", r"(?i)/gaussdb|/opengauss"),
    ("mssql", r"(?i)/var/opt/mssql|\.mdf$|\.ndf$|\.ldf$"),
    ("redis", r"(?i)/var/lib/redis|/redis/data|/redis/"),
    ("mongodb", r"(?i)/var/lib/mongo|/data/db/|/mongodb/"),
)

DB_TYPE_LABELS = {
    "oracle": "Oracle", "mysql": "MySQL / MariaDB", "mariadb": "MariaDB",
    "postgresql": "PostgreSQL", "kingbase": "金仓 Kingbase",
    "opengauss": "openGauss / GaussDB", "dameng": "达梦 DM",
    "mssql": "SQL Server", "redis": "Redis", "mongodb": "MongoDB",
    "sqlite": "SQLite", "unknown": "通用文件",
}

# 实例清单的候选数据目录（进程探测不到时按目录兜底）
_CANDIDATE_DIRS = (
    ("mysql", ("/var/lib/mysql", "/data/mysql", "/usr/local/mysql/data")),
    ("postgresql", ("/data/postgresql", "/var/lib/postgresql", "/pgdb/data")),
    ("kingbase", ("/opt/Kingbase/ES/V9/data", "/kes/data")),
    ("dameng", ("/dm/data/DAMENG", "/dm8/data/DAMENG")),
    ("opengauss", ("/data/gaussdb5/data/db1",)),
    ("mssql", ("/var/opt/mssql/data",)),
    ("redis", ("/var/lib/redis", "/data/redis")),
    ("mongodb", ("/var/lib/mongo", "/data/mongo")),
)

_PS_CMD = (
    "ps -eo args= 2>/dev/null | grep -Ei "
    "'mysqld|mariadbd|postgres|kingbase|gaussdb|dmserver|redis-server|mongod|sqlservr' "
    "| grep -v grep || true")


class ScanFSError(ValueError):
    """扫描失败（SSH 不可达 / 参数非法），消息可直接透出。"""


def _client(task: dict):
    ssh = remote_dump.resolve_ssh_host(dict(task or {}))
    if ssh is None:
        raise ScanFSError("未找到可用的 SSH 主机；请纳管主机或在任务高级选项配置 SSH 凭据")
    try:
        return remote_dump._connect_isolated(ssh)
    except Exception as e:
        raise ScanFSError(f"SSH 连接失败: {e}")


def _exec(cli, cmd: str, timeout: int = 60) -> str:
    try:
        _, out, err = cli.exec_command(cmd, timeout=timeout)
        text = out.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
    except Exception as e:
        raise ScanFSError(f"远端命令执行失败: {e}")
    if rc not in (0, 1):  # find/grep 对不存在路径返回 1 属正常
        detail = err.read().decode("utf-8", "replace").strip()
        raise ScanFSError(f"远端命令 rc={rc}: {detail[:300]}")
    return text


def _volumes(cli) -> list:
    text = _exec(cli, "lsblk -J -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null || true")
    try:
        data = json.loads(text)
        out = []
        for dev in data.get("blockdevices", []):
            out.append({
                "name": dev.get("name"), "type": dev.get("type"),
                "size": dev.get("size"), "fstype": dev.get("fstype"),
                "mountpoint": dev.get("mountpoint"),
            })
            for child in dev.get("children") or []:
                out.append({
                    "name": child.get("name"), "type": child.get("type"),
                    "size": child.get("size"), "fstype": child.get("fstype"),
                    "mountpoint": child.get("mountpoint"),
                })
        return out
    except Exception:
        # 老系统无 lsblk -J：回退 blkid 纯文本
        return [{"name": line.split(":", 1)[0], "raw": line[:200]}
                for line in _exec(cli, "blkid 2>/dev/null || true").splitlines()
                if ":" in line]


def _db_type(path: str) -> str:
    """按路径 + 文件名特征推断该文件属于哪个数据库（未知返回 unknown）。

    目标不是精确到实例，而是让「特殊恢复」清单能按库型归类、逐库拉回：
    文件名强特征优先（.ibd/.mdf/dump.rdb/PG_VERSION），其次路径上下文。
    """
    low = path.lower()
    name = low.rsplit("/", 1)[-1]

    # ---- 文件名强特征 ----
    if name in ("pg_version", "postmaster.pid", "postgresql.conf", "pg_hba.conf"):
        if "kingbase" in low or "/kes/" in low or name.startswith("sys_"):
            return "kingbase"
        if "gauss" in low:
            return "opengauss"
        return "postgresql"
    if name.startswith(("sys_",)) and name in (
            "sys_hba.conf", "sys_ident.conf", "kingbase.conf"):
        return "kingbase"
    if name.startswith(("ibdata", "ib_logfile", "undo_00", "undo_01")):
        return "mysql"
    if name.endswith((".ibd", ".frm", ".myd", ".myi", ".ib_logfile")):
        return "mysql"
    if name.startswith(("mysql-bin.", "relay-bin.", "binlog.", "mariadb-bin.")):
        return "mysql"
    if name.endswith((".mdf", ".ndf", ".ldf")):
        return "mssql"
    if name in ("dump.rdb", "appendonly.aof") or name.endswith(".aof"):
        return "redis"
    if name.endswith(".wt") or name in ("wiredtiger", "wiredtiger.turtle",
                                       "storage.bson", "mongod.lock"):
        return "mongodb"
    if name.startswith("spfile") or (name.startswith("control") and name.endswith(".ctl")):
        return "oracle"
    if name.endswith(".sqlite") or name.endswith(".sqlite3"):
        return "sqlite"
    if name.endswith(".dbf"):
        # Oracle 与达梦都用 .dbf：先看路径上下文，再回落 Oracle（最常见）
        if re.search(r"(?i)/dameng|/dm/data|/dm8/|/dm/", low):
            return "dameng"
        return "oracle"
    if name in ("dm.ctl", "dm.ini", "dmwatcher.ini", "dmmal.ini"):
        return "dameng"

    # ---- 路径上下文 ----
    for db_type, pat in _DB_PATH_RULES:
        if re.search(pat, low):
            return db_type
    if name.endswith(".db"):
        return "sqlite"
    return "unknown"


def _kind(path: str, db_type: str = "") -> str:
    """对象类型：db_data / db_log / db_control / db_config / backup_artifact / file。"""
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    # 日志类（redo / 归档 / binlog / WAL / AOF）
    if re.search(r"(?i)(binlog|relay-bin|mysql-bin\.|\.wal$|redo|archivelog|"
                 r"/pg_wal/|/sys_wal/|\.arc$|/appendonlydir/|\.aof$|"
                 r"/arch/|\.trn$|/\d{6}\.log$)", low):
        return "db_log"
    if name.endswith(".ctl") or "/control" in low:
        return "db_control"
    if name.endswith((".cnf", ".conf", ".ini", ".ora", ".hba")) or name == "my.cnf":
        return "db_config"
    if name.endswith((".dmp", ".bak", ".sql", ".tar", ".gz", ".zst", ".bz2", ".xz")):
        return "backup_artifact"
    if (name.endswith((".dbf", ".ibd", ".mdf", ".ndf", ".ldf", ".frm", ".myd",
                       ".myi", ".wt", ".rdb", ".sqlite", ".sqlite3", ".dmp"))
            or name in ("pg_version", "ibdata1", "storage.bson")
            or re.search(r"(?i)/(base|global|pg_xact|sys_xact)/", low)):
        return "db_data"
    if db_type and db_type != "unknown":
        return "db_data"
    return "file"


def _cnf_datadir(cli, cnf: str) -> str:
    """从 my.cnf / mariadb.cnf 里读 datadir（只读 grep，路径需白名单校验）。"""
    if not re.match(r"^/[\w./+-]+$", cnf or ""):
        return ""
    try:
        out = _exec(cli,
                    "grep -E '^[[:space:]]*datadir[[:space:]]*=' %s 2>/dev/null | head -1"
                    % cnf, timeout=15)
    except ScanFSError:
        return ""
    m = re.search(r"=\s*(\S+)", out)
    return m.group(1).strip("\"'") if m else ""


def _conf_value(cli, path: str, key: str) -> str:
    """从配置文件读单值：兼容 ini/cnf（key=）、yaml（key:）、redis.conf（key ）三种写法。"""
    if not re.match(r"^/[\w./+-]+$", path or ""):
        return ""
    try:
        out = _exec(cli, "cat %s 2>/dev/null | head -200" % path, timeout=20)
    except ScanFSError:
        return ""
    m = re.search(r"(?m)^\s*%s\s*[=:\s]\s*(\S+)" % re.escape(key), out)
    return m.group(1).strip("\"'") if m else ""


def _instances(cli) -> list:
    """发现本机数据库实例（进程参数 + /etc/oratab + 候选目录，全部只读）。"""
    found = {}

    def add(db_type: str, name: str, datadir: str, port: str = "", source: str = ""):
        # port 参与去重：同机多实例（如 redis 6379/6389）不能因为进程名相同被合并
        key = (db_type, datadir or name, port)
        if key in found:
            return
        found[key] = {"db_type": db_type,
                      "db_label": DB_TYPE_LABELS.get(db_type, db_type),
                      "name": name, "datadir": datadir, "port": port,
                      "source": source}

    # 1) 运行中进程（最准确：能拿到真实 --datadir/-D/--dbpath）
    try:
        text = _exec(cli, _PS_CMD, timeout=25)
    except ScanFSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 跳过 PG / 金仓的子进程（"postgres: checkpointer"、"kingbase: logger"），
        # 它们只是同一实例的后台 worker，重复计入会让清单里出现一堆空目录条目
        if re.match(r"^\S+:", line):
            continue
        exe = os.path.basename(line.split()[0]).lower()
        datadir, port, db_type, name = "", "", "", ""

        def _arg(*flags):
            for f in flags:
                m = re.search(r"(?:^|\s)" + f + r"[= ]+(\S+)", line)
                if m:
                    return m.group(1).strip("'\"")
            return ""

        if "mysqld" in exe or "mariadbd" in exe:
            db_type = "mysql"
            datadir = _arg("--datadir")
            port = _arg("--port") or "3306"
            name = "mysqld" if "mysqld" in exe else "mariadbd"
            # mysqld 常用 --defaults-file 启动（datadir 写在 cnf 里）：
            # 不补这一步，清单里 MySQL 实例的数据目录会是空的，无法据此扫描
            if not datadir:
                datadir = _cnf_datadir(cli, _arg("--defaults-file") or "/etc/my.cnf")
        elif exe.startswith("postgres"):
            db_type = "postgresql"
            datadir = _arg("-D", "--pgdata")
            port = _arg("-p", "--port") or "5432"
            name = "postgres"
        elif exe.startswith("kingbase"):
            db_type = "kingbase"
            datadir = _arg("-D")
            port = _arg("-p") or "54321"
            name = "kingbase"
        elif "gaussdb" in exe:
            db_type = "opengauss"
            datadir = _arg("-D")
            port = _arg("-p") or "26000"
            name = "gaussdb"
        elif "dmserver" in exe:
            db_type = "dameng"
            # 两种启动形态：`dmserver /data/dm/DAMENG/dm.ini mount` 与
            # `dmserver path=/data/dm2/DAMENG/dm.ini -noconsole`（后者要去掉 path= 前缀）
            m = re.search(r"(\S*/dm\.ini)", line)
            if m:
                ini = m.group(1).split("=")[-1]
                datadir = os.path.dirname(ini)
            port = "5236"
            name = "dmserver"
        elif exe.startswith("redis-server"):
            db_type = "redis"
            m = re.search(r"\*?:(\d{2,5})\s", line + " ")
            port = m.group(1) if m else "6379"
            datadir = _arg("--dir")
            name = "redis-server"
            if not datadir:
                datadir = _conf_value(cli, _arg("-c") or "/etc/redis.conf", "dir")
        elif exe.startswith("mongod"):
            db_type = "mongodb"
            datadir = _arg("--dbpath")
            port = _arg("--port") or "27017"
            name = "mongod"
            if not datadir:
                datadir = _conf_value(cli, _arg("--config") or "/etc/mongod.conf", "dbPath")
        elif "sqlservr" in exe:
            db_type = "mssql"
            datadir = "/var/opt/mssql/data"
            port = "1433"
            name = "sqlservr"
        if db_type:
            add(db_type, name, datadir, port, "process")

    # 2) Oracle /etc/oratab（SID + ORACLE_HOME）
    oracle_homes = []
    try:
        oratab = _exec(cli, "cat /etc/oratab 2>/dev/null || true", timeout=15)
    except ScanFSError:
        oratab = ""
    for line in oratab.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split(":")
        if len(cols) >= 2 and cols[1].startswith("/"):
            oracle_homes.append({"sid": cols[0], "home": cols[1]})
            add("oracle", cols[0], cols[1], "1521", "oratab")

    # 3) 候选目录兜底（进程已退出但数据目录还在）
    probes = []
    for db_type, dirs in _CANDIDATE_DIRS:
        for d in dirs:
            probes.append((db_type, d))
    if probes:
        cmd = ("for p in %s; do [ -d \"$p\" ] && echo \"$p\"; done"
               % " ".join("'%s'" % p.replace("'", "'\\''") for _, p in probes))
        try:
            present = set(x.strip() for x in _exec(cli, cmd, timeout=25).splitlines() if x.strip())
        except ScanFSError:
            present = set()
        for db_type, d in probes:
            if d not in present:
                continue
            same = [x for x in found.values() if x["db_type"] == db_type]
            if not same:
                # 该库型没有任何运行实例：目录即线索（进程已退出的场景）
                add(db_type, "default", d, "", "dir")
            elif any(not x["datadir"] for x in same):
                # 有实例但拿不到数据目录（如 redis 自定义配置/多实例）：用目录补齐，
                # 而不是新增重复条目；同库型的缺目录实例一起补
                for x in same:
                    if not x["datadir"]:
                        x["datadir"] = d
            # 已有带目录的运行实例：跳过，避免同一实例出现两条

    return sorted(found.values(), key=lambda x: (x["db_type"], x["name"])), oracle_homes


def scan_host(task: dict, roots: list = None, max_files: int = 5000,
              timeout: int = 60) -> dict:
    """只读扫描远端文件系统，返回可恢复对象清单。"""
    roots = [str(r).strip() for r in (roots or DEFAULT_ROOTS) if str(r).strip()]
    if not roots:
        raise ScanFSError("roots 不能为空")
    for r in roots:
        if (not r.startswith("/") or ".." in r
                or re.search(r"[;&|`$<>\n\r\"'\\]", r)):
            raise ScanFSError(
                f"扫描根目录必须是安全的绝对路径（禁止 .. 与 shell 元字符）: {r}")
    roots = roots[:20]
    max_files = max(1, min(int(max_files), 50000))
    cli = _client(task)
    try:
        volumes = _volumes(cli)
        # 单次 find 列出全部文件（零临时文件、零 agent）。
        # 每个根目录单独限额：否则排在前面的大目录（如金仓数据目录上万文件）
        # 会把总配额吃满，后面的库型一个文件都看不到（2026-11-14 实测暴露）。
        per_root = max(200, max_files // max(len(roots), 1))
        quoted = " ".join("'" + r.replace("'", "'\\''") + "'" for r in roots)
        cmd = (f"for d in {quoted}; do [ -d \"$d\" ] || continue; "
               f"find \"$d\" -xdev -type f -printf '%p\\t%s\\t%T@\\n' 2>/dev/null "
               f"| head -n {per_root}; "
               f"done | head -n {max_files}")
        files = []
        for line in _exec(cli, cmd, timeout=timeout).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                size, mtime = int(parts[1]), float(parts[2])
            except ValueError:
                continue
            p = parts[0]
            dbt = _db_type(p)
            files.append({"path": p, "size": size, "mtime": mtime,
                          "db_type": dbt,
                          "db_label": DB_TYPE_LABELS.get(dbt, dbt),
                          "kind": _kind(p, dbt)})
        # 实例清单（进程 / oratab / 候选目录，覆盖全部已支持库型）
        try:
            instances, oracle_homes = _instances(cli)
        except Exception:  # noqa: BLE001 - 实例探测失败不影响文件清单
            instances, oracle_homes = [], []
        by_type = {}
        for f in files:
            if f["db_type"] != "unknown":
                by_type[f["db_type"]] = by_type.get(f["db_type"], 0) + 1
        candidates = [f for f in files if f["db_type"] != "unknown"][:500]
        return {
            "ok": True,
            "host": str((task or {}).get("host") or ""),
            "roots": roots,
            "volumes": volumes,
            "files": files,
            "file_count": len(files),
            "instances": instances,
            "db_types": by_type,
            "candidates": candidates,
            "oracle_homes": oracle_homes,
            "oracle_candidates": [f for f in files if f["db_type"] == "oracle"][:200],
            "truncated": len(files) >= max_files,
            "method": "SSH 只读 find/lsblk/blkid/ps；零安装、不读裸块、不做扇区雕刻",
        }
    finally:
        try:
            cli.close()
        except Exception:
            pass


def scan_instances(task: dict) -> dict:
    """只做实例发现（不扫文件），供页面「实例清单」按钮快速查询。

    覆盖 Oracle / MySQL / PostgreSQL / 金仓 / openGauss / 达梦 / SQL Server /
    Redis / MongoDB：先读运行中进程的真实数据目录参数，再以常见目录兜底。
    """
    cli = _client(task)
    try:
        instances, oracle_homes = _instances(cli)
        return {
            "ok": True,
            "host": str((task or {}).get("host") or ""),
            "instances": instances,
            "instance_count": len(instances),
            "db_types": sorted({i["db_type"] for i in instances}),
            "oracle_homes": oracle_homes,
            "method": "SSH 只读 ps / /etc/oratab / 目录探测；零安装、零写入",
        }
    finally:
        try:
            cli.close()
        except Exception:
            pass


def pull_objects(task: dict, paths: list, dest_dir: str = "",
                 max_bytes: int = 2 * 1024 ** 3) -> dict:
    """按扫描清单经 SFTP 拉回平台暂存目录，并逐文件计算 sha256。"""
    import hashlib
    import time

    paths = [str(p) for p in (paths or []) if str(p).startswith("/")]
    if not paths:
        raise ScanFSError("paths 不能为空且必须是绝对路径")
    root = dest_dir or os.path.join(
        str(os.environ.get("BACKUP_ROOT") or "/tmp"), "scan_fs_restore")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(root, stamp)
    os.makedirs(dest, exist_ok=True)
    cli = _client(task)
    pulled, failures, total = [], [], 0
    try:
        sftp = cli.open_sftp()
        for src in paths[:500]:
            base = os.path.basename(src.rstrip("/")) or "object"
            local = os.path.join(dest, f"{len(pulled):04d}__{base}")
            try:
                st = sftp.stat(src)
                if total + (st.st_size or 0) > max_bytes:
                    failures.append({"path": src, "error": "超过本次拉取字节上限"})
                    continue
                sftp.get(src, local)
                h = hashlib.sha256()
                with open(local, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                size = os.path.getsize(local)
                total += size
                pulled.append({"source": src, "local": local,
                               "size": size, "sha256": h.hexdigest()})
            except Exception as e:
                failures.append({"path": src, "error": str(e)[:200]})
        sftp.close()
    finally:
        try:
            cli.close()
        except Exception:
            pass
    return {"ok": True, "dest": dest, "pulled": pulled,
            "pulled_count": len(pulled), "bytes": total,
            "failures": failures}
