# -*- coding: utf-8 -*-
"""
SSH 主机纳管：无 Agent 远程备份所需的主机凭据存储与连接测试。

- 凭据（密码 / 私钥）加密存储于 SQLite 的 ssh_hosts 表
- host_key 唯一标识一台主机，格式 "user@hostname:port"，供 FileBackupEngine 引用
- test_connection 在不污染全局连接池的前提下做一次真实连通性探测
"""
from typing import Optional

import core.db as db
import logging

_logger = logging.getLogger("ssh_hosts")


HOST_FIELDS = [
    "name", "host_key", "hostname", "port", "username",
    "password", "auth_type", "private_key", "os_type", "remark",
]


def _build_host_key(username: str, hostname: str, port: int) -> str:
    u = username or "root"
    return f"{u}@{hostname}:{port or 22}"


def list_hosts(include_secret: bool = False) -> list:
    rows = db.query("SELECT * FROM ssh_hosts ORDER BY id DESC")
    out = []
    for r in rows:
        r = dict(r)
        if include_secret:
            r["password"] = db.decrypt_secret(r.get("password") or "")
        else:
            r["password"] = ""
        r["has_password"] = bool(r.get("password"))
        out.append(r)
    return out


def get_host(host_id: int, include_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM ssh_hosts WHERE id=?", (host_id,))
    if not row:
        return None
    row = dict(row)
    if include_secret:
        row["password"] = db.decrypt_secret(row.get("password") or "")
    else:
        row["password"] = ""
    row["has_password"] = bool(row.get("password"))
    return row


def create_host(data: dict) -> int:
    data = {k: data.get(k) for k in HOST_FIELDS}
    now = db.now_iso()
    data["created_at"] = now
    data["updated_at"] = now

    hostname = (data.get("hostname") or "").strip()
    username = (data.get("username") or "root").strip()
    port = data.get("port") or 22
    if not data.get("host_key"):
        data["host_key"] = _build_host_key(username, hostname, port)
    if not data.get("hostname"):
        data["hostname"] = data["host_key"].split("@")[-1].split(":")[0]
    if data.get("password"):
        data["password"] = db.encrypt_secret(data["password"])
    else:
        data["password"] = ""

    cols = list(data.keys())
    sql = "INSERT INTO ssh_hosts ({}) VALUES ({})".format(
        ",".join(cols), ",".join("?" * len(cols)))
    return db.execute(sql, tuple(data.values()))


def update_host(host_id: int, data: dict) -> bool:
    data = {k: v for k, v in data.items() if k in HOST_FIELDS}
    if not data:
        return False
    data["updated_at"] = db.now_iso()

    # 若修改了连接信息，重建 host_key
    row = get_host(host_id, include_secret=False)
    if not row:
        return False
    if any(k in data for k in ("username", "hostname", "port")):
        u = data.get("username", row.get("username")) or "root"
        h = data.get("hostname", row.get("hostname")) or ""
        p = data.get("port", row.get("port")) or 22
        data["host_key"] = _build_host_key(u, h, p)

    sets, params = [], []
    for k, v in data.items():
        if k == "password":
            if v in (None, ""):
                continue  # 不覆盖原密码
            v = db.encrypt_secret(v)
        sets.append(f"{k}=?")
        params.append(v)
    params.append(host_id)
    db.execute(
        "UPDATE ssh_hosts SET {} WHERE id=?".format(",".join(sets)),
        tuple(params))
    return True


def delete_host(host_id: int) -> bool:
    db.execute("DELETE FROM ssh_hosts WHERE id=?", (host_id,))
    return True


def test_connection(host_id: int) -> dict:
    """
    对指定主机做一次真实 SSH 连通性探测（独立连接，不进全局池）。
    返回 {ok, message, banner?}。
    """
    try:
        import paramiko
    except ImportError:
        return {"ok": False, "message": "服务端未安装 paramiko，无法测试远程连接"}

    host = get_host(host_id, include_secret=True)
    if not host:
        return {"ok": False, "message": "主机不存在"}

    hostname = host.get("hostname") or ""
    port = int(host.get("port") or 22)
    username = host.get("username") or "root"
    password = host.get("password") or ""
    auth_type = host.get("auth_type") or "password"
    private_key = host.get("private_key") or ""

    client = None
    result = {"ok": False, "message": "未知错误"}  # 关键：始终初始化
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = dict(hostname=hostname, port=port, username=username,
                              timeout=15, allow_agent=False, look_for_keys=False)
        if auth_type == "key" and private_key:
            import io
            try:
                key = paramiko.RSAKey.from_private_key(io.StringIO(private_key))
            except Exception:
                key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key))
            connect_kwargs["pkey"] = key
        else:
            connect_kwargs["password"] = password
        client.connect(**connect_kwargs)

        stdin, stdout, stderr = client.exec_command(
            "uname -a; id; echo __OK__", timeout=20)
        out = stdout.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        ok = rc == 0 and "__OK__" in out
        banner = out.strip().splitlines()[0] if out.strip() else ""
        result = {
            "ok": ok,
            "message": "连接成功" if ok else "连接已建立但命令执行异常",
            "banner": banner,
            "os_type": host.get("os_type"),
        }
    except Exception as e:
        result = {"ok": False, "message": f"连接失败: {e}"}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
        # 持久化探测结果到主表（始终执行）
        try:
            db.execute(
                "UPDATE ssh_hosts SET last_status=?, last_check_at=? WHERE id=?",
                ("ok" if result.get("ok") else "failed", db.now_iso(), host_id))
        except Exception as e:
            _logger.warning("[ssh_hosts.test_connection] update status failed: %s", e)
    return result
