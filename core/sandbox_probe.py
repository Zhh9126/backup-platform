# -*- coding: utf-8 -*-
"""净室演练沙箱能力探测（对标 PB 级方案 §6，只探测不启用）。

方案里的净室沙箱依赖三样东西：
- **Network Namespace 隔离**（`ip netns` + 1:1 NAT + egress DROP）——需要 root
  与 CAP_NET_ADMIN；
- **Live Mount 块设备导出**（NBD 或 iSCSI）——需要内核模块与 root；
- **KVM 虚拟机底座**（QEMU/KVM + QMP）——需要 /dev/kvm。

AIDBM 的演练目前是**库级/文件级隔离**（恢复到临时库或临时目录），不依赖上述
能力。本模块只做"有没有"的诚实探测，供演练页标注，避免对客户承诺做不到的
隔离级别（比如声称"净室沙箱"实际只是异名库）。

探测原则：
- 只读优先；netns 需要真建一次才能确认权限，建完立即删除（失败也如实报）；
- 任何异常都归为"不可用 + 原因"，绝不抛给页面。
"""
import os
import platform
import shutil
import subprocess
import uuid

_PROBE_TIMEOUT = 8


def _run(cmd, timeout=_PROBE_TIMEOUT):
    """执行命令，返回 (rc, stdout+stderr)。异常统一成 rc=-1。"""
    try:
        p = subprocess.run(["/bin/bash", "-c", cmd] if isinstance(cmd, str) else cmd,
                           shell=False, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return -1, "命令不存在: %s" % cmd
    except subprocess.TimeoutExpired:
        return -2, "执行超时（%ss）" % timeout
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        return any(k in txt for k in ("docker", "kubepods", "containerd", "lxc"))
    except OSError:
        return False


def probe_netns() -> dict:
    """Network Namespace：命令可用 + 有权限创建（建完即删）。"""
    item = {"item": "netns", "name": "Network Namespace 隔离",
            "hint": "具备隔离网络的基础能力（平台的演练默认不启用，需显式开启）"}
    try:
        if not shutil.which("ip"):
            return dict(item, available=False,
                        reason="ip 命令不存在（iproute2 未安装）")
        rc, out = _run(["ip", "netns", "list"])
        if rc != 0:
            return dict(item, available=False,
                        reason="ip netns 不可用: %s" % out[:120])
        probe = "aidbm_probe_%s" % uuid.uuid4().hex[:8]
        rc2, out2 = _run(["ip", "netns", "add", probe])
        if rc2 != 0:
            return dict(item, available=False,
                        reason="无权限创建命名空间: %s" % out2[:120])
        _run(["ip", "netns", "delete", probe])      # 立即回滚，不留痕迹
        return dict(item, available=True, reason="可创建/删除命名空间")
    except Exception as exc:  # noqa: BLE001 - 探测绝不抛给页面
        return dict(item, available=False, reason="探测异常: %s" % str(exc)[:120])


def probe_kvm() -> dict:
    """/dev/kvm 是否存在且可读写（QEMU/KVM 底座）。"""
    p = "/dev/kvm"
    if not os.path.exists(p):
        return {"item": "kvm", "name": "KVM 虚拟机底座",
                "available": False, "reason": "%s 不存在（未开启虚拟化或未加载 kvm）" % p,
                "hint": "虚拟机级净室演练需要 KVM；AIDBM 当前演练为库级/文件级"}
    if not os.access(p, os.R_OK | os.W_OK):
        return {"item": "kvm", "name": "KVM 虚拟机底座",
                "available": False, "reason": "%s 存在但当前用户无读写权限" % p,
                "hint": "需要把运行用户加入 kvm 组或使用 root"}
    return {"item": "kvm", "name": "KVM 虚拟机底座",
            "available": True, "reason": "/dev/kvm 可读写",
            "hint": "具备虚拟机底座，但平台未接入 VM 净室（不承诺）"}


def probe_nbd() -> dict:
    """NBD 块设备（Live Mount 的一种导出方式）。"""
    devs = [d for d in ("/dev/nbd0", "/dev/nbd15") if os.path.exists(d)]
    mod = "/sys/module/nbd"
    has_mod = os.path.isdir(mod)
    if devs or has_mod:
        return {"item": "nbd", "name": "NBD 块设备导出（Live Mount）",
                "available": True,
                "reason": "设备或模块存在（%s）" % (devs[0] if devs else "nbd 模块已加载"),
                "hint": "平台未实现 NBD Live Mount（需内核模块 + 零拷贝映射），不作为承诺能力"}
    return {"item": "nbd", "name": "NBD 块设备导出（Live Mount）",
            "available": False, "reason": "无 /dev/nbd* 且 nbd 模块未加载",
            "hint": "modprobe nbd 可加载；平台未实现该能力"}


def probe_iscsi() -> dict:
    """iSCSI target 工具链（另一种 Live Mount 导出方式）。"""
    tool = shutil.which("targetcli") or shutil.which("tgtd")
    if tool:
        return {"item": "iscsi", "name": "iSCSI 目标导出", "available": True,
                "reason": "工具存在: %s" % tool,
                "hint": "平台未实现 iSCSI Live Mount，不作为承诺能力"}
    return {"item": "iscsi", "name": "iSCSI 目标导出", "available": False,
            "reason": "targetcli/tgtd 均未安装",
            "hint": "平台未实现该能力，安装工具也不会改变"}


def probe_sandbox_readiness() -> dict:
    """总览：能否做「隔离网络演练」。"""
    checks = [probe_netns(), probe_kvm(), probe_nbd(), probe_iscsi()]
    netns_ok = next(c for c in checks if c["item"] == "netns")["available"]
    kvm_ok = next(c for c in checks if c["item"] == "kvm")["available"]
    return {
        "checks": checks,
        "in_container": _in_container(),
        "kernel": platform.release(),
        "isolation_level": ("netns+kvm" if (netns_ok and kvm_ok)
                            else ("netns" if netns_ok else "db-level")),
        "current_drill_level": "db-level",
        "note": ("平台演练当前是**库级/文件级隔离**（恢复到临时库/临时目录后校验再销毁），"
                 "不依赖 netns/KVM/NBD。上面的探测只描述宿主机能力，不代表平台已启用"
                 "净室沙箱——若要启用需另行开发与评估。"),
    }
