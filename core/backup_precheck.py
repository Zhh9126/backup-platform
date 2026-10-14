# -*- coding: utf-8 -*-
"""备份/恢复 预检测（Precheck）。

目的：在真正发起备份/恢复**之前**，用轻量探测回答"这次操作能不能成功"，
把失败原因提前暴露（连通性 / 凭据 / 工具 / 空间 / 产物完整性 / 目标端可达），
避免排班半夜跑批才发现配置错误。

诚实边界：
- 预检是**探测**不是演练，不落盘备份产物、不改目标库数据；
- 预检通过 ≠ 备份必然成功（网络抖动/中途故障无法预知），
  只代表"以当前配置发起操作的所有已知前置条件均满足"。

复用现有能力：引擎 preflight()（工具就绪）、remote_dump（SSH/远端工具）、
native_conn（原生连通）、backup_anomaly._sniff_head（产物结构）。
零库表改动，不影响现有功能。
"""
import os
import shutil

_ITEMS = ("item", "level", "message")
_LEVEL_ORDER = {"pass": 0, "skip": 0, "warn": 1, "fail": 2}


def _add(items, item, level, message):
    items.append({"item": item, "level": level, "message": message})


def _summarize(items):
    worst = "pass"
    for it in items:
        if _LEVEL_ORDER.get(it["level"], 0) > _LEVEL_ORDER.get(worst, 0):
            worst = it["level"]
    return {"ok": worst != "fail", "level": worst, "items": items}


def _disk_usage():
    import config
    root = getattr(config, "BACKUP_ROOT", None) or "/tmp"
    try:
        total, used, free = shutil.disk_usage(root)
        return total, used, free
    except Exception:
        try:
            total, used, free = shutil.disk_usage("/tmp")
            return total, used, free
        except Exception:
            return None


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0


# 恢复侧客户端工具集（与各引擎 restore 通道一致）
_RESTORE_TOOLS = {
    "mysql": ["mysql"],
    "mariadb": ["mysql"],
    "postgresql": ["psql"],
    "kingbase": ["sys_restore", "ksql"],
    "opengauss": ["gs_restore", "gsql"],
    "dameng": ["dimp"],
    "oracle": ["impdp"],
}


def precheck_backup(task: dict) -> dict:
    """对单个备份任务做「可备份性」预检。task 为已解密（include_secret=True）行。"""
    items = []
    import config
    from core import native_conn, remote_dump
    from core.engines import get_engine
    from core.engines.base import BackupMode

    db_type = (task.get("db_type") or "").lower()
    tid = task.get("id")

    # 1) 任务配置完整性
    missing = [k for k in ("host", "port", "username")
               if not task.get(k) and db_type not in ("file",)]
    if task.get("password") is None and db_type not in ("file",):
        missing.append("password")
    if missing:
        _add(items, "task_config", "fail",
             "任务缺少必填配置: " + ", ".join(missing))
    else:
        _add(items, "task_config", "pass",
             "任务配置完整（%s %s:%s / %s）" % (
                 db_type, task.get("host"), task.get("port"),
                 task.get("username")))

    # 2) SSH 通道（配置了 SSH 主机时）
    ssh_host = None
    try:
        ssh_host = remote_dump.resolve_ssh_host(task)
    except Exception:
        pass
    if ssh_host:
        try:
            client = remote_dump._connect(ssh_host)
            client.close()
            _add(items, "ssh_channel", "pass",
                 "SSH 通道连通（%s）" % ssh_host.get("host_key"))
        except Exception as e:
            _add(items, "ssh_channel", "fail",
                 "SSH 通道不可达: %s" % str(e)[:120])
    else:
        _add(items, "ssh_channel", "skip", "未配置 SSH 主机（本机通道）")

    # 3) 源库连通（文件备份跳过）
    if db_type == "file":
        _add(items, "source_connectivity", "skip", "文件备份无需源库连通")
    else:
        try:
            conn = native_conn.connect(
                db_type, task.get("host"), task.get("port"),
                task.get("db_name") or "", task.get("username"),
                task.get("password") or "", timeout=8)
            try:
                conn.close()
            except Exception:
                pass
            _add(items, "source_connectivity", "pass",
                 "源库连通（原生驱动直连成功）")
        except Exception as e:
            # 原生直连失败不代表引擎链路必然失败（某些库型走驱动器/JDBC/SSH
            # 通道），标 warn 并给出行内提示
            _add(items, "source_connectivity", "warn",
                 "原生直连失败（%s）——若该库型走 SSH/JDBC 通道可忽略此项，"
                 "但请确认凭据正确" % str(e)[:120])

    # 4) 备份工具就绪（复用引擎 preflight：本机 PATH / 远端 su 探测 / 自带插件）
    try:
        engine = get_engine(db_type, task, getattr(config, "BACKUP_ROOT", "/tmp"))
        ok, msg = engine.preflight()
        _add(items, "backup_tools", "pass" if ok else "fail", str(msg)[:200])
    except Exception as e:
        _add(items, "backup_tools", "fail",
             "工具预检异常: %s" % str(e)[:140])

    # 5) 物理备份附加提示
    try:
        mode = engine.backup_mode
        is_phys = mode == BackupMode.PHYSICAL
    except Exception:
        is_phys = False
    if is_phys:
        _add(items, "physical_note", "warn",
             "物理备份模式：请确认数据库归档/日志配置满足该库型物理备份要求"
             "（如达梦归档开启、MySQL binlog、PG wal_level=replica）")

    # 6) 平台存储空间
    du = _disk_usage()
    if du:
        total, used, free = du
        level = "pass" if free > 1024 ** 3 else ("warn" if free > 0 else "fail")
        _add(items, "disk_space", level,
             "平台存储可用 %s（共 %s）%s" % (
                 _human(free), _human(total),
                 "" if level == "pass" else "——空间紧张，备份可能中途失败"))
    else:
        _add(items, "disk_space", "unknown", "无法获取存储空间信息")

    return _summarize(items)


