# -*- coding: utf-8 -*-
"""运行环境检测：Docker 容器内外路径透明化。

背景：容器化部署时备份产物落在容器文件系统（如 /data/backups），用户在
宿主机上找不到，误以为备份丢失。本模块提供：

- ``in_docker()``：判断平台是否运行在容器内（/.dockerenv / cgroup 探测）；
- ``host_path_for(path)``：解析 /proc/self/mountinfo，找出该路径所在的
  bind mount / volume，返回宿主机对应路径（无挂载返回 None）；
- ``path_transparency(path)``：一次调用产出 UI/日志可直接展示的说明，
  「容器内路径 → 宿主机路径」或「未挂载卷的丢数据风险警告」。
"""
import os


def in_docker() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "/docker" in line or "/containerd" in line or "/kubepods" in line:
                    return True
    except Exception:
        pass
    return False


def _parse_mountinfo() -> list:
    """返回 [(mount_point_in_container, source_on_host)]，按挂载点长度降序。"""
    out = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # mountID parentID major:minor root mountpoint mountopts ...
                parts = line.split()
                if len(parts) < 5:
                    continue
                mount_point = parts[4]
                # optional 段里 source=xxx 为宿主机来源（bind mount / volume）
                source = ""
                for tok in parts[5:]:
                    if tok.startswith("source:"):
                        source = tok[len("source:"):]
                        break
                if source and source not in ("", "none"):
                    out.append((mount_point, source))
    except Exception:
        return []
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out


def host_path_for(path: str):
    """容器内路径 → 宿主机路径（bind mount/volume 前缀匹配，最长优先）。

    非容器环境返回 None（路径本身就是宿主机路径）；路径不在任何挂载点下
    也返回 None（位于容器可写层，容器删除即丢失）。
    """
    if not path or not in_docker():
        return None
    path = os.path.normpath(path)
    for mp, source in _parse_mountinfo():
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            rel = path[len(mp.rstrip("/")):]
            return (source.rstrip("/") + rel) or source
    return None


def path_transparency(path: str) -> dict:
    """产出 UI 可直接展示的路径说明。"""
    d = in_docker()
    hp = host_path_for(path) if (d and path) else None
    if not d:
        return {"in_docker": False, "path": path or "", "host_path": None,
                "mounted": None, "level": "ok", "hint": ""}
    if hp:
        return {"in_docker": True, "path": path or "", "host_path": hp,
                "mounted": True, "level": "ok",
                "hint": f"Docker 容器内路径。宿主机对应：{hp}（由 -v 挂载映射）"}
    return {"in_docker": True, "path": path or "", "host_path": None,
            "mounted": False, "level": "danger",
            "hint": ("该路径在 Docker 容器内部，且未检测到卷挂载——容器删除/重建后"
                     "备份将全部丢失！请在 docker run 增加 -v <宿主机目录>:/data "
                     "挂载数据目录，或在「备份存储管理」把存储位置改到已挂载的目录。")}


def warn_unmounted_backup_root(backup_root: str) -> str:
    """启动检查：容器内 + 备份根目录未挂载 → 返回警告文本（无风险返回空）。"""
    info = path_transparency(backup_root)
    return info["hint"] if info["level"] == "danger" else ""
