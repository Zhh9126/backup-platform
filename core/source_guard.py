# -*- coding: utf-8 -*-
"""源端资源感知 + 离线负载周期学习（零安装、零公网依赖）。

对标「AI 预测性备份调度 / 源端资源感知 / 三层 CPU 保护」，实现方式严格
遵循 AIDBM 的硬性约束：

* 采集通过 **SSH + paramiko 只读读取 /proc/stat、/proc/loadavg**，被管数据库
  服务器不装任何 agent、不装任何工具；
* 窗口推荐是**纯离线统计**（按主机+小时聚合采样），不调用任何公网 LLM /
  embedding API，因此不属于 AI 收费模块，可随开源仓库交付；
* 采样失败一律 fail-open 并如实标注，绝不因为探测不到就把本机能拿到的
  CPU 冒充远端，也绝不阻断正常备份。

三层挂接：
  决策端 evaluate() → API 端 api/tasks.py::run_task 实时复查
  → 执行端 core/scheduler.py::_execute_backup（cron/手动/异步统一经过它）。

阈值语义（可在 system_config / 设置页调整）：
  cpu > block_pct(70%)     → 拒绝备份，写 failed 记录 + 审计日志
  throttle_pct(50%) ≤ cpu  → 并行度临时降到 parallel(2) + 注入限速(50MB/s)
  否则                     → 正常执行
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time

import core.db as db

DEFAULTS = {
    "source_guard_enabled": "0",          # 默认关闭：存量部署零破坏
    "source_guard_block_pct": "70",
    "source_guard_throttle_pct": "50",
    "source_guard_throttle_kbps": "51200",  # 50 MB/s（KB/s 口径，与任务字段一致）
    "source_guard_parallel": "2",
}
_NUM_KEYS = ("source_guard_block_pct", "source_guard_throttle_pct",
             "source_guard_throttle_kbps", "source_guard_parallel")

# 一次 SSH 会话内完成双采样，避免两次握手放大抖动
_PROBE = (
    "head -1 /proc/stat; sleep 0.35; head -1 /proc/stat; "
    "cat /proc/loadavg; nproc 2>/dev/null || getconf _NPROCESSORS_ONLN"
)

_THROTTLE_TTL = 300.0          # 降级状态 5 分钟后自动恢复
_lock = threading.Lock()
_throttle_until = 0.0
_throttle_parallel = 0
_TABLE_READY = False


# ---------------------------------------------------------------- 配置
def get_config() -> dict:
    out = {}
    for k, default in DEFAULTS.items():
        try:
            v = db.get_system_config(k)
        except Exception:
            v = None
        out[k] = default if v in (None, "") else str(v)
    return out


def set_config(data: dict) -> dict:
    """写入配置；非法数值拒绝，enabled 只接受 0/1。"""
    data = data or {}
    if "source_guard_enabled" in data:
        on = str(data["source_guard_enabled"]).strip().lower() in ("1", "true", "on", "yes")
        db.set_system_config("source_guard_enabled", "1" if on else "0")
    for k in _NUM_KEYS:
        if k not in data:
            continue
        try:
            n = float(data[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} 必须是数值")
        if n < 0 or (k.endswith("_pct") and n > 100):
            raise ValueError(f"{k} 超出合理范围")
        db.set_system_config(k, str(int(n)))
    return get_config()


# ---------------------------------------------------------------- 样本表
def _ensure_tables():
    global _TABLE_READY
    if _TABLE_READY:
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS source_load_samples ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id INTEGER, host TEXT, ts TEXT, hour INTEGER, "
        "cpu_pct REAL, load1 REAL, source TEXT)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_load_host_hour "
        "ON source_load_samples(host, hour)")
    _TABLE_READY = True


def record_sample(task: dict, sample: dict):
    """持久化一次采样，供负载周期学习使用；失败不影响备份主流程。"""
    if not sample or sample.get("cpu_pct") is None:
        return
    try:
        _ensure_tables()
        now = _dt.datetime.now()
        db.execute(
            "INSERT INTO source_load_samples "
            "(task_id, host, ts, hour, cpu_pct, load1, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task.get("id"), sample.get("host") or "", now.isoformat(timespec="seconds"),
             now.hour, float(sample["cpu_pct"]), sample.get("load1"),
             sample.get("source") or ""))
    except Exception:
        pass


# ---------------------------------------------------------------- CPU 采集
def _cpu_fields(line: str) -> list:
    parts = line.split()
    vals = []
    for p in parts[1:9]:
        try:
            vals.append(float(p))
        except ValueError:
            vals.append(0.0)
    while len(vals) < 8:
        vals.append(0.0)
    return vals


def _parse_probe(text: str) -> dict:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    cpu_lines = [l for l in lines if l.startswith("cpu ")]
    cpu_pct = None
    if len(cpu_lines) >= 2:
        a, b = _cpu_fields(cpu_lines[0]), _cpu_fields(cpu_lines[1])
        total = sum(b) - sum(a)
        # idle + iowait 视为空闲（iowait 期间 CPU 未执行指令）
        idle = (b[3] + b[4]) - (a[3] + a[4])
        if total > 0:
            cpu_pct = round(max(0.0, min(100.0, (1.0 - idle / total) * 100.0)), 1)
    load1, cores = None, None
    for line in lines:
        if line.startswith("cpu "):
            continue
        if "/" in line:
            try:
                load1 = float(line.split()[0])
            except (ValueError, IndexError):
                pass
        elif line.isdigit():
            cores = int(line)
    if cpu_pct is None and load1 is not None and cores:
        # 双采样解析失败时用 1 分钟负载均值估算（如实标注为估算）
        cpu_pct = round(min(100.0, load1 / cores * 100.0), 1)
    return {"cpu_pct": cpu_pct, "load1": load1, "cores": cores,
            "estimated": len(cpu_lines) < 2 and cpu_pct is not None}


def _local_probe_text() -> str:
    with open("/proc/stat", "r", encoding="utf-8") as f:
        first = f.readline().strip()
    time.sleep(0.35)
    with open("/proc/stat", "r", encoding="utf-8") as f:
        second = f.readline().strip()
    with open("/proc/loadavg", "r", encoding="utf-8") as f:
        load = f.read().strip()
    return "\n".join([first, second, load, str(os.cpu_count() or 1)])


def sample_cpu(task: dict, timeout: int = 8) -> dict:
    """只读采集源端 CPU 使用率。远端走 SSH，本机任务直接读 /proc。

    远端 SSH 不可达时返回 error，由 evaluate() fail-open；**不会**拿平台机
    自己的 CPU 冒充远端，避免误拒/误放。
    """
    host = str((task or {}).get("host") or "127.0.0.1")
    text, source, err = "", "local", ""
    try:
        from core import remote_dump
        ssh = remote_dump.resolve_ssh_host(dict(task or {}))
    except Exception as e:
        ssh, err = None, f"SSH 主机解析失败: {e}"
    if err:
        return {"host": host, "cpu_pct": None, "source": "unavailable", "error": err}

    if ssh is not None:
        cli = None
        try:
            cli = remote_dump._connect_isolated(ssh)
            _, out, _ = cli.exec_command(_PROBE, timeout=timeout)
            text = out.read().decode("utf-8", "replace")
            source = "ssh"
        except Exception as e:
            return {"host": host, "cpu_pct": None, "source": "unavailable",
                    "error": f"SSH 采样失败: {e}"}
        finally:
            if cli is not None:
                try:
                    cli.close()
                except Exception:
                    pass
    else:
        # 回环/平台同机：本机 /proc 就是源端真实负载
        try:
            text = _local_probe_text()
        except Exception as e:
            return {"host": host, "cpu_pct": None, "source": "unavailable",
                    "error": f"本机采样失败: {e}"}

    data = _parse_probe(text)
    data.update({"host": host, "source": source,
                 "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                 "error": "" if data["cpu_pct"] is not None else "CPU 数据无法解析"})
    return data


# ---------------------------------------------------------------- 决策
def _mark_throttled(parallel: int):
    global _throttle_until, _throttle_parallel
    with _lock:
        _throttle_parallel = max(1, int(parallel))
        _throttle_until = time.monotonic() + _THROTTLE_TTL


def current_parallel_cap(default: int) -> int:
    """降级窗口内的有效并发上限（供 _ConcurrencyController.limit 调用）。"""
    with _lock:
        if time.monotonic() < _throttle_until and _throttle_parallel > 0:
            return max(1, min(int(default), _throttle_parallel))
    return max(1, int(default))


def evaluate(task: dict, force: bool = False, sample: dict = None) -> dict:
    """决策端：返回 ok / throttle / block / disabled / unknown。"""
    cfg = get_config()
    if cfg["source_guard_enabled"] != "1" and not force:
        return {"level": "disabled", "cpu_pct": None,
                "message": "源端资源保护未启用"}
    s = sample or sample_cpu(task)
    record_sample(task, s)
    if s.get("cpu_pct") is None:
        return {"level": "unknown", "cpu_pct": None, "sample": s,
                "fail_open": True,
                "message": f"源端负载采集失败，放行备份（不阻断）: {s.get('error') or '未知原因'}"}
    pct = float(s["cpu_pct"])
    block = float(cfg["source_guard_block_pct"])
    throttle = float(cfg["source_guard_throttle_pct"])
    base = {"cpu_pct": pct, "sample": s, "config": cfg}
    if pct > block:
        return {**base, "level": "block",
                "message": f"源端 CPU {pct}% > 拒绝阈值 {block}%，为保护生产系统拒绝本次备份"}
    if pct >= throttle:
        _mark_throttled(int(float(cfg["source_guard_parallel"])))
        return {**base, "level": "throttle",
                "throttle_kbps": int(float(cfg["source_guard_throttle_kbps"])),
                "parallel": int(float(cfg["source_guard_parallel"])),
                "message": (f"源端 CPU {pct}% 处于 {throttle}~{block}%，"
                            f"自动降并行至 {cfg['source_guard_parallel']} 并限速 "
                            f"{cfg['source_guard_throttle_kbps']} KB/s")}
    return {**base, "level": "ok",
            "message": f"源端 CPU {pct}% 正常（< {throttle}%）"}


def guard_before_backup(task: dict, operator: str = None) -> dict:
    """执行端挂接：block 写 failed 记录 + 审计；throttle 返回本次限速值。"""
    v = evaluate(task)
    if v["level"] == "block":
        now = db.now_iso()
        rec_id = None
        try:
            from core import models
            rec_id = models.create_record({
                "task_id": task.get("id"), "db_type": task.get("db_type"),
                "backup_type": task.get("backup_type") or "FULL",
                "started_at": now, "status": "failed",
            })
            db.execute(
                "UPDATE backup_records SET finished_at=?, message=?, size_bytes=0 "
                "WHERE id=?", (now, v["message"], rec_id))
            models.set_task_status(task["id"], now, "failed")
        except Exception:
            pass
        try:
            db.add_log("WARNING", "system",
                       f"[source_guard] 任务 {task.get('id')} 被拒绝: {v['message']}"
                       f"（operator={operator or '-'}）")
        except Exception:
            pass
        return {"blocked": True, "verdict": v,
                "record": {"id": rec_id, "status": "failed", "message": v["message"],
                           "source_guard": v["level"]}}
    return {"blocked": False, "verdict": v,
            "throttle_kbps": v.get("throttle_kbps")}


def check_api_guard(task_id: int) -> dict:
    """API 端实时复查：>70% 直接 400 拒绝；50~70% 由执行端自动降级。"""
    try:
        from core import models
        task = models.get_task(int(task_id), include_secret=True)
    except Exception:
        return {"blocked": False, "level": "unknown"}
    if not task:
        return {"blocked": False, "level": "unknown"}
    v = evaluate(task)
    return {"blocked": v["level"] == "block", "level": v["level"],
            "cpu_pct": v.get("cpu_pct"), "message": v.get("message"),
            "throttle": v["level"] == "throttle"}


# ---------------------------------------------------------------- 离线负载周期学习
def recommend_window(task_id: int = None, host: str = None,
                     days: int = 30, min_samples: int = 5) -> dict:
    """按主机+小时聚合历史 CPU 样本，推荐平均负载最低的备份小时。

    纯统计、零公网依赖；样本不足时如实返回 ok=False，不编造窗口。
    """
    try:
        _ensure_tables()
        cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))) \
            .isoformat(timespec="seconds")
        where, args = ["ts >= ?"], [cutoff]
        if task_id:
            where.append("task_id = ?")
            args.append(int(task_id))
        if host:
            where.append("host = ?")
            args.append(str(host))
        rows = db.query(
            "SELECT hour, AVG(cpu_pct) AS avg_cpu, COUNT(*) AS n "
            "FROM source_load_samples WHERE " + " AND ".join(where) +
            " GROUP BY hour ORDER BY hour", tuple(args)) or []
    except Exception as e:
        return {"ok": False, "reason": f"样本查询失败: {e}", "samples": 0}
    by_hour = [{"hour": int(r["hour"]), "avg_cpu_pct": round(float(r["avg_cpu"]), 1),
                "samples": int(r["n"])} for r in rows]
    total = sum(x["samples"] for x in by_hour)
    if total < int(min_samples):
        return {"ok": False, "reason": f"样本不足（{total}/{min_samples}），继续采集后再推荐",
                "samples": total, "min_samples": int(min_samples),
                "by_hour": by_hour}
    best = min(by_hour, key=lambda x: x["avg_cpu_pct"])
    return {
        "ok": True, "best_hour": best["hour"],
        "cron": f"0 {best['hour']} * * *",
        "avg_cpu_pct": best["avg_cpu_pct"], "samples": total,
        "by_hour": by_hour,
        "method": "离线按小时均值统计（无 LLM、无公网依赖）",
    }


def apply_window(task_id: int, cron: str = None) -> dict:
    """把推荐窗口写到任务 cron 并重载调度；样本不足时拒绝，不伪装成功。"""
    rec = recommend_window(task_id=task_id)
    if not rec.get("ok"):
        return {"ok": False, "reason": rec.get("reason"), "samples": rec.get("samples", 0)}
    expr = str(cron or rec["cron"]).strip()
    try:
        from core import models
        models.update_task(int(task_id), {"schedule_type": "cron", "cron_expr": expr})
    except Exception as e:
        return {"ok": False, "reason": f"写入任务调度失败: {e}"}
    try:
        from core import scheduler
        scheduler.reload_scheduler()
    except Exception:
        pass
    return {"ok": True, "cron_expr": expr, "best_hour": rec["best_hour"],
            "avg_cpu_pct": rec["avg_cpu_pct"], "samples": rec["samples"]}
