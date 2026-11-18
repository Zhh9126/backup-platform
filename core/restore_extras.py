# -*- coding: utf-8 -*-
"""
高级恢复能力：PITR / 对象级 / 副本克隆。

PITR（Point-in-Time Recovery）：
- MySQL：用 mysqlbinlog 解析 binlog，replay 到目标 timestamp
- PG：用 WAL + recovery_target_time（需配置 archive_command）

对象级精准恢复：
- MySQL：解析 mysqldump 输出，提取指定表的 CREATE + INSERT
- PG：解析 pg_dump，提取指定表/schema 段

副本克隆（VDB-style）：
- 从备份快速拉起一个隔离的测试库实例
- 记录在 vdb_instances 表，附过期时间，自动清理
"""
import glob
import os
import re
import shutil
import time
import datetime
import subprocess
import tempfile
import logging
from typing import Optional, Dict, Any

import core.db as db

logger = logging.getLogger("restore_extras")

# ---------------------------------------------------------------------------
# PG 客户端解析（2026-11-13）
# ---------------------------------------------------------------------------
# 平台机的 /usr/bin/psql 可能是 9.2（CentOS7 自带），连 PG10+ 的服务端会直接报
# "SCRAM authentication requires libpq version 10 or above"。克隆/PITR 等恢复
# 通道若用裸命令名（走 PATH）就会命中这个老版本 —— 本机明明装了 /pgdb/pgsql/psql 14
# 却用不上。这里统一按版本择优解析绝对路径，并要求 >= 10。
_PG_BIN_DIRS = (
    "/pgdb/pgsql/bin", "/usr/pgsql-*/bin", "/usr/local/pgsql/bin",
    "/opt/pg*/bin", "/opt/database/bin", "/opt/postgresql/bin",
)
_PG_TOOL_CACHE: Dict[str, str] = {}


def _pg_version(path: str) -> int:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return 0
    m = re.search(r"(\d+)\.", out or "")
    return int(m.group(1)) if m else 0


def _pg_tool(name: str) -> str:
    """解析 PG 客户端（psql/pg_restore/pg_dump/pg_ctl）绝对路径，版本择优 ≥10。

    返回绝对路径；找不到合格版本时回落到 PATH 里的同名命令（保持旧行为）。
    """
    if name in _PG_TOOL_CACHE:
        return _PG_TOOL_CACHE[name]
    cands = []
    w = shutil.which(name)
    if w:
        cands.append(w)
    for pat in _PG_BIN_DIRS:
        for d in glob.glob(pat):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                cands.append(p)
    best, best_v = "", -1
    for c in cands:
        v = _pg_version(c)
        if v > best_v:
            best, best_v = c, v
    if best and best_v >= 10:
        _PG_TOOL_CACHE[name] = best
        return best
    logger.warning("[restore] 未找到 >=10 的 %s（候选 %s），回落到 PATH", name, cands[:4])
    return w or name


# ============================================================
# 1. CDC 位置捕获（MySQL binlog / PG WAL）
# ============================================================
def capture_mysql_cdc(task: dict, password: str) -> Dict[str, Any]:
    """调用 mysql -e 'SHOW MASTER STATUS' 拿 binlog 位点。"""
    try:
        cmd = ["mysql", "--no-defaults", "-h", str(task.get("host") or "127.0.0.1"),
               "-P", str(task.get("port") or 3306),
               "-u", str(task.get("username") or "root"),
               f"--password={password}"]
        env = os.environ.copy()
        # 用 .my.cnf 风格避免密码出现在命令行
        if password:
            env["MYSQL_PWD"] = password
        cmd = cmd[:6]  # 不带 --password
        cmd += ["-N", "-e", "SHOW MASTER STATUS"]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            parts = out.stdout.strip().split("\t")
            return {"file": parts[0], "pos": int(parts[1]) if len(parts) > 1 else 0}
    except Exception as e:
        logger.warning("[cdc] mysql capture failed: %s", e)
    return {}


def capture_pg_cdc(task: dict, password: str) -> Dict[str, Any]:
    """调用 psql 拿当前 WAL LSN。"""
    try:
        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password
        cmd = [_pg_tool("psql"), "-h", str(task.get("host") or "127.0.0.1"),
               "-p", str(task.get("port") or 5432),
               "-U", str(task.get("username") or "postgres"),
               "-d", str(task.get("db_name") or "postgres"),
               "-tAc", "SELECT pg_current_wal_lsn();"]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return {"lsn": out.stdout.strip()}
    except Exception as e:
        logger.warning("[cdc] pg capture failed: %s", e)
    return {}


# ============================================================
# 2. PITR：MySQL binlog replay / PG recovery_target_time
# ============================================================
def _norm_stop_datetime(ts: str) -> str:
    """把 ISO8601 时间归一化为 mysqlbinlog --stop-datetime 认可的格式。

    ``2026-09-02T08:51:00+08:00`` → ``2026-09-02 08:51:00``（本地时区）。
    解析失败时退化为简单的 T/时区剥离，保证不向 mysqlbinlog 传非法值。
    """
    s = (ts or "").strip()
    if not s:
        return s
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s.replace("T", " ").split("+")[0].split(".")[0].strip()


