# -*- coding: utf-8 -*-
"""Agent 执行通道注册表：CRUD + 健康探测 + 执行通道选择。

通道选择语义（transport 字段，作用于任务解析出的目标主机）：
  agent  — 强制走 Agent（未注册/不在线则报错，绝不静默回退 SSH）
  ssh    — 强制走 SSH
  auto   — 优先 Agent（在线即用），失败/未注册回退 SSH（默认）
"""
import json
import time

from core import db, crypto


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def list_hosts(enabled_only: bool = False) -> list:
    rows = db.query("SELECT * FROM agent_hosts ORDER BY id")
    out = []
    for r in rows or []:
        if enabled_only and not r.get("enabled"):
            continue
        r = dict(r)
        if r.get("token"):
            try:
                r["token_masked"] = db.decrypt_secret(r["token"])[:6] + "***"
            except Exception:
                r["token_masked"] = "***"
        r.pop("token", None)
        out.append(r)
    return out


def get_by_host(hostname: str, db_type: str = "") -> dict:
    """按主机 IP（+可选库型）取 Agent 注册记录。

    匹配优先：同 host_key 且 db_type 匹配 > 通用（db_type=''）。
    """
    rows = db.query("SELECT * FROM agent_hosts WHERE enabled=1 "
                    "AND hostname=?", (hostname,)) or []
    if not rows:
        return {}
    for r in rows:
        if db_type and (r.get("db_type") or "") == db_type:
            return dict(r)
    for r in rows:
        if not (r.get("db_type") or ""):
            return dict(r)
    return {}


def get_by_id(agent_id: int) -> dict:
    r = db.query_one("SELECT * FROM agent_hosts WHERE id=?", (int(agent_id),))
    return dict(r) if r else {}


def create_host(data: dict) -> int:
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        raise ValueError("hostname 必填")
    port = int(data.get("port") or 8466)
    # token 会写进 systemd unit 与安装脚本：换行可注入 ExecStartPre，
    # shell 元字符可注入安装命令。这里强制字符集白名单（生成值天然合规）。
    import re
    token = (data.get("token") or "").strip() or crypto.generate_token()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        raise ValueError("token 必须为 16-128 位字母/数字/下划线/连字符，"
                         "不允许空格、换行或 shell 元字符")
    now = _now()
    return db.execute(
        "INSERT INTO agent_hosts(name, host_key, hostname, port, token, "
        "transport, db_type, enabled, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (data.get("name") or hostname, "agent@%s:%d" % (hostname, port),
         hostname, port, db.encrypt_secret(token),
         data.get("transport") or "auto", data.get("db_type") or "",
         1 if data.get("enabled", 1) else 0, now, now))


def update_host(agent_id: int, data: dict):
    fields, params = [], []
    for k in ("name", "transport", "db_type", "enabled", "port"):
        if k in data:
            fields.append("%s=?" % k)
            params.append(data[k])
    if data.get("token"):
        fields.append("token=?")
        params.append(db.encrypt_secret(data["token"].strip()))
    if fields:
        fields.append("updated_at=?")
        params.append(_now())
        params.append(int(agent_id))
        db.execute("UPDATE agent_hosts SET " + ",".join(fields) + " WHERE id=?",
                   tuple(params))


def delete_host(agent_id: int):
    db.execute("DELETE FROM agent_hosts WHERE id=?", (int(agent_id),))


def get_token(rec: dict) -> str:
    return db.decrypt_secret(rec.get("token") or "")


def probe(agent_id: int) -> dict:
    """健康探测 + 能力自报回写。返回 {ok, caps|error}。"""
    rec = get_by_id(agent_id)
    if not rec:
        return {"ok": False, "error": "注册记录不存在"}
    from core.agent_client import AgentTransport, AgentError
    try:
        t = AgentTransport(rec["hostname"], rec["port"], get_token(rec),
                           timeout=10)
        caps = t.capabilities(refresh=True)
        db.execute(
            "UPDATE agent_hosts SET last_status=?, enc_algo=?, "
            "agent_version=?, last_check_at=?, updated_at=? WHERE id=?",
            ("online", caps.get("algo") or "", caps.get("version") or "",
             _now(), _now(), int(agent_id)))
        return {"ok": True, "caps": caps}
    except Exception as e:
        db.execute(
            "UPDATE agent_hosts SET last_status=?, last_check_at=?, "
            "updated_at=? WHERE id=?",
            ("offline: %s" % str(e)[:120], _now(), _now(), int(agent_id)))
        return {"ok": False, "error": str(e)[:200]}


def resolve_channel(hostname: str, db_type: str = "") -> dict:
    """执行通道选择（auto/agent/ssh 语义）。返回：

    {"channel": "agent", "agent": rec} 或 {"channel": "ssh"} / {"channel":"agent_forced_missing"}
    """
    rec = get_by_host(hostname, db_type)
    if not rec:
        return {"channel": "ssh"}
    mode = (rec.get("transport") or "auto").lower()
    if mode == "ssh":
        return {"channel": "ssh"}
    # agent / auto：探测在线性（轻量：直接 health，失败缓存 30s 由调用方处理）
    if mode == "agent":
        p = probe(rec["id"])
        if not p.get("ok"):
            return {"channel": "agent_forced_missing", "error": p.get("error")}
        return {"channel": "agent", "agent": rec}
    # auto：在线即用
    try:
        from core.agent_client import AgentTransport, AgentError
        t = AgentTransport(rec["hostname"], rec["port"], get_token(rec),
                           timeout=5)
        t.health()
        return {"channel": "agent", "agent": rec}
    except Exception:
        return {"channel": "ssh"}


def install_cmd(hostname: str, port: int = 8466) -> str:
    """生成远端安装命令行（由平台经 SSH 执行：零 SSH 场景由用户手工执行）。"""
    return (
        "mkdir -p /opt/aidbm-agent && echo '<AGENT_PY>' > /opt/aidbm-agent/"
        "backup_agent.py")
