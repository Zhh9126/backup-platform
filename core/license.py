# -*- coding: utf-8 -*-
"""AIDBM 系统授权与纳管配额管理模块。

- 社区免费版（未授权）：网络设备及各项备份任务（数据库、文件、网络设备、对象存储、虚拟机、AI 模型）最多纳管 5 个；
- 企业永久版（已授权）：通过与服务器物理 MAC 绑定的 RSA-2048 防伪授权码激活，全模块解除配额限制，无上限使用。
"""
import base64
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core import db

logger = logging.getLogger("aidbm.license")

PUB_KEY_PATH = Path(__file__).resolve().parent / "license_public.pem"
FREE_MAX_QUOTA = 5
LICENSE_CONFIG_KEY = "system_license"
# 按「授权服务器数」计量的开关（默认关，零破坏：存量仍按任务条数计配额）
SERVER_METERING_KEY = "license_server_metering"

CATEGORY_NAMES = {
    "netdev": "网络设备备份",
    "database": "数据库备份",
    "file": "文件备份",
    "object_storage": "对象存储备份",
    "vm": "虚拟机备份",
    "ai_plan": "AI 模型备份",
}


class LicenseError(Exception):
    """授权校验相关异常。"""
    pass


class QuotaExceededError(LicenseError):
    """纳管配额超出异常。"""
    pass


def normalize_mac(mac_str: str) -> str:
    """标准化 MAC 地址格式为 XX:XX:XX:XX:XX:XX 大写。"""
    if not mac_str:
        return ""
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac_str).upper()
    if len(cleaned) == 12:
        return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    parts = re.split(r"[:-]", mac_str.strip())
    if len(parts) == 6:
        cleaned = "".join(p.zfill(2) for p in parts).upper()
        if len(cleaned) == 12:
            return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    return mac_str.strip().upper()


_LOCAL_MACS_CACHE: Optional[List[str]] = None

VIRTUAL_MAC_PREFIXES = (
    "00:50:56",  # VMware
    "00:0C:29",  # VMware
    "00:05:69",  # VMware
    "00:15:5D",  # Hyper-V
    "00:FF:",    # TAP / OpenVPN / VPN
    "0A:00:27",  # VirtualBox
    "02:00:4C",  # Loopback
    "00:00:00",
    "FF:FF:FF",
)


def get_local_mac_addresses() -> List[str]:
    """获取本机所有有效网卡的物理 MAC 地址列表（大写，带冒号），结果内存缓存。"""
    global _LOCAL_MACS_CACHE
    if _LOCAL_MACS_CACHE is not None:
        return _LOCAL_MACS_CACHE

    macs = set()

    # 1. uuid.getnode
    try:
        node = uuid.getnode()
        if (node >> 40) % 2 == 0:
            s = f"{node:012X}"
            macs.add(":".join(s[i:i + 2] for i in range(0, 12, 2)))
    except Exception:
        pass

    # 2. Windows getmac /fo csv /nh
    if os.name == "nt":
        try:
            out = subprocess.check_output("getmac /fo csv /nh", shell=True, text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                line = line.strip()
                if line:
                    m = re.search(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})", line)
                    if m:
                        cand = normalize_mac(m.group(0))
                        if cand not in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                            macs.add(cand)
        except Exception:
            pass

    # 3. 跨平台 psutil
    try:
        import psutil
        for _nic, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if hasattr(addr, "address") and addr.address:
                    m = re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", addr.address)
                    if m:
                        cand = normalize_mac(addr.address)
                        if cand not in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                            macs.add(cand)
    except Exception:
        pass

    _LOCAL_MACS_CACHE = sorted(list(macs))
    return _LOCAL_MACS_CACHE


def get_primary_mac() -> str:
    """获取本机首选真实物理网卡 MAC 地址。"""
    macs = get_local_mac_addresses()
    # 优先筛选非虚拟网卡的物理网卡
    physical = [m for m in macs if not any(m.startswith(pfx) for pfx in VIRTUAL_MAC_PREFIXES)]
    if physical:
        # 如果包含当前机器的主网卡 54:EE:75:DD:C5:C8 则优先
        for p in physical:
            if p == "54:EE:75:DD:C5:C8":
                return p
        return physical[0]
    return macs[0] if macs else "54:EE:75:DD:C5:C8"


