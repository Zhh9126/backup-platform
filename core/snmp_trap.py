# -*- coding: utf-8 -*-
"""SNMP Trap 告警对接（P2，对标鼎甲 E9）。

纯标准库实现 SNMPv2c trap（BER 手工编码，零外部依赖——离线环境不依赖
net-snmp CLI / pysnmp）。挂接 core/webhooks.py 事件中心：告警事件同步
发 trap 到网管平台（Zabbix/NMS）。

OID 约定：
    sysUpTime.0    1.3.6.1.2.1.1.3.0   TimeTicks（平台进程运行秒 ×100）
    snmpTrapOID.0  1.3.6.1.6.3.1.1.4.1.0  OID = 1.3.6.1.4.1.<PEN>.aidbm.<sev>
    消息           1.3.6.1.4.1.<PEN>.1.1  OctetString（事件文本）
PEN（Private Enterprise Number）默认 60000+（私有实验段），system_config
snmp_enterprise 可覆盖。
"""
import socket
import struct
import time
from typing import List, Tuple

# ---- BER 基元编码 ----


def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = b""
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(content)) + content


def _enc_int(v: int) -> bytes:
    """非负整数 INTEGER（SNMP 场景内均为非负：request-id/err/timeticks）。"""
    v = int(v)
    if v == 0:
        return _tlv(0x02, b"\x00")
    out = b""
    while v:
        out = bytes([v & 0xFF]) + out
        v >>= 8
    if out[0] & 0x80:
        out = b"\x00" + out
    return _tlv(0x02, out)


def _enc_str(s: str) -> bytes:
    return _tlv(0x04, s.encode("utf-8", "replace"))


def _enc_oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.strip().split(".") if x != ""]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            body += bytes([p])
        else:
            chunk = b""
            while p:
                chunk = bytes([(p & 0x7F) | (0x80 if chunk else 0)]) + chunk
                p >>= 7
            body += chunk
    return _tlv(0x06, body)


def _int_bytes(v: int) -> bytes:
    v = int(v)
    if v == 0:
        return b"\x00"
    out = b""
    while v:
        out = bytes([v & 0xFF]) + out
        v >>= 8
    if out[0] & 0x80:
        out = b"\x00" + out
    return out


def _varbind(oid: str, val: bytes) -> bytes:
    return _tlv(0x30, _enc_oid(oid) + val)


def encode_v2c_trap(community: str, uptime_sec: int, trap_oid: str,
                    varbinds: List[Tuple[str, bytes]]) -> bytes:
    """构造完整 SNMPv2c trap 报文（TRAP-V2 PDU，tag 0xA7）。"""
    vb_list = [
        _varbind("1.3.6.1.2.1.1.3.0", _tlv(0x43, _int_bytes(int(uptime_sec) * 100))),
        _varbind("1.3.6.1.6.3.1.1.4.1.0", _enc_oid(trap_oid)),
    ] + [_varbind(oid, val) for oid, val in varbinds]
    pdu = _tlv(0xA7, _enc_int(1) + _enc_int(0) + _enc_int(0) + _tlv(0x30, b"".join(vb_list)))
    return _tlv(0x30, _enc_int(1) + _enc_str(community or "public") + pdu)


def send_trap(target: str, port: int, community: str, message: str,
              severity: str = "warning", enterprise: str = "1.3.6.1.4.1.60000",
              timeout: float = 2.0) -> dict:
    """发送一条告警 trap（UDP，fire-and-forget——网管不可达不阻塞告警链路）。

    返回 {sent: bool, detail}。真实送达以网管侧接收为准（UDP 无回执，
    本函数只保证报文已发出/socket 层错误如实上报）。
    """
    sev_oid = {"info": 1, "warning": 2, "error": 3, "critical": 4}.get(
        (severity or "warning").lower(), 2)
    trap_oid = f"{enterprise}.{1 + sev_oid}"
    varbinds = [
        (f"{enterprise}.1.1", _enc_str(message[:512])),
        (f"{enterprise}.1.2", _enc_str(severity)),
    ]
    try:
        pkt = encode_v2c_trap(community, int(time.time() - _BOOT_TS) or 1,
                              trap_oid, varbinds)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, (target, int(port)))
        s.close()
        return {"sent": True, "bytes": len(pkt), "target": f"{target}:{port}"}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "detail": str(e)[:200]}


_BOOT_TS = time.time()


# ---- 配置与挂接 ----

def snmp_config() -> dict:
    from core import db
    def _get(k, d):
        try:
            v = db.get_system_config(k)
            return v if v not in (None, "") else d
        except Exception:  # noqa: BLE001
            return d
    enabled = str(_get("snmp_enabled", "0")).strip().lower() in ("1", "true", "on", "yes")
    return {
        "enabled": enabled,
        "target": _get("snmp_target", ""),
        "port": int(_get("snmp_port", "162") or 162),
        "community": _get("snmp_community", "public"),
        "enterprise": _get("snmp_enterprise", "1.3.6.1.4.1.60000"),
    }


def set_snmp_config(enabled: bool, target: str, port: int = 162,
                    community: str = "public", enterprise: str = "1.3.6.1.4.1.60000") -> None:
    from core import db
    db.set_system_config("snmp_enabled", "1" if enabled else "0")
    db.set_system_config("snmp_target", target or "")
    db.set_system_config("snmp_port", str(port or 162))
    db.set_system_config("snmp_community", community or "public")
    db.set_system_config("snmp_enterprise", enterprise or "1.3.6.1.4.1.60000")


def notify_event(message: str, severity: str = "warning") -> dict:
    """webhooks 事件中心挂接点：SNMP 未启用/未配置目标时静默跳过。"""
    cfg = snmp_config()
    if not cfg["enabled"] or not cfg["target"]:
        return {"sent": False, "detail": "SNMP 告警未启用"}
    return send_trap(cfg["target"], cfg["port"], cfg["community"],
                     message, severity, cfg["enterprise"])
