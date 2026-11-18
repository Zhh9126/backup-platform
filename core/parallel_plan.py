"""单任务内部并行度（引擎级并行）解析与「总并行预算」治理。

为什么需要这个模块
------------------
平台层 ``max_concurrent_backups`` 控制的是「同时跑几个备份任务」，单任务内部
一直是串行（mysqldump / pg_dump / xtrabackup 都是单流）。当平台并发被配得
很小（现场常见 1~2，或 CPU 保护把上限压到 2）时，批量/组合任务的成员只能
排队，排队时间直接体现为 drift（成员完成时间偏离计划时间的幅度）。

所以并行能力要分两层看：

* 平台层：同时执行的任务数（max_concurrent_backups / BACKUP_WORKERS）；
* 引擎层：**单个任务内部**的并行（xtrabackup ``--parallel``、``pg_dump -j``、
  全实例逐库并发 dump）。

本模块把两层统一成一个预算，避免"要么串行慢、要么并发把生产库压垮"：

    平台并发上限 × 单任务引擎并行度 ≈ BACKUP_PARALLEL_TARGET（默认 8）

* 平台并发 = 2 → 引擎并行 4（core 级任务）；
* 平台并发 = 8 → 引擎并行 1（回归串行，总量不变）。

优先级（高 → 低）
-----------------
1. 任务 ``extra_options.engine_parallel``：显式指定 0-32（0 = 强制串行，
   便于"这台库不能再压"的现场兜底）。
2. 保护策略 ``backup_strategy.parallel``：该任务**允许**的最大并行度
   （core=4 / important=2 / general=1，见 core/policy.py）——策略是上限，
   自动计算值不会越过它。
3. 自动推算：ceil(BACKUP_PARALLEL_TARGET / 平台并发上限)。

``BACKUP_PARALLEL_TARGET=0`` 时关闭自动放大（引擎并行恒 1），等价于旧行为。
"""

from __future__ import annotations

import json
import os

import config

# 硬上限兜底：即便配置写错，也不让单任务开出几十路把源库打死
_HARD_MAX = 32


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def task_extra(task: dict) -> dict:
    """把 task.extra_options 解析为字典（dict / JSON 字符串 / 脏数据全兼容）。"""
    raw = (task or {}).get("extra_options")
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def platform_limit() -> int:
    """当前平台并发上限（含环境变量覆盖与 CPU 保护压低后的真实值）。"""
    try:
        from core import scheduler
        return max(1, int(scheduler._CONCURRENCY.limit()))
    except Exception:
        pass
    try:
        env_limit = int(getattr(config, "MAX_CONCURRENT_BACKUPS", 0) or 0)
        if env_limit > 0:
            return max(1, min(env_limit, 64))
        from core import db
        return max(1, min(int(db.get_system_config("max_concurrent_backups") or 2), 64))
    except Exception:
        return 2


def policy_parallel(task: dict) -> int:
    """该任务保护策略允许的最大并行度；解析失败按 1（保守）。"""
    try:
        from core.policy import policy_service
        pol = policy_service.resolve(task or {})
        return max(1, int((pol.get("backup_strategy") or {}).get("parallel", 1) or 1))
    except Exception:
        lvl = str((task or {}).get("protection_level") or "general").lower()
        return {"core": 4, "important": 2}.get(lvl, 1)


def cap() -> int:
    """单任务引擎并行硬上限。"""
    try:
        n = int(getattr(config, "BACKUP_PARALLEL_MAX", 8) or 8)
    except (TypeError, ValueError):
        n = 8
    return max(1, min(n, _HARD_MAX))


def target() -> int:
    """总并行预算（平台并发 × 引擎并行 的上界）；0 = 关闭自动放大。"""
    try:
        return max(0, min(int(getattr(config, "BACKUP_PARALLEL_TARGET", 8) or 0), 256))
    except (TypeError, ValueError):
        return 8


def resolve(task: dict = None) -> dict:
    """解析该任务本次应使用的引擎并行度。

    Returns:
        dict:
          workers       : int  引擎并行度（1 = 串行，保持旧行为）
          source        : str  task / auto / off
          platform_limit: int  当前平台并发上限
          target        : int  总并行预算
          cap           : int  硬上限
          policy        : int  策略允许上限
          label         : str  人读口径（写入消息/日志）
    """
    task = task or {}
    extra = task_extra(task)
    lim = platform_limit()
    tgt = target()
    hard = cap()
    pol = policy_parallel(task)

    workers, source = 1, "auto"
    raw = extra.get("engine_parallel", extra.get("parallel_workers"))
    if raw not in (None, ""):
        try:
            workers = int(raw)
            source = "task"
        except (TypeError, ValueError):
            workers, source = 1, "auto"

    if source != "task":
        if tgt <= 0 or lim >= tgt:
            workers = 1          # 平台并发已够：不额外加压
        else:
            workers = (tgt + lim - 1) // lim   # ceil 目标/并发
        workers = min(workers, pol)            # 策略是硬上限

    workers = max(1, min(int(workers), hard))
    return {
        "workers": workers,
        "source": source,
        "platform_limit": lim,
        "target": tgt,
        "cap": hard,
        "policy": pol,
        "label": mode_label(workers, lim, source),
    }


def mode_label(workers: int, limit: int = 0, source: str = "auto") -> str:
    """并行模式的人读文案（写入备份消息，便于解释 drift）。"""
    if workers and workers > 1:
        why = "任务指定" if source == "task" else "自动（平台并发 %s 路）" % (limit or "?")
        return "引擎并行 %d 路 · %s" % (workers, why)
    return "单流串行"


def note(task: dict = None, tool: str = "") -> str:
    """给备份结果消息追加的并行口径后缀，例如「[并行 引擎并行 4 路 · 自动…]」。"""
    plan = resolve(task)
    text = plan["label"]
    if tool and plan["workers"] > 1:
        text += " · %s" % tool
    return "（%s）" % text


def refresh_now() -> dict:
    """供 API/界面展示的实时快照（不做任务级解析）。"""
    lim = platform_limit()
    tgt = target()
    auto = 1 if (tgt <= 0 or lim >= tgt) else (tgt + lim - 1) // lim
    return {
        "platform_limit": lim,
        "target": tgt,
        "cap": cap(),
        "auto_workers": max(1, min(auto, cap())),
        "max_concurrent_backups": int(getattr(config, "MAX_CONCURRENT_BACKUPS", 0) or 0),
        "backup_workers": int(getattr(config, "BACKUP_WORKERS", 0) or 0),
    }
