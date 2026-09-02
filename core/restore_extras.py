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
import os
import re
import time
import datetime
import subprocess
import tempfile
import logging
from typing import Optional, Dict, Any

import core.db as db

logger = logging.getLogger("restore_extras")


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
        cmd = ["psql", "-h", str(task.get("host") or "127.0.0.1"),
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


def mysql_pitr_restore(backup_path: str, target_time: str, target: dict) -> Dict[str, Any]:
    """MySQL PITR：先做全量恢复，再调用 mysqlbinlog replay 到 target_time。
    target: {host, port, user, password, db}
    """
    # 1) 解压 .gz（如果是）
    actual = backup_path
    if backup_path.endswith(".gz"):
        out = subprocess.run(["gunzip", "-k", backup_path], capture_output=True, text=True)
        actual = backup_path[:-3]
    # 安全整改：目标库名必须是合法 MySQL 标识符，防参数注入
    db_name = str(target.get("db") or "")
    if db_name and not re.match(r"^[A-Za-z0-9_$]{1,64}$", db_name):
        return {"ok": False, "message": "目标库名不合法（仅允许字母/数字/下划线/$，最长 64）"}
    # 2) 全量导入
    env = os.environ.copy()
    if target.get("password"):
        env["MYSQL_PWD"] = target["password"]
    db_arg = f" {db_name}" if db_name else ""
    cmd_full = ["mysql", "--no-defaults", "-h", str(target.get("host") or "127.0.0.1"),
                "-P", str(target.get("port") or 3306),
                "-u", str(target.get("user") or "root")] + db_arg.split()
    logger.info("[pitr] mysql full restore: %s", " ".join(cmd_full))
    with open(actual, "rb") as f:
        r = subprocess.run(cmd_full, env=env, stdin=f, capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "message": f"全量恢复失败: {r.stderr[:200]}"}
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
    stop_dt = _norm_stop_datetime(target_time)
    # --skip-gtids：源库 GTID_MODE=ON 而目标可能为 OFF，回放 SQL 中的
    # SET @@SESSION.GTID_NEXT 会导致 ERROR 1781，统一剥离 GTID 语句
    cmd_binlog = ["mysqlbinlog", "--skip-gtids=true",
                  f"--start-position={binlog_pos}",
                  f"--stop-datetime={stop_dt}", binlog_path]
    logger.info("[pitr] mysqlbinlog replay: %s", " ".join(cmd_binlog))
    r2 = subprocess.run(cmd_binlog, capture_output=True, text=True, timeout=1800)
    if r2.returncode != 0:
        return {"ok": False, "message": f"binlog replay 失败: {r2.stderr[:200]}"}
    # 5) 把 replay 出的 SQL 灌入目标
    cmd_apply = ["mysql", "--no-defaults", "-h", str(target.get("host") or "127.0.0.1"),
                 "-P", str(target.get("port") or 3306),
                 "-u", str(target.get("user") or "root")] + db_arg.split()
    r3 = subprocess.run(cmd_apply, env=env, input=r2.stdout, capture_output=True, text=True)
    if r3.returncode != 0:
        return {"ok": False, "message": f"apply binlog SQL 失败: {r3.stderr[:200]}"}
    return {"ok": True, "message": f"PITR 成功，已 replay 至 {stop_dt}",
            "binlog_replayed": True, "target_time": target_time}


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
    cmd = ["pg_restore", "-h", str(target.get("host") or "127.0.0.1"),
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
    cmd = ["pg_restore", "-h", str(target.get("host") or "127.0.0.1"),
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
    base = ["psql", "-h", pg_host, "-p", str(pg_port), "-U", pg_user]
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
            cmd = ["pg_restore", "-h", pg_host, "-p", str(pg_port), "-U", pg_user,
                   "-d", instance_name, "--no-owner", "--no-privileges", backup_path]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
            # pg_restore 对个别对象报错也可能整体可用：仅在有输出错误且 rc!=0 时失败
            if r.returncode != 0:
                raise RuntimeError(r.stderr[:300])
        else:
            if lower.endswith(".gz"):
                dec = ["gzip", "-dc", backup_path]
                p1 = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p2 = subprocess.Popen(base + ["-d", instance_name, "-v", "ON_ERROR_STOP=1",
                                              "-f", "-"],
                                      stdin=p1.stdout, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env)
                p1.stdout.close()
                _, err = p2.communicate(timeout=3600)
                p1.wait()
                if p2.returncode != 0:
                    raise RuntimeError((err or b"").decode("utf-8", "ignore")[:300])
            else:
                r = subprocess.run(base + ["-d", instance_name, "-v", "ON_ERROR_STOP=1",
                                           "-f", backup_path],
                                   env=env, capture_output=True, text=True, timeout=3600)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[:300])
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
               mysql_password: str = "", pg_password: str = "") -> Dict[str, Any]:
    """清理 VDB 测试库/库（与 pg_clone_to_test 的库级克隆语义对齐）。"""
    env = os.environ.copy()
    if mysql_password:
        env["MYSQL_PWD"] = mysql_password
    if pg_password:
        env["PGPASSWORD"] = pg_password
    if db_type == "mysql":
        cmd = ["mysql", "--no-defaults", "-h", "127.0.0.1", "-P", "3306", "-u", "root", "-N", "-e",
               f"DROP DATABASE IF EXISTS `{instance_name}`"]
    elif db_type == "postgresql":
        pg_port = os.environ.get("PGPORT") or 5432
        cmd = ["psql", "-h", "127.0.0.1", "-p", str(pg_port), "-U", "postgres",
               "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{instance_name}"']
    else:
        return {"ok": False, "message": f"不支持的类型 {db_type}"}
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    return {"ok": r.returncode == 0, "message": (r.stderr or r.stdout or "OK")[:200]}
