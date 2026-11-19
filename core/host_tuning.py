# -*- coding: utf-8 -*-
"""备份服务器内核/IO 调优诊断（对标 PB 级方案 §3.2）。

只做**只读检查 + 建议**，绝不自动改系统参数：改 sysctl / IO 调度器会
影响机器上所有业务，必须由运维在维护窗口执行（页面给出可直接复制的命令）。

检查项：
  1. 关键 sysctl（脏页回写、swappiness、连接队列、socket 缓冲、max_map_count）
  2. 磁盘 IO 调度器（NVMe 建议 none，HDD 建议 mq-deadline）
  3. io_uring 可用性（异步 IO，顺序写吞吐关键）
  4. 文件系统与挂载选项（noatime 可减少元数据写）
"""
import os
import platform
import subprocess

# 项 → (推荐值, 比较方式, 说明)
RECOMMENDED_SYSCTL = {
    "vm.dirty_background_ratio": {"recommend": "5", "op": "le",
                                  "hint": "脏页后台回写阈值，过大易造成写抖动"},
    "vm.dirty_ratio": {"recommend": "15", "op": "le",
                       "hint": "进程同步回写阈值，过大时 fsync 停顿明显"},
    "vm.swappiness": {"recommend": "1", "op": "le",
                      "hint": "备份机应尽量不用 swap，避免 IO 抖动"},
    "vm.max_map_count": {"recommend": "262144", "op": "ge",
                         "hint": "重删索引/大文件 mmap 需要较多映射区"},
    "net.core.somaxconn": {"recommend": "65535", "op": "ge",
                           "hint": "高并发连接积压队列"},
    "net.core.rmem_max": {"recommend": "67108864", "op": "ge",
                          "hint": "WAN 复制的大窗口读缓冲"},
    "net.core.wmem_max": {"recommend": "67108864", "op": "ge",
                          "hint": "WAN 复制的大窗口写缓冲"},
}


def _read_sysctl(key: str):
    path = "/proc/sys/" + key.replace(".", "/")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _cmp(actual, recommend, op):
    try:
        a = int(str(actual).split()[0])
        r = int(recommend)
    except (TypeError, ValueError):
        return None
    if op == "le":
        return a <= r
    if op == "ge":
        return a >= r
    return a == r


def check_sysctl():
    out = []
    for key, spec in RECOMMENDED_SYSCTL.items():
        actual = _read_sysctl(key)
        ok = _cmp(actual, spec["recommend"], spec["op"])
        out.append({
            "item": key,
            "actual": actual if actual is not None else "(不可读)",
            "recommend": ("≤" if spec["op"] == "le" else "≥") + spec["recommend"],
            "ok": ok,
            "hint": spec["hint"],
            "apply_cmd": f"sysctl -w {key}={spec['recommend']}",
        })
    return out


def _is_rotational(dev: str):
    try:
        with open(f"/sys/block/{dev}/queue/rotational", "r") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def check_io_scheduler():
    """逐块设备读取当前调度器并给出建议（NVMe→none，机械→mq-deadline）。"""
    out = []
    try:
        devices = [d for d in os.listdir("/sys/block")
                   if not d.startswith(("loop", "ram", "dm-", "sr"))]
    except OSError:
        return out
    for dev in sorted(devices):
        sched_path = f"/sys/block/{dev}/queue/scheduler"
        try:
            with open(sched_path, "r") as f:
                raw = f.read().strip()
        except OSError:
            continue
        cur = None
        for tok in raw.split():
            if tok.startswith("["):
                cur = tok.strip("[]")
                break
        rot = _is_rotational(dev)
        if rot is None:
            continue
        want = "mq-deadline" if rot else "none"
        out.append({
            "item": dev,
            "type": "HDD" if rot else "SSD/NVMe",
            "actual": cur or "(未知)",
            "recommend": want,
            "ok": (cur == want) if cur else None,
            "hint": "机械盘顺序写用 mq-deadline；NVMe 用 none 减少调度开销",
            "apply_cmd": f"echo {want} > {sched_path}",
        })
    return out


def check_io_uring():
    """io_uring 可用性：内核 ≥5.1 且未被禁用。"""
    kv = platform.release()
    disabled = None
    for p in ("/proc/sys/kernel/io_uring_disabled",):
        if os.path.exists(p):
            try:
                with open(p) as f:
                    disabled = f.read().strip()
            except OSError:
                pass
    major = minor = 0
    try:
        parts = kv.split("-")[0].split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass
    supported = (major, minor) >= (5, 1)
    enabled = supported and (disabled in (None, "0"))
    return {
        "item": "io_uring",
        "kernel": kv,
        "actual": ("可用" if enabled else
                   ("内核版本过低（<5.1）" if not supported else "已被 sysctl 禁用")),
        "recommend": "可用",
        "ok": enabled,
        "hint": "异步 IO 可显著提升顺序写吞吐；平台当前走同步 IO，不可用不影响功能",
        "apply_cmd": "sysctl -w kernel.io_uring_disabled=0" if supported else "",
    }


def check_mounts():
    """检查备份目录所在挂载是否启用 noatime（减少元数据写放大）。"""
    out = []
    try:
        with open("/proc/mounts", "r") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        dev, mnt, _fs, opts = parts[0], parts[1], parts[2], parts[3]
        if mnt not in ("/", "/data", "/backup") and not mnt.startswith("/opt"):
            continue
        out.append({
            "item": mnt,
            "actual": opts,
            "recommend": "含 noatime",
            "ok": "noatime" in opts.split(","),
            "hint": f"{dev} 的挂载选项；noatime 可减少读放大（需改 /etc/fstab 后 remount）",
            "apply_cmd": "",
        })
    return out


def _disk_usage():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round((total - free) / total * 100, 1) if total else 0
        return {"total_gb": round(total / 1024 ** 3, 1),
                "free_gb": round(free / 1024 ** 3, 1),
                "used_pct": used_pct,
                "ok": used_pct < 85}
    except OSError:
        return {}


def inspect():
    """汇总诊断结果：{summary, checks[], disk, platform}。"""
    checks = []
    checks.extend(check_sysctl())
    checks.extend(check_io_scheduler())
    checks.append(check_io_uring())
    checks.extend(check_mounts())

    bad = [c for c in checks if c.get("ok") is False]
    unknown = [c for c in checks if c.get("ok") is None]
    return {
        "summary": {
            "total": len(checks),
            "ok": len([c for c in checks if c.get("ok") is True]),
            "advice": len(bad),
            "unknown": len(unknown),
            "verdict": ("建议调优" if bad else "符合推荐"),
        },
        "checks": checks,
        "disk": _disk_usage(),
        "platform": {"kernel": platform.release(), "machine": platform.machine(),
                     "python": platform.python_version()},
    }
