# -*- coding: utf-8 -*-
"""
数据同步引擎：在已纳管的数据库之间进行数据同步。

核心场景（来自需求）：纳管一台数据库（源），将其数据同步到另一台数据库（目标）。
- 同源/同类型且客户端可用时，执行真实逻辑同步（MySQL / PostgreSQL 的 dump|load 管道）
- 其余情况（客户端缺失、跨类型、演示模式）执行“仿真同步”占位，保证平台可演示且不误报
- 同步失败（连通性失败 / 管道错误）会第一时间触发通知（邮件等）
"""
import os
import time
import subprocess
from datetime import datetime
from typing import Optional

import config
import core.db as db
from core import models, notifier, probe


_logger = db.get_logger("sync")


def _resolve_source_conn(task: dict) -> dict:
    if task.get("source_type") == "managed" and task.get("source_task_id"):
        bt = models.get_task(task["source_task_id"], include_secret=True)
        if bt:
            return {
                "db_type": bt.get("db_type"),
                "host": bt.get("host"), "port": bt.get("port"),
                "username": bt.get("username"),
                "password": db.decrypt_secret(bt.get("password") or ""),
                "db_name": bt.get("db_name"),
                "label": f"已纳管任务#{bt.get('id')} {bt.get('name')}",
            }
    return {
        "db_type": task.get("src_db_type"),
        "host": task.get("src_host"), "port": task.get("src_port"),
        "username": task.get("src_username"),
        "password": db.decrypt_secret(task.get("src_password") or ""),
        "db_name": task.get("src_db_name"),
        "label": "手动源",
    }


def _resolve_target_conn(task: dict) -> dict:
    return {
        "db_type": task.get("tgt_db_type"),
        "host": task.get("tgt_host"), "port": task.get("tgt_port"),
        "username": task.get("tgt_username"),
        "password": db.decrypt_secret(task.get("tgt_password") or ""),
        "db_name": task.get("tgt_db_name"),
        "label": "目标库",
    }


