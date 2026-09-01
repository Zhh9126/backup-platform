# -*- coding: utf-8 -*-
"""PG 系（PostgreSQL / KingbaseES）全实例逻辑备份/恢复 —— 本机实现。

与 core/remote_dump.py 的远端（SSH）实现语义一致：
- 全实例备份 = 枚举库 → 逐库 dump（-Fc，各自一致性快照）
  + dumpall -g（角色/表空间等全局对象）→ tar.gz + manifest.json；
  pg_dump/sys_dump 是单库工具，不存在 mysqldump 的 --all-databases 参数。
- 全实例恢复 = 解包 → 恢复全局对象 → 缺失库自动 CREATE → 逐库 restore --clean。

产物格式（manifest.json）：
  {"format": "multi-db-tar", "db_type": "kingbase", "generated_at": "...",
   "globals": "yes|no|failed", "databases": ["db1", "db2"]}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time

# 各库类型差异点（与 remote_dump._PG_FAMILY_TOOLING 对齐的本地版）
TOOLING = {
    "postgresql": {
        "catalog_table": "pg_database",
        "maint_candidates": ("postgres", "template1"),
        "env_exports": ("PGPASSWORD",),
        "default_query": "psql",
        "default_dumpall": "pg_dumpall",
    },
    "kingbase": {
        "catalog_table": "sys_database",
        "maint_candidates": ("test", "postgres", "security", "template1"),
        "env_exports": ("KINGBASE_PASSWORD", "PGPASSWORD"),
        "default_query": "ksql",
        "default_dumpall": "sys_dumpall",
    },
}


def _build_env(db_type: str, password: str) -> dict:
    env = os.environ.copy()
    if password:
        for name in TOOLING[db_type]["env_exports"]:
            env[name] = password
    return env


def _run(cmd: list, env: dict, timeout: int = 3600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def _which_any(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def enumerate_databases(db_type: str, query_tool: str, host: str, port,
                        user: str, env: dict):
    """枚举用户库。返回 (命中的维护库, 库名列表)；全部失败返回 ("", [])。"""
    cfg = TOOLING[db_type]
    for mdb in cfg["maint_candidates"]:
        rc, out, _err = _run([
            query_tool, "-h", str(host), "-p", str(port), "-U", user,
            "-d", mdb, "-t", "-A", "-c",
            f"SELECT datname FROM {cfg['catalog_table']} "
            "WHERE NOT datistemplate ORDER BY 1",
        ], env)
        if rc == 0 and out.strip():
            return mdb, [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "", []


def backup_full_instance(db_type: str, *, host, port, user, password,
                         dump_tool: str, out_path: str,
                         query_tool: str = "", dumpall_tool: str = "") -> dict:
    """本机全实例备份：逐库 -Fc + globals + manifest → tar.gz。返回 manifest。"""
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法枚举数据库执行全实例备份")
    dumpall_tool = dumpall_tool or _which_any(cfg["default_dumpall"])

    maint, dbs = enumerate_databases(db_type, query_tool, host, port, user, env)
    if not dbs:
        raise RuntimeError(
            f"无法连接 {host}:{port} 枚举数据库（尝试维护库: "
            f"{', '.join(cfg['maint_candidates'])}），请检查连接信息")

    work = tempfile.mkdtemp(prefix="bp_fullinst_")
    try:
        dbs_dir = os.path.join(work, "dbs")
        os.makedirs(dbs_dir)
        for d in dbs:
            rc, _o, err = _run([
                dump_tool, "-h", str(host), "-p", str(port), "-U", user,
                "-Fc", "-f", os.path.join(dbs_dir, f"{d}.dump"), d,
            ], env, timeout=7200)
            if rc != 0:
                raise RuntimeError(f"库 {d} dump 失败(rc={rc}): {err[:300]}")

        globals_status = "no"
        if dumpall_tool:
            rc, out, _err = _run([
                dumpall_tool, "-h", str(host), "-p", str(port), "-U", user, "-g",
            ], env, timeout=1800)
            if rc == 0 and out.strip():
                with open(os.path.join(work, "globals.sql"), "w",
                          encoding="utf-8") as f:
                    f.write(out)
                globals_status = "yes"
            else:
                globals_status = "failed"

        manifest = {
            "format": "multi-db-tar",
            "db_type": db_type,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "globals": globals_status,
            "databases": dbs,
        }
        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(os.path.join(work, "manifest.json"), arcname="manifest.json")
            tf.add(dbs_dir, arcname="dbs")
            gpath = os.path.join(work, "globals.sql")
            if os.path.exists(gpath):
                tf.add(gpath, arcname="globals.sql")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_full_instance(db_type: str, *, host, port, user, password,
                          backup_path: str, restore_tool: str,
                          query_tool: str = "") -> dict:
    """本机全实例恢复：解包 → globals → 缺失库自动建库 → 逐库 restore --clean。

    返回 {"restored": [库名...], "globals": bool}；任一库恢复失败即抛异常。
    """
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法执行全实例恢复")

    work = tempfile.mkdtemp(prefix="bp_restore_")
    try:
        with tarfile.open(backup_path, "r:gz") as tf:
            tf.extractall(work)
        mpath = os.path.join(work, "manifest.json")
        if not os.path.exists(mpath):
            raise RuntimeError("tar 包内缺少 manifest.json，不是全实例备份产物")
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        dbs = manifest.get("databases") or []

        maint, existing = enumerate_databases(
            db_type, query_tool, host, port, user, env)
        if not maint:
            raise RuntimeError(
                f"无法连接 {host}:{port}（尝试维护库: "
                f"{', '.join(cfg['maint_candidates'])}），请检查目标实例连接信息")

        globals_ok = False
        gpath = os.path.join(work, "globals.sql")
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
            if rc != 0:
                raise RuntimeError(f"库 {d} 恢复失败(rc={rc}): {err[:300]}")
            restored.append(d)
        return {"restored": restored, "globals": globals_ok,
                "declared": dbs}
    finally:
        shutil.rmtree(work, ignore_errors=True)
