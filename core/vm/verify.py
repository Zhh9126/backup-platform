# -*- coding: utf-8 -*-
"""自动恢复验证（Auto Recovery Verification）——对应 Veeam SureBackup 的核心理念。

「备份完成」≠「可以恢复」。本模块在**隔离网络**里把恢复点真正拉起一台 VM，
执行真实健康检查（心跳、端口、HTTP、自定义脚本），确认这是一份**可用**的备份，
随后销毁验证 VM。整个过程不改变生产环境，也不需要人工值守。

所有检查都是真实动作，不允许「默认通过」：
* 拿不到 IP / ping 不通 / 端口不通 / 脚本非零退出码 → 判定失败，并写清原因。
"""
import re
import socket
import subprocess
import time
from typing import List

from core.vm.types import CloneSpec, HealthCheckResult, VerifyResult

# 心跳等待默认值：VM 起 OS + 服务就绪通常需要 60~180 秒
DEFAULT_HEARTBEAT_TIMEOUT = 300
# Windows 常见远程端口，ping 被禁时用它做可达性回退
_FALLBACK_PORTS = {False: 22, True: 3389}


def run_platform_check(item: str, ip_hint: str = "") -> HealthCheckResult:
    """在**备份平台侧**执行一次健康检查（不依赖 guest agent）。

    支持的写法：
        ping / ping:<ip>          —— ICMP 探测（失败回退 TCP 22/3389）
        tcp:<ip>:<port>           —— TCP 建连探测
        url:<http(s)://...>       —— HTTP(S) 请求，状态码 <400 视为通过
        exec:<cmd>                —— 默认禁用；仅当开启 vm_allow_exec_checks 且命中白名单命令时执行
    """
    name = str(item or "").strip()
    started = time.monotonic()

    def _done(ok: bool, msg: str) -> HealthCheckResult:
        return HealthCheckResult(name=name, ok=ok, message=msg,
                                 duration_sec=round(time.monotonic() - started, 2))

    try:
        if name.startswith("exec:"):
            # 安全整改：检查项可由 API 请求体 / 任务 extra 传入，不能把请求
            # 内容交给 shell=True（等于任意命令执行）。默认禁用；显式开启时
            # 也只允许白名单二进制 + 参数列表，并拒绝所有 shell 元字符。
            cmd = name[len("exec:"):].strip()
            try:
                import core.db as _cfg_db
                allow = str(_cfg_db.get_system_config(
                    "vm_allow_exec_checks") or "").lower() in ("1", "true", "on")
            except Exception:
                allow = False
            if not allow:
                return _done(False, "exec: 检查项已禁用（平台不允许执行任意命令）")
            import shlex
            if any(ch in cmd for ch in (";", "|", "&", "$", "`", ">", "<",
                                        "\n", "\r")):
                return _done(False, "exec: 含 shell 元字符，已拒绝执行")
            try:
                argv = shlex.split(cmd)
            except ValueError as e:
                return _done(False, "exec: 命令解析失败: %s" % e)
            allowed = ("ping", "curl", "nc", "true", "test")
            if not argv or argv[0].rsplit("/", 1)[-1] not in allowed:
                return _done(False, "exec: 仅允许白名单命令 %s" % ", ".join(allowed))
            p = subprocess.run(argv, shell=False, capture_output=True, timeout=300)
            tail = (p.stdout.decode("utf-8", "ignore") +
                    p.stderr.decode("utf-8", "ignore")).strip()[-200:]
            return _done(p.returncode == 0,
                         "退出码 %d %s" % (p.returncode, tail))

        if name.startswith("url:"):
            url = name[len("url:"):].strip()
            # 内网私网目标允许（VM 健康检查常见），但回环/元数据/非 http(s)
            # 一律拒绝，避免借平台进程探测本机服务或读取云元数据。
            from core import net_guard
            ok, reason = net_guard.validate_url(url)
            if not ok:
                return _done(False, "url: 目标被拒绝（%s）" % reason)
            from core.vm import http as vmhttp
            r = vmhttp.request("GET", url, timeout=30)
            return _done(r["ok"], "HTTP %s %s" % (r.get("status"), url))

        if name.startswith("tcp:"):
            parts = name.split(":")
            if len(parts) < 3:
                return _done(False, "tcp 检查格式应为 tcp:<ip>:<port>")
            host = parts[1] or ip_hint
            port = int(parts[2])
            from core import net_guard
            ok, reason = net_guard.validate_tcp_target(host, port)
            if not ok:
                return _done(False, "tcp: 目标被拒绝（%s）" % reason)
            return _tcp_check(_done, host, port)

        if name == "ping" or name.startswith("ping:"):
            host = name[len("ping:"):].strip() if ":" in name else ip_hint
            if not host:
                return _done(False, "未提供 IP，无法 ping")
            ok, msg = _icmp(host)
            if ok:
                return _done(True, msg)
            # ICMP 常被禁：回退常见远程端口建连
            r = _tcp_check(_done, host, _FALLBACK_PORTS[False])
            if r.ok:
                return _done(True, "ICMP 不通，TCP %d 可达 → 视为心跳通过" % 22)
            return _done(False, "ICMP 不通（%s）；TCP 22 亦不可达" % msg)
    except subprocess.TimeoutExpired:
        return _done(False, "检查超时（300s）")
    except Exception as e:
        return _done(False, "检查执行异常: %s" % str(e)[:200])

    return _done(False, "未知的检查项: %s" % name)


