# -*- coding: utf-8 -*-
"""出站网络目标安全校验（SSRF 防护）。

平台运行在完全离线环境，Webhook / KMS / 恢复验证检查项等出站目标通常是
**内网私网地址**，因此私网默认允许；但回环地址、链路本地地址与云元数据
地址必须拒绝，否则任一能改配置的登录用户都可借平台进程探测本机服务或
读取云上实例元数据凭据。

用法::

    from core import net_guard
    ok, reason = net_guard.validate_url(url)
    if not ok:
        raise ValueError(reason)

设计边界（如实）：
* 仅做「目标地址」校验，不做 DNS rebinding 的持续绑定校验；域名解析成功时
  校验解析出的每一个 IP，解析失败时按离线环境常见情况放行并记录原因。
* 不拦截重定向后的目标，调用方如需严格防护应禁用自动重定向。
"""
import ipaddress
import socket
from urllib.parse import urlparse

# 云元数据与链路本地网段（含 IPv6）
_BLOCKED_NETS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
)
_METADATA_HOSTS = (
    "metadata.google.internal", "metadata", "instance-data",
)


def _ips_of(host: str):
    """返回主机名解析出的 IP 列表；解析失败返回空列表（离线环境常见）。"""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        out = []
        for info in infos:
            try:
                out.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        return out
    except Exception:
        return []


def validate_ip(ip, *, allow_private=True, allow_loopback=False) -> tuple:
    """校验单个 IP 是否允许作为出站目标，返回 (ok, reason)。"""
    if ip.is_loopback and not allow_loopback:
        return False, "禁止访问回环地址 %s" % ip
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False, "禁止访问保留/组播地址 %s" % ip
    for net in _BLOCKED_NETS:
        if ip in net:
            return False, "禁止访问链路本地/元数据网段 %s" % ip
    if not allow_private and ip.is_private:
        return False, "禁止访问私网地址 %s" % ip
    return True, ""


def validate_url(url: str, *, allow_private=True,
                 allow_loopback=False) -> tuple:
    """校验 http(s) URL 是否可作为出站目标，返回 (ok, reason)。"""
    u = str(url or "").strip()
    if not u:
        return False, "URL 为空"
    try:
        o = urlparse(u)
    except Exception as e:
        return False, "URL 解析失败: %s" % e
    if o.scheme.lower() not in ("http", "https"):
        return False, "仅允许 http/https 出站，拒绝 %s" % (o.scheme or "空协议")
    host = (o.hostname or "").strip().lower().strip("[]")
    if not host:
        return False, "URL 缺少主机名"
    if host in _METADATA_HOSTS:
        return False, "禁止访问云元数据主机名 %s" % host
    try:
        if o.port is not None and not (1 <= o.port <= 65535):
            return False, "非法端口 %s" % o.port
    except ValueError:
        return False, "URL 端口非法"
    ips = _ips_of(host)
    if not ips:
        # 离线内网可能无 DNS；域名无法解析时放行，但字面量 IP 必须可判定
        try:
            ipaddress.ip_address(host)
            return False, "无法判定目标地址 %s" % host
        except ValueError:
            return True, ""
    for ip in ips:
        ok, reason = validate_ip(ip, allow_private=allow_private,
                                 allow_loopback=allow_loopback)
        if not ok:
            return False, reason
    return True, ""


def validate_tcp_target(host: str, port: int,
                        *, allow_private=True,
                        allow_loopback=False) -> tuple:
    """校验恢复验证的 tcp/ping 目标，返回 (ok, reason)。"""
    h = str(host or "").strip().lower().strip("[]")
    if not h:
        return False, "目标主机为空"
    try:
        p = int(port)
        if not (1 <= p <= 65535):
            return False, "非法端口 %s" % port
    except (TypeError, ValueError):
        return False, "非法端口 %s" % port
    if h in _METADATA_HOSTS:
        return False, "禁止访问云元数据主机名 %s" % h
    ips = _ips_of(h)
    if not ips:
        try:
            ipaddress.ip_address(h)
            return False, "无法判定目标地址 %s" % h
        except ValueError:
            return True, ""
    for ip in ips:
        ok, reason = validate_ip(ip, allow_private=allow_private,
                                 allow_loopback=allow_loopback)
        if not ok:
            return False, reason
    return True, ""
