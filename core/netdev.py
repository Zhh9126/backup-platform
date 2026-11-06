# -*- coding: utf-8 -*-
"""网络设备配置备份核心服务（core.netdev）。

功能涵盖：
- 交换机（Switch）、路由器（Router）、防火墙（Firewall）等网络设备的纳管与凭据管理
- 统一凭据令牌管理（Token）：支持添加/复用统一账号凭据，批量绑定网络设备
- 自定义配置采集指令（如 display current-configuration / show running-config 等）
- SSH 连通性探测与状态维持（在线/离线/未测试），强制「只有 SSH 连接成功的设备才允许备份」
- 备份目标支持本地目标路径或远程 FTP 服务器
- 定时自动备份调度（全局定时 / 单设备排程）
- 批量选择设备备份与批量连接探测
- Excel (.xlsx) 模板生成与批量设备导入
- 备份文件管理、配置在线预览与版本对比（Diff）
"""
import difflib
import ftplib
import io
import json
import logging
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import config
import core.db as db
from core import license as license_mod

_logger = logging.getLogger("aidbm.netdev")

# 设备类型字典
DEVICE_TYPES = {
    "switch": "交换机",
    "router": "路由器",
    "firewall": "防火墙",
    "other": "其他网络设备",
}

# 常见厂商指令预设
VENDOR_PRESETS = {
    "huawei": {
        "name": "华为 (Huawei VRP)",
        "init_commands": "screen-length 0 temporary",
        "backup_commands": "display current-configuration",
    },
    "h3c": {
        "name": "华三 (H3C Comware)",
        "init_commands": "screen-length 0 temporary",
        "backup_commands": "display current-configuration",
    },
    "cisco": {
        "name": "思科 (Cisco IOS / NX-OS)",
        "init_commands": "terminal length 0",
        "backup_commands": "show running-config",
    },
    "ruijie": {
        "name": "锐捷 (Ruijie RGOS)",
        "init_commands": "terminal length 0",
        "backup_commands": "show running-config",
    },
    "juniper": {
        "name": "瞻博 (Juniper Junos)",
        "init_commands": "set cli screen-length 0",
        "backup_commands": "show configuration | no-more",
    },
    "linux": {
        "name": "Linux 网关 / 软路由",
        "init_commands": "",
        "backup_commands": "cat /etc/network/interfaces 2>/dev/null || ip addr show; ip route show",
    },
    "custom": {
        "name": "自定义设备",
        "init_commands": "",
        "backup_commands": "display current-configuration",
    },
}

DEVICE_FIELDS = [
    "name", "device_type", "vendor", "host", "port", "token_id",
    "username", "password", "auth_mode", "private_key", "enable_password",
    "backup_commands", "init_commands", "target_type", "target_path",
    "ftp_host", "ftp_port", "ftp_user", "ftp_password", "ftp_path",
    "schedule_enabled", "schedule_type", "schedule_expr",
    "enabled", "remark",
]

TOKEN_FIELDS = [
    "name", "auth_mode", "username", "password",
    "enable_password", "private_key", "remark"
]


def _clean_ansi(text: str) -> str:
    """去除终端输出中的 ANSI 转义序列与退格/回车符。"""
    if not text:
        return ""
    ansi_regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    cleaned = ansi_regex.sub("", text)
    cleaned = cleaned.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    return cleaned


def _safe_filename(name: str) -> str:
    """转义为合法的文件名。"""
    return re.sub(r'[\/:*?"<>|\\ ]', '_', (name or "device").strip())


def _get_local_netdev_dir(device_name: str, custom_target_path: str = "") -> Path:
    """获取本地配置存储目录。"""
    if custom_target_path and custom_target_path.strip():
        p = Path(custom_target_path.strip())
    else:
        root = Path(config.get_backup_root())
        p = root / "netdev" / _safe_filename(device_name)
    p.mkdir(parents=True, exist_ok=True)
    return p


# =========================================================================
# 令牌管理（Token Management）
# =========================================================================

def list_tokens(include_secret: bool = False) -> List[Dict[str, Any]]:
    """获取所有统一凭据令牌列表。"""
    rows = db.query("SELECT * FROM netdev_tokens ORDER BY id DESC")
    out = []
    for r in rows:
        d = dict(r)
        if include_secret:
            d["password"] = db.decrypt_secret(d.get("password") or "")
            d["enable_password"] = db.decrypt_secret(d.get("enable_password") or "")
        else:
            d["password"] = ""
            d["enable_password"] = ""
        d["has_password"] = bool(r.get("password"))
        d["has_enable_password"] = bool(r.get("enable_password"))
        d["has_private_key"] = bool(r.get("private_key"))
        out.append(d)
    return out