def _tcp_check(done, host: str, port: int, timeout: int = 5) -> HealthCheckResult:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return done(True, "TCP %s:%d 可连接" % (host, port))
    except Exception as e:
        return done(False, "TCP %s:%d 不可达: %s" % (host, port, str(e)[:120]))


def _icmp(host: str, count: int = 2) -> tuple:
    """平台侧 ICMP 探测，返回 (ok, msg)。"""
    try:
        p = subprocess.run(
            ["ping", "-c", str(count), "-W", "3", host],
            capture_output=True, timeout=(count * 4 + 5))
        if p.returncode == 0:
            m = re.search(r"time[=<]\s*([\d.]+)\s*ms",
                          p.stdout.decode("utf-8", "ignore"))
            return True, "ICMP 通%s" % (("，平均延迟 %sms" % m.group(1)) if m else "")
        return False, "退出码 %d" % p.returncode
    except FileNotFoundError:
        return False, "平台未安装 ping"
    except Exception as e:
        return False, str(e)[:120]


def default_checks(guest_is_windows: bool = False, ip: str = "") -> List[str]:
    """默认检查项：心跳（OS 起来）+ 远程管理端口（服务起来了）。"""
    port = _FALLBACK_PORTS[bool(guest_is_windows)]
    return ["power_on", "ping:%s" % ip if ip else "ping", "tcp:%s:%d" % (ip, port)]


def wait_heartbeat(provider, target_ref: str, node: str = "",
                   timeout_sec: int = DEFAULT_HEARTBEAT_TIMEOUT):
    """等待验证 VM 拿到 IP（VM Tools / QEMU GA / KVP 上报）。返回 (ip, waited)。"""
    waited = 0
    step = 10
    while waited < timeout_sec:
        try:
            ip = provider.guest_ip(target_ref, node) or ""
        except Exception:
            ip = ""
        if ip:
            return ip, waited
        time.sleep(step)
        waited += step
    return "", waited


def run_verify(provider, chain_artifacts: List[str], spec: CloneSpec,
               checks: List[str] = None, node: str = "", logger=None,
               heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
               cleanup: bool = True) -> VerifyResult:
    """从恢复点克隆一台隔离 VM → 真实健康检查 → 销毁验证 VM。

    Args:
        provider: VMProvider 实例
        chain_artifacts: 链上产物（平台侧或远端路径，取决于 Provider）
        spec: 克隆规格（隔离网络必须开启，否则不允许验证——会污染生产网络）
    """
    started = time.monotonic()
    result = VerifyResult()
    if not spec.isolate_network:
        result.message = "恢复验证必须在隔离网络中进行（禁止接入生产网络）"
        return result

    try:
        clone = provider.clone_to_new_vm("", list(chain_artifacts), spec)
    except Exception as e:
        result.message = "从恢复点拉起验证 VM 失败: %s" % str(e)[:300]
        return result
    if not clone.ok:
        result.message = "从恢复点拉起验证 VM 失败: %s" % (clone.message or "克隆未成功")
        return result

    result.target_ref = clone.target_ref
    target = clone.target_ref or clone.target_name
    ip, waited = wait_heartbeat(provider, target, node, heartbeat_timeout)
    ip = ip or clone.ip

    ck = list(checks) if checks else default_checks(ip=ip)
    ck = [c.replace("<ip>", ip) if ip else c for c in ck]
    try:
        results = provider.health_checks(target, node or spec.target_node, ck,
                                         timeout_sec=heartbeat_timeout)
    except Exception as e:
        results = [HealthCheckResult(name="health_checks", ok=False,
                                     message="健康检查执行异常: %s" % str(e)[:200])]
    if ip:
        results.insert(0, HealthCheckResult(
            name="heartbeat", ok=True,
            message="VM 已上报 IP %s（等待 %ds）" % (ip, waited)))
    else:
        results.insert(0, HealthCheckResult(
            name="heartbeat", ok=False,
            message="等待 %ds 未拿到 IP（VM Tools/GA 未上报，后续网络检查可能失败）"
                    % waited))
    result.checks = results
    result.ok = bool(ip) and all(c.ok for c in results)
    failed = [c for c in results if not c.ok]
    result.message = ("恢复验证通过：%d/%d 项检查成功"
                      % (len(results) - len(failed), len(results))) if result.ok \
        else ("恢复验证失败：" + "; ".join("%s(%s)" % (c.name, c.message)
                                        for c in failed[:5]))

    if cleanup and result.target_ref:
        try:
            provider.delete_vm(result.target_ref, node or spec.target_node)
            result.cleaned = True
        except Exception as e:
            result.cleaned = False
            result.message += "（验证 VM 销毁失败，请手动回收: %s）" % str(e)[:120]
    result.duration_sec = round(time.monotonic() - started, 2)
    if logger:
        logger.info("[vm.verify] %s", result.message)
    return result