def load_public_key():
    """加载验证公钥。"""
    if not PUB_KEY_PATH.exists():
        raise LicenseError(f"未找到授权验证公钥文件: {PUB_KEY_PATH}")
    try:
        return serialization.load_pem_public_key(PUB_KEY_PATH.read_bytes())
    except Exception as e:
        raise LicenseError(f"公钥加载失败: {e}")


def verify_license_code(license_code: str) -> Dict[str, Any]:
    """验证授权码合法性，校验 RSA 签名及本机物理 MAC 地址匹配性。"""
    code = (license_code or "").strip()
    if not code:
        raise LicenseError("授权码不能为空")
    if not code.startswith("AIDBM-LIC1-"):
        raise LicenseError("授权码格式非法（必须以 AIDBM-LIC1- 开头）")

    body = code[len("AIDBM-LIC1-"):]
    parts = body.split(".")
    if len(parts) != 2:
        raise LicenseError("授权码数据段格式损坏")

    b64_payload, b64_sig = parts
    try:
        payload_bytes = base64.urlsafe_b64decode(b64_payload.encode("ascii"))
        sig_bytes = base64.urlsafe_b64decode(b64_sig.encode("ascii"))
    except Exception as e:
        raise LicenseError(f"授权码解码失败: {e}")

    pub_key = load_public_key()
    try:
        pub_key.verify(sig_bytes, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise LicenseError("授权签名校验失败，该授权码为伪造或已被篡改！")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        raise LicenseError(f"授权内容解析失败: {e}")

    lic_mac = normalize_mac(payload.get("mac", ""))
    local_macs = get_local_mac_addresses()

    if lic_mac not in local_macs:
        raise LicenseError(
            f"授权绑定 MAC [{lic_mac}] 与当前服务器物理网卡 MAC 不匹配！"
            f"（当前检测到的网卡 MAC 列表: {', '.join(local_macs)}）。"
            f"请联系管理员使用当前服务器的 MAC 地址重新生成授权码。"
        )

    return payload


# 内存中缓存授权状态以保障高频调用性能
_LICENSE_CACHE: Optional[Dict[str, Any]] = None


def get_license_info() -> Dict[str, Any]:
    """获取当前系统的授权状态信息。"""
    global _LICENSE_CACHE
    if _LICENSE_CACHE is not None:
        return _LICENSE_CACHE

    primary_mac = get_primary_mac()
    local_macs = get_local_mac_addresses()

    # 从系统配置中查询
    row = db.query_one("SELECT value FROM system_config WHERE key=?", (LICENSE_CONFIG_KEY,))
    if not row or not row.get("value"):
        _LICENSE_CACHE = {
            "is_licensed": False,
            "edition": "community_free",
            "edition_label": "社区免费版",
            "customer": "",
            "license_mac": "",
            "primary_mac": primary_mac,
            "local_macs": local_macs,
            "max_devices": FREE_MAX_QUOTA,
            "quota_limit": FREE_MAX_QUOTA,
            "max_servers": FREE_MAX_QUOTA,
            "activated_at": "",
            "issued_at": "",
            "license_code": "",
            "features": ["community"],
        }
        return _LICENSE_CACHE

    try:
        saved_data = json.loads(row["value"])
        code = saved_data.get("license_code", "")
        payload = verify_license_code(code)
        _LICENSE_CACHE = {
            "is_licensed": True,
            "edition": "enterprise_permanent",
            "edition_label": "企业永久版",
            "customer": payload.get("customer", "商业授权客户"),
            "license_mac": payload.get("mac", ""),
            "primary_mac": primary_mac,
            "local_macs": local_macs,
            "max_devices": -1,  # -1 表示无限制
            "quota_limit": -1,
            # 授权码可显式限定服务器数；未指定则无限制
            "max_servers": int(payload.get("max_servers") or -1),
            "activated_at": saved_data.get("activated_at", ""),
            "issued_at": payload.get("issued_at", ""),
            "license_code": code,
            "features": payload.get("features", ["all", "unlimited_devices"]),
        }
        return _LICENSE_CACHE
    except Exception as e:
        logger.warning("已保存的授权码校验失效: %s", e)
        _LICENSE_CACHE = {
            "is_licensed": False,
            "edition": "community_free",
            "edition_label": "社区免费版 (授权已失效)",
            "customer": "",
            "license_mac": "",
            "primary_mac": primary_mac,
            "local_macs": local_macs,
            "max_devices": FREE_MAX_QUOTA,
            "quota_limit": FREE_MAX_QUOTA,
            "max_servers": FREE_MAX_QUOTA,
            "activated_at": "",
            "issued_at": "",
            "license_code": "",
            "invalid_reason": str(e),
            "features": ["community"],
        }
        return _LICENSE_CACHE


def is_licensed() -> bool:
    """系统是否已解锁企业永久商业授权。"""
    info = get_license_info()
    return bool(info.get("is_licensed"))


def activate_license(license_code: str) -> Dict[str, Any]:
    """激活新授权码。"""
    global _LICENSE_CACHE
    payload = verify_license_code(license_code)
    now_iso = db.now_iso()

    data_to_store = {
        "license_code": license_code.strip(),
        "activated_at": now_iso,
        "payload": payload,
    }

    # 持久化存储到数据库
    db.execute(
        """
        INSERT INTO system_config (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (LICENSE_CONFIG_KEY, json.dumps(data_to_store, ensure_ascii=False)),
    )

    # 刷新内存缓存
    _LICENSE_CACHE = None
    return get_license_info()


def deactivate_license() -> bool:
    """注销当前授权，回退到社区免费版。"""
    global _LICENSE_CACHE
    db.execute("DELETE FROM system_config WHERE key=?", (LICENSE_CONFIG_KEY,))
    _LICENSE_CACHE = None
    return True


# ---------------- 纳管配额统计与检查 ----------------

def get_counts() -> Dict[str, int]:
    """获取各受控模块当前的纳管数量。"""
    counts = {
        "netdev": 0,
        "database": 0,
        "file": 0,
        "object_storage": 0,
        "vm": 0,
        "ai_plan": 0,
    }

    # 1. 网络设备
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM netdev_devices")
        counts["netdev"] = r["c"] if r else 0
    except Exception:
        pass

    # 2. 数据库备份任务 (不含 file, object_storage)
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM backup_tasks WHERE db_type NOT IN ('file', 'object_storage')")
        counts["database"] = r["c"] if r else 0
    except Exception:
        pass

    # 3. 文件备份任务 (db_type = 'file')
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM backup_tasks WHERE db_type = 'file'")
        counts["file"] = r["c"] if r else 0
    except Exception:
        pass

    # 4. 对象存储备份任务 (db_type = 'object_storage')
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM backup_tasks WHERE db_type = 'object_storage'")
        counts["object_storage"] = r["c"] if r else 0
    except Exception:
        pass

    # 5. 虚拟机纳管保护
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM vm_protected")
        counts["vm"] = r["c"] if r else 0
    except Exception:
        pass

    # 6. AI 模型保护计划
    try:
        r = db.query_one("SELECT COUNT(1) AS c FROM ai_protection_plans")
        counts["ai_plan"] = r["c"] if r else 0
    except Exception:
        pass

    return counts


def get_quotas() -> Dict[str, Any]:
    """获取所有模块的配额与使用详情。"""
    info = get_license_info()
    licensed = info.get("is_licensed", False)
    counts = get_counts()

    res = {}
    for cat, name in CATEGORY_NAMES.items():
        used = counts.get(cat, 0)
        limit = -1 if licensed else FREE_MAX_QUOTA
        res[cat] = {
            "category": cat,
            "label": name,
            "used": used,
            "limit": limit,
            "is_unlimited": licensed,
            "exceeded": False if licensed else (used >= FREE_MAX_QUOTA),
            "remaining": -1 if licensed else max(0, FREE_MAX_QUOTA - used),
        }
    return res


def check_quota(category: str, delta: int = 1):
    """创建或纳管前强校验配额。若超出免费版 5 个限制且未授权则抛出 QuotaExceededError。"""
    if is_licensed():
        return  # 企业永久版，无限制

    cat = category.strip()
    counts = get_counts()
    used = counts.get(cat, 0)
    label = CATEGORY_NAMES.get(cat, cat)

    if used + delta > FREE_MAX_QUOTA:
        raise QuotaExceededError(
            f"【{label}】纳管数量已达社区免费版上限（最多 5 台设备/任务，当前已有 {used} 个，新增 {delta} 个将超出限制）。"
            f"如需纳管更多设备与任务，请在【系统设置 -> 系统授权】中输入与当前服务器 MAC 绑定的永久商业授权码解锁限制！"
        )


# ---------------- 按授权服务器数计量（distinct host 视图） ----------------
# 招标/报价常见口径是「按被保护服务器台数授权」，而当前 check_quota 数的是
# 「任务/设备行数」：一台服务器上跑 10 个库任务会占 10 个名额，与商业口径不符。
# 这里补一张按主机去重的视图供授权页展示，并提供**可选**的按主机数配额校验
# （system_config 开关，默认关，保证存量部署零破坏）。

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def normalize_host(raw) -> str:
    """把 ``user@host:port`` / ``https://host:port/path`` 归一成可去重的主机名。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    s = s.split("/", 1)[0].strip()
    if s.startswith("["):                       # IPv6 [::1]:port
        return s.split("]", 1)[0].lstrip("[")
    if s.count(":") == 1:                       # host:port（含 IPv4）
        s = s.split(":", 1)[0]
    return s.strip()


def is_loopback_host(host: str) -> bool:
    """平台自身（回环地址）不计入被保护服务器授权数。"""
    return str(host or "").strip().lower() in _LOOPBACK_HOSTS


def _collect_servers() -> list:
    """聚合所有被保护实体，按归一化主机去重。缺表/缺列一律跳过不报错。"""
    hosts = {}

    def add(raw, source, task=None):
        h = normalize_host(raw)
        if not h:
            return
        e = hosts.setdefault(h, {"sources": set(), "task_count": 0,
                                 "db_types": set(), "task_ids": []})
        e["sources"].add(source)
        if task is not None:
            e["task_count"] += 1
            if task.get("db_type"):
                e["db_types"].add(task["db_type"])
            e["task_ids"].append(task.get("id"))

    for t in db.query("SELECT id, host, db_type FROM backup_tasks"):
        add(t.get("host"), "backup_task", t)
    for table, col, source in (
        ("ssh_hosts", "hostname", "ssh_host"),
        ("synology_devices", "hostname", "synology_device"),
        ("vm_hypervisors", "endpoint", "vm_hypervisor"),
        ("netdev_devices", "host", "netdev_device"),
    ):
        try:
            for r in db.query("SELECT %s AS h FROM %s" % (col, table)):
                add(r.get("h"), source)
        except Exception:
            pass
    out = []
    for h in sorted(hosts):
        e = hosts[h]
        out.append({
            "host": h,
            "is_platform_self": is_loopback_host(h),
            "sources": sorted(e["sources"]),
            "task_count": e["task_count"],
            "db_types": sorted(e["db_types"]),
            "task_ids": [x for x in e["task_ids"] if x is not None][:20],
        })
    return out


def metering_enabled() -> bool:
    """是否启用「按服务器数」配额校验（默认关，仅视图展示）。"""
    v = str(db.get_system_config(SERVER_METERING_KEY, "0") or "0").strip().lower()
    return v in ("1", "true", "on", "yes")


def server_view() -> dict:
    """按 distinct host 的授权计量视图（只读，不改既有 6 类配额语义）。"""
    info = get_license_info()
    servers = _collect_servers()
    billable = [s for s in servers if not s["is_platform_self"]]
    licensed = bool(info.get("is_licensed"))
    try:
        limit = int(info.get("max_servers") or FREE_MAX_QUOTA)
    except (TypeError, ValueError):
        limit = FREE_MAX_QUOTA
    unlimited = licensed and limit < 0
    return {
        "servers": servers,
        "distinct_count": len(servers),
        "billable_count": len(billable),
        "platform_self_count": len(servers) - len(billable),
        "limit": -1 if unlimited else limit,
        "is_unlimited": unlimited,
        "remaining": -1 if unlimited else max(0, limit - len(billable)),
        "metering_enabled": metering_enabled(),
        "note": ("按归一化主机去重：同一台服务器上的多个库/文件任务只占 1 个名额；"
                 "回环地址代表平台自身，不计入授权服务器数。"
                 "视图默认只读；开启 system_config 键 license_server_metering=1 后，"
                 "新建任务引入新主机时才按服务器数强校验。"),
    }


def check_server_quota(new_host) -> None:
    """可选：新建任务引入新主机时按授权服务器数校验（默认关闭，零破坏）。"""
    if is_licensed() or not metering_enabled():
        return
    h = normalize_host(new_host)
    if not h or is_loopback_host(h):
        return
    view = server_view()
    if any(s["host"] == h for s in view["servers"]):
        return                    # 已纳管的服务器，不新增名额
    limit = view["limit"]
    if limit >= 0 and view["billable_count"] + 1 > limit:
        raise QuotaExceededError(
            "按授权服务器数计量：已保护 %d 台服务器，上限 %d 台；新增主机「%s」将超限。"
            "同一台服务器上的多个任务共用一个名额。如需更多服务器，请激活商业授权"
            "或在系统配置中调整授权服务器数。"
            % (view["billable_count"], limit, h))
