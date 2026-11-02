# -*- coding: utf-8 -*-
"""Agent 执行通道 API：注册 / 列表 / 探活 / 删除 / 远端安装命令生成。"""
import os

from flask import jsonify, request

from core import agent_hosts
from . import api_bp


def _operator():
    from flask import session
    return session.get("username") or "system"


@api_bp.route("/agents", methods=["GET"])
def list_agents():
    return jsonify(agent_hosts.list_hosts())


@api_bp.route("/agents", methods=["POST"])
def create_agent():
    data = request.get_json(force=True, silent=True) or {}
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        return jsonify({"error": "hostname 必填"}), 400
    try:
        aid = agent_hosts.create_host(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        if "UNIQUE" in str(e):
            return jsonify({"error": "该主机已注册 Agent"}), 409
        return jsonify({"error": str(e)[:200]}), 500
    rec = agent_hosts.get_by_id(aid)
    # 注册即探活
    p = agent_hosts.probe(aid)
    return jsonify({
        "id": aid,
        "token": agent_hosts.get_token(rec),  # 仅注册响应返回一次明文
        "probe": p,
    })


@api_bp.route("/agents/<int:aid>", methods=["PUT"])
def update_agent(aid):
    data = request.get_json(force=True, silent=True) or {}
    agent_hosts.update_host(aid, data)
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>", methods=["DELETE"])
def delete_agent(aid):
    agent_hosts.delete_host(aid)
    return jsonify({"ok": True})


@api_bp.route("/agents/<int:aid>/probe", methods=["POST"])
def probe_agent(aid):
    p = agent_hosts.probe(aid)
    return jsonify(p), (200 if p.get("ok") else 502)


@api_bp.route("/agents/<int:aid>/install-cmd", methods=["GET"])
def install_cmd(aid):
    """生成在数据库服务器上执行的一键安装命令（经 SSH 推送，或复制手工执行）。"""
    rec = agent_hosts.get_by_id(aid)
    if not rec:
        return jsonify({"error": "注册记录不存在"}), 404
    token = agent_hosts.get_token(rec)
    agent_src = "/root/CodeBuddy/20260826095855/backup-platform/agent/backup_agent.py"
    port = int(rec.get("port") or 8466)
    remote_sh = (
        "mkdir -p /opt/aidbm-agent && "
        "cat > /opt/aidbm-agent/backup_agent.py <<'AIDBM_AGENT_EOF'\n"
        "{AGENT_SRC}\n"
        "AIDBM_AGENT_EOF\n"
        "chmod 700 /opt/aidbm-agent/backup_agent.py && "
        "cat > /etc/systemd/system/aidbm-agent.service <<'UNIT_EOF'\n"
        "[Unit]\nDescription=AIDBM Backup Agent\nAfter=network.target\n\n"
        "[Service]\nExecStart=/usr/bin/python3 /opt/aidbm-agent/backup_agent.py "
        " --token {TOKEN} --port {PORT}\nRestart=always\nRestartSec=3\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
        "UNIT_EOF\n"
        "systemctl daemon-reload && systemctl enable --now aidbm-agent && "
        "sleep 1 && systemctl is-active aidbm-agent\n")
    script = remote_sh.replace("{AGENT_SRC}", open(agent_src, encoding="utf-8").read()) \
                      .replace("{TOKEN}", token).replace("{PORT}", str(port))
    return jsonify({"script_file": "/tmp/aidbm_install_agent.sh",
                    "script": script})


@api_bp.route("/agents/remote-install", methods=["POST"])
def remote_install():
    """一键把备份执行器插件安装到目标主机（用户在添加备份/恢复任务时的
    「安装 Agent 插件」动作）。

    body: {"host_id": 已纳管 SSH 主机 id, "agent_port": 可选，默认 8466}
    流程：取纳管凭据 → SFTP 推送执行器 + systemd 单元 → enable --now →
    自动注册（复用既有 token）→ 探活。
    """
    data = request.get_json(force=True, silent=True) or {}
    host_id = int(data.get("host_id") or 0)
    agent_port = int(data.get("agent_port") or 8466)
    from core import ssh_hosts
    hp = ssh_hosts.get_host(host_id, include_secret=True)
    if not hp:
        return jsonify({"error": "纳管主机不存在"}), 404
    hostname = hp.get("hostname") or ""
    if not hostname:
        return jsonify({"error": "纳管主机缺少 hostname"}), 400
    # 注册记录：已存在则复用 token（升级场景），否则新建
    existing = agent_hosts.get_by_host(hostname)
    if existing:
        agent_id = int(existing["id"])
        agent_hosts.update_host(agent_id, {"port": agent_port, "enabled": 1})
    else:
        agent_id = agent_hosts.create_host({
            "name": hp.get("name") or hostname, "hostname": hostname,
            "port": agent_port, "transport": "auto"})
    rec = agent_hosts.get_by_id(agent_id)
    token = agent_hosts.get_token(rec)
    try:
        import paramiko
        from core.ssh_hosts import load_pkey
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=hp["hostname"], port=int(hp.get("port") or 22),
                  username=hp.get("username") or "root", timeout=15,
                  allow_agent=False, look_for_keys=False)
        pkey = load_pkey(hp.get("private_key") or "") \
            if hp.get("private_key") else None
        if pkey is not None:
            kw["pkey"] = pkey
        else:
            kw["password"] = crypto_decrypt(hp)
        c.connect(**kw)
        sftp = c.open_sftp()
        try:
            try:
                sftp.mkdir("/opt/aidbm-agent")
            except IOError:
                pass
            agent_src = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "agent", "backup_agent.py")
            sftp.put(agent_src, "/opt/aidbm-agent/backup_agent.py")
            unit = (
                "[Unit]\nDescription=AIDBM Backup Agent\n"
                "After=network.target\n\n[Service]\n"
                "ExecStart=/usr/bin/python3 /opt/aidbm-agent/backup_agent.py "
                "--token %s --port %d\nRestart=always\nRestartSec=3\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (token, agent_port))
            with sftp.open("/etc/systemd/system/aidbm-agent.service", "w") as f:
                f.write(unit)
        finally:
            sftp.close()
        _chn, _o, _rc = c.exec_command(
            "chmod 700 /opt/aidbm-agent/backup_agent.py && "
            "systemctl daemon-reload && systemctl enable --now aidbm-agent "
            "&& sleep 1 && systemctl is-active aidbm-agent", timeout=60)
        out = _o.read().decode(errors="replace").strip()
        c.close()
        p = agent_hosts.probe(agent_id)
        return jsonify({"ok": bool(p.get("ok")),
                        "agent_id": agent_id, "service": out[:60],
                        "probe": p})
    except Exception as e:
        return jsonify({"ok": False, "agent_id": agent_id,
                        "error": str(e)[:300]}), 502


def crypto_decrypt(hp: dict) -> str:
    # get_host(include_secret=True) 已解密（ssh_hosts.py 内部统一处理）
    return hp.get("password") or ""