def _dump_source_db(path: str) -> str:
    """从 mysqldump 产物里解析源库名（``USE `db`;`` 或 ``-- Host: ... Database: db``）。

    只读文件头，避免大 dump 全量扫描；解析失败返回空串。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(262144)
    except Exception:
        return ""
    m = re.search(r"(?m)^USE `([^`]+)`;", head)
    if m:
        return m.group(1)
    m = re.search(r"(?m)^-- Host: .*Database: (\S+)", head)
    return m.group(1) if m else ""


def _rewrite_dump_db(src_path: str, src_db: str, dst_db: str) -> str:
    """把 dump 里的 ``CREATE DATABASE 源库`` / ``USE 源库`` 改写成目标库。

    仅用于「只做全量恢复」的场景（无 binlog 段）；带 ROW binlog 回放时异名恢复
    本身不可行，调用方会先拒绝。返回改写后的临时文件路径。
    """
    import tempfile

    fd, out_path = tempfile.mkstemp(prefix="aidbm_pitr_rewrite_", suffix=".sql")
    use_re = re.compile(r"^USE `" + re.escape(src_db) + r"`;")
    create_re = re.compile(r"^CREATE DATABASE (.*)`" + re.escape(src_db) + r"`")
    with os.fdopen(fd, "w", encoding="utf-8") as wf, \
            open(src_path, "r", encoding="utf-8", errors="replace") as rf:
        for line in rf:
            if use_re.match(line):
                wf.write("USE `{}`;\n".format(dst_db))
                continue
            if create_re.match(line):
                wf.write(create_re.sub(
                    lambda m: "CREATE DATABASE " + m.group(1) + "`" + dst_db + "`", line))
                continue
            wf.write(line)
    logger.info("[pitr] dump 库名改写: %s → %s (%s)", src_db, dst_db, out_path)
    return out_path


def _probe_target_gtid(target: dict) -> Dict[str, Any]:
    """探测目标实例 GTID 状态：{gtid_mode, gtid_executed, ok}。

    GTID 模式 PITR 分流依据：
    - gtid_mode=ON 且 gtid_executed 为空（刚恢复完全量的干净目标）→
      binlog 回放**保留 GTID**（恢复出的数据带源库事务标识，
      恢复后可直接作为 GTID 复制源 / 挂下游从库）；
    - 其余情形（目标 OFF / gtid_executed 非空 / 探测失败）→
      维持 --skip-gtids 剥离语义（跨实例兼容优先），如实标注。
    """
    out = {"gtid_mode": "UNKNOWN", "gtid_executed": None, "ok": False}
    try:
        import pymysql
        conn = pymysql.connect(
            host=str(target.get("host") or "127.0.0.1"),
            port=int(target.get("port") or 3306),
            user=str(target.get("user") or "root"),
            password=str(target.get("password") or ""),
            connect_timeout=8)
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@global.gtid_mode, @@global.gtid_executed")
            mode, executed = cur.fetchone()
            out = {"gtid_mode": str(mode or "OFF"),
                   "gtid_executed": str(executed or ""), "ok": True}
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass   # 探测失败不阻断（回落剥离语义，消息中如实标注）
    return out


def mysql_pitr_restore(backup_path: str, target_time: str, target: dict) -> Dict[str, Any]:
    """MySQL PITR：先做全量恢复，再调用 mysqlbinlog replay 到 target_time。
    target: {host, port, user, password, db}
    target["segments"]（可选，推荐）：恢复链 db-log 段有序列表，
        每项 {"path": 段文件路径, "start_pos": 起始位点, "stop_pos": 结束位点|None}。
        传入时按段逐文件回放（修复旧版只能回放单个 binlog 文件的跨段缺口），
        最后一段优先用 --stop-position 精确截断（--stop-datetime 仍作为兜底）。
    """
    # 1) 解压 .gz / .zst（平台产物压缩链：gzip 或 zstd）
    actual = backup_path
    if backup_path.endswith(".gz"):
        out = subprocess.run(["gunzip", "-k", backup_path], capture_output=True, text=True)
        actual = backup_path[:-3]
    elif backup_path.endswith(".zst"):
        import zstandard  # 平台 requirements 自带（产物校验读 zstd 头依赖）
        out_path = backup_path[:-4]
        if not os.path.exists(out_path):
            with open(backup_path, "rb") as fh, open(out_path, "wb") as wf:
                zstandard.ZstdDecompressor().copy_stream(fh, wf)
        actual = out_path
    # 安全整改：目标库名必须是合法 MySQL 标识符，防参数注入
    db_name = str(target.get("db") or "")
    if db_name and not re.match(r"^[A-Za-z0-9_$]{1,64}$", db_name):
        return {"ok": False, "message": "目标库名不合法（仅允许字母/数字/下划线/$，最长 64）"}
    # 2) 全量导入
    env = os.environ.copy()
    if target.get("password"):
        env["MYSQL_PWD"] = target["password"]
    db_arg = f" {db_name}" if db_name else ""
    # 1.5) 目标库自动创建：单库逻辑备份产物里没有 CREATE DATABASE，
    #      恢复到异名新库时 mysql 直接报 ERROR 1049 Unknown database。
    #      （2026-11-13 MySQL170 PITR 真机暴露）
    if db_name:
        cmd_create = ["mysql", "--no-defaults",
                      "-h", str(target.get("host") or "127.0.0.1"),
                      "-P", str(target.get("port") or 3306),
                      "-u", str(target.get("user") or "root"),
                      "-e", "CREATE DATABASE IF NOT EXISTS `{}` "
                            "DEFAULT CHARACTER SET utf8mb4".format(db_name)]
        r_c = subprocess.run(cmd_create, env=env, capture_output=True, text=True)
        if r_c.returncode != 0:
            return {"ok": False,
                    "message": f"目标库 {db_name} 创建失败: {r_c.stderr[:200]}"}
    cmd_full = ["mysql", "--no-defaults", "-h", str(target.get("host") or "127.0.0.1"),
                "-P", str(target.get("port") or 3306),
                "-u", str(target.get("user") or "root")] + db_arg.split()
    logger.info("[pitr] mysql full restore: %s", " ".join(cmd_full))
    with open(actual, "rb") as f:
        r = subprocess.run(cmd_full, env=env, stdin=f, capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "message": f"全量恢复失败: {r.stderr[:200]}"}

    stop_dt = _norm_stop_datetime(target_time)
    segments = target.get("segments") or []

    # 2) GTID 模式 PITR：目标 gtid_mode=ON 且 gtid_executed 为空（干净目标）
    #    → binlog 回放保留 GTID（恢复后可作 GTID 复制源）；否则剥离兼容。
    gtid_probe = _probe_target_gtid(target)
    gtid_preserve = bool(gtid_probe.get("ok")
                         and gtid_probe.get("gtid_mode") == "ON"
                         and not (gtid_probe.get("gtid_executed") or ""))
    gtid_skip_args = [] if gtid_preserve else ["--skip-gtids=true"]
    gtid_note = ("GTID 已保留（恢复库可作为 GTID 复制源）" if gtid_preserve
                 else "GTID 已剥离（目标 gtid_mode={}，跨实例兼容）"
                 .format(gtid_probe.get("gtid_mode")))
    logger.info("[pitr] gtid probe: mode=%s preserve=%s",
                gtid_probe.get("gtid_mode"), gtid_preserve)

    # 1.6) 异名库 PITR 安全门（2026-11-13 MySQL170 真机暴露的严重缺陷）
    #      单库逻辑备份产物带 `CREATE DATABASE 源库` + `USE 源库`，而 ROW 格式
    #      binlog 的 mysqlbinlog 输出是「按源库.表」的行事件——两者都会把数据写回
    #      **源库**。目标库名与产物里的源库不同时，看似恢复到新库，实际把源库
    #      覆盖掉了（实测源库 9 行被改写成 7 行）。这里必须硬拒绝，避免误删生产数据。
    src_db_in_dump = _dump_source_db(actual)
    if db_name and src_db_in_dump and db_name != src_db_in_dump:
        if segments:
            return {
                "ok": False,
                "message": (
                    f"MySQL PITR 异名恢复不受支持：产物源库为 `{src_db_in_dump}`，"
                    f"请求目标库 `{db_name}`。ROW 格式 binlog 回放固定写回源库，"
                    f"继续执行会覆盖源库数据。请改用「同名库 + 另一实例（host/port）」"
                    f"作为目标，或先把 binlog_format 改为 STATEMENT 再重做全量。"),
            }
        # 无日志段（只做全量恢复）：把 dump 里的库名改写成目标库后再导入
        actual = _rewrite_dump_db(actual, src_db_in_dump, db_name)
    if segments:
        # ---- 多段逐文件回放（v2：修复跨段链只回放单个文件的缺口） ----
        sql_parts = []
        replayed = []
        for seg in segments:
            seg_path = str(seg.get("path") or "")
            if not seg_path or not os.path.isfile(seg_path):
                return {"ok": False,
                        "message": f"binlog 段文件缺失: {seg_path}"}
            cmd_seg = ["mysqlbinlog"] + gtid_skip_args
            start_pos = int(seg.get("start_pos") or 0)
            if start_pos > 4:
                cmd_seg.append(f"--start-position={start_pos}")
            stop_pos = int(seg.get("stop_pos") or 0)
            is_last = seg is segments[-1]
            if is_last and stop_pos > 4:
                # 精确位点截断（journal 段尾位点）；--stop-datetime 并用兜底
                cmd_seg.append(f"--stop-position={stop_pos}")
            cmd_seg.append(seg_path)
            logger.info("[pitr] mysqlbinlog replay 段: %s", " ".join(cmd_seg))
            r2 = subprocess.run(cmd_seg, capture_output=True, text=True,
                                timeout=1800)
            if r2.returncode != 0:
                return {"ok": False,
                        "message": f"binlog replay 失败（{os.path.basename(seg_path)}）"
                                   f": {r2.stderr[:200]}"}
            sql_parts.append(r2.stdout)
            replayed.append(os.path.basename(seg_path))
        # --binary-mode 必须加：ROW 格式 binlog 的 mysqlbinlog 输出含 BINLOG
        # 的 base64 语句与二进制转义，普通文本模式会被 mysql 客户端**静默跳过**
        # （rc=0 但一行都没写）。2026-11-13 MySQL170 真机实测：不加时
        # 「PITR 成功」却只恢复出全量基线数据，加了之后 B 批 2 行立刻落地。
        cmd_apply = ["mysql", "--no-defaults", "--binary-mode",
                     "-h", str(target.get("host") or "127.0.0.1"),
                     "-P", str(target.get("port") or 3306),
                     "-u", str(target.get("user") or "root")] + db_arg.split()
        r3 = subprocess.run(cmd_apply, env=env, input="".join(sql_parts),
                            capture_output=True, text=True)
        if r3.returncode != 0:
            return {"ok": False, "message": f"apply binlog SQL 失败: {r3.stderr[:200]}"}
        return {"ok": True,
                "message": (f"PITR 成功：全量 + {len(replayed)} 个 binlog 段"
                            f"（{' → '.join(replayed[:3])}"
                            f"{'…' if len(replayed) > 3 else ''}）；{gtid_note}"),
                "binlog_replayed": True, "segments": len(replayed),
                "target_time": target_time,
                "gtid_mode": gtid_probe.get("gtid_mode"),
                "gtid_preserved": gtid_preserve}

    # ---- 旧单文件路径（无 segments 时保留兼容） ----
    # 3) 找到对应 binlog 文件（简化：从备份路径推断或在 binlog 目录查最新）
    binlog_file = os.path.basename(target.get("binlog_file") or "")
    binlog_pos = target.get("binlog_pos", 0)
    if not binlog_file:
        return {"ok": True, "message": "全量恢复成功（无 binlog 位置信息，未执行增量 replay）",
                "skipped_replay": True}
    # 4) 调用 mysqlbinlog replay 到 target_time
    # 安全整改：binlog 文件名仅取 basename，防路径穿越
    binlog_path = os.path.join(target.get("binlog_dir", "/var/lib/mysql"), binlog_file)
    if not os.path.exists(binlog_path):
        return {"ok": True, "message": f"全量恢复成功（找不到 binlog {binlog_path}，未执行增量）",
                "skipped_replay": True}
    # --skip-gtids：源库 GTID_MODE=ON 而目标可能为 OFF，回放 SQL 中的
    # SET @@SESSION.GTID_NEXT 会导致 ERROR 1781；目标 GTID 就绪时保留
    # （恢复库可作 GTID 复制源），其余剥离（跨实例兼容优先）
    cmd_binlog = ["mysqlbinlog"] + gtid_skip_args + [
                  f"--start-position={binlog_pos}",
                  f"--stop-datetime={stop_dt}", binlog_path]
    logger.info("[pitr] mysqlbinlog replay: %s", " ".join(cmd_binlog))
    r2 = subprocess.run(cmd_binlog, capture_output=True, text=True, timeout=1800)
    if r2.returncode != 0:
        return {"ok": False, "message": f"binlog replay 失败: {r2.stderr[:200]}"}
    # 5) 把 replay 出的 SQL 灌入目标
    cmd_apply = ["mysql", "--no-defaults", "--binary-mode",
                 "-h", str(target.get("host") or "127.0.0.1"),
                 "-P", str(target.get("port") or 3306),
                 "-u", str(target.get("user") or "root")] + db_arg.split()
    r3 = subprocess.run(cmd_apply, env=env, input=r2.stdout, capture_output=True, text=True)
    if r3.returncode != 0:
        return {"ok": False, "message": f"apply binlog SQL 失败: {r3.stderr[:200]}"}
    return {"ok": True, "message": f"PITR 成功，已 replay 至 {stop_dt}；{gtid_note}",
            "binlog_replayed": True, "target_time": target_time,
            "gtid_mode": gtid_probe.get("gtid_mode"),
            "gtid_preserved": gtid_preserve}


def pg_pitr_restore(backup_path: str, target_time: str, target: dict) -> Dict[str, Any]:
    """PG PITR：写 recovery.conf + recovery_target_time。
    注意：需要在 PG 数据目录配置，恢复期间要 stop server。
    """
    # 1) 还原全量
    env = os.environ.copy()
    if target.get("password"):
        env["PGPASSWORD"] = target["password"]
    data_dir = target.get("data_dir")
    if not data_dir:
        return {"ok": False, "message": "PG PITR 需要 data_dir 参数"}
    # 简化实现：pg_restore
    cmd = [_pg_tool("pg_restore"), "-h", str(target.get("host") or "127.0.0.1"),
           "-p", str(target.get("port") or 5432),
           "-U", str(target.get("user") or "postgres"),
           "-d", str(target.get("db") or "postgres"),
           "-c", backup_path]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        return {"ok": False, "message": f"PG 全量恢复失败: {r.stderr[:200]}"}
    # 2) 写 recovery.signal + recovery.conf（需要在 data_dir 提前 stop 服务）
    # 这里只生成配置示例，不直接操作远端
    recovery_conf = (
        f"recovery_target_time = '{target_time}'\n"
        f"recovery_target_action = 'promote'\n"
    )
    return {"ok": True, "message": f"PG PITR 基础已恢复。需在目标机将以下写入 {data_dir}/recovery.signal 和 recovery.conf：\n{recovery_conf}",
            "needs_manual_step": True, "recovery_conf": recovery_conf}


# ============================================================
# 3. 对象级精准恢复（MySQL/PG dump 解析）
# ============================================================
def mysql_restore_object(backup_path: str, object_name: str, target: dict) -> Dict[str, Any]:
    """从 mysqldump 中提取指定表的 CREATE + INSERT 并导入。
    object_name: 表名（不含库名）
    """
    if not object_name or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", object_name):
        return {"ok": False, "message": "object_name 非法（仅支持表名）"}
    # 1) 解压
    actual = backup_path
    if backup_path.endswith(".gz"):
        subprocess.run(["gunzip", "-k", backup_path], capture_output=True)
        actual = backup_path[:-3]
    # 2) 扫描文件，提取该表相关行
    logger.info("[object] scanning %s for table %s", actual, object_name)
    create_re = re.compile(rf"^CREATE\s+TABLE\s+`?{re.escape(object_name)}`?\s*[\(]", re.IGNORECASE)
    insert_re = re.compile(rf"^INSERT\s+INTO\s+`?{re.escape(object_name)}`?\s", re.IGNORECASE)
    lock_re = re.compile(rf"^LOCK\s+TABLES\s+`?{re.escape(object_name)}`?\s", re.IGNORECASE)
    unlock_re = re.compile(r"^UNLOCK\s+TABLES", re.IGNORECASE)
    in_target = False
    saved_lines = []
    with open(actual, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # DROP TABLE IF EXISTS for the target
            if re.match(rf"^DROP\s+TABLE.*`?{re.escape(object_name)}`?", line, re.IGNORECASE):
                saved_lines.append(line)
                continue
            if create_re.match(line):
                in_target = True
                saved_lines.append(line)
                continue
            if in_target:
                if insert_re.match(line) or lock_re.match(line) or line.startswith("/*") or line.startswith("--"):
                    saved_lines.append(line)
                    continue
                if line.startswith(")") or line.rstrip().endswith(";"):
                    saved_lines.append(line)
                    in_target = False
                    continue
                saved_lines.append(line)
    if not saved_lines:
        return {"ok": False, "message": f"备份中未找到表 {object_name}"}
    # 3) 灌入目标
    env = os.environ.copy()
    if target.get("password"):
        env["MYSQL_PWD"] = target["password"]
    cmd = ["mysql", "--no-defaults", "-h", str(target.get("host") or "127.0.0.1"),
           "-P", str(target.get("port") or 3306),
           "-u", str(target.get("user") or "root"),
           str(target.get("db") or "")]
    sql = "".join(saved_lines)
    r = subprocess.run(cmd, env=env, input=sql, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return {"ok": False, "message": f"对象级恢复失败: {r.stderr[:300]}"}
    return {"ok": True, "message": f"已恢复表 {object_name}（{len(saved_lines)} 行 SQL）",
            "object": object_name, "sql_lines": len(saved_lines)}


def pg_restore_object(backup_path: str, object_name: str, target: dict) -> Dict[str, Any]:
    """PG 对象级恢复（针对 pg_dump -Fc/-Ft 格式，需 pg_restore 的 -t 参数）。
    object_name: 表名（含 schema，如 public.users）或 schema 名
    """
    env = os.environ.copy()
    if target.get("password"):
        env["PGPASSWORD"] = target["password"]
    cmd = [_pg_tool("pg_restore"), "-h", str(target.get("host") or "127.0.0.1"),
           "-p", str(target.get("port") or 5432),
           "-U", str(target.get("user") or "postgres"),
           "-d", str(target.get("db") or "postgres"),
           "-t", object_name,
           "-c",  # 先清理同名对象
           backup_path]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return {"ok": False, "message": f"PG 对象级恢复失败: {r.stderr[:300]}"}
    return {"ok": True, "message": f"PG 已恢复对象 {object_name}", "object": object_name}


# ============================================================
# 4. 副本克隆（VDB - Virtual Database）
# ============================================================
def mysql_clone_to_test(backup_path: str, instance_name: str, base_port: int = 33060,
                        mysql_host: str = "127.0.0.1", mysql_port: int = 3306,
                        mysql_user: str = "root", mysql_password: str = "") -> Dict[str, Any]:
    """从备份创建一个新的 MySQL 数据库（通过 create database + restore），返回连接信息。

    instance_name: 新库名（要求唯一）
    - 密码经 MYSQL_PWD 环境变量注入，避免出现在进程参数中；
    - 支持明文 .sql / .gz / .zst / .bz2 备份，流式解压导入（不落中间文件）；
    - 导入前剥离 dump 内的 ``CREATE DATABASE`` / ``USE`` 语句（mysqldump 输出
      依赖 USE 选库，不剥离会把数据写回源库而克隆库为空）。
    """
    env = os.environ.copy()
    if mysql_password:
        env["MYSQL_PWD"] = mysql_password
    base = ["mysql", "--no-defaults", "-h", mysql_host, "-P", str(mysql_port), "-u", mysql_user]
    # 1) 创建克隆库
    cmd_create = base + ["-N", "-e",
                         f"CREATE DATABASE IF NOT EXISTS `{instance_name}` CHARACTER SET utf8mb4"]
    r = subprocess.run(cmd_create, env=env, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"ok": False, "message": f"创建库失败: {r.stderr[:200]}"}
    # 2) 流式导入：解压 → 剥离库名语句 → 导入目标克隆库
    lower = (backup_path or "").lower()
    if lower.endswith(".zst"):
        dec = ["zstd", "-dc", backup_path]
    elif lower.endswith(".gz"):
        dec = ["gzip", "-dc", backup_path]
    elif lower.endswith(".bz2"):
        dec = ["bzip2", "-dc", backup_path]
    else:
        dec = ["cat", backup_path]
    # 剥离整行的 CREATE DATABASE ...; 与 USE `db`;（mysqldump 输出均为独立行）
    strip = ["sed", "-E", "/^[[:space:]]*(CREATE[[:space:]]+DATABASE|USE[[:space:]])/Id"]
    try:
        p1 = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(strip, stdin=p1.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p1.stdout.close()
        p3 = subprocess.Popen(base + [instance_name], stdin=p2.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p2.stdout.close()
        _, err3 = p3.communicate(timeout=3600)
        p2.wait()
        p1.wait()
        if p3.returncode != 0:
            raise RuntimeError((err3 or b"").decode("utf-8", "ignore")[:300])
    except Exception as e:
        # 回滚克隆库
        subprocess.run(base + ["-N", "-e", f"DROP DATABASE IF EXISTS `{instance_name}`"],
                       env=env, capture_output=True, text=True, timeout=10)
        return {"ok": False, "message": f"导入失败: {str(e)[:300]}"}
    return {"ok": True, "message": f"测试库 {instance_name} 已创建并导入",
            "connection": f"mysql -h{mysql_host} -u{mysql_user} {instance_name}",
            "instance_name": instance_name,
            "port": mysql_port}


def mysql_full_instance_clone(backup_path: str, instance_prefix: str,
                              mysql_host: str = "127.0.0.1", mysql_port: int = 3306,
                              mysql_user: str = "root",
                              mysql_password: str = "",
                              databases: list = None, progress=None,
                              max_threads: int = 24) -> Dict[str, Any]:
    """**整实例**克隆：把全实例逻辑备份归档（多库 tar）逐库恢复到目标实例。

    与 :func:`mysql_clone_to_test`（单库克隆）的区别：
    - 单库克隆把**一个**产物灌进**一个**库；
    - 整实例克隆按库拆开，每个源库落到 ``<instance_prefix>__<源库名>``，
      保持库边界——此前把多库 tar 当 SQL 灌进单库，会直接报
      ``ASCII '\\0' appeared in the statement``（2026-11-14 实测）。

    返回 ``{ok, databases:[{source,target,tables}], db_count, table_total,
    instance_prefix, elapsed}``；任一库失败即回滚已建库并返回失败原因。
    """
    import json as _json
    import tarfile as _tarfile

    t0 = time.time()
    low = (backup_path or "").lower()
    if not (low.endswith(".tar.gz") or low.endswith(".tgz") or low.endswith(".tar")):
        return {"ok": False,
                "message": "不是全实例归档（需 .tar / .tar.gz）——单库逻辑备份请走单库克隆"}

    env = os.environ.copy()
    if mysql_password:
        env["MYSQL_PWD"] = mysql_password
    base = ["mysql", "--no-defaults", "-h", mysql_host, "-P", str(mysql_port),
            "-u", mysql_user]
    tmp = tempfile.mkdtemp(prefix="aidbm_clone_full_")
    created: list = []
    try:
        # 1) 解包（拒绝绝对路径/穿越成员，避免归档污染任意目录）
        with _tarfile.open(backup_path, "r:*") as tf:
            for m in tf.getmembers():
                name = m.name.lstrip("./")
                if name.startswith("/") or ".." in name.split("/"):
                    return {"ok": False, "message": f"归档成员路径非法: {m.name}"}
            tf.extractall(tmp)

        # 2) 库清单：manifest.json 优先，其次 dbs/*.sql
        dbs: list = []
        man = os.path.join(tmp, "manifest.json")
        if os.path.isfile(man):
            try:
                dbs = list((_json.load(open(man, encoding="utf-8")) or {}).get("databases") or [])
            except Exception:
                dbs = []
        if not dbs:
            ddir = os.path.join(tmp, "dbs")
            if os.path.isdir(ddir):
                dbs = [os.path.splitext(f)[0] for f in sorted(os.listdir(ddir))
                       if f.endswith(".sql")]
        if not dbs:
            return {"ok": False,
                    "message": "归档内未找到库文件（dbs/*.sql 或 manifest.json）——"
                               "该产物可能不是平台全实例逻辑备份"}
        skip = {"mysql", "sys", "information_schema", "performance_schema"}
        strip = ["sed", "-E",
                 "/^[[:space:]]*(CREATE[[:space:]]+DATABASE|USE[[:space:]])/Id"]
        # 库白名单（UI 勾选）：整实例可能有十几个库，全量导入会把目标机打爆
        # （2026-11-14 实测：2 核机器 load 46，SSH 都进不去）
        if databases:
            want = {str(x).strip() for x in databases if str(x).strip()}
            dbs = [d for d in dbs if str(d).strip() in want]

        def _threads_running():
            try:
                rr = subprocess.run(
                    base + ["-N", "-B", "-e",
                            "SHOW GLOBAL STATUS LIKE 'Threads_running'"],
                    env=env, capture_output=True, text=True, timeout=20)
                val = (rr.stdout or "").strip().split("\t")[-1]
                return int(val) if val.isdigit() else 0
            except Exception:  # noqa: BLE001
                return 0

        out = []
        _total = len([d for d in dbs if str(d).strip() and str(d).strip() not in skip])
        for _idx, db in enumerate(dbs, 1):
            db = str(db).strip()
            if not db or db in skip:
                continue
            # 目标实例保护：并发线程过高就等（最多 60s），避免把生产实例拖死
            for _wait in range(12):
                _tr = _threads_running()
                if _tr < max_threads:
                    break
                time.sleep(5)
            else:
                raise RuntimeError(
                    "目标实例负载过高（Threads_running ≥ %d 持续 60s），已中止整实例克隆——"
                    "请减少要克隆的库，或等目标实例空闲后重试" % max_threads)
            if callable(progress):
                try:
                    progress(_idx, _total, db)
                except Exception:  # noqa: BLE001
                    pass
            sqlf = os.path.join(tmp, "dbs", db + ".sql")
            if not os.path.isfile(sqlf):
                continue
            target = "%s__%s" % (instance_prefix, db)
            r = subprocess.run(
                base + ["-N", "-e",
                        "DROP DATABASE IF EXISTS `%s`; CREATE DATABASE `%s` "
                        "CHARACTER SET utf8mb4" % (target, target)],
                env=env, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError("建库失败 %s: %s" % (target, r.stderr[:200]))
            created.append(target)
            # 流式导入：剥离 CREATE DATABASE / USE（否则数据会写回源库名）
            p1 = subprocess.Popen(["cat", sqlf], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            p2 = subprocess.Popen(strip, stdin=p1.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            p1.stdout.close()
            p3 = subprocess.Popen(base + [target], stdin=p2.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            p2.stdout.close()
            _, err3 = p3.communicate(timeout=3600)
            p2.wait()
            p1.wait()
            if p3.returncode != 0:
                raise RuntimeError("%s 导入失败: %s"
                                   % (target, (err3 or b"").decode("utf-8", "ignore")[:240]))
            rt = subprocess.run(
                base + ["-N", "-B", "-e",
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema='%s'" % target],
                env=env, capture_output=True, text=True, timeout=60)
            try:
                tables = int((rt.stdout or "0").strip() or 0)
            except ValueError:
                tables = 0
            out.append({"source": db, "target": target, "tables": tables})
            time.sleep(0.3)   # 库间让目标实例喘口气（小库多时避免瞬时堆积）

        if not out:
            return {"ok": False, "message": "归档内没有可恢复的业务库（仅系统库或文件缺失）"}
        return {"ok": True,
                "message": "整实例克隆完成：%d 个库，共 %d 张表"
                           % (len(out), sum(x["tables"] for x in out)),
                "databases": out, "db_count": len(out),
                "table_total": sum(x["tables"] for x in out),
                "instance_prefix": instance_prefix,
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        for t in created:
            try:
                subprocess.run(base + ["-N", "-e", "DROP DATABASE IF EXISTS `%s`" % t],
                               env=env, capture_output=True, text=True, timeout=30)
            except Exception:
                pass
        return {"ok": False, "message": "整实例克隆失败: %s" % str(e)[:300]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify_full_instance_clone(host: str, port, user: str, password: str,
                               instance_prefix: str) -> Dict[str, Any]:
    """整实例克隆的就绪校验：统计 ``<prefix>__*`` 的库数与表数。"""
    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    base = ["mysql", "--no-defaults", "-h", host or "127.0.0.1",
            "-P", str(port or 3306), "-u", user or "root"]
    try:
        r = subprocess.run(
            base + ["-N", "-B", "-e",
                    "SELECT table_schema, COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema LIKE '%s\\_\\_%%' GROUP BY table_schema"
                    % instance_prefix],
            env=env, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败: {str(e)[:150]}", "tables": 0}
    if r.returncode != 0:
        return {"ok": False, "message": f"连接失败: {(r.stderr or '').strip()[:200]}",
                "tables": 0}
    dbs, total = [], 0
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip().isdigit():
            dbs.append(parts[0])
            total += int(parts[1])
    if not dbs:
        return {"ok": False, "tables": 0,
                "message": f"未找到整实例克隆库（前缀 {instance_prefix}__），克隆可能未完成"}
    return {"ok": True, "tables": total, "databases": dbs, "db_count": len(dbs),
            "message": f"连接正常，整实例 {len(dbs)} 库共 {total} 张表：{', '.join(dbs[:4])}"
                       + ("…" if len(dbs) > 4 else "")}


def pg_clone_to_test(backup_path: str, instance_name: str,
                     pg_host: str = "127.0.0.1", pg_port: int = None,
                     pg_user: str = "postgres", pg_password: str = "") -> Dict[str, Any]:
    """从 PG 备份创建一个新的数据库（库级克隆），返回连接信息。

    - 端口默认取环境变量 PGPORT（未设置时 5432）；
    - ``.dump``（pg_dump -Fc 自定义格式）用 pg_restore 恢复到新建库，
      纯文本 ``.sql``（可 .gz）用 psql 导入到新建库。
    """
    env = os.environ.copy()
    if pg_password:
        env["PGPASSWORD"] = pg_password
    if not pg_port:
        try:
            pg_port = int(os.environ.get("PGPORT") or 5432)
        except ValueError:
            pg_port = 5432
    base = [_pg_tool("psql"), "-h", pg_host, "-p", str(pg_port), "-U", pg_user]
    # 1) 建克隆库（先清理同名残留）
    r = subprocess.run(base + ["-d", "postgres", "-c",
                               f'DROP DATABASE IF EXISTS "{instance_name}"'],
                       env=env, capture_output=True, text=True, timeout=30)
    r = subprocess.run(base + ["-d", "postgres", "-c",
                               f'CREATE DATABASE "{instance_name}"'],
                       env=env, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"ok": False, "message": f"创建库失败: {r.stderr[:200]}"}
    # 2) 导入
    lower = (backup_path or "").lower()
    try:
        if lower.endswith(".dump"):
            cmd = [_pg_tool("pg_restore"), "-h", pg_host, "-p", str(pg_port), "-U", pg_user,
                   "-d", instance_name, "--no-owner", "--no-privileges", backup_path]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
            # pg_restore 对个别对象报错也可能整体可用：仅在有输出错误且 rc!=0 时失败
            if r.returncode != 0:
                raise RuntimeError(r.stderr[:300])
        else:
            # 新版 pg_dump（17.6+/18 等 CVE-2025-8714 安全回补丁起）会在产物头部
            # 写入 psql 元命令 \restrict/\unrestrict（变量守卫）与
            # SET transaction_timeout（17+ 服务端参数）。导入端 psql/服务器版本
            # 较旧时会直接中断——实测 /opt/pgsql18 pg_dump 备份 + 14.12 psql/服务器
            # 导入。导入前剥离这两类行（守卫/会话参数，不涉及数据/DDL）。
            strip = ["sed", "-E",
                     "/^[[:space:]]*\\\\(un)?restrict[[:space:]]/Id;"
                     "/^[[:space:]]*SET[[:space:]]+transaction_timeout[[:space:]]*=/Id"]
            if lower.endswith(".gz"):
                dec = ["gzip", "-dc", backup_path]
                p1 = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p1s = subprocess.Popen(strip, stdin=p1.stdout, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
                p1.stdout.close()
                p2 = subprocess.Popen(base + ["-d", instance_name, "-v", "ON_ERROR_STOP=1",
                                              "-f", "-"],
                                      stdin=p1s.stdout, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env)
                p1s.stdout.close()
                _, err = p2.communicate(timeout=3600)
                p1.wait()
                p1s.wait()
                if p2.returncode != 0:
                    raise RuntimeError((err or b"").decode("utf-8", "ignore")[:300])
            else:
                p1 = subprocess.Popen(strip, stdin=open(backup_path, "rb"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p2 = subprocess.Popen(base + ["-d", instance_name, "-v", "ON_ERROR_STOP=1",
                                              "-f", "-"],
                                      stdin=p1.stdout, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env)
                p1.stdout.close()
                _, err = p2.communicate(timeout=3600)
                p1.wait()
                if p2.returncode != 0:
                    raise RuntimeError((err or b"").decode("utf-8", "ignore")[:300])
    except Exception as e:
        subprocess.run(base + ["-d", "postgres", "-c",
                               f'DROP DATABASE IF EXISTS "{instance_name}"'],
                       env=env, capture_output=True, text=True, timeout=30)
        return {"ok": False, "message": f"导入失败: {str(e)[:300]}"}
    return {"ok": True, "message": f"测试库 {instance_name} 已创建并导入",
            "connection": f"psql -h {pg_host} -p {pg_port} -U {pg_user} -d {instance_name}",
            "instance_name": instance_name,
            "port": pg_port}


def drop_clone(db_type: str, instance_name: str,
               host: str = "127.0.0.1", port: int = None,
               mysql_password: str = "", pg_password: str = "") -> Dict[str, Any]:
    """清理 VDB 测试库/库（与 pg_clone_to_test 的库级克隆语义对齐）。

    host/port：目标实例地址（支持远程主机，默认本机默认端口）。
    """
    env = os.environ.copy()
    if mysql_password:
        env["MYSQL_PWD"] = mysql_password
    if pg_password:
        env["PGPASSWORD"] = pg_password
    if db_type in ("mysql", "mariadb"):
        # 整实例克隆会产生 <instance_name>__<源库> 多个库：按前缀一并清理，
        # 否则销毁/到期只删掉名字本体，留下一堆 clone_N__xxx 残留库。
        base_m = ["mysql", "--no-defaults", "-h", host or "127.0.0.1",
                  "-P", str(port or 3306), "-u", "root", "-N", "-B"]
        # instance_name 由系统生成（clone_<id>），这里仍做引号剔除以防注入
        safe = str(instance_name).replace("'", "").replace("\\", "")
        r = subprocess.run(
            base_m + ["-e",
                      "SELECT schema_name FROM information_schema.schemata "
                      "WHERE schema_name='%s' OR schema_name LIKE '%s\\_\\_%%'" % (safe, safe)],
            env=env, capture_output=True, text=True, timeout=20)
        names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if names:
            stmt = ";".join("DROP DATABASE IF EXISTS `%s`" % n.replace("`", "")
                            for n in names)
            cmd = base_m + ["-e", stmt]
        else:
            cmd = base_m + ["-e", f"DROP DATABASE IF EXISTS `{instance_name}`"]
    elif db_type == "postgresql":
        pg_port = port or os.environ.get("PGPORT") or 5432
        cmd = [_pg_tool("psql"), "-h", host or "127.0.0.1", "-p", str(pg_port), "-U", "postgres",
               "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{instance_name}"']
    elif db_type == "kingbase":
        # 金仓：psycopg2 直连维护库，先踢掉连接再 DROP（克隆库常被校验连接占用）
        try:
            import psycopg2
        except ImportError:
            return {"ok": False, "message": "需要 psycopg2 驱动"}
        try:
            conn = psycopg2.connect(host=host or "127.0.0.1", port=int(port or 54321),
                                    user="system", password=pg_password or "",
                                    dbname="test", connect_timeout=10)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=%s AND pid<>pg_backend_pid()", (instance_name,))
            cur.execute('DROP DATABASE IF EXISTS "%s"' % instance_name.replace('"', ""))
            cur.close()
            conn.close()
            return {"ok": True, "message": f"已删除克隆库 {instance_name}"}
        except Exception as e:
            return {"ok": False, "message": f"删除失败: {str(e)[:200]}"}
    elif db_type == "dameng":
        # 达梦：dmPython 直连源实例 DROP SCHEMA CASCADE（克隆实例=独立模式，
        # 删模式即删全部克隆对象；不影响源实例其他模式）。
        try:
            import dmPython
        except ImportError:
            return {"ok": False, "message": "需要 dmPython 驱动"}
        try:
            conn = dmPython.connect(user="SYSDBA", password=pg_password or "",
                                    server=host or "127.0.0.1",
                                    port=int(port or 5236), login_timeout=10)
            # dmPython 无 autocommit 属性（170 实测 AttributeError），
            # 默认即自动提交，DDL 无需显式提交
            cur = conn.cursor()
            name = str(instance_name or "").replace('"', "")
            # 大小写双试（克隆通道统一大写；兜底旧记录小写形态）
            dropped, last_err = False, ""
            for cand in (name, name.upper(), name.lower()):
                if not cand:
                    continue
                try:
                    cur.execute('DROP SCHEMA "{0}" CASCADE'.format(cand))
                    dropped = True
                    break
                except Exception as ee:
                    msg = str(ee)
                    # 模式不存在 = 幂等成功（DM8 中文报错「不存在/无效」；
                    # 英文版 [-2103] Invalid schema name，170 实测）
                    if (any(k in msg for k in ("不存在", "无效", "-2108",
                                               "-2103", "Invalid schema name",
                                               "not exist", "does not exist"))
                            or "invalid" in msg.lower()):
                        continue
                    last_err = msg
                    break
            cur.close()
            conn.close()
            if dropped:
                return {"ok": True, "message": f"已删除克隆模式 {name}"}
            if not last_err:
                return {"ok": True, "message": f"克隆模式 {name} 已不存在"}
            return {"ok": False, "message": f"删除失败: {last_err[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"删除失败: {str(e)[:200]}"}
    else:
        return {"ok": False, "message": f"不支持的类型 {db_type}"}
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    return {"ok": r.returncode == 0, "message": (r.stderr or r.stdout or "OK")[:200]}


def verify_clone_conn(db_type: str, host: str, port, database: str, user: str,
                      password: str = "", service: str = "") -> Dict[str, Any]:
    """克隆库就绪校验：远程连接探活 + 统计表数量。

    返回 {ok, message, tables}。ok=False 时 message 带原因（连不上/库不存在等）。
    Oracle：database 传 schema 名（克隆出的用户），按 all_tables 统计该 owner 表数。
    """
    env = os.environ.copy()
    host = host or "127.0.0.1"
    if db_type == "oracle":
        try:
            import oracledb
        except ImportError:
            return {"ok": False, "tables": 0,
                    "message": "Oracle 校验需要 oracledb 驱动（镜像已内置）"}
        dsn = f"{host}:{port or 1521}/{service or 'ORCL'}"
        try:
            from core.restore_extras_clone import _ora_connect_retry
            conn = _ora_connect_retry(lambda _i: oracledb.connect(
                user=user or "system", password=password, dsn=dsn,
                tcp_connect_timeout=10))
        except TypeError:
            conn = oracledb.connect(user=user or "system", password=password, dsn=dsn)
        except Exception as e:
            return {"ok": False, "tables": 0,
                    "message": f"连接失败: {str(e)[:200]}"}
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM all_tables WHERE owner = UPPER(:owner)",
                {"owner": database or ""})
            tables = int(cur.fetchone()[0] or 0)
            return {"ok": True,
                    "message": f"连接正常，schema {database} 共 {tables} 张表",
                    "tables": tables}
        finally:
            cur.close()
            conn.close()
    if db_type in ("kingbase",):
        # 金仓与 PG 协议兼容，用 psycopg2 直连校验（不依赖远端 ksql/psql 二进制，
        # 避免 PATH/版本问题让「克隆已成功却校验失败」）。
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            return {"ok": False, "tables": 0,
                    "message": "金仓校验需要 psycopg2 驱动"}
        try:
            conn = psycopg2.connect(host=host, port=int(port or 54321),
                                    user=user or "system",
                                    password=password or "",
                                    dbname=database, connect_timeout=10)
        except Exception as e:
            return {"ok": False, "tables": 0,
                    "message": f"连接失败: {str(e)[:200]}"}
        cur = conn.cursor()
        try:
            # 2026-11-17 深度测试发现：金仓 pg_tables 含 pg_catalog/sys_hm 等
            # 内部 schema，白名单枚举排不净导致克隆表数量虚高（fresh 库实测
            # 103 = pg_catalog 99 + sys_hm 3 + 用户表 1）。统一改为排除 sys%
            # 前缀与已知内部 schema（PG 侧行为不变：无 sys% schema）。
            cur.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname NOT LIKE 'sys%' "
                "AND schemaname NOT IN ('pg_catalog','information_schema','anon',"
                "'src_restrict')")
            tables = int(cur.fetchone()[0] or 0)
            return {"ok": True,
                    "message": f"连接正常，库 {database} 共 {tables} 张表",
                    "tables": tables}
        finally:
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
    if db_type in ("dameng",):
        # 达梦：dmPython 直连校验（达梦无库级隔离，database=克隆出的模式名）。
        # 占位符用 ? 不用 %s（dmPython paramstyle）。
        try:
            import dmPython
        except ImportError:
            return {"ok": False, "tables": 0,
                    "message": "达梦校验需要 dmPython 驱动（平台已内置）"}
        try:
            conn = dmPython.connect(user=user or "SYSDBA", password=password or "",
                                    server=host, port=int(port or 5236),
                                    login_timeout=10)
        except Exception as e:
            return {"ok": False, "tables": 0,
                    "message": f"连接失败: {str(e)[:200]}"}
        cur = conn.cursor()
        try:
            # 模式名大小写兼容：克隆通道统一写大写，此处按原样匹配，
            # 未命中再试大写形态（兜底旧记录/手输小写）
            owner = str(database or "").strip()
            cur.execute("SELECT count(*) FROM dba_tables WHERE owner = ?",
                        (owner,))
            tables = int(cur.fetchone()[0] or 0)
            if tables == 0 and owner != owner.upper():
                cur.execute("SELECT count(*) FROM dba_tables WHERE owner = ?",
                            (owner.upper(),))
                tables = int(cur.fetchone()[0] or 0)
            return {"ok": True,
                    "message": f"连接正常，模式 {database} 共 {tables} 张表",
                    "tables": tables}
        finally:
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
    if db_type in ("mysql", "mariadb"):
        if password:
            env["MYSQL_PWD"] = password
        cmd = ["mysql", "--no-defaults", "--connect-timeout=8", "-h", host,
               "-P", str(port or 3306), "-u", user or "root", "-N", "-e",
               f"SELECT COUNT(*) FROM information_schema.tables "
               f"WHERE table_schema='{database}'"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return {"ok": False, "message": f"连接失败: {r.stderr.strip()[:200]}", "tables": 0}
        try:
            tables = int(r.stdout.strip() or 0)
        except ValueError:
            tables = 0
        return {"ok": True,
                "message": f"连接正常，库 {database} 共 {tables} 张表",
                "tables": tables}
    if db_type == "postgresql":
        if password:
            env["PGPASSWORD"] = password
        pg_port = port or os.environ.get("PGPORT") or 5432
        cmd = [_pg_tool("psql"), "-h", host, "-p", str(pg_port), "-U", user or "postgres",
               "-d", database, "-t", "-A", "-c",
               "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return {"ok": False, "message": f"连接失败: {r.stderr.strip()[:200]}", "tables": 0}
        try:
            tables = int(r.stdout.strip() or 0)
        except ValueError:
            tables = 0
        return {"ok": True,
                "message": f"连接正常，库 {database} 共 {tables} 张表",
                "tables": tables}
    return {"ok": False, "message": f"不支持的类型 {db_type}", "tables": 0}