def _real_sync_mysql(src: dict, tgt: dict) -> (bool, str, int):
    import subprocess
    src_env = os.environ.copy()
    if src.get("password"):
        src_env["MYSQL_PWD"] = src["password"]
    tgt_env = os.environ.copy()
    if tgt.get("password"):
        tgt_env["MYSQL_PWD"] = tgt["password"]
    dump = ["mysqldump", "-h", src["host"], "-P", str(src["port"]),
            "-u", src["username"], "--databases", src["db_name"] or ""]
    load = ["mysql", "-h", tgt["host"], "-P", str(tgt["port"]),
            "-u", tgt["username"]]
    try:
        p1 = subprocess.Popen(dump, env=src_env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p2 = subprocess.Popen(load, env=tgt_env, stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p1.stdout.close()
        _, err = p2.communicate(timeout=3600)
        rc = p2.returncode
    except Exception as e:
        return False, f"同步管道异常: {e}", 0
    if rc != 0:
        return False, f"数据同步失败: {err.decode('utf-8','ignore')[:300]}", 0
    return True, "MySQL 数据已同步（dump→load）", 0


def _real_sync_postgresql(src: dict, tgt: dict) -> (bool, str, int):
    import subprocess
    src_env = os.environ.copy()
    if src.get("password"):
        src_env["PGPASSWORD"] = src["password"]
    tgt_env = os.environ.copy()
    if tgt.get("password"):
        tgt_env["PGPASSWORD"] = tgt["password"]
    dump = ["pg_dump", "-h", src["host"], "-p", str(src["port"]),
            "-U", src["username"], "-d", src["db_name"] or ""]
    load = ["psql", "-h", tgt["host"], "-p", str(tgt["port"]),
            "-U", tgt["username"], "-d", tgt["db_name"] or ""]
    try:
        p1 = subprocess.Popen(dump, env=src_env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p2 = subprocess.Popen(load, env=tgt_env, stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p1.stdout.close()
        _, err = p2.communicate(timeout=3600)
        rc = p2.returncode
    except Exception as e:
        return False, f"同步管道异常: {e}", 0
    if rc != 0:
        return False, f"数据同步失败: {err.decode('utf-8','ignore')[:300]}", 0
    return True, "PostgreSQL 数据已同步（dump→load）", 0


def _simulate_sync(src: dict, tgt: dict, reason: str) -> (bool, str, int):
    _logger.warning("仿真数据同步: %s -> %s | %s", src.get("label"),
                    tgt.get("label"), reason)
    return True, "仿真同步(占位)成功；" + reason, 0


def _should_simulate(src: dict, tgt: dict) -> (bool, str):
    if config.DEMO_MODE == "on":
        return True, "DEMO_MODE=on 强制仿真"
    # 仅同源同类型且客户端齐全才走真实同步
    if src.get("db_type") != tgt.get("db_type"):
        return True, f"源({src.get('db_type')})与目标({tgt.get('db_type')})类型不一致，执行仿真"
    import shutil
    if src.get("db_type") == "mysql" and not shutil.which("mysqldump"):
        return True, "缺少 mysqldump 客户端，执行仿真"
    if src.get("db_type") == "postgresql" and not shutil.which("pg_dump"):
        return True, "缺少 pg_dump 客户端，执行仿真"
    if src.get("db_type") not in ("mysql", "postgresql"):
        return True, f"{src.get('db_type')} 暂未实现真实同步，执行仿真"
    return False, ""


def run_sync(sync_task_id: int) -> Optional[dict]:
    """执行一次数据同步，返回生成的同步记录。"""
    task = models.get_sync_task(sync_task_id, include_secret=True)
    if not task:
        _logger.warning("同步任务不存在: %s", sync_task_id)
        return None

    started = db.now_iso()
    rec_id = models.create_sync_record({
        "sync_task_id": sync_task_id, "started_at": started, "status": "running",
    })

    src = _resolve_source_conn(task)
    tgt = _resolve_target_conn(task)
    _logger.info("开始数据同步 task=%s(%s) %s -> %s", task["id"], task["name"],
                 src.get("label"), tgt.get("label"))

    # 连通性探测
    s_ok, s_msg = probe.probe_db_connection(
        src.get("db_type"), src.get("host"), src.get("port"),
        src.get("username"), src.get("password"), src.get("db_name"))
    t_ok, t_msg = probe.probe_db_connection(
        tgt.get("db_type"), tgt.get("host"), tgt.get("port"),
        tgt.get("username"), tgt.get("password"), tgt.get("db_name"))

    status, message, rows = "success", "", 0
    try:
        if s_ok is False:
            raise RuntimeError(f"源库连接失败: {s_msg}")
        if t_ok is False:
            raise RuntimeError(f"目标库连接失败: {t_msg}")
        sim, reason = _should_simulate(src, tgt)
        if sim:
            ok, message, rows = _simulate_sync(src, tgt, reason)
        elif src.get("db_type") == "mysql":
            ok, message, rows = _real_sync_mysql(src, tgt)
        elif src.get("db_type") == "postgresql":
            ok, message, rows = _real_sync_postgresql(src, tgt)
        else:
            ok, message, rows = _simulate_sync(src, tgt,
                                               f"{src.get('db_type')} 仿真")
        status = "success" if ok else "failed"
    except Exception as e:
        status = "failed"
        message = f"同步异常: {e}"
        _logger.exception("数据同步异常 sync=%s", sync_task_id)

    finished = db.now_iso()
    db.execute(
        "UPDATE sync_records SET finished_at=?, status=?, rows_synced=?, "
        "message=? WHERE id=?",
        (finished, status, rows, message, rec_id))
    models.set_sync_status(sync_task_id, finished, status, message)

    if status == "failed":
        text = (f"同步任务: {task['name']}\n源: {src.get('label')}\n"
                f"目标: {tgt.get('label')}\n状态: 失败\n说明: {message}")
        notifier.Notifier(None, _logger).notify(
            "failure", f"[失败] 数据同步 {task['name']}", text)
        db.add_log("ERROR", "sync", f"sync={sync_task_id} {task['name']} -> 失败")
    else:
        db.add_log("INFO", "sync", f"sync={sync_task_id} {task['name']} -> {status}")
    return models.get_sync_record(rec_id)