def get_token(token_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    """获取单个凭据令牌详情。"""
    row = db.query_one("SELECT * FROM netdev_tokens WHERE id=?", (token_id,))
    if not row:
        return None
    d = dict(row)
    if include_secret:
        d["password"] = db.decrypt_secret(d.get("password") or "")
        d["enable_password"] = db.decrypt_secret(d.get("enable_password") or "")
    else:
        d["password"] = ""
        d["enable_password"] = ""
    d["has_password"] = bool(row.get("password"))
    d["has_enable_password"] = bool(row.get("enable_password"))
    d["has_private_key"] = bool(row.get("private_key"))
    return d


def create_token(data: dict) -> int:
    """创建统一凭据令牌。"""
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip()
    if not name:
        raise ValueError("令牌名称必填")
    if not username:
        raise ValueError("SSH 用户名必填")

    auth_mode = data.get("auth_mode") or "password"
    password = data.get("password") or ""
    enable_password = data.get("enable_password") or ""
    private_key = data.get("private_key") or ""
    remark = (data.get("remark") or "").strip()

    now = db.now_iso()
    sql = """
    INSERT INTO netdev_tokens (
        name, auth_mode, username, password, enable_password,
        private_key, remark, created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?)
    """
    row_id = db.execute(sql, (
        name, auth_mode, username, db.encrypt_secret(password),
        db.encrypt_secret(enable_password), private_key, remark, now, now
    ))
    db.add_log("INFO", "netdev", f"创建网络设备凭据令牌 [{name}] (用户: {username})")
    return row_id


def update_token(token_id: int, data: dict) -> bool:
    """更新凭据令牌。"""
    row = db.query_one("SELECT * FROM netdev_tokens WHERE id=?", (token_id,))
    if not row:
        return False

    updates = {}
    for k in ("name", "auth_mode", "username", "private_key", "remark"):
        if k in data:
            updates[k] = data[k]

    if "password" in data and data["password"]:
        updates["password"] = db.encrypt_secret(data["password"])
    if "enable_password" in data and data["enable_password"]:
        updates["enable_password"] = db.encrypt_secret(data["enable_password"])

    if not updates:
        return True

    updates["updated_at"] = db.now_iso()
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    db.execute(f"UPDATE netdev_tokens SET {set_clause} WHERE id=?",
               tuple(list(updates.values()) + [token_id]))
    return True


def delete_token(token_id: int) -> bool:
    """删除凭据令牌。"""
    tok = db.query_one("SELECT name FROM netdev_tokens WHERE id=?", (token_id,))
    name = tok["name"] if tok else str(token_id)
    # 解绑关联的设备
    db.execute("UPDATE netdev_devices SET token_id=NULL WHERE token_id=?", (token_id,))
    db.execute("DELETE FROM netdev_tokens WHERE id=?", (token_id,))
    db.add_log("WARNING", "netdev", f"删除网络设备凭据令牌 [{name}]")
    return True


# =========================================================================
# 设备 CRUD 与令牌继承
# =========================================================================

def list_devices(include_secret: bool = False) -> List[Dict[str, Any]]:
    """获取所有网络设备列表。"""
    rows = db.query("""
        SELECT d.*, t.name as token_name, t.username as token_username
        FROM netdev_devices d
        LEFT JOIN netdev_tokens t ON d.token_id = t.id
        ORDER BY d.id DESC
    """)
    out = []
    for r in rows:
        d = dict(r)
        if include_secret:
            d["password"] = db.decrypt_secret(d.get("password") or "")
            d["enable_password"] = db.decrypt_secret(d.get("enable_password") or "")
            d["ftp_password"] = db.decrypt_secret(d.get("ftp_password") or "")
        else:
            d["password"] = ""
            d["enable_password"] = ""
            d["ftp_password"] = ""
        d["has_password"] = bool(r.get("password"))
        d["has_enable_password"] = bool(r.get("enable_password"))
        d["has_ftp_password"] = bool(r.get("ftp_password"))
        d["device_type_label"] = DEVICE_TYPES.get(d.get("device_type") or "switch", "交换机")
        d["vendor_label"] = VENDOR_PRESETS.get(d.get("vendor") or "custom", {}).get("name", d.get("vendor"))
        out.append(d)
    return out


def get_device(device_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    """获取单个网络设备详情（支持自动继承关联令牌的凭据）。"""
    row = db.query_one("""
        SELECT d.*, t.name as token_name
        FROM netdev_devices d
        LEFT JOIN netdev_tokens t ON d.token_id = t.id
        WHERE d.id=?
    """, (device_id,))
    if not row:
        return None
    d = dict(row)

    token_data = None
    if d.get("token_id"):
        token_data = get_token(d["token_id"], include_secret=True)

    if include_secret:
        dev_pwd = db.decrypt_secret(d.get("password") or "")
        dev_epwd = db.decrypt_secret(d.get("enable_password") or "")
        d["ftp_password"] = db.decrypt_secret(d.get("ftp_password") or "")

        # 如果设备未单独设置密码但绑定了令牌，自动继承令牌凭据
        if not dev_pwd and token_data:
            dev_pwd = token_data.get("password") or ""
            d["auth_mode"] = token_data.get("auth_mode") or d.get("auth_mode")
            if not d.get("private_key"):
                d["private_key"] = token_data.get("private_key") or ""
        if not dev_epwd and token_data:
            dev_epwd = token_data.get("enable_password") or ""
        if not d.get("username") and token_data:
            d["username"] = token_data.get("username") or ""

        d["password"] = dev_pwd
        d["enable_password"] = dev_epwd
    else:
        d["password"] = ""
        d["enable_password"] = ""
        d["ftp_password"] = ""

    d["has_password"] = bool(row.get("password") or (token_data and token_data.get("has_password")))
    d["has_enable_password"] = bool(row.get("enable_password") or (token_data and token_data.get("has_enable_password")))
    d["has_ftp_password"] = bool(row.get("ftp_password"))
    d["device_type_label"] = DEVICE_TYPES.get(d.get("device_type") or "switch", "交换机")
    d["vendor_label"] = VENDOR_PRESETS.get(d.get("vendor") or "custom", {}).get("name", d.get("vendor"))
    return d


def create_device(data: dict) -> int:
    """新增网络设备。"""
    import core.license as license_mod
    license_mod.check_quota("netdev")

    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    token_id = data.get("token_id")
    if token_id:
        try:
            token_id = int(token_id)
        except Exception:
            token_id = None

    # 如果绑定了令牌且用户名为空，使用令牌中的用户名
    if token_id and not username:
        tok = get_token(token_id)
        if tok:
            username = tok.get("username") or ""

    if not name:
        raise ValueError("设备名称必填")
    if not host:
        raise ValueError("设备 IP / 地址必填")
    if not username and not token_id:
        raise ValueError("SSH 用户名必填（或选择已有的认证令牌）")

    device_type = (data.get("device_type") or "switch").strip().lower()
    vendor = (data.get("vendor") or "general").strip().lower()
    port = int(data.get("port") or 22)
    auth_mode = data.get("auth_mode") or "password"
    password = data.get("password") or ""
    enable_password = data.get("enable_password") or ""
    private_key = data.get("private_key") or ""

    backup_commands = (data.get("backup_commands") or "").strip()
    init_commands = (data.get("init_commands") or "").strip()
    if not backup_commands and vendor in VENDOR_PRESETS:
        backup_commands = VENDOR_PRESETS[vendor]["backup_commands"]
    if not init_commands and vendor in VENDOR_PRESETS:
        init_commands = VENDOR_PRESETS[vendor]["init_commands"]

    target_type = (data.get("target_type") or "local").strip().lower()
    target_path = (data.get("target_path") or "").strip()
    ftp_host = (data.get("ftp_host") or "").strip()
    ftp_port = int(data.get("ftp_port") or 21)
    ftp_user = (data.get("ftp_user") or "").strip()
    ftp_password = data.get("ftp_password") or ""
    ftp_path = (data.get("ftp_path") or "/").strip()
    remark = (data.get("remark") or "").strip()

    schedule_enabled = 1 if data.get("schedule_enabled") else 0
    schedule_type = data.get("schedule_type") or "none"
    schedule_expr = data.get("schedule_expr") or "02:00"

    now = db.now_iso()
    sql = """
    INSERT INTO netdev_devices (
        name, device_type, vendor, host, port, token_id, username,
        password, auth_mode, private_key, enable_password,
        backup_commands, init_commands, target_type, target_path,
        ftp_host, ftp_port, ftp_user, ftp_password, ftp_path,
        schedule_enabled, schedule_type, schedule_expr,
        status, last_status_msg, enabled, remark, created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    row_id = db.execute(sql, (
        name, device_type, vendor, host, port, token_id, username,
        db.encrypt_secret(password), auth_mode, private_key, db.encrypt_secret(enable_password),
        backup_commands, init_commands, target_type, target_path,
        ftp_host, ftp_port, ftp_user, db.encrypt_secret(ftp_password), ftp_path,
        schedule_enabled, schedule_type, schedule_expr,
        "untested", "未进行连接测试", 1, remark, now, now
    ))
    db.add_log("INFO", "netdev", f"新增网络设备 [{name}] ({host}:{port})")
    return row_id


def update_device(device_id: int, data: dict) -> bool:
    """更新网络设备信息。"""
    row = db.query_one("SELECT * FROM netdev_devices WHERE id=?", (device_id,))
    if not row:
        return False

    updates = {}
    for k in ("name", "device_type", "vendor", "host", "port", "token_id",
              "username", "auth_mode", "private_key", "backup_commands",
              "init_commands", "target_type", "target_path", "ftp_host",
              "ftp_port", "ftp_user", "ftp_path", "schedule_enabled",
              "schedule_type", "schedule_expr", "enabled", "remark"):
        if k in data:
            val = data[k]
            if k == "token_id" and val in ("", 0, "0", None):
                val = None
            elif k == "schedule_enabled":
                val = 1 if val else 0
            updates[k] = val

    if "password" in data and data["password"]:
        updates["password"] = db.encrypt_secret(data["password"])
    if "enable_password" in data and data["enable_password"]:
        updates["enable_password"] = db.encrypt_secret(data["enable_password"])
    if "ftp_password" in data and data["ftp_password"]:
        updates["ftp_password"] = db.encrypt_secret(data["ftp_password"])

    if not updates:
        return True

    updates["updated_at"] = db.now_iso()
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    db.execute(f"UPDATE netdev_devices SET {set_clause} WHERE id=?",
               tuple(list(updates.values()) + [device_id]))
    return True


def delete_device(device_id: int) -> bool:
    """删除网络设备。"""
    dev = db.query_one("SELECT name FROM netdev_devices WHERE id=?", (device_id,))
    name = dev["name"] if dev else str(device_id)
    db.execute("DELETE FROM netdev_devices WHERE id=?", (device_id,))
    db.execute("DELETE FROM netdev_backups WHERE device_id=?", (device_id,))
    db.add_log("WARNING", "netdev", f"删除网络设备 [{name}] 及对应备份记录")
    return True


# =========================================================================
# SSH / FTP 连通性测试
# =========================================================================

def _connect_ssh_client(host: str, port: int, username: str,
                        password: str = "", auth_mode: str = "password",
                        private_key: str = "", timeout: int = 15):
    """创建并连接 Paramiko SSHClient。"""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=host,
        port=port or 22,
        username=username,
        timeout=timeout,
        banner_timeout=25,
        auth_timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    if auth_mode == "key" and private_key:
        classes = (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey)
        pkey = None
        s = private_key.strip()
        data = s if s.endswith("\n") else s + "\n"
        for cls in classes:
            try:
                pkey = cls.from_private_key(io.StringIO(data))
                break
            except Exception:
                continue
        if not pkey:
            raise ValueError("无法解析 SSH 私钥，请检查格式")
        connect_kwargs["pkey"] = pkey
    else:
        connect_kwargs["password"] = password

    client.connect(**connect_kwargs)
    return client


def test_ssh_connection(device_or_id) -> Dict[str, Any]:
    """测试网络设备 SSH 连通性并持久化状态。"""
    if isinstance(device_or_id, int):
        device = get_device(device_or_id, include_secret=True)
        if not device:
            return {"ok": False, "message": "设备不存在", "status": "disconnected"}
    else:
        device = dict(device_or_id)
        # 如果是传入的未保存对象但有 token_id
        if device.get("token_id") and not device.get("password"):
            tok = get_token(int(device["token_id"]), include_secret=True)
            if tok:
                device["password"] = tok.get("password") or ""
                device["username"] = device.get("username") or tok.get("username") or ""
                device["auth_mode"] = tok.get("auth_mode") or device.get("auth_mode") or "password"
                device["private_key"] = device.get("private_key") or tok.get("private_key") or ""

    host = device.get("host") or ""
    port = int(device.get("port") or 22)
    username = device.get("username") or ""
    password = device.get("password") or ""
    auth_mode = device.get("auth_mode") or "password"
    private_key = device.get("private_key") or ""

    device_id = device.get("id")
    client = None
    start_t = time.time()
    try:
        client = _connect_ssh_client(
            host=host, port=port, username=username,
            password=password, auth_mode=auth_mode,
            private_key=private_key, timeout=12
        )
        transport = client.get_transport()
        banner = ""
        if transport:
            banner = transport.get_banner() or ""
        cost_ms = int((time.time() - start_t) * 1000)
        msg = f"SSH 远程连接成功（耗时 {cost_ms} ms）"
        if banner:
            msg += f" - {banner.strip()[:100]}"
        res = {
            "ok": True,
            "status": "connected",
            "message": msg,
            "cost_ms": cost_ms,
        }
    except Exception as e:
        cost_ms = int((time.time() - start_t) * 1000)
        err_msg = str(e)
        if "Authentication failed" in err_msg:
            err_msg = "认证失败，请检查用户名或密码/私钥是否正确"
        elif "timed out" in err_msg:
            err_msg = f"连接超时，请检查 IP {host}:{port} 是否可达或防火墙放行"
        res = {
            "ok": False,
            "status": "disconnected",
            "message": f"SSH 连接失败: {err_msg}",
            "cost_ms": cost_ms,
        }
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    if device_id:
        try:
            status = "connected" if res["ok"] else "disconnected"
            db.execute(
                "UPDATE netdev_devices SET status=?, last_status_msg=?, last_check_at=? WHERE id=?",
                (status, res["message"], db.now_iso(), device_id)
            )
        except Exception as _db_err:
            _logger.warning("更新设备状态失败: %s", _db_err)

    return res


def test_ftp_connection(ftp_host: str, ftp_port: int = 21,
                        ftp_user: str = "", ftp_password: str = "",
                        ftp_path: str = "/") -> Dict[str, Any]:
    """测试远程 FTP 服务器连通性与写入权限。"""
    ftp_host = (ftp_host or "").strip()
    if not ftp_host:
        return {"ok": False, "message": "FTP 服务器地址未填写"}
    ftp_port = int(ftp_port or 21)
    ftp = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(ftp_host, ftp_port, timeout=10)
        ftp.login(ftp_user or "anonymous", ftp_password or "")
        target_dir = (ftp_path or "/").strip()
        if target_dir and target_dir != "/":
            try:
                ftp.cwd(target_dir)
            except Exception:
                _ensure_ftp_dir(ftp, target_dir)
        ftp.quit()
        return {"ok": True, "message": f"FTP 连通成功，目标目录 [{target_dir}] 访问正常"}
    except Exception as e:
        return {"ok": False, "message": f"FTP 连接测试失败: {e}"}
    finally:
        if ftp:
            try:
                ftp.close()
            except Exception:
                pass


def _ensure_ftp_dir(ftp: ftplib.FTP, path: str):
    """确保 FTP 上的目录层级存在。"""
    parts = [p for p in path.strip("/").split("/") if p]
    ftp.cwd("/")
    for part in parts:
        try:
            ftp.cwd(part)
        except Exception:
            try:
                ftp.mkd(part)
                ftp.cwd(part)
            except Exception as e:
                _logger.warning("FTP 创建目录 [%s] 异常: %s", part, e)


# =========================================================================
# 执行配置备份与批量执行
# =========================================================================

def _collect_device_config(client, init_commands: str, backup_commands: str,
                           enable_password: str = "") -> str:
    """通过交互式 Shell 发送指令并采集网络设备配置输出。"""
    chan = client.invoke_shell(term="vt100", width=300, height=1000)
    chan.settimeout(30.0)

    time.sleep(1.0)
    buffer = ""
    while chan.recv_ready():
        buffer += chan.recv(4096).decode("utf-8", errors="replace")

    if enable_password:
        chan.send("enable\n")
        time.sleep(0.5)
        out = ""
        while chan.recv_ready():
            out += chan.recv(4096).decode("utf-8", errors="replace")
        if "Password" in out or "password" in out:
            chan.send(enable_password + "\n")
            time.sleep(0.8)
            while chan.recv_ready():
                chan.recv(4096)

    if init_commands:
        for line in init_commands.strip().splitlines():
            cmd = line.strip()
            if cmd:
                chan.send(cmd + "\n")
                time.sleep(0.5)
                while chan.recv_ready():
                    chan.recv(4096)

    collected_output = []
    commands = [c.strip() for c in (backup_commands or "").splitlines() if c.strip()]
    if not commands:
        commands = ["display current-configuration"]

    for cmd in commands:
        chan.send(cmd + "\n")
        cmd_output = ""
        idle_count = 0
        max_wait_seconds = 60
        start_wait = time.time()

        while True:
            if chan.recv_ready():
                chunk = chan.recv(8192).decode("utf-8", errors="replace")
                cmd_output += chunk
                idle_count = 0
            else:
                time.sleep(0.3)
                idle_count += 1

            if idle_count >= 7 and len(cmd_output) > 0:
                break
            if time.time() - start_wait > max_wait_seconds:
                break

        collected_output.append(f"# ===== Command: {cmd} =====\n" + cmd_output.strip())

    try:
        chan.close()
    except Exception:
        pass

    full_output = "\n\n".join(collected_output)
    return _clean_ansi(full_output)


def execute_backup(device_id: int, operator: str = "system") -> Dict[str, Any]:
    """执行网络设备配置备份。

    约束规则：
    只有 SSH 远程连接成功的设备才可以进行备份！如果当前 SSH 测试不通，拒绝执行。
    """
    device = get_device(device_id, include_secret=True)
    if not device:
        raise ValueError("指定网络设备不存在")

    device_name = device["name"]
    host = device["host"]
    port = int(device["port"] or 22)
    device_type = device.get("device_type") or "switch"
    vendor = device.get("vendor") or "general"

    # ================= 强校验：检查连接状态 =================
    test_res = test_ssh_connection(device_id)
    if not test_res.get("ok"):
        err_msg = f"只有 SSH 远程连接成功的设备才可执行备份！当前连接测试失败: {test_res.get('message')}"
        _record_backup_history(
            device_id=device_id,
            device_name=device_name,
            device_ip=host,
            device_type=device_type,
            vendor=vendor,
            target_type=device.get("target_type") or "local",
            target_location="",
            file_name="",
            file_size=0,
            sha256="",
            status="failed",
            error_message=err_msg,
            duration_sec=0.0,
            commands_run=device.get("backup_commands") or "",
            content_preview="",
            created_by=operator
        )
        db.execute(
            "UPDATE netdev_devices SET last_backup_at=?, last_backup_status='failed' WHERE id=?",
            (db.now_iso(), device_id)
        )
        db.add_log("ERROR", "netdev", f"网络设备 [{device_name}] 备份被拦截: {err_msg}")
        raise RuntimeError(err_msg)

    # ================= 开始执行备份 =================
    start_time = time.time()
    client = None
    try:
        client = _connect_ssh_client(
            host=host,
            port=port,
            username=device["username"],
            password=device.get("password") or "",
            auth_mode=device.get("auth_mode") or "password",
            private_key=device.get("private_key") or "",
            timeout=20
        )

        config_text = _collect_device_config(
            client=client,
            init_commands=device.get("init_commands") or "",
            backup_commands=device.get("backup_commands") or "",
            enable_password=device.get("enable_password") or ""
        )

        if not config_text or len(config_text.strip()) < 10:
            raise RuntimeError("从设备抓取的配置内容为空，请检查备份指令是否有效")

    except Exception as e:
        duration = round(time.time() - start_time, 2)
        err_msg = f"设备配置抓取失败: {e}"
        _record_backup_history(
            device_id=device_id,
            device_name=device_name,
            device_ip=host,
            device_type=device_type,
            vendor=vendor,
            target_type=device.get("target_type") or "local",
            target_location="",
            file_name="",
            file_size=0,
            sha256="",
            status="failed",
            error_message=err_msg,
            duration_sec=duration,
            commands_run=device.get("backup_commands") or "",
            content_preview="",
            created_by=operator
        )
        db.execute(
            "UPDATE netdev_devices SET last_backup_at=?, last_backup_status='failed' WHERE id=?",
            (db.now_iso(), device_id)
        )
        db.add_log("ERROR", "netdev", f"网络设备 [{device_name}] 备份异常: {err_msg}")
        raise RuntimeError(err_msg)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    # ================= 产物保存与目标推送 =================
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{_safe_filename(device_name)}_{device_type}_{ts}.cfg"
    local_dir = _get_local_netdev_dir(device_name, device.get("target_path") or "")
    local_file_path = local_dir / filename
    if local_file_path.exists():
        ts_micro = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        filename = f"{_safe_filename(device_name)}_{device_type}_{ts_micro}.cfg"
        local_file_path = local_dir / filename

    with open(local_file_path, "w", encoding="utf-8") as f:
        f.write(config_text)

    file_size = os.path.getsize(local_file_path)
    sha256 = db.sha256_file(str(local_file_path))
    target_type = device.get("target_type") or "local"
    target_location = str(local_file_path)

    if target_type == "ftp":
        ftp_host = device.get("ftp_host") or ""
        ftp_port = int(device.get("ftp_port") or 21)
        ftp_user = device.get("ftp_user") or ""
        ftp_password = device.get("ftp_password") or ""
        ftp_path = (device.get("ftp_path") or "/").strip()

        if not ftp_host:
            raise RuntimeError("选择备份到 FTP，但未配置 FTP 服务器地址")

        ftp = None
        try:
            ftp = ftplib.FTP()
            ftp.connect(ftp_host, ftp_port, timeout=20)
            ftp.login(ftp_user or "anonymous", ftp_password or "")
            if ftp_path and ftp_path != "/":
                _ensure_ftp_dir(ftp, ftp_path)
            with open(local_file_path, "rb") as f:
                ftp.storbinary(f"STOR {filename}", f)
            ftp.quit()
            clean_path = ftp_path.rstrip("/") + "/" + filename
            target_location = f"ftp://{ftp_user}@{ftp_host}:{ftp_port}{clean_path}"
        except Exception as ftp_err:
            duration = round(time.time() - start_time, 2)
            err_msg = f"配置抓取成功但推送至远程 FTP 失败: {ftp_err}"
            _record_backup_history(
                device_id=device_id,
                device_name=device_name,
                device_ip=host,
                device_type=device_type,
                vendor=vendor,
                target_type="ftp",
                target_location=target_location,
                file_name=filename,
                file_size=file_size,
                sha256=sha256,
                status="failed",
                error_message=err_msg,
                duration_sec=duration,
                commands_run=device.get("backup_commands") or "",
                content_preview=config_text[:3000],
                created_by=operator
            )
            db.execute(
                "UPDATE netdev_devices SET last_backup_at=?, last_backup_status='failed' WHERE id=?",
                (db.now_iso(), device_id)
            )
            db.add_log("ERROR", "netdev", f"网络设备 [{device_name}] FTP 上传失败: {ftp_err}")
            raise RuntimeError(err_msg)
        finally:
            if ftp:
                try:
                    ftp.close()
                except Exception:
                    pass

    duration = round(time.time() - start_time, 2)
    preview = config_text[:4000]

    record_id = _record_backup_history(
        device_id=device_id,
        device_name=device_name,
        device_ip=host,
        device_type=device_type,
        vendor=vendor,
        target_type=target_type,
        target_location=target_location,
        file_name=filename,
        file_size=file_size,
        sha256=sha256,
        status="success",
        error_message="",
        duration_sec=duration,
        commands_run=device.get("backup_commands") or "",
        content_preview=preview,
        created_by=operator
    )

    now_iso = db.now_iso()
    db.execute(
        "UPDATE netdev_devices SET last_backup_at=?, last_backup_status='success' WHERE id=?",
        (now_iso, device_id)
    )
    db.add_log("INFO", "netdev",
               f"网络设备 [{device_name}] 配置备份成功（{db.human_size(file_size)}，目标: {target_type}）",
               record_id=record_id)

    return {
        "ok": True,
        "backup_id": record_id,
        "file_name": filename,
        "file_size": file_size,
        "file_size_human": db.human_size(file_size),
        "sha256": sha256,
        "target_type": target_type,
        "target_location": target_location,
        "duration_sec": duration,
        "created_at": now_iso
    }


def batch_backup(device_ids: List[int], operator: str = "batch") -> Dict[str, Any]:
    """批量对所选网络设备执行配置备份。"""
    results = []
    success_count = 0
    failed_count = 0

    for dev_id in device_ids:
        dev = get_device(dev_id)
        if not dev:
            results.append({"device_id": dev_id, "ok": False, "message": "设备不存在"})
            failed_count += 1
            continue

        try:
            res = execute_backup(dev_id, operator=operator)
            results.append({
                "device_id": dev_id,
                "device_name": dev.get("name"),
                "ok": True,
                "file_name": res.get("file_name"),
                "file_size_human": res.get("file_size_human"),
                "message": "备份成功"
            })
            success_count += 1
        except Exception as e:
            results.append({
                "device_id": dev_id,
                "device_name": dev.get("name"),
                "ok": False,
                "message": str(e)
            })
            failed_count += 1

    return {
        "total": len(device_ids),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results
    }


def _record_backup_history(device_id: int, device_name: str, device_ip: str,
                           device_type: str, vendor: str, target_type: str,
                           target_location: str, file_name: str, file_size: int,
                           sha256: str, status: str, error_message: str,
                           duration_sec: float, commands_run: str,
                           content_preview: str, created_by: str) -> int:
    """插入备份记录。"""
    sql = """
    INSERT INTO netdev_backups (
        device_id, device_name, device_ip, device_type, vendor,
        target_type, target_location, file_name, file_size, sha256,
        status, error_message, duration_sec, commands_run,
        content_preview, created_by, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    return db.execute(sql, (
        device_id, device_name, device_ip, device_type, vendor,
        target_type, target_location, file_name, file_size, sha256,
        status, error_message, duration_sec, commands_run,
        content_preview, created_by, db.now_iso()
    ))


# =========================================================================
# 定时自动备份逻辑（Scheduler Integration）
# =========================================================================

def get_auto_backup_config() -> Dict[str, Any]:
    """获取全局网络设备自动备份配置。"""
    cfg_raw = db.get_system_config("netdev_auto_backup_config")
    if cfg_raw:
        try:
            return json.loads(cfg_raw)
        except Exception:
            pass
    return {
        "enabled": False,
        "schedule_type": "daily",     # daily | cron
        "schedule_time": "02:00",     # 每天 02:00
        "cron_expr": "0 2 * * *",
        "last_run": "",
    }


def set_auto_backup_config(data: dict) -> None:
    """设置全局网络设备自动备份配置。"""
    cur = get_auto_backup_config()
    cur["enabled"] = bool(data.get("enabled"))
    cur["schedule_type"] = data.get("schedule_type") or "daily"
    cur["schedule_time"] = (data.get("schedule_time") or "02:00").strip()
    cur["cron_expr"] = (data.get("cron_expr") or "0 2 * * *").strip()
    db.set_system_config("netdev_auto_backup_config", json.dumps(cur, ensure_ascii=False))


def run_scheduled_backups() -> Dict[str, Any]:
    """由后台调度器 APScheduler 每分钟轮询执行。

    检查：
    1. 全局定时配置是否命中当前时刻；
    2. 各网络设备独立配置的定时排程是否命中。
    """
    now = datetime.now()
    cur_time_str = now.strftime("%H:%M")
    today_date_str = now.strftime("%Y-%m-%d")

    triggered_devices = set()

    # 1. 检查全局配置
    global_cfg = get_auto_backup_config()
    if global_cfg.get("enabled"):
        is_hit = False
        if global_cfg.get("schedule_type") == "daily":
            cfg_time = (global_cfg.get("schedule_time") or "02:00").strip()
            last_run = global_cfg.get("last_run") or ""
            if cur_time_str == cfg_time and not last_run.startswith(today_date_str):
                is_hit = True
                global_cfg["last_run"] = db.now_iso()
                db.set_system_config("netdev_auto_backup_config", json.dumps(global_cfg, ensure_ascii=False))
        elif global_cfg.get("schedule_type") == "cron":
            # 简易 cron 表达式校验（分 时）
            parts = (global_cfg.get("cron_expr") or "").split()
            if len(parts) >= 2:
                try:
                    c_min, c_hour = int(parts[0]), int(parts[1])
                    if now.minute == c_min and now.hour == c_hour:
                        last_run = global_cfg.get("last_run") or ""
                        if not last_run.startswith(today_date_str + f"T{c_hour:02d}"):
                            is_hit = True
                            global_cfg["last_run"] = db.now_iso()
                            db.set_system_config("netdev_auto_backup_config", json.dumps(global_cfg, ensure_ascii=False))
                except Exception:
                    pass

        if is_hit:
            # 获取所有已启用的设备
            all_devs = db.query("SELECT id FROM netdev_devices WHERE enabled=1")
            for r in all_devs:
                triggered_devices.add(r["id"])
            _logger.info("[netdev.schedule] 触发网络设备全局自动备份，待执行设备数: %d", len(triggered_devices))

    # 2. 检查单设备定时排程
    dev_rows = db.query("SELECT * FROM netdev_devices WHERE enabled=1 AND schedule_enabled=1")
    for d in dev_rows:
        dev_id = d["id"]
        st = d.get("schedule_type") or "none"
        expr = (d.get("schedule_expr") or "").strip()
        last_sched = d.get("last_scheduled_run") or ""

        hit = False
        if st == "daily" and expr:
            # 每日定时，例如 "03:00"
            if cur_time_str == expr and not last_sched.startswith(today_date_str):
                hit = True
        elif st == "interval" and expr:
            # 间隔分钟数
            try:
                minutes = int(expr)
                if minutes > 0:
                    last_backup_at = d.get("last_backup_at") or d.get("created_at")
                    if last_backup_at:
                        last_dt = datetime.fromisoformat(last_backup_at.replace("Z", "+00:00"))
                        if (now - last_dt.replace(tzinfo=None)).total_seconds() >= minutes * 60:
                            hit = True
            except Exception:
                pass

        if hit:
            triggered_devices.add(dev_id)
            db.execute("UPDATE netdev_devices SET last_scheduled_run=? WHERE id=?", (db.now_iso(), dev_id))

    # 3. 依次对命中的设备执行备份（只有 SSH 连通正常的设备才能成功）
    results = []
    for dev_id in triggered_devices:
        try:
            res = execute_backup(dev_id, operator="scheduler")
            results.append({"device_id": dev_id, "ok": True, "file": res.get("file_name")})
        except Exception as e:
            results.append({"device_id": dev_id, "ok": False, "error": str(e)})

    return {"triggered": len(triggered_devices), "results": results}


# =========================================================================
# Excel 导入与模板生成
# =========================================================================

def generate_excel_template() -> io.BytesIO:
    """生成网络设备批量导入的标准 Excel (.xlsx) 模板。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "网络设备导入清单"

    headers = [
        "设备名称*", "设备类型*", "厂商系统预设*", "主机IP*", "SSH端口",
        "统一令牌名称", "SSH用户名", "SSH密码", "特权密码",
        "自定义备份指令(留空使用厂商预设)", "交互前置指令(如禁用分页)",
        "存储目标(本地/FTP)", "本地指定路径",
        "FTP主机", "FTP端口", "FTP用户名", "FTP密码", "FTP远程路径", "备注"
    ]

    header_font = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1B5E20", end_color="1B5E20", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC")
    )

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    sample_rows = [
        [
            "核心交换机-A01", "交换机", "华为", "192.168.1.254", 22,
            "", "admin", "HuaweiAdmin123", "",
            "display current-configuration", "screen-length 0 temporary",
            "本地", "/data/backups/network/switch/",
            "", "", "", "", "", "机房 A 区核心交换机"
        ],
        [
            "出口路由器-GW01", "路由器", "思科", "192.168.1.1", 22,
            "网络设备统一令牌", "", "", "",
            "show running-config", "terminal length 0",
            "FTP", "",
            "192.168.10.100", 21, "ftpuser", "FtpPassword123", "/netdev_backups/", "边界路由器，绑定令牌"
        ],
        [
            "汇聚交换机-H3C", "交换机", "华三", "192.168.2.1", 22,
            "", "admin", "H3CPassword123", "",
            "display current-configuration", "screen-length 0 temporary",
            "本地", "",
            "", "", "", "", "", "研发部汇聚交换机"
        ]
    ]

    data_font = Font(name="Microsoft YaHei", size=10)
    for row_data in sample_rows:
        ws.append(row_data)
        current_row = ws.max_row
        for col_idx in range(1, len(row_data) + 1):
            cell = ws.cell(row=current_row, column=col_idx)
            cell.font = data_font
            cell.border = thin_border

    # 自动调整列宽
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len * 1.8, 14)

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def import_devices_from_excel(file_bytes: bytes, auto_test: bool = True) -> Dict[str, Any]:
    """解析 Excel 文件并批量导入网络设备。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows or len(rows) < 2:
        return {"imported": 0, "skipped": 0, "errors": ["Excel 文件为空或未包含数据行"]}

    imported_count = 0
    skipped_count = 0
    errors = []

    # 建立现有令牌的名称 -> id 映射字典
    all_tokens = db.query("SELECT id, name, username FROM netdev_tokens")
    token_map = {t["name"].strip(): t["id"] for t in all_tokens if t.get("name")}

    for row_idx, r in enumerate(rows[1:], start=2):
        if not r or not any(r):
            continue  # 空行跳过

        name = str(r[0] or "").strip()
        dev_type_raw = str(r[1] or "").strip()
        vendor_raw = str(r[2] or "").strip()
        host = str(r[3] or "").strip()

        if not name or not host:
            skipped_count += 1
            errors.append(f"第 {row_idx} 行缺少设备名称或主机 IP，已跳过")
            continue

        # 解析设备类型
        device_type = "switch"
        if "路由" in dev_type_raw or "router" in dev_type_raw.lower():
            device_type = "router"
        elif "防火墙" in dev_type_raw or "firewall" in dev_type_raw.lower():
            device_type = "firewall"
        elif "其他" in dev_type_raw:
            device_type = "other"

        # 解析厂商预设
        vendor = "general"
        v_lower = vendor_raw.lower()
        if "华为" in vendor_raw or "huawei" in v_lower:
            vendor = "huawei"
        elif "华三" in vendor_raw or "h3c" in v_lower:
            vendor = "h3c"
        elif "思科" in vendor_raw or "cisco" in v_lower:
            vendor = "cisco"
        elif "锐捷" in vendor_raw or "ruijie" in v_lower:
            vendor = "ruijie"
        elif "瞻博" in vendor_raw or "juniper" in v_lower:
            vendor = "juniper"
        elif "linux" in v_lower:
            vendor = "linux"
        else:
            vendor = "custom"

        port = 22
        if len(r) > 4 and r[4]:
            try:
                port = int(r[4])
            except Exception:
                port = 22

        token_name = str(r[5] or "").strip() if len(r) > 5 else ""
        token_id = token_map.get(token_name)

        username = str(r[6] or "").strip() if len(r) > 6 else ""
        password = str(r[7] or "") if len(r) > 7 else ""
        enable_password = str(r[8] or "") if len(r) > 8 else ""

        backup_commands = str(r[9] or "").strip() if len(r) > 9 else ""
        init_commands = str(r[10] or "").strip() if len(r) > 10 else ""

        target_type_raw = str(r[11] or "").strip().lower() if len(r) > 11 else ""
        target_type = "ftp" if "ftp" in target_type_raw else "local"

        target_path = str(r[12] or "").strip() if len(r) > 12 else ""
        ftp_host = str(r[13] or "").strip() if len(r) > 13 else ""
        ftp_port = int(r[14]) if (len(r) > 14 and r[14]) else 21
        ftp_user = str(r[15] or "").strip() if len(r) > 15 else ""
        ftp_password = str(r[16] or "") if len(r) > 16 else ""
        ftp_path = str(r[17] or "/").strip() if len(r) > 17 else "/"
        remark = str(r[18] or "").strip() if len(r) > 18 else ""

        try:
            dev_id = create_device({
                "name": name,
                "device_type": device_type,
                "vendor": vendor,
                "host": host,
                "port": port,
                "token_id": token_id,
                "username": username,
                "password": password,
                "enable_password": enable_password,
                "backup_commands": backup_commands,
                "init_commands": init_commands,
                "target_type": target_type,
                "target_path": target_path,
                "ftp_host": ftp_host,
                "ftp_port": ftp_port,
                "ftp_user": ftp_user,
                "ftp_password": ftp_password,
                "ftp_path": ftp_path,
                "remark": remark,
            })
            imported_count += 1

            if auto_test:
                test_ssh_connection(dev_id)

        except license_mod.QuotaExceededError as qe:
            skipped_count += 1
            errors.append(f"【社区版配额达到上限】第 {row_idx} 行设备未能导入并终止后续导入: {qe}")
            break
        except Exception as err:
            skipped_count += 1
            errors.append(f"第 {row_idx} 行导入失败: {err}")

    db.add_log("INFO", "netdev", f"批量导入网络设备完成：成功 {imported_count} 台，失败/跳过 {skipped_count} 台")
    return {
        "imported": imported_count,
        "skipped": skipped_count,
        "errors": errors
    }


# =========================================================================
# 备份记录查询与配置比对
# =========================================================================

def list_backups(device_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """查询配置备份记录。"""
    sql = "SELECT * FROM netdev_backups WHERE 1=1"
    params = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        d["file_size_human"] = db.human_size(d.get("file_size") or 0)
        d["device_type_label"] = DEVICE_TYPES.get(d.get("device_type") or "switch", "交换机")
        out.append(d)
    return out


def get_backup(backup_id: int) -> Optional[Dict[str, Any]]:
    """获取单条备份记录详情。"""
    row = db.query_one("SELECT * FROM netdev_backups WHERE id=?", (backup_id,))
    if not row:
        return None
    d = dict(row)
    d["file_size_human"] = db.human_size(d.get("file_size") or 0)
    d["device_type_label"] = DEVICE_TYPES.get(d.get("device_type") or "switch", "交换机")
    return d


def get_backup_file_content(backup_id: int) -> str:
    """读取备份配置文件的完整内容。"""
    b = get_backup(backup_id)
    if not b:
        raise ValueError("备份记录不存在")

    loc = b.get("target_location") or ""
    file_name = b.get("file_name") or ""
    device_name = b.get("device_name") or ""

    if loc and os.path.isfile(loc):
        try:
            with open(loc, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            pass

    local_dir = _get_local_netdev_dir(device_name)
    cand = local_dir / file_name
    if cand.is_file():
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            pass

    return b.get("content_preview") or "（配置文件未能定位到本地物理路径）"


def diff_backups(backup_id1: int, backup_id2: int) -> str:
    """比较两份配置备份的差异（Unified Diff）。"""
    b1 = get_backup(backup_id1)
    b2 = get_backup(backup_id2)
    if not b1 or not b2:
        raise ValueError("指定的备份记录不存在")

    c1 = get_backup_file_content(backup_id1)
    c2 = get_backup_file_content(backup_id2)

    diff = difflib.unified_diff(
        c1.splitlines(keepends=True),
        c2.splitlines(keepends=True),
        fromfile=f"备份 #{b1['id']} ({b1['device_name']} @ {b1['created_at']})",
        tofile=f"备份 #{b2['id']} ({b2['device_name']} @ {b2['created_at']})",
    )
    return "".join(diff)


def delete_backup(backup_id: int) -> bool:
    """删除备份记录及本地对应文件。"""
    b = get_backup(backup_id)
    if not b:
        return False
    loc = b.get("target_location") or ""
    if loc and os.path.isfile(loc):
        try:
            os.remove(loc)
        except Exception:
            pass
    db.execute("DELETE FROM netdev_backups WHERE id=?", (backup_id,))
    return True
