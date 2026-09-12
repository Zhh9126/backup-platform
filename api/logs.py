# -*- coding: utf-8 -*-
"""日志与诊断 API。

排查入口（失败可定位）
----------------------
1. 系统日志  GET /api/logs                  —— 已支持 task_id / record_id / q / since 过滤
2. 日志位置  GET /api/logs/locations        —— 日志目录从哪来、是否可写、各文件绝对路径
3. 平台日志  GET /api/logs/files            —— platform.log / error.log / crash.log 及轮转件
             GET /api/logs/file             —— 读取某个平台日志（支持 tail）
4. 操作日志  GET /api/logs/operations       —— 每次备份/恢复的独立详细日志文件列表
             GET /api/logs/operations/content —— 读取某份操作日志全文
             POST /api/logs/operations/purge  —— 清理过期操作日志
5. 诊断包    GET /api/diagnostics/export    —— 一键打包（环境+日志+失败记录），便于离线外发

安全：仅允许读取日志目录内的 *.log，名称白名单校验，杜绝路径穿越；
内容在写入时已脱敏（core.logging_setup.mask_secrets），导出包可安全外发。
"""
import io
import os
import re
import json
import zipfile
import datetime as _dt
from pathlib import Path

from flask import jsonify, request, send_file

from auth import login_required
from core import db, models
import config
from . import api_bp

_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MAX_READ_BYTES = 8 * 1024 * 1024      # 单次读取上限
_MAX_PACK_BYTES = 6 * 1024 * 1024      # 诊断包中单个日志上限


# --------------------------------------------------------------------------- #
# 日志位置
# --------------------------------------------------------------------------- #
@api_bp.route("/logs/locations", methods=["GET"])
@login_required
def log_locations():
    """返回日志真实落点（含降级原因）——用户找不到日志时的第一入口。"""
    try:
        from core import logging_setup
        info = logging_setup.log_locations()
    except Exception as e:
        return jsonify({"ok": False, "error": f"日志模块不可用: {e}"}), 500
    try:
        info["backup_root"] = str(config.get_backup_root())
    except Exception:
        pass
    try:
        info["meta_db"] = str(config.META_DB_PATH)
    except Exception:
        pass
    info["ok"] = True
    return jsonify(info)


def _log_dir() -> Path:
    from core import logging_setup
    from core.logging_setup import LOG_DIR
    return Path(LOG_DIR) if LOG_DIR else logging_setup.resolve_log_dir()[0]


def _safe_log_file(name: str):
    """解析日志目录内的日志文件，拒绝任何路径穿越。"""
    if not name or not _SAFE.match(name) or not name.endswith(".log"):
        return None
    root = _log_dir().resolve()
    p = (root / name)
    try:
        real = p.resolve()
    except Exception:
        return None
    if not str(real).startswith(str(root)) or real.parent != root:
        return None
    if not real.is_file():
        return None
    return real


@api_bp.route("/logs/files", methods=["GET"])
@login_required
def log_files():
    """列出日志目录下的文件（含轮转件），按修改时间倒序。"""
    root = _log_dir()
    out = []
    try:
        for p in root.glob("*.log*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            out.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "size_human": db.human_size(st.st_size),
                "mtime": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "is_error": p.name.startswith("error"),
                "is_crash": p.name.startswith("crash"),
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"ok": True, "dir": str(root), "files": out, "count": len(out)})


@api_bp.route("/logs/file", methods=["GET"])
@login_required
def log_file_content():
    """读取某个平台日志文件内容（默认尾部 500 行）。"""
    name = (request.args.get("name") or "platform.log").strip()
    try:
        tail = int(request.args.get("tail") or 500)
    except ValueError:
        tail = 500
    tail = max(1, min(tail, 20000))
    real = _safe_log_file(name)
    if not real:
        return jsonify({"ok": False, "error": f"非法或不存在日志文件: {name}"}), 400
    try:
        size = real.stat().st_size
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            if size > _MAX_READ_BYTES:
                f.seek(max(0, size - _MAX_READ_BYTES))
                f.readline()   # 丢弃半行
            lines = f.read().splitlines()
        content = "\n".join(lines[-tail:])
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取失败: {e}"}), 500
    return jsonify({"ok": True, "name": name, "path": str(real),
                    "tail": tail, "size_bytes": size, "content": content})


