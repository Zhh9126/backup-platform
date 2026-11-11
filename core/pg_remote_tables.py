# -*- coding: utf-8 -*-
"""PG 系远端表级恢复 + 表改名（零安装、使用目标机自带匹配版本工具）。

解决两类真实问题：
1. 平台侧 pg_dump 可能比服务器版本低（实测平台 14.12 vs 171 PG16.9），
   且服务器自带的新版客户端因 glibc 版本无法拷到平台机运行 —— 因此恢复
   也在**远端数据库服务器**执行，工具版本天然匹配，不依赖平台镜像内置
   PG16+ 客户端；
2. 既有 `_remote_pg_restore` 会把整包灌回，丢弃 `tables` / 表改名语义。
   本模块用 `pg_restore/sys_restore/gs_restore --table` 只恢复指定表，再执行
   `ALTER TABLE ... RENAME TO ...` 完成改名，全程经 SSH + paramiko。

只处理自定义格式(-Fc)/tar 归档；纯文本 .sql 无对象级 TOC，如实失败。
"""
from __future__ import annotations

import gzip
import os
import shlex
import subprocess
import time


class PGRemoteTableError(RuntimeError):
    """远端表级恢复失败，消息可直接透出给用户。"""


def _read_artifact(path: str) -> tuple:
    """返回 (字节, 是否自定义格式)；透明解压 zstd/gzip。"""
    if not path or not os.path.isfile(path):
        raise PGRemoteTableError(f"备份产物不存在: {path}")
    lower = path.lower()
    if lower.endswith(".zst"):
        r = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=600)
        if r.returncode != 0:
            raise PGRemoteTableError(f"zstd 解压失败: {r.stderr[:200]}")
        data, stem = r.stdout, path[:-4]
    elif lower.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            data = f.read()
        stem = path[:-3]
    else:
        with open(path, "rb") as f:
            data = f.read()
        stem = path
    if not data:
        raise PGRemoteTableError(f"备份产物为空: {path}")
    is_custom = stem.lower().endswith((".dump", ".tar", ".dmp"))
    return data, is_custom


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def restore_tables(task: dict, ssh_host: dict, backup_path: str,
                   db_type: str, target_db: str,
                   tables: list, table_renames: dict = None) -> dict:
    """在远端执行表级恢复，返回 {ok, message, tables, renamed}。"""
    from core import remote_dump as rd
    from core.engines.file import _ssh_exec_pipe

    tables = [str(t).strip().strip('"') for t in (tables or []) if str(t).strip()]
    if not tables:
        raise PGRemoteTableError("未指定要恢复的表")
    target_db = str(target_db or task.get("db_name") or "").strip()
    if not target_db:
        raise PGRemoteTableError("未指定目标库名（target_db）")
    data, is_custom = _read_artifact(backup_path)
    if not is_custom:
        raise PGRemoteTableError(
            "纯文本 .sql 产物无对象级 TOC，不支持远端表级恢复；"
            "请改用自定义格式(-Fc)备份，或走整库恢复")

    cfg = rd._PG_FAMILY_TOOLING.get(db_type) or rd._PG_FAMILY_TOOLING["postgresql"]
    user = task.get("username") or cfg["default_user"]
    pw = rd.db.decrypt_secret(task.get("password") or "")
    port = int(task.get("port") or cfg["default_port"])
    renames = {str(k).strip(): str(v).strip() for k, v in (table_renames or {}).items()
               if str(k).strip() and str(v).strip()}

    client = rd._connect(ssh_host)
    tmp_remote = ""
    try:
        query_bin = rd._pg_family_resolve_query_bin(client, cfg)
        if not query_bin:
            raise PGRemoteTableError("远端未找到 SQL 客户端（psql/ksql/gsql）")
        restore_bin = ""
        for cand in (cfg.get("restore_candidates") or ("pg_restore",)):
            restore_bin = rd._resolve_remote_bin(client, cand) or ""
            if restore_bin:
                break
        if not restore_bin:
            raise PGRemoteTableError("远端未找到还原工具（pg_restore/sys_restore/gs_restore）")
        env = rd._pg_family_env_exports(cfg, pw, bin_path=query_bin)
        wpass = rd._pg_w_pass(cfg, pw)

        # 1) 产物推到远端临时文件（表级恢复需要可 seek 的归档文件）
        tmp_remote = "/tmp/aidbm_tbl_rst_%d.dump" % int(time.time() * 1000)
        sftp = client.open_sftp()
        try:
            with sftp.open(tmp_remote, "wb") as fh:
                fh.write(data)
        finally:
            sftp.close()

        # 2) 重建目标库（UAT 异名库；不存在则创建，存在则干净重建）
        maint = rd._pg_family_detect_maint_db(client, cfg, query_bin, user, pw, port)
        safe_db = target_db.replace('"', '""')
        lit = target_db.replace("'", "''")
        base = (f"{query_bin} -h 127.0.0.1 -p {port} "
                f"-U {shlex.quote(user)}{wpass} -d {shlex.quote(maint)} ")
        drop_force_sql = f'DROP DATABASE IF EXISTS "{safe_db}" WITH (FORCE);'
        term_sql = ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{lit}';")
        drop_sql = f'DROP DATABASE IF EXISTS "{safe_db}";'
        create_sql = f'CREATE DATABASE "{safe_db}";'
        prep = (
            f"set -o pipefail; {env} "
            f"{base} -c {shlex.quote(drop_force_sql)} "
            f"|| {{ {base} -c {shlex.quote(term_sql)} ; "
            f"{base} -c {shlex.quote(drop_sql)} ; }} "
            f"&& {base} -c {shlex.quote(create_sql)}")
        _o, perr, prc = _ssh_exec_pipe(client, rd._wrap_login(prep),
                                       input_data=b"", timeout=300)
        if prc != 0:
            raise PGRemoteTableError(f"重建目标库 {target_db} 失败: {perr[:300]}")

        # 3) 只恢复指定表（--table 可重复；金仓/部分版本用 -t 兼容）
        def _cmd(flag):
            targs = " ".join(flag + " " + shlex.quote(t) for t in tables)
            return (f"set -o pipefail; {env} {shlex.quote(restore_bin)} "
                    f"-h 127.0.0.1 -p {port} -U {shlex.quote(user)}{wpass} "
                    f"--dbname {shlex.quote(target_db)} {targs} "
                    f"{shlex.quote(tmp_remote)}")

        _o, err, rc = _ssh_exec_pipe(client, rd._wrap_login(_cmd("--table")),
                                     input_data=b"", timeout=3600)
        err_txt = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        # 只有「选项本身不被识别」才回退 -t；不能把 SQL 层的
        # "unrecognized configuration parameter" 当成命令行选项错误，
        # 否则会对已导入的库重复导入并报 already exists。
        if rc != 0 and any(k in err_txt for k in
                           ("unrecognized option", "invalid option", "unknown option")):
            # 部分版本（如 sys_restore）只认 -t 短选项
            _o, err, rc = _ssh_exec_pipe(client, rd._wrap_login(_cmd("-t")),
                                         input_data=b"", timeout=3600)
            err_txt = (err.decode("utf-8", "replace")
                       if isinstance(err, bytes) else str(err))
        if rc != 0 and "transaction_timeout" in err_txt:
            # pg_restore 默认不因单条 SQL 错误中断，数据实际已完整导入
            # （171 真机硬核对已确认）。仅当**全部**错误都是这个跨版本
            # GUC（18.6 导出 → 16.9 回放）时判兼容成功，其它错误照常失败，
            # 不做「忽略所有错误」式假成功，也不重复导入。
            err_lines = [ln for ln in err_txt.splitlines()
                         if "ERROR:" in ln or "error:" in ln]
            if err_lines and all("transaction_timeout" in ln for ln in err_lines):
                rc, err = 0, ""
        if rc != 0:
            raise PGRemoteTableError(f"远端表级恢复失败(rc={rc}): {err[:400]}")

        # 4) 表改名（pg_restore 无 rename 语义，恢复后置 ALTER TABLE）
        renamed = []
        for src, dst in renames.items():
            sql = f"ALTER TABLE IF EXISTS {_qi(src)} RENAME TO {_qi(dst)};"
            qcmd = (f"set -o pipefail; {env} {query_bin} -v ON_ERROR_STOP=1 "
                    f"-h 127.0.0.1 -p {port} -U {shlex.quote(user)}{wpass} "
                    f"-d {shlex.quote(target_db)} -c {shlex.quote(sql)}")
            _o, qerr, qrc = _ssh_exec_pipe(client, rd._wrap_login(qcmd),
                                           input_data=b"", timeout=120)
            if qrc != 0:
                raise PGRemoteTableError(f"表改名 {src}→{dst} 失败: {qerr[:300]}")
            renamed.append(f"{src}→{dst}")
        return {"ok": True, "tables": tables, "renamed": renamed,
                "target_db": target_db, "tool": restore_bin,
                "message": f"远端表级恢复成功（{len(tables)} 表 → {target_db}）"
                           + (f"；表改名: {','.join(renamed)}" if renamed else "")}
    finally:
        if tmp_remote:
            try:
                _, _o, _e = client.exec_command(
                    "rm -f " + shlex.quote(tmp_remote), timeout=15)
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
