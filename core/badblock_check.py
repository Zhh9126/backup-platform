# -*- coding: utf-8 -*-
"""备份前坏块检测（A7，对标 DSG/鼎甲「备份前数据块校验」）。

定位：备份**之前**对源库数据块健康度做检测，把「带病备份」风险前置暴露。
与既有能力的关系：备份后完整性 sha256（防传输损坏）、恢复校验（防产物不可用）、
本模块（防源头坏块被原样备走）——三者互补。

通道与诚实边界：
- MySQL/MariaDB：原生直连 CHECK TABLE ... QUICK 逐表（最多 _MAX_TABLES 张，
  超出如实标注抽样）；非 QUICK 无法在线做（FOR UPGRADE 需要更重 IO）。
- Oracle fast 通道：v$database_block_corruption（历史 VALIDATE 结果）+
  v$recover_file（需恢复的数据文件，SQL 层只读，秒级）。
- Oracle full 通道：RMAN VALIDATE CHECK LOGICAL DATABASE（读全库，大库耗时长）
  + 校验后回读 v$database_block_corruption 拿真实坏块清单。需 SSH 纳管。
- PG 系/达梦/其他：无通用在线坏块检测接口，如实 skip（PG amcheck 需建扩展且
  逐索引代价高，不做假检测）。

结果 status 语义：clean=无坏块 / corrupt=发现坏块 / skip=不支持或未配置 /
failed=检测本身失败（不等于有坏块）。
"""
import shlex
import time

_MAX_TABLES = 50  # MySQL 逐表抽样上限
_MAX_CORRUPT_ROWS = 20  # 明细返回上限


def check_bad_blocks(task: dict, mode: str = "fast") -> dict:
    """对任务源库执行坏块检测。task 为已解密任务行。

    返回 {supported, status, message, details, checked, duration_sec}。
    """
    db_type = (task.get("db_type") or "").lower()
    t0 = time.time()
    if db_type in ("mysql", "mariadb"):
        try:
            return _check_mysql(task, db_type)
        finally:
            pass
    if db_type == "oracle":
        return _check_oracle(task, mode)
    return {"supported": False, "status": "skip",
            "message": "库型 %s 暂无通用在线坏块检测通道（如实跳过，非检测通过）"
                       % db_type,
            "details": [], "checked": 0,
            "duration_sec": round(time.time() - t0, 2)}


# ---------------------------------------------------------------------------
# MySQL / MariaDB：CHECK TABLE ... QUICK
# ---------------------------------------------------------------------------

def _check_mysql(task: dict, db_type: str) -> dict:
    from core import native_conn
    t0 = time.time()
    # 连接不带库名：information_schema 枚举 + 全限定 CHECK TABLE db.t；
    # 带不存在的库名连接会 1049 直接失败（检测前置于备份，库名可能配错）
    conn = native_conn.connect(
        db_type, task.get("host"), int(task.get("port") or 0),
        "", task.get("username") or "",
        _pw(task), timeout=30)
    try:
        cur = conn.cursor()
        try:
            # 目标表清单：单库任务=该库全部表；全实例=逐业务库枚举
            dbname = (task.get("db_name") or "").strip()
            if dbname:
                targets = [(dbname,)]
            else:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('mysql','sys','information_schema',"
                    "'performance_schema')")
                targets = [(r[0],) for r in cur.fetchall()]
            details, corrupt, checked, truncated = [], [], 0, False
            for (d,) in targets:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE'", (d,))
                tables = [r[0] for r in cur.fetchall()]
                if len(tables) > _MAX_TABLES:
                    tables = tables[:_MAX_TABLES]
                    truncated = True
                for tb in tables:
                    checked += 1
                    fq = "`%s`.`%s`" % (d.replace("`", "``"), tb.replace("`", "``"))
                    try:
                        cur.execute("CHECK TABLE %s QUICK" % fq)
                        rows = cur.fetchall()
                        # (Table, Op, Msg_type, Msg_text)
                        bad = [r for r in rows
                               if str(r[2]).lower() in ("error", "warning")
                               or "corrupt" in str(r[3]).lower()
                               and str(r[2]).lower() != "status"]
                        if bad:
                            corrupt.append("%s.%s: %s" % (
                                d, tb, "; ".join(str(r[3]) for r in bad)))
                        if checked <= 5 or bad:
                            details.append("CHECK %s.%s -> %s" % (
                                d, tb, rows[-1][3] if rows else "?"))
                    except Exception as e:
                        # CHECK 本身失败通常意味着表已损坏或权限不足——如实区分
                        msg = str(e)
                        if "1146" in msg:  # 表不存在（并发 drop）
                            continue
                        corrupt.append("%s.%s: CHECK 执行失败 %s" % (d, tb, msg[:120]))
            status = "corrupt" if corrupt else "clean"
            note = "（全库型超过 %d 张表已抽样）" % _MAX_TABLES if truncated else ""
            return {"supported": True, "status": status,
                    "message": "MySQL 坏块检测：%d 张表 CHECK QUICK，%s%s"
                               % (checked,
                                  "发现 %d 处异常" % len(corrupt) if corrupt
                                  else "全部 OK",
                                  note),
                    "details": (corrupt or details)[:_MAX_CORRUPT_ROWS],
                    "checked": checked,
                    "duration_sec": round(time.time() - t0, 2)}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Oracle：fast（SQL 视图）/ full（RMAN VALIDATE）