# --------------------------------------------------------------------------- #
# 操作日志（每次备份/恢复一份）
# --------------------------------------------------------------------------- #
@api_bp.route("/logs/operations", methods=["GET"])
@login_required
def log_operations():
    """列出操作日志文件，可按 kind / 任务 / 记录 / 日期过滤。"""
    from core import oplog
    try:
        limit = min(int(request.args.get("limit") or 200), 1000)
    except ValueError:
        limit = 200

    def _int_or_none(name):
        raw = (request.args.get(name) or "").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    rows = oplog.list_operations(
        limit=limit,
        kind=(request.args.get("kind") or "").strip(),
        task_id=_int_or_none("task_id"),
        record_id=_int_or_none("record_id"),
        day=(request.args.get("day") or "").strip(),
        keyword=(request.args.get("q") or "").strip(),
    )
    for r in rows:
        r["size_human"] = db.human_size(r["size_bytes"])
    return jsonify({"ok": True, "operations": rows, "count": len(rows),
                    "dir": str(oplog.operations_root())})


@api_bp.route("/logs/operations/content", methods=["GET"])
@login_required
def log_operation_content():
    """读取某份操作日志的完整内容（排查失败的主入口）。"""
    from core import oplog
    name = (request.args.get("name") or "").strip()
    day = (request.args.get("day") or "").strip()
    try:
        tail = int(request.args.get("tail") or 0)
    except ValueError:
        tail = 0
    content = oplog.read_operation(name, day=day, tail=tail)
    if not content:
        return jsonify({"ok": False, "error": f"未找到操作日志: {name}"}), 404
    return jsonify({"ok": True, "name": name, "day": day,
                    "tail": tail, "content": content})


@api_bp.route("/logs/operations/purge", methods=["POST"])
@login_required
def log_operations_purge():
    """清理超过保留期的操作日志。"""
    from core import oplog
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days") or oplog.OPLOG_RETENTION_DAYS)
    except (TypeError, ValueError):
        days = oplog.OPLOG_RETENTION_DAYS
    if days < 1:
        return jsonify({"ok": False, "error": "保留天数必须 ≥ 1"}), 400
    removed = oplog.purge_old(days)
    db.add_log("INFO", "system", f"清理操作日志：保留 {days} 天，删除 {removed} 个日期目录")
    return jsonify({"ok": True, "days": days, "removed_days": removed})


# --------------------------------------------------------------------------- #
# 单条记录的详细日志（从记录页直接跳转）
# --------------------------------------------------------------------------- #
@api_bp.route("/logs/record/<int:record_id>", methods=["GET"])
@login_required
def log_by_record(record_id):
    """按备份记录 ID 汇总：记录信息 + 操作日志 + 关联系统日志。"""
    from core import oplog
    rec = models.get_record(record_id)
    if not rec:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    log_path = (rec.get("log_path") or "").strip()
    content = ""
    if log_path:
        try:
            p = Path(log_path)
            if p.is_file():
                size = p.stat().st_size
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if size > _MAX_READ_BYTES:
                        f.seek(max(0, size - _MAX_READ_BYTES))
                        f.readline()
                    content = f.read()
        except Exception as e:
            content = f"（读取日志失败: {e}）"
    if not content:
        content = oplog.read_operation("", day="") or ""
    logs = models.list_logs(limit=300, record_id=record_id, with_detail=False)
    return jsonify({"ok": True, "record": rec, "log_path": log_path,
                    "content": content, "logs": logs})


