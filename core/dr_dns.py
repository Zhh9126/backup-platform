# -*- coding: utf-8 -*-
"""DNS 记录管理（容灾切换的流量牵引可视化底座）。

管理 dnsmasq 配置中的 A 记录（域名 → IP），供容灾链路页可视化编辑：
  - 读取当前记录清单（domain / ip / 备注）
  - 增删改 A 记录（写配置 + 重载 dnsmasq）
  - 解析验证（切换后回读，确认流量牵引真实生效）

配置来源（优先级）：环境变量 AIDBM_DNSMASQ_CONF > /opt/dnsmasq/dnsmasq.conf
> /etc/dnsmasq.d/dr.conf。容器名默认 dnsmasq-test（兼容演练环境），
生产可用 AIDBM_DNSMASQ_CONTAINER 覆盖。
"""
import os
import re

DEFAULT_CONF_CANDIDATES = (
    os.environ.get("AIDBM_DNSMASQ_CONF", ""),
    "/opt/dnsmasq/dnsmasq.conf",
    "/etc/dnsmasq.d/dr.conf",
)
DEFAULT_CONTAINER = os.environ.get("AIDBM_DNSMASQ_CONTAINER", "dnsmasq-test")


def _conf_path():
    for c in DEFAULT_CONF_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    # 都不存在时返回第一个可写候选（首次创建）
    return DEFAULT_CONF_CANDIDATES[1]


def _reload():
    """重载 dnsmasq：容器化用 docker restart，systemd 用 systemctl reload。"""
    import subprocess
    container = DEFAULT_CONTAINER
    r = subprocess.run(["docker", "restart", container],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        # 容器不存在 → 尝试 systemd（dnsmasq yum 安装场景）
        r2 = subprocess.run(["systemctl", "restart", "dnsmasq"],
                            capture_output=True, text=True, timeout=30)
        if r2.returncode != 0:
            raise RuntimeError("dnsmasq 重载失败（docker 与 systemd 均未成功）")
    import time
    time.sleep(1.5)


def list_records() -> list:
    """解析配置中的 A 记录：[{domain, ip, type:"address"}]。"""
    path = _conf_path()
    records = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*address=/([^/]+)/(\S+)", line.strip())
                if m:
                    records.append({"domain": m.group(1), "ip": m.group(2),
                                    "type": "address"})
    return records


def resolve_domain(domain: str, server="127.0.0.1", port=53) -> str:
    """解析域名（UDP A 查询，零依赖），返回 IP 或空串。"""
    import socket
    import struct
    import random
    try:
        tid = random.randint(0, 65535)
        pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        for part in domain.split("."):
            pkt += bytes([len(part)]) + part.encode()
        pkt += b"\x00" + struct.pack(">HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(pkt, (server, port))
        data, _ = s.recvfrom(512)
        s.close()
        idx = 12
        while data[idx] != 0:
            idx += data[idx] + 1
        idx += 5
        answers = struct.unpack(">H", data[6:8])[0]
        for _ in range(answers):
            idx += 2
            rtype, _rc, _ttl, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
            idx += 10
            if rtype == 1 and rdlen == 4:
                return ".".join(str(b) for b in data[idx:idx + 4])
            idx += rdlen
    except Exception:
        pass
    # DNS 不可达时回退系统解析
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return ""


def upsert_record(domain: str, ip: str) -> dict:
    """新增/更新一条 A 记录并重载。"""
    if not domain or not ip:
        raise ValueError("域名与 IP 均必填")
    path = _conf_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
    new_line = "address=/%s/%s" % (domain, ip)
    replaced = False
    for i, l in enumerate(lines):
        m = re.match(r"\s*address=/([^/]+)/", l)
        if m and m.group(1) == domain:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _reload()
    resolved = resolve_domain(domain)
    return {"domain": domain, "ip": ip, "replaced": replaced,
            "resolved": resolved,
            "verified": resolved == ip}


def delete_record(domain: str) -> dict:
    path = _conf_path()
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
    kept, removed = [], []
    for l in lines:
        m = re.match(r"\s*address=/([^/]+)/", l)
        if m and m.group(1) == domain:
            removed.append(l)
        else:
            kept.append(l)
    if not removed:
        raise ValueError("记录不存在: %s" % domain)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    _reload()
    return {"domain": domain, "removed": len(removed)}
