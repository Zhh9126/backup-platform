# -*- coding: utf-8 -*-
"""压缩级别统一策略（对标 DBackup 的 29 级压缩）。

任务 compress_level 允许 1~29，映射到实际算法与级别：
  1~8   → gzip 级别 1~8      （极速低压缩 → 高压缩）
  9~22  → zstd 级别 1~14     （zstd 高速高比，产物 .zst，恢复端已支持）
  23~29 → zstd 级别 15~19    （低速极高压缩）
0 = 不压缩（兼容旧任务）。
"""
import os

MAX_LEVEL = 29


def resolve(level: int) -> dict:
    """返回 {"algo": "none|gzip|zstd", "level": int, "ext": ""|.gz|.zst}。"""
    level = int(level or 0)
    if level <= 0:
        return {"algo": "none", "level": 0, "ext": ""}
    if level <= 8:
        return {"algo": "gzip", "level": level, "ext": ".gz"}
    if level <= 22:
        return {"algo": "zstd", "level": max(1, level - 8), "ext": ".zst"}
    return {"algo": "zstd", "level": min(19, 15 + (level - 23)), "ext": ".zst"}


def zstd_available() -> bool:
    """产物压缩用 zstd 命令（恢复端解压已支持 .zst）。"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isfile(os.path.join(d, "zstd")):
            return True
    return os.path.isfile("/usr/bin/zstd") or os.path.isfile("/usr/local/bin/zstd")


def normalize_task_level(raw) -> int:
    """任务表单/元库中的压缩级 → 0~29（非法回退 0）。"""
    try:
        v = int(raw or 0)
    except Exception:
        return 0
    return max(0, min(MAX_LEVEL, v))