# --------------------------------------------------------------------------- #
# 诊断包导出
# --------------------------------------------------------------------------- #
def _tail_read(path: Path, limit: int = _MAX_PACK_BYTES) -> str:
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()
            return f.read()
    except Exception as e:
        return f"（读取失败: {e}）"


@api_bp.route("/diagnostics/export", methods=["GET"])
@login_required
def diagnostics_export():
    """一键导出诊断包 zip：环境信息 + 日志 + 失败记录。

    完全离线可用：生成的文件可直接通过邮件/摆渡盘外发，
    内容已在写入时脱敏，不含明文口令。
    """
    import sys
    import platform as _pf
    from core import logging_setup, oplog

    log_dir = _log_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:

        # ① 环境信息
        env_lines = [
            "AIDBM 诊断包",
            "生成时间: " + _dt.datetime.now().isoformat(timespec="seconds"),
            "-" * 60,
            "版本: " + str(getattr(config, "APP_VERSION", "") or "-"),
            "Python: " + sys.version.replace("\n", " "),
            "运行形态: " + logging_setup.runtime_form(),
            "可执行文件: " + (sys.executable or "-"),
            "frozen: " + str(getattr(sys, "frozen", False)),
            "操作系统: " + _pf.platform(),
            "主机名: " + _pf.node(),
            "PID: " + str(os.getpid()),
            "-" * 60,
        ]
        try:
            for k, v in logging_setup.log_locations().items():
                env_lines.append(f"{k}: {v}")
        except Exception:
            pass
        for key, getter in (
            ("backup_root", lambda: config.get_backup_root()),
            ("meta_db", lambda: config.META_DB_PATH),
            ("web", lambda: f"{config.WEB_HOST}:{config.WEB_PORT}"),
        ):
            try:
                env_lines.append(f"{key}: {getter()}")
            except Exception:
                pass
        env_lines.append("-" * 60)
        # 日志目录内容清单
        try:
            env_lines.append("日志目录文件：")
            for p in sorted(log_dir.glob("*.log*")):
                try:
                    env_lines.append(f"  {p.name}  {p.stat().st_size} bytes")
                except Exception:
                    pass
        except Exception:
            pass
        z.writestr("environment.txt", logging_setup.mask_secrets("\n".join(env_lines)))

        # ② 平台日志（尾部）
        for fn in ("platform.log", "error.log", "crash.log"):
            text = _tail_read(log_dir / fn)
            if text:
                z.writestr(f"logs/{fn}", text)

        # ③ 最近操作日志（失败排查核心现场）
        try:
            ops = oplog.list_operations(limit=40)
            for o in ops:
                text = _tail_read(Path(o["path"]))
                if text:
                    z.writestr(f"operations/{o['day']}/{o['name']}", text)
        except Exception:
            pass

        # ④ 最近失败记录
        try:
            fails = db.query(
                "SELECT id, task_id, db_type, backup_type, started_at, finished_at, "
                "status, message, log_path FROM backup_records "
                "WHERE status != 'success' ORDER BY id DESC LIMIT 50")
            z.writestr("recent_failures.json", json.dumps(
                fails, ensure_ascii=False, indent=2, default=str))
        except Exception:
            pass
        try:
            rfails = db.query(
                "SELECT id, task_id, record_id, target_host, target_db, started_at, "
                "finished_at, status, message, log_path FROM restore_records "
                "WHERE status != 'success' ORDER BY id DESC LIMIT 50")
            z.writestr("recent_restore_failures.json", json.dumps(
                rfails, ensure_ascii=False, indent=2, default=str))
        except Exception:
            pass

        # ⑤ 系统日志（最近错误，便于快速定位）
        try:
            errs = models.list_logs(limit=500, level="ERROR", with_detail=True)
            z.writestr("system_errors.json", json.dumps(
                errs, ensure_ascii=False, indent=2, default=str))
        except Exception:
            pass

    buf.seek(0)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"aidbm_diagnostics_{stamp}.zip"
    db.add_log("INFO", "system", f"导出诊断包 {fname}")
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=fname)
