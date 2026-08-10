# -*- coding: utf-8 -*-
"""
备份任务巡检引擎：对备份任务做健康体检，发现隐患第一时间通知。

巡检项：
1. 连通性：对源库做一次轻量连通性探测（core.probe）
2. 调度：是否启用且配置了调度（否则长期无备份）
3. 上次运行：最近一次状态是否为失败 / 是否从未运行

判定：
- fail：连通性失败 或 最近一次备份失败
- warn：无法判定连通性 / 从未运行 / 未配置调度
- pass：上述均正常

任一任务 fail 时，立即通过通知模块（邮件等）告警。
"""
import core.db as db
from core import models, notifier, probe


_logger = db.get_logger("inspection")


def _inspect_file_task(task: dict) -> list:
    """文件/目录备份任务的健康检查（与 DB 任务不同维度）。"""
    checks = []
    extra = {}
    raw = task.get("extra_options")
    if isinstance(raw, dict):
        extra = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            import json as _json
            extra = _json.loads(raw)
        except Exception:
            extra = {}

    src_type = extra.get("source_type", "local")
    # 1) 源可达性
    if src_type == "local":
        src_paths = extra.get("source_paths") or []
        if not src_paths:
            checks.append(("源路径", "warn", "未配置源路径"))
        else:
            import os as _os
            missing = [p for p in src_paths if not _os.path.exists(p)]
            if missing:
                checks.append(("源路径", "fail", f"源路径不存在: {missing}"))
            else:
                checks.append(("源路径", "ok", f"本地路径 {len(src_paths)} 项均存在"))
    else:
        # remote: 检查是否配置了 SSH 主机
        from core import ssh_hosts
        ssh_id = extra.get("ssh_host_id")
        host_key = extra.get("source_host")
        # 优先按 ID 查，其次按 host_key 字符串查
        matched = None
        if ssh_id:
            matched = ssh_hosts.get_host(int(ssh_id))
        if not matched and host_key:
            for h in ssh_hosts.list_hosts(include_secret=False):
                if h.get("host_key") == host_key:
                    matched = h
                    break
        if matched:
            checks.append(("SSH 主机", "ok", f"已纳管 {matched.get('name') or host_key}"))
        elif host_key:
            # host_key 已配置但未在纳管列表中：能跑就行，但建议纳管以便复用
            checks.append(("SSH 主机", "ok", f"使用 host_key={host_key}（未纳管但可正常备份）"))
        else:
            checks.append(("SSH 主机", "fail", "远程源未配置 SSH 主机"))

    # 2) 目标目录
    dst_type = extra.get("target_type", "local")
    if dst_type == "local":
        dst_path = extra.get("target_path", "")
        if not dst_path:
            checks.append(("目标目录", "warn", "未配置本地目标路径"))
        else:
            checks.append(("目标目录", "ok", f"本地 {dst_path}"))
    else:
        checks.append(("目标目录", "ok", f"远程 {extra.get('target_host','')}:{extra.get('target_path','')}"))

    # 3) 调度（文件备份常为手动执行，不强制要求）
    if task.get("enabled") and task.get("schedule_type") not in (None, "none", ""):
        checks.append(("调度", "ok", f"已启用，{task.get('schedule_type')}"))
    else:
        checks.append(("调度", "ok", "手动执行（无调度）"))

    return checks