def precheck_restore(record: dict, target_host_info: dict = None,
                     target_db: str = None) -> dict:
    """对一条备份记录做「可恢复性」预检。

    record 为 backup_records 行；target_host_info 为可选的恢复目标主机
    （不传=本机恢复语义）；target_db 为可选目标库（仅做连通探测，不写数据）。
    """
    items = []
    import config
    from core import db, native_conn, remote_dump
    from core import backup_anomaly

    rec_id = record.get("id")
    db_type = (record.get("db_type") or "").lower()
    path = record.get("backup_path") or ""

    # 1) 产物存在
    if not path or not os.path.exists(path):
        _add(items, "artifact", "fail",
             "产物文件不存在（%s）——远端留存的备份请先确认已拉回平台" % (path or "空路径"))
        return _summarize(items)
    size = os.path.getsize(path)
    if size <= 0:
        _add(items, "artifact", "fail", "产物为空文件（0 B），恢复必然失败")
        return _summarize(items)
    _add(items, "artifact", "pass", "产物存在（%s，%s）" % (path, _human(size)))

    # 2) 完整性（sha256 基线比对）
    checksum = record.get("checksum") or ""
    if checksum and len(checksum) == 64:
        try:
            from core import db as _db
            actual = _db.sha256_file(path)
            if actual == checksum:
                _add(items, "integrity", "pass",
                     "sha256 校验一致（%s…）" % checksum[:12])
            else:
                _add(items, "integrity", "fail",
                     "sha256 不一致（记录 %s… / 实际 %s…）——产物已损坏或被改动"
                     % (checksum[:12], actual[:12]))
        except Exception as e:
            _add(items, "integrity", "unknown", "校验和计算失败: %s" % str(e)[:100])
    else:
        _add(items, "integrity", "unknown",
             "记录无 sha256 基线（历史记录），仅做结构探测")

    # 3) 结构探测（复用安全检测的嗅探：gzip/zip 首块可解 / tar 头 / 文本）
    try:
        fmt, ok, detail = backup_anomaly._sniff_head(path)
        if not ok:
            _add(items, "structure", "fail", "产物结构异常: %s" % detail)
        elif fmt == "gzip":
            _add(items, "structure", "pass", "压缩归档结构正常（%s）" % detail)
        else:
            _add(items, "structure", "pass", str(detail)[:120])
    except Exception as e:
        _add(items, "structure", "unknown", "结构探测失败: %s" % str(e)[:100])

    # 4) 恢复目标通道
    if target_host_info:
        try:
            client = remote_dump._connect(target_host_info)
            client.close()
            _add(items, "target_channel", "pass",
                 "恢复目标 SSH 主机连通（%s）"
                 % target_host_info.get("host_key"))
        except Exception as e:
            _add(items, "target_channel", "fail",
                 "恢复目标 SSH 主机不可达: %s" % str(e)[:120])
    else:
        _add(items, "target_channel", "pass",
             "恢复目标=平台本机（无需 SSH）")

    # 5) 恢复工具就绪
    tools = _RESTORE_TOOLS.get(db_type)
    if not tools:
        _add(items, "restore_tools", "skip",
             "%s 无本机恢复工具要求（恢复在数据库服务器端执行）" % db_type)
    elif target_host_info:
        try:
            check_user = None
            from core.remote_dump import resolve_db_os_user
            check_user = resolve_db_os_user(None, target_host_info, db_type=db_type)
            missing = [t for t in tools
                       if not remote_dump.remote_has_tool(
                           target_host_info, t, check_user=check_user)]
            if missing:
                _add(items, "restore_tools", "fail",
                     "目标主机缺少恢复工具: %s" % ", ".join(missing))
            else:
                _add(items, "restore_tools", "pass",
                     "恢复工具就绪（%s，OS 用户 %s）" % (", ".join(tools), check_user))
        except Exception as e:
            _add(items, "restore_tools", "unknown",
                 "远端工具探测失败: %s" % str(e)[:120])
    else:
        # 本机恢复：用引擎 check_client 通道
        try:
            import config
            from core.engines import get_engine
            task = {"id": 0, "db_type": db_type, "name": "precheck",
                    "host": "127.0.0.1", "port": 0, "username": "",
                    "password": "", "db_name": target_db or ""}
            engine = get_engine(db_type, task, getattr(config, "BACKUP_ROOT", "/tmp"))
            ok, msg = engine.check_client()
            _add(items, "restore_tools", "pass" if ok else "fail", str(msg)[:180])
        except Exception as e:
            _add(items, "restore_tools", "unknown",
                 "本机工具检查异常: %s" % str(e)[:120])

    # 6) 目标库连通（给出目标库时；只连不写）
    if target_db and target_host_info is None:
        task_row = None
        try:
            task_row = db.query_one(
                "SELECT * FROM backup_tasks WHERE id=?", (record.get("task_id"),))
        except Exception:
            pass
        if task_row:
            try:
                from core import models
                t = models.get_task(task_row["id"], include_secret=True)
                conn = native_conn.connect(
                    db_type, t.get("host"), t.get("port"), target_db,
                    t.get("username"), t.get("password") or "", timeout=8)
                try:
                    conn.close()
                except Exception:
                    pass
                _add(items, "target_db", "pass",
                     "目标库 %s 可连通（恢复向导将在提交时做最终写入确认）" % target_db)
            except Exception as e:
                _add(items, "target_db", "warn",
                     "目标库直连失败（%s）——同库型恢复通常在源实例上执行，"
                     "此探测失败不一定阻断，请确认凭据与库名" % str(e)[:110])

    # 7) 平台存储空间（恢复解包临时空间）
    du = _disk_usage()
    if du and size > 0:
        free = du[2]
        need = size * 3  # 解包/解压经验倍数
        level = "pass" if free > need else ("warn" if free > size else "fail")
        _add(items, "restore_space", level,
             "平台可用 %s / 产物 %s（建议保留 3 倍解包空间）"
             % (_human(free), _human(size)))

    return _summarize(items)


def precheck_backup_by_id(task_id: int) -> dict:
    from core import models
    task = models.get_task(task_id, include_secret=True)
    if not task:
        raise ValueError("任务不存在: %s" % task_id)
    res = precheck_backup(task)
    res["task_id"] = task_id
    return res


def precheck_restore_by_id(record_id: int, target_host_id: int = None,
                           target_db: str = None) -> dict:
    from core import db, ssh_hosts
    rec = db.query_one("SELECT * FROM backup_records WHERE id=?", (record_id,))
    if not rec:
        raise ValueError("备份记录不存在: %s" % record_id)
    host_info = None
    if target_host_id:
        host_info = ssh_hosts.get_host(target_host_id, include_secret=True)
        if not host_info:
            raise ValueError("恢复目标主机不存在: %s" % target_host_id)
    res = precheck_restore(dict(rec), host_info, target_db)
    res["record_id"] = record_id
    return res