# ---------------------------------------------------------------------------

_ORACLE_FAST_SQL = (
    "SET PAGESIZE 0 FEEDBACK OFF HEADING OFF LINESIZE 400\n"
    "SELECT 'CORRUPT='||count(*) FROM v$database_block_corruption;\n"
    "SELECT 'RECOVER_FILE='||count(*) FROM v$recover_file;\n"
    "EXIT;\n")


def _check_oracle(task: dict, mode: str) -> dict:
    t0 = time.time()
    ssh_host = _ssh_host(task)
    if not ssh_host:
        return {"supported": True, "status": "skip",
                "message": "Oracle 坏块检测需要 SSH 纳管数据库服务器"
                           "（fast 通道经 sqlplus / full 通道经 rman）",
                "details": [], "checked": 0,
                "duration_sec": round(time.time() - t0, 2)}
    from core import remote_dump
    from core.engines.file import _ssh_exec_pipe
    client = remote_dump._connect(ssh_host)
    try:
        sftp = client.open_sftp()
        ts = time.strftime("%Y%m%d_%H%M%S")
        sqlfile, cmfile, shfile = ("/tmp/bbc_%s.sql" % ts,
                                   "/tmp/bbc_%s.rman" % ts, "/tmp/bbc_%s.sh" % ts)
        try:
            with sftp.open(sqlfile, "w") as f:
                f.write(_ORACLE_FAST_SQL)
            if mode == "full":
                with sftp.open(cmfile, "w") as f:
                    f.write("VALIDATE CHECK LOGICAL DATABASE;\nexit;\n")
            script = ["#!/bin/bash", "set -u",
                      "echo '--- FAST SQL ---'",
                      "sqlplus -s '/ as sysdba' @%s" % sqlfile]
            if mode == "full":
                script += ["echo '--- RMAN VALIDATE START ---'",
                           "rman target / @%s msgno 2>&1 | tail -40" % cmfile,
                           "echo '--- POST VALIDATE ---'",
                           "sqlplus -s '/ as sysdba' @%s" % sqlfile]
            script.append("echo '--- DONE ---'")
            with sftp.open(shfile, "w") as f:
                f.write("\n".join(script) + "\n")
            try:
                sftp.chmod(shfile, 0o755)
            except Exception:
                pass
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        check_user = _oracle_user(ssh_host)
        shell = remote_dump._wrap_login(
            "su - %s -c %s" % (check_user, shlex.quote("bash " + shfile)))
        out, err, rc = _ssh_exec_pipe(client, shell, timeout=14400 if mode == "full" else 300)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        n_corrupt, n_recover, _ = _parse_oracle_fast(text)
        if n_corrupt is None:
            return {"supported": True, "status": "failed",
                    "message": "Oracle 坏块检测失败：无法解析 sqlplus 输出"
                               "（检查 sysdba 权限） %s" % text[-200:],
                    "details": [], "checked": 0,
                    "duration_sec": round(time.time() - t0, 2)}
        details = []
        if n_corrupt:
            details.append("v$database_block_corruption: %d 条坏块记录" % n_corrupt)
        if n_recover:
            details.append("v$recover_file: %d 个数据文件需要恢复" % n_recover)
        status = "corrupt" if (n_corrupt or n_recover) else "clean"
        mode_note = ("RMAN VALIDATE CHECK LOGICAL 全库校验完成" if mode == "full"
                     else "快速检测（视图级；extra.bad_block_mode=full 可做 RMAN 全库校验）")
        return {"supported": True, "status": status,
                "message": "Oracle 坏块检测：%s——坏块 %d 条 / 待恢复文件 %d 个"
                           % (mode_note, n_corrupt, n_recover),
                "details": details or ["无坏块记录、无待恢复文件"],
                "checked": 0, "duration_sec": round(time.time() - t0, 2)}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _parse_oracle_fast(text: str):
    n_c = n_r = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CORRUPT="):
            try:
                n_c = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("RECOVER_FILE="):
            try:
                n_r = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return n_c, n_r, text


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------

def _pw(task: dict) -> str:
    pw = task.get("password") or ""
    if pw.startswith("sm4v1:") or pw.startswith("enc:"):
        import core.db as db
        try:
            return db.decrypt_secret(pw) or ""
        except Exception:
            return ""
    return pw


def _ssh_host(task: dict):
    from core import remote_dump
    try:
        return remote_dump.resolve_ssh_host(task)
    except Exception:
        return None


def _oracle_user(ssh_host) -> str:
    """Oracle OS 用户：任务/主机配置 db_os_user 优先，缺省 oracle。"""
    try:
        return (ssh_host or {}).get("db_os_user") or "oracle"
    except Exception:
        return "oracle"