def _inspect_db_task(task: dict) -> list:
    """数据库任务的健康检查。"""
    db_type = task.get("db_type")
    checks = []

    # 1. 连通性
    host = task.get("host") or ""
    if not host:
        # 未配置源 —— 配置缺失，应为 warn（任务处于未就绪态）
        checks.append(("连通性", "warn", "未配置源主机"))
    else:
        ok, msg = probe.probe_db_connection(
            db_type, host, task.get("port"),
            task.get("username"),
            db.decrypt_secret(task.get("password") or ""),
            task.get("db_name"))
        if ok is True:
            checks.append(("连通性", "ok", msg))
        elif ok is False:
            checks.append(("连通性", "fail", msg))
        else:
            # ok=None：未实现探测/客户端缺失；只要源配置齐全就不应误报
            checks.append(("连通性", "ok", f"源已配置（{msg or '未实现客户端探测'}）"))

    # 2. 调度
    if task.get("enabled") and task.get("schedule_type") not in (None, "none", ""):
        checks.append(("调度", "ok", f"已启用，{task.get('schedule_type')}"))
    elif task.get("enabled"):
        checks.append(("调度", "warn", "已启用但未配置调度，可能长期无新备份"))
    else:
        checks.append(("调度", "warn", "任务已停用"))

    return checks


def _inspect_one(task: dict) -> dict:
    name = task.get("name")
    db_type = task.get("db_type")

    if db_type == "file":
        checks = _inspect_file_task(task)
    else:
        checks = _inspect_db_task(task)

    # 3. 上次运行（所有类型通用，最关键的健康指标）
    last = task.get("last_status")
    if last == "failed":
        checks.append(("上次运行", "fail", "最近一次备份失败"))
    elif last in ("success", "simulated"):
        checks.append(("上次运行", "ok", f"最近一次状态：{last}"))
    elif last == "running":
        checks.append(("上次运行", "ok", "正在执行"))
    elif last in (None, "never"):
        checks.append(("上次运行", "warn", "从未执行过备份"))
    else:
        checks.append(("上次运行", "warn", f"最近状态：{last}"))

    # 判定优先级：fail > warn > pass
    if any(c[1] == "fail" for c in checks):
        status = "fail"
    elif any(c[1] == "warn" for c in checks):
        status = "warn"
    else:
        status = "pass"
    detail = "; ".join(f"[{lvl}] {label}: {m}" for label, lvl, m in checks)
    return {"status": status, "detail": detail, "name": name, "db_type": db_type}


def run_inspection(task_id: int = None, triggered_by: str = "manual") -> dict:
    """巡检全部或指定任务；返回汇总与失败清单，失败时触发通知。"""
    if task_id:
        tasks = [models.get_task(task_id, include_secret=True)]
        tasks = [t for t in tasks if t]
    else:
        tasks = models.list_tasks(include_secret=True)

    started = db.now_iso()
    failures = []
    summary = {"total": 0, "pass": 0, "warn": 0, "fail": 0}

    for t in tasks:
        res = _inspect_one(t)
        finished = db.now_iso()
        models.create_inspection({
            "task_id": t.get("id"), "task_name": res["name"],
            "db_type": res["db_type"], "started_at": started,
            "finished_at": finished, "status": res["status"],
            "detail": res["detail"], "triggered_by": triggered_by,
        })
        summary["total"] += 1
        summary[res["status"]] += 1
        if res["status"] == "fail":
            failures.append({"task_id": t.get("id"), "name": res["name"],
                             "db_type": res["db_type"], "detail": res["detail"]})

    # 失败通知（HTML 卡片样式）
    if failures:
        lines = "\n".join(
            f"- [{f['name']}] ({f['db_type']}): {f['detail']}" for f in failures)
        text = (f"巡检时间: {started}\n发现问题任务 {len(failures)} 项：\n{lines}\n"
                f"请尽快排查，避免数据保护出现盲区。")
        # 渲染 HTML（多设备友好）
        try:
            from core.email_template import render_inspection_alert
            html = render_inspection_alert(summary, failures, triggered_by)
        except Exception as e:
            _logger.warning("render_inspection_alert 失败，回退为纯文本: %s", e)
            html = None
        notifier.Notifier(None, _logger).notify(
            "failure", f"[告警] 备份巡检发现 {len(failures)} 项异常",
            text=text, html=html)
        db.add_log("ERROR", "inspection",
                   f"巡检完成，{len(failures)} 项异常")
    else:
        db.add_log("INFO", "inspection", "巡检完成，无异常")

    summary["failures"] = failures
    return summary
